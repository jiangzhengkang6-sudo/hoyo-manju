"""事件总线：事件落库 + SSE 增量拉取。

事件持久化在 ``event_log`` 表中，SSE 端点按自增 id 增量读取，
因此 worker 与 API 即使在不同进程也能保证前端状态一致与断线补齐。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EventLog

#: 事件类型常量
JOB_CREATED = "job.created"
JOB_STATUS_CHANGED = "job.status_changed"
JOB_PROGRESS = "job.progress"
JOB_SUCCEEDED = "job.succeeded"
JOB_FAILED = "job.failed"
JOB_CANCELLED = "job.cancelled"
WORKFLOW_GATE_WAITING = "workflow.gate_waiting"
WORKFLOW_RESUMED = "workflow.resumed"
WORKFLOW_NODE_CHANGED = "workflow.node_changed"
EPISODE_STAGE_CHANGED = "episode.stage_changed"
SHOT_CONFIRMED = "shot.confirmed"
MEDIA_CREATED = "media.created"
RENDER_PROGRESS = "render.progress"
HEARTBEAT = "heartbeat"


def publish(
    session: Session,
    event_type: str,
    payload: dict[str, Any],
    *,
    episode_id: str | None = None,
    project_id: str | None = None,
) -> EventLog:
    """写入一条事件（调用方负责 commit）。"""
    row = EventLog(
        episode_id=episode_id,
        project_id=project_id,
        event_type=event_type,
        payload={**payload, "ts": dt.datetime.now(dt.timezone.utc).isoformat()},
    )
    session.add(row)
    session.flush()
    return row


def fetch_since(
    session: Session,
    last_id: int,
    *,
    episode_id: str | None = None,
    project_id: str | None = None,
    limit: int = 200,
) -> list[EventLog]:
    stmt = select(EventLog).where(EventLog.id > last_id).order_by(EventLog.id).limit(limit)
    if episode_id:
        stmt = stmt.where(EventLog.episode_id == episode_id)
    if project_id:
        stmt = stmt.where(EventLog.project_id == project_id)
    return list(session.scalars(stmt))


def latest_id(session: Session, *, episode_id: str | None = None) -> int:
    stmt = select(EventLog.id).order_by(EventLog.id.desc()).limit(1)
    if episode_id:
        stmt = stmt.where(EventLog.episode_id == episode_id)
    return int(session.scalar(stmt) or 0)
