"""FR-004 版本记录服务。

PRD 要求「剧本、角色描述、提示词和时间线必须保留版本；新版本产生后，
引用旧版本的下游产物必须标记为『可能过期』，不能静默继续复用」。

存储复用既有的 `EntityVersion` 表（此前只被剧本与角色写入，且没有任何查询入口）。
本模块负责「留档 + 查询 + 指纹去重」；「标记下游 STALE」由各 agent 的
`mark_stale()` 负责，因为只有它们知道下游是什么。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EntityVersion

#: 版本对象类型（与前端展示标签一一对应）
TARGET_SCRIPT = "script"
TARGET_CHARACTER = "character"
TARGET_SHOT = "shot"
TARGET_SHOT_PROMPT = "shot_prompt"
TARGET_TIMELINE = "timeline"
TARGET_STYLE = "style"

TARGET_LABELS: dict[str, str] = {
    TARGET_SCRIPT: "剧本",
    TARGET_CHARACTER: "角色描述",
    TARGET_SHOT: "分镜",
    TARGET_SHOT_PROMPT: "分镜提示词",
    TARGET_TIMELINE: "时间线",
    TARGET_STYLE: "画风",
}


def fingerprint(payload: Any) -> str:
    """内容指纹：只有内容真的变了才应该产生新版本。"""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def latest(
    session: Session, *, entity_type: str, entity_id: str
) -> EntityVersion | None:
    return session.scalars(
        select(EntityVersion)
        .where(
            EntityVersion.entity_type == entity_type,
            EntityVersion.entity_id == entity_id,
        )
        .order_by(EntityVersion.version.desc())
        .limit(1)
    ).first()


def history(
    session: Session,
    *,
    episode_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
) -> list[EntityVersion]:
    stmt = select(EntityVersion).order_by(EntityVersion.created_at.desc()).limit(limit)
    if episode_id:
        stmt = stmt.where(EntityVersion.episode_id == episode_id)
    if entity_type:
        stmt = stmt.where(EntityVersion.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(EntityVersion.entity_id == entity_id)
    return list(session.scalars(stmt))


def record(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
    note: str = "",
    episode_id: str | None = None,
    force: bool = False,
    version: int | None = None,
) -> EntityVersion | None:
    """写入一个新版本快照。

    内容与上一版完全一致时返回 `None`（不制造空版本），除非 `force=True`。
    这样「保存两次但没改内容」不会把版本号刷上去。

    `version` 用于调用方已经自行维护版本号（例如 `StoryDocument.version`）的场景，
    传了就按它写，避免同一个剧本出现两套互相矛盾的版本序列。
    """
    current = latest(session, entity_type=entity_type, entity_id=entity_id)
    digest = fingerprint(payload)
    if current is not None and current.input_digest == digest and not force:
        return None
    if version is None:
        version = (current.version if current else 0) + 1
    row = EntityVersion(
        episode_id=episode_id,
        entity_type=entity_type,
        entity_id=entity_id,
        version=version,
        payload=payload,
        input_digest=digest,
        note=note,
    )
    session.add(row)
    session.flush()
    return row


def diff_summary(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    """给出两个快照的顶层字段差异，供「版本对比」展示。"""
    a = a or {}
    b = b or {}
    changed: list[dict[str, Any]] = []
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            changed.append({"field": key, "before": a.get(key), "after": b.get(key)})
    return {"changed_fields": changed, "changed_count": len(changed)}


def to_dict(row: EntityVersion, *, with_payload: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": row.id,
        "entity_type": row.entity_type,
        "entity_label": TARGET_LABELS.get(row.entity_type, row.entity_type),
        "entity_id": row.entity_id,
        "version": row.version,
        "input_digest": row.input_digest,
        "note": row.note,
        "episode_id": row.episode_id,
        "created_at": row.created_at,
    }
    if with_payload:
        data["payload"] = row.payload
    return data
