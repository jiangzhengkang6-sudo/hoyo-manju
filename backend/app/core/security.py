"""密钥加密、签名 URL、脱敏与 ID 生成。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------- #
# 密钥加密（服务端加密，前端不可读）
# --------------------------------------------------------------------------- #

def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def mask_secret(plain: str) -> str:
    """密钥类脱敏：只保留前 4 位与后 4 位，中间用 ``***``。

    长度不足 8 位时无法安全保留片段，整体打码。全仓唯一的密钥打码口径：
    API 回显的 ``api_key_masked`` 与日志/payload 脱敏共用本函数。
    """
    if not plain:
        return ""
    text = plain if isinstance(plain, str) else str(plain)
    if len(text) < 8:
        return "*" * len(text)
    return f"{text[:4]}***{text[-4:]}"


# --------------------------------------------------------------------------- #
# 签名 URL
# --------------------------------------------------------------------------- #

def sign_payload(payload: dict[str, Any], ttl_seconds: int | None = None) -> str:
    ttl = ttl_seconds or settings.signed_url_ttl_seconds
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(settings.secret_key.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{token}.{sig}"


def verify_payload(token: str) -> dict[str, Any] | None:
    try:
        raw_b64, _, sig = token.partition(".")
        pad = "=" * (-len(raw_b64) % 4)
        raw = base64.urlsafe_b64decode(raw_b64 + pad)
    except Exception:
        return None
    expected = hmac.new(settings.secret_key.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if int(body.get("exp", 0)) < int(time.time()):
        return None
    return body


# --------------------------------------------------------------------------- #
# 日志与落库数据脱敏（FR-093、PRD §11.1）
# --------------------------------------------------------------------------- #

_SECRET_KEY_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|AK[A-Za-z0-9]{10,})")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-]{1,3})[A-Za-z0-9._%+\-]*(@[A-Za-z0-9.\-]+)")

#: 内联键值形式的密钥（api_key=xxx / token: "xxx"）。
#: 只认 `:` / `=` 分隔，避免把普通英文短语（如 "token expires"）误判成密钥。
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|secret|token|password|passwd|authorization)\b"
    r"(\s*[:=]\s*)([\"']?)([A-Za-z0-9._\-/+]{4,})\3"
)

#: strict 等级下正文/提示词保留的最大字数
STRICT_TEXT_LIMIT = 40

#: strict 截断标记；也用于判断某段文本是否已被截断
TRUNCATION_MARK = "…（已截断"

#: 自由文本字段名：入库前按 strict 等级截断（正文、提示词、台词、备注、原始响应等）。
#: 结构化小字段（shot_nos、preset、candidates_per_shot 等）不在其中，原样保留。
#: 注意 `instruction`（用户手写的重生成指令）**不截断**——它是下游真正要消费的
#: 用户输入，截断会造成功能回归；它仍会走 normal 级脱敏（密钥/手机号/邮箱）。
FREE_TEXT_KEYS = frozenset({
    "source_text", "prompt", "prompts", "negative_prompt",
    "note", "notes", "text", "content", "dialogue", "narration",
    "raw", "raw_summary", "summary", "response", "message", "body", "detail",
})

#: 字段名（小写后包含匹配）命中即按密钥处理
SENSITIVE_KEY_HINTS: tuple[str, ...] = (
    "key", "secret", "token", "password", "authorization",
)


def mask_phone(text: str) -> str:
    """把文中 11 位手机号（1 开头）的中间四位打码为 ``****``。"""
    if not text:
        return ""
    source = text if isinstance(text, str) else str(text)
    return _PHONE_RE.sub(r"\1****\2", source)


def _mask_inline_secret(match: re.Match[str]) -> str:
    name, sep, quote, raw = match.group(1), match.group(2), match.group(3), match.group(4)
    return f"{name}{sep}{quote}{mask_secret(raw)}{quote}"


def redact(text: str | None, *, level: str = "normal") -> str:
    """按等级脱敏：normal 脱敏密钥/手机号/邮箱；strict 额外截断长正文与提示词。

    - ``normal``：内联密钥、密钥样式串、手机号、邮箱；
    - ``strict``：在 normal 基础上，超过 40 字的文本截断为前 40 字，
      并追加 ``…（已截断 N 字）``（N 为被截掉的字符数）。
    """
    if not text:
        return ""
    out = text if isinstance(text, str) else str(text)
    # 先处理键值对形式，再处理独立密钥串，避免内联 token 被重复打码
    out = _INLINE_SECRET_RE.sub(_mask_inline_secret, out)
    out = _SECRET_KEY_RE.sub(lambda m: mask_secret(m.group(0)), out)
    out = _PHONE_RE.sub(r"\1****\2", out)
    out = _EMAIL_RE.sub(r"\1***\2", out)
    if level == "strict" and len(out) > STRICT_TEXT_LIMIT:
        truncated = len(out) - STRICT_TEXT_LIMIT
        out = f"{out[:STRICT_TEXT_LIMIT]}{TRUNCATION_MARK} {truncated} 字）"
    return out


def is_truncated(text: str | None) -> bool:
    """判断一段文本是否已被 strict 等级截断（用于取回被截断前的原始来源）。"""
    return TRUNCATION_MARK in (text or "")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in SENSITIVE_KEY_HINTS)


def redact_payload(
    payload: Any,
    *,
    level: str = "normal",
    truncate_keys: Iterable[str] | None = None,
) -> Any:
    """递归脱敏 dict/list/str，只改值、不改字段名与结构。

    - key 名命中 :data:`SENSITIVE_KEY_HINTS` 的字符串一律按密钥 ``mask_secret``；
    - key 名在 ``truncate_keys`` 中的字符串按 strict 等级截断；
    - 其余字符串按 ``level`` 脱敏；数字、布尔、None 原样保留。
    """
    truncate = set(truncate_keys or ())

    def walk(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: walk(v, key=str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(item, key=key) for item in value]
        if isinstance(value, str):
            if key is not None and _is_sensitive_key(key):
                return mask_secret(value)
            item_level = "strict" if key is not None and key in truncate else level
            return redact(value, level=item_level)
        return value

    return walk(payload)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
