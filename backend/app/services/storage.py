"""对象存储抽象：本地文件系统（默认）/ S3 兼容（MinIO）。

媒体资源不可原地覆盖：每次写入都生成新的 storage_key。
"""

from __future__ import annotations

import datetime as dt
import mimetypes
import re
import shutil
from pathlib import Path

from app.core.config import settings
from app.core.errors import StorageError
from app.core.security import sign_payload, verify_payload


class Storage:
    backend = "local"

    def put(self, key: str, content: bytes, content_type: str = "") -> str:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def local_path(self, key: str) -> Path | None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        ttl = ttl_seconds or settings.signed_url_ttl_seconds
        token = sign_payload({"key": key}, ttl)
        return f"/api/v1/storage/signed/{token}"

    def verify_signed(self, token: str) -> str | None:
        body = verify_payload(token)
        if not body:
            return None
        return body.get("key")


class LocalStorage(Storage):
    backend = "local"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.replace("\\", "/").lstrip("/")
        if ".." in safe.split("/"):
            raise StorageError("非法的存储路径")
        target = (self.root / safe).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise StorageError("非法的存储路径")
        return target

    def put(self, key: str, content: bytes, content_type: str = "") -> str:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        except OSError as exc:
            raise StorageError(f"写入对象存储失败：{exc}") from exc
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise StorageError(f"对象不存在：{key}")
        return path.read_bytes()

    def local_path(self, key: str) -> Path | None:
        return self._path(key)

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).exists()
        except StorageError:
            return False

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()


class S3Storage(Storage):
    backend = "s3"

    def __init__(self) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise StorageError("未安装 boto3，无法使用 S3 存储") from exc
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )
        self.bucket = settings.s3_bucket
        self.cache = settings.data_dir / "s3cache"
        self.cache.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, content: bytes, content_type: str = "") -> str:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentType=content_type or mimetypes.guess_type(key)[0] or "application/octet-stream",
            )
        except Exception as exc:
            raise StorageError(f"上传对象失败：{exc}") from exc
        return key

    def get(self, key: str) -> bytes:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        except Exception as exc:
            raise StorageError(f"读取对象失败：{exc}") from exc

    def local_path(self, key: str) -> Path | None:
        """下载到本地缓存，供 FFmpeg 使用。"""
        target = self.cache / key.replace("/", "_")
        if not target.exists():
            target.write_bytes(self.get(key))
        return target

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        ttl = ttl_seconds or settings.signed_url_ttl_seconds
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=ttl,
            )
        except Exception:
            return super().signed_url(key, ttl)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is not None:
        return _storage
    if settings.storage_backend == "s3":
        try:
            _storage = S3Storage()
        except Exception:
            _storage = LocalStorage(settings.storage_dir)
    else:
        _storage = LocalStorage(settings.storage_dir)
    return _storage


def storage_health() -> dict[str, object]:
    try:
        storage = get_storage()
        probe_key = f"health/probe_{dt.datetime.now(dt.timezone.utc).timestamp():.0f}.txt"
        storage.put(probe_key, b"ok", "text/plain")
        ok = storage.exists(probe_key)
        storage.delete(probe_key)
        return {"backend": storage.backend, "ok": ok}
    except Exception as exc:
        return {"backend": settings.storage_backend, "ok": False, "error": str(exc)}


_UNSAFE_KEY_CHARS = re.compile(r"[^0-9A-Za-z._\-]+")


def sanitize_filename(filename: str, fallback: str = "file") -> str:
    """把文件名转成只含 ASCII 安全字符的形式，避免存储路径与 HTTP 头编码问题。"""
    stem, _, ext = filename.rpartition(".")
    if not stem:
        stem, ext = filename, ""
    safe_stem = _UNSAFE_KEY_CHARS.sub("_", stem).strip("_")[:80] or fallback
    safe_ext = _UNSAFE_KEY_CHARS.sub("", ext)[:12]
    return f"{safe_stem}.{safe_ext}" if safe_ext else safe_stem


def build_key(*parts: str, filename: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    clean = [sanitize_filename(p, "part") for p in parts if p]
    return "/".join([*clean, stamp, sanitize_filename(filename)])


def free_space_mb() -> float:
    try:
        usage = shutil.disk_usage(settings.data_dir)
        return round(usage.free / 1024 / 1024, 1)
    except OSError:
        return 0.0
