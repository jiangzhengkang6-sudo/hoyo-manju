"""媒体资产服务：落库、签名 URL、质量检查（FR-MVP-034）。"""

from __future__ import annotations

import datetime as dt
import mimetypes
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import NotFound, StorageError
from app.core.security import new_id, sha256_of
from app.db.models import AssetStatus, MediaAsset
from app.services import events, safety
from app.services.storage import build_key, get_storage

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
ALLOWED_AUDIO_TYPES = {"audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4", "audio/aac"}
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm"}


def quality_check(
    *,
    kind: str,
    content: bytes,
    width: int | None = None,
    height: int | None = None,
    mime_type: str = "",
) -> dict[str, Any]:
    """检查文件完整性、尺寸、类型与内容安全；失败资源不得用于视频生成。"""
    problems: list[str] = []
    if not content:
        problems.append("文件为空")
    if kind == "image":
        if mime_type and mime_type not in ALLOWED_IMAGE_TYPES:
            problems.append(f"不支持的图片格式：{mime_type}")
        if width and height:
            if width < 256 or height < 256:
                problems.append(f"图片尺寸过小：{width}x{height}")
        elif content:
            try:
                import io

                from PIL import Image

                with Image.open(io.BytesIO(content)) as img:
                    width, height = img.size
                    img.verify()
            except Exception as exc:
                problems.append(f"图片文件损坏：{exc}")
    elif kind == "video":
        if mime_type and mime_type not in ALLOWED_VIDEO_TYPES:
            problems.append(f"不支持的视频格式：{mime_type}")
    elif kind == "audio":
        if mime_type and mime_type not in ALLOWED_AUDIO_TYPES:
            problems.append(f"不支持的音频格式：{mime_type}")
    return {
        "ok": not problems,
        "problems": problems,
        "width": width,
        "height": height,
    }


def save_media(
    session: Session,
    *,
    content: bytes,
    kind: str,
    role: str = "",
    filename: str = "",
    mime_type: str = "",
    owner_type: str = "episode",
    owner_id: str = "",
    episode_id: str | None = None,
    project_id: str | None = None,
    width: int | None = None,
    height: int | None = None,
    duration_ms: int | None = None,
    status: str = AssetStatus.CANDIDATE.value,
    version: int = 1,
    source: str = "generated",
    rights_declared: bool = False,
    rights_note: str = "",
    metadata: dict[str, Any] | None = None,
) -> MediaAsset:
    if not filename:
        ext = mimetypes.guess_extension(mime_type) or ".bin"
        filename = f"{new_id('f')}{ext}"
    if not mime_type:
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    check = quality_check(kind=kind, content=content, width=width, height=height, mime_type=mime_type)
    safety_result = safety.check_media_metadata(kind, metadata)

    key = build_key(project_id or "shared", episode_id or "misc", kind, filename=filename)
    try:
        get_storage().put(key, content, mime_type)
    except StorageError:
        raise

    asset = MediaAsset(
        id=new_id("med"),
        project_id=project_id,
        episode_id=episode_id,
        owner_type=owner_type,
        owner_id=owner_id or episode_id or project_id or "",
        kind=kind,
        role=role,
        uri=key,
        storage_key=key,
        filename=filename,
        mime_type=mime_type,
        sha256=sha256_of(content),
        size_bytes=len(content),
        width=check["width"] or width,
        height=check["height"] or height,
        duration_ms=duration_ms,
        version=version,
        status=status if check["ok"] else AssetStatus.REJECTED.value,
        aigc_labeled=True,
        safety_status=safety_result["status"],
        safety_detail={**safety_result, "quality": check},
        rights_declared=rights_declared,
        rights_note=rights_note,
        source=source,
        metadata_json=metadata or {},
    )
    session.add(asset)
    session.flush()
    events.publish(
        session,
        events.MEDIA_CREATED,
        {
            "media_id": asset.id,
            "kind": asset.kind,
            "role": asset.role,
            "status": asset.status,
            "episode_id": episode_id,
            "owner_id": asset.owner_id,
        },
        episode_id=episode_id,
        project_id=project_id,
    )
    return asset


def save_artifact(
    session: Session,
    artifact: dict[str, Any],
    *,
    kind: str,
    role: str,
    **kwargs: Any,
) -> MediaAsset:
    return save_media(
        session,
        content=artifact["content"],
        kind=kind,
        role=role,
        filename=artifact.get("filename", ""),
        mime_type=artifact.get("mime_type", ""),
        width=artifact.get("width"),
        height=artifact.get("height"),
        duration_ms=artifact.get("duration_ms"),
        **kwargs,
    )


def get_media(session: Session, media_id: str) -> MediaAsset:
    asset = session.get(MediaAsset, media_id)
    if asset is None:
        raise NotFound(f"媒体资源不存在：{media_id}")
    return asset


