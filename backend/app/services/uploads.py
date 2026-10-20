"""上传限制与工作区容量配额（需求 §11.3）。

「上传单文件大小、格式和总容量由工作区配额限制。」

三道闸门，全部在上传落盘**之前**判断：
1. 单文件大小上限（`UPLOAD_MAX_BYTES`）；
2. MIME 格式白名单（`UPLOAD_ALLOWED_MIMES`）；
3. 工作区已用容量 + 本次上传 ≤ 工作区配额（`UPLOAD_WORKSPACE_QUOTA_BYTES`）。

另外复用图像供应商的能力声明（FR-091）校验参考图格式，避免上传了供应商
不认的格式、生成时才失败。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ErrorCode, QuotaExceeded, StateConflict
from app.db.models import MediaAsset


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def workspace_usage_bytes(session: Session) -> int:
    """工作区已占用的媒体字节数（只统计上传来源，生成产物另算）。"""
    total = session.scalar(select(func.coalesce(func.sum(MediaAsset.size_bytes), 0)))
    return int(total or 0)


def upload_quota_snapshot(session: Session) -> dict[str, Any]:
    quota = int(settings.upload_workspace_quota_bytes)
    used = workspace_usage_bytes(session)
    return {
        "quota_bytes": quota,
        "used_bytes": used,
        "remaining_bytes": max(0, quota - used),
        "max_file_bytes": int(settings.upload_max_bytes),
        "allowed_mimes": list(settings.upload_allowed_mimes),
    }


def check_upload(
    session: Session,
    *,
    size_bytes: int,
    mime_type: str,
    kind: str = "",
) -> None:
    """上传前校验；不通过时抛统一错误体异常。"""
    mime = (mime_type or "").split(";")[0].strip().lower()

    if size_bytes <= 0:
        raise StateConflict("上传文件为空")
    if size_bytes > int(settings.upload_max_bytes):
        raise StateConflict(
            f"文件大小 {human_bytes(size_bytes)} 超过单文件上限 "
            f"{human_bytes(int(settings.upload_max_bytes))}",
            details={
                "size_bytes": size_bytes,
                "max_file_bytes": int(settings.upload_max_bytes),
            },
        )

    allowed = list(settings.upload_allowed_mimes)
    if allowed and mime and mime not in allowed:
        raise StateConflict(
            f"不支持的上传格式 {mime}，允许：{'、'.join(allowed)}",
            details={"mime_type": mime, "allowed_mimes": allowed},
        )

    if kind == "image" and mime:
        _check_provider_image_format(session, mime)

    quota = int(settings.upload_workspace_quota_bytes)
    used = workspace_usage_bytes(session)
    if quota > 0 and used + size_bytes > quota:
        raise QuotaExceeded(
            "工作区存储配额不足，请先删除部分素材",
            details={
                "code": ErrorCode.QUOTA_EXCEEDED,
                "quota_bytes": quota,
                "used_bytes": used,
                "requested_bytes": size_bytes,
                "remaining_bytes": max(0, quota - used),
            },
        )


def _check_provider_image_format(session: Session, mime: str) -> None:
    """FR-091：参考图格式也要符合图像供应商的能力声明。"""
    from app.providers import registry
    from app.providers.base import ProviderKind
    from app.services import capabilities as caps_service

    try:
        caps = registry.resolve(ProviderKind.IMAGE, session).capabilities()
    except Exception:  # noqa: BLE001 - 供应商不可用时不阻断上传
        return
    message = caps_service.check_image_formats(caps, mime_type=mime)
    if message:
        raise StateConflict(message, details={"mime_type": mime, "field": "format"})
