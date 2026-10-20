"""§11.1-3 审计日志：删除、导出、配额调整、供应商变更必须留痕。

统一入口，避免每个路由各写一份 `AuditLog(...)`（此前项目/供应商有审计，
导出与删除完全没有）。这里同时负责 **detail 脱敏**（FR-093），
调用方不必再各自记得调 `redact_payload`。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import FREE_TEXT_KEYS, redact_payload
from app.db.models import AuditLog

#: 动作常量 —— 用常量而不是散落的字符串字面量，便于审计查询与前端归类
ACTION_CREATE = "CREATE"
ACTION_UPDATE = "UPDATE"
ACTION_DELETE = "DELETE"
ACTION_ARCHIVE = "ARCHIVE"
ACTION_EXPORT = "EXPORT"
ACTION_RENDER = "RENDER"
ACTION_DOWNLOAD = "DOWNLOAD"
ACTION_QUOTA_ADJUST = "QUOTA_ADJUST"
ACTION_PROVIDER_CHANGE = "PROVIDER_CHANGE"
ACTION_GATE_DECISION = "GATE_DECISION"
ACTION_RIGHTS_CHANGE = "RIGHTS_CHANGE"

#: 目标类型
TARGET_PROJECT = "project"
TARGET_EPISODE = "episode"
TARGET_MEDIA = "media"
TARGET_SHOT = "shot"
TARGET_CHARACTER = "character"
TARGET_STYLE = "style"
TARGET_JOB = "job"
TARGET_EXPORT = "export"
TARGET_QUOTA = "quota"
TARGET_PROVIDER = "provider"


def record(
    session: Session,
    *,
    action: str,
    target_type: str,
    target_id: str,
    project_id: str | None = None,
    episode_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """写一条审计记录（detail 自动脱敏）。调用方负责 `session.flush()`。"""
    entry = AuditLog(
        project_id=project_id,
        episode_id=episode_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=redact_payload(detail or {}, truncate_keys=FREE_TEXT_KEYS),
    )
    session.add(entry)
    return entry


def history(
    session: Session,
    *,
    project_id: str | None = None,
    episode_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """按条件查审计流水，新的在前。"""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    if project_id:
        stmt = stmt.where(AuditLog.project_id == project_id)
    if episode_id:
        stmt = stmt.where(AuditLog.episode_id == episode_id)
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = session.scalars(stmt.limit(max(1, min(int(limit), 500))))
    return [
        {
            "id": row.id,
            "project_id": row.project_id,
            "episode_id": row.episode_id,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "detail": row.detail or {},
            "created_at": row.created_at,
        }
        for row in rows
    ]