def clamp_ttl(ttl_seconds: int | None) -> int:
    """§11.1-2：签名 URL 有效期默认不超过 30 分钟，且服务端强制上限。

    调用方可以传更短的值，但**不能**超过 `settings.signed_url_max_ttl_seconds`——
    否则任意长 TTL 就等于把私有素材变成了永久公开链接。
    """
    default = int(settings.signed_url_ttl_seconds)
    ceiling = int(getattr(settings, "signed_url_max_ttl_seconds", 1800) or 1800)
    try:
        requested = int(ttl_seconds) if ttl_seconds is not None else default
    except (TypeError, ValueError):
        requested = default
    if requested <= 0:
        requested = default
    return max(1, min(requested, ceiling))


def media_url(asset: MediaAsset, ttl_seconds: int | None = None) -> str:
    return get_storage().signed_url(asset.storage_key, clamp_ttl(ttl_seconds))


def read_media(asset: MediaAsset) -> bytes:
    return get_storage().get(asset.storage_key)


def local_path(asset: MediaAsset) -> Path | None:
    return get_storage().local_path(asset.storage_key)


def exists(session: Session, asset: MediaAsset | None) -> bool:
    """FR-060：素材记录存在≠文件存在（存储被清理、上传中断都会留下空壳记录）。

    本地存储直接看文件；远端存储没有廉价的探测手段，按「存在」处理，
    以免把正常的远端素材误判成缺失。
    """
    if asset is None:
        return False
    try:
        path = local_path(asset)
    except Exception:  # noqa: BLE001 - 远端存储无本地路径
        return True
    if path is None:
        return True
    return Path(path).exists()


def to_dict(asset: MediaAsset, *, with_url: bool = True) -> dict[str, Any]:
    meta = dict(asset.metadata_json or {})
    data = {
        "id": asset.id,
        "project_id": asset.project_id,
        "episode_id": asset.episode_id,
        "owner_type": asset.owner_type,
        "owner_id": asset.owner_id,
        "kind": asset.kind,
        "role": asset.role,
        # 角色资产四类（设定/正面/半身/表情）等标记原本只嵌在 metadata_json 里，
        # 前端按顶层字段读取，这里拍平出来（同时保留 metadata_json 兼容旧调用方）。
        "asset_kind": meta.get("asset_kind"),
        "character_id": meta.get("character_id"),
        "shot_no": meta.get("shot_no"),
        "model": meta.get("model"),
        "prompt": meta.get("prompt"),
        "negative_prompt": meta.get("negative_prompt"),
        "filename": asset.filename,
        "mime_type": asset.mime_type,
        "sha256": asset.sha256,
        "size_bytes": asset.size_bytes,
        "width": asset.width,
        "height": asset.height,
        "duration_ms": asset.duration_ms,
        "version": asset.version,
        "status": asset.status,
        "aigc_labeled": asset.aigc_labeled,
        "safety_status": asset.safety_status,
        #: 别名：前端候选图组件按 `safety` 读取安全检查结果
        "safety": {
            "status": asset.safety_status,
            "detail": asset.safety_detail or {},
        },
        "safety_detail": asset.safety_detail,
        "rights_declared": asset.rights_declared,
        "rights_note": asset.rights_note,
        "source": asset.source,
        "metadata_json": asset.metadata_json,
        "created_at": asset.created_at,
        "aigc": safety.aigc_label(),
    }
    if with_url:
        data["url"] = media_url(asset)
        data["content_url"] = f"/api/v1/media/{asset.id}/content"
    return data


def list_media(
    session: Session,
    *,
    episode_id: str | None = None,
    project_id: str | None = None,
    kind: str | None = None,
    role: str | None = None,
    owner_id: str | None = None,
    limit: int = 200,
) -> list[MediaAsset]:
    stmt = select(MediaAsset).order_by(MediaAsset.created_at.desc()).limit(limit)
    for field, value in (
        ("episode_id", episode_id),
        ("project_id", project_id),
        ("kind", kind),
        ("role", role),
        ("owner_id", owner_id),
    ):
        if value:
            stmt = stmt.where(getattr(MediaAsset, field) == value)
    return list(session.scalars(stmt))


def count_by_kind(session: Session, episode_id: str) -> dict[str, int]:
    rows = session.execute(
        select(MediaAsset.kind, func.count(MediaAsset.id))
        .where(MediaAsset.episode_id == episode_id)
        .group_by(MediaAsset.kind)
    ).all()
    return {kind: int(count) for kind, count in rows}


def supersede(session: Session, asset: MediaAsset) -> None:
    """媒体资源不可原地覆盖：旧资源标记为替代，保留历史。"""
    asset.status = AssetStatus.SUPERSEDED.value
    session.flush()


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def storage_root() -> Path:
    return settings.storage_dir
