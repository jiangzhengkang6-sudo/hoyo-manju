"""SafetyAgent：内容检查、AIGC 标识、风险记录（FR-080/081/083）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import NotFound, SafetyBlocked, StateConflict
from app.db.models import Episode, MediaAsset
from app.services import safety as safety_service
from app.services import events


def check_input(session: Session, text: str, *, episode_id: str | None = None) -> dict[str, Any]:
    result = safety_service.check_text(text, scope="input")
    if not result["allowed"]:
        events.publish(
            session,
            "safety.blocked",
            {"scope": "input", "risk_level": result["risk_level"],
             "categories": result["categories"]},
            episode_id=episode_id,
        )
    return result


def assert_input_allowed(session: Session, text: str, *, episode_id: str | None = None) -> None:
    result = check_input(session, text, episode_id=episode_id)
    if not result["allowed"]:
        raise SafetyBlocked(
            "输入内容未通过安全检查，请根据建议修改后重试",
            details=result,
        )


def check_output(session: Session, asset: MediaAsset) -> dict[str, Any]:
    return safety_service.check_media_metadata(asset.kind, asset.safety_detail)


def episode_risk_summary(session: Session, episode: Episode) -> dict[str, Any]:
    from sqlalchemy import select

    assets = list(
        session.scalars(
            select(MediaAsset).where(MediaAsset.episode_id == episode.id).limit(500)
        )
    )
    blocked = [a.id for a in assets if a.safety_status == "BLOCKED"]
    pending = [a.id for a in assets if a.safety_status == "PENDING"]
    return {
        "total": len(assets),
        "blocked": blocked,
        "pending": pending,
        "can_export": not blocked,
        "aigc": safety_service.aigc_label(),
    }


# --------------------------------------------------------------------------- #
# §12-8 内容拦截：导出闸门 + 申诉
# --------------------------------------------------------------------------- #

APPEAL_PENDING = "PENDING"
APPEAL_APPROVED = "APPROVED"
APPEAL_REJECTED = "REJECTED"
APPEAL_STATUSES = (APPEAL_PENDING, APPEAL_APPROVED, APPEAL_REJECTED)


def appeal_to_dict(appeal: Any) -> dict[str, Any]:
    """申诉的对外形态。

    时间戳统一转成 ISO 字符串：这个 dict 会出现在**错误体的 details 里**
    （`assert_export_allowed` 抛 422 时），而错误处理器用 `json.dumps` 序列化，
    datetime 对象会直接抛 `TypeError: Object of type datetime is not JSON serializable`。
    """
    return {
        "id": appeal.id,
        "episode_id": appeal.episode_id,
        "project_id": appeal.project_id,
        "target_type": appeal.target_type,
        "target_id": appeal.target_id,
        "reason": appeal.reason,
        "detail": appeal.detail,
        "status": appeal.status,
        "resolution_note": appeal.resolution_note,
        "created_at": appeal.created_at.isoformat() if appeal.created_at else None,
        "updated_at": appeal.updated_at.isoformat() if appeal.updated_at else None,
    }


def list_appeals(session: Session, episode_id: str) -> list[Any]:
    from sqlalchemy import select

    from app.db.models import SafetyAppeal

    return list(
        session.scalars(
            select(SafetyAppeal)
            .where(SafetyAppeal.episode_id == episode_id)
            .order_by(SafetyAppeal.created_at.desc())
        )
    )


def export_gate(session: Session, episode: Episode) -> dict[str, Any]:
    """§12-8 导出闸门。

    默认「有 BLOCKED 素材即禁止导出」；但申诉是**人工复核通道**：
    某素材的申诉被人工置为 `APPROVED` 后，它就不再阻断导出。
    这样既满足「禁止确认和导出」，又让申诉入口真的有意义（否则申诉永远解不开锁）。
    """
    from sqlalchemy import select

    from app.db.models import SafetyAppeal

    assets = list(
        session.scalars(
            select(MediaAsset).where(MediaAsset.episode_id == episode.id).limit(500)
        )
    )
    blocked = [a.id for a in assets if a.safety_status == "BLOCKED"]

    appeals = list_appeals(session, episode.id)
    approved = {a.target_id for a in appeals if a.status == APPEAL_APPROVED and a.target_id}
    pending = {a.target_id for a in appeals if a.status == APPEAL_PENDING and a.target_id}
    rejected = {a.target_id for a in appeals if a.status == APPEAL_REJECTED and a.target_id}

    unresolved = [asset_id for asset_id in blocked if asset_id not in approved]
    return {
        "can_export": not unresolved,
        "blocked": blocked,
        "unresolved": unresolved,
        # 素材已被放行但申诉还在审：提示用户「已提交，等复核」
        "pending": [i for i in blocked if i in pending],
        "rejected": [i for i in blocked if i in rejected],
        "approved": [i for i in blocked if i in approved],
        "appeals": [appeal_to_dict(a) for a in appeals],
        "aigc": safety_service.aigc_label(),
    }


def assert_export_allowed(session: Session, episode: Episode) -> dict[str, Any]:
    """确认/导出前的统一门禁；不通过就抛 422 `SAFETY_BLOCKED`，details 里带申诉入口所需信息。"""
    gate = export_gate(session, episode)
    if not gate["can_export"]:
        raise SafetyBlocked(
            "本集存在未通过内容安全检查的素材，已禁止确认与导出；"
            "请按建议修改输入，或提交申诉等待人工复核",
            details=gate,
        )
    return gate


def submit_appeal(
    session: Session,
    *,
    episode: Episode,
    target_type: str = "asset",
    target_id: str | None = None,
    reason: str,
    detail: str = "",
) -> Any:
    """§12-8 申诉入口：记录用户诉求，等待人工复核。"""
    from app.db.models import SafetyAppeal

    reason = (reason or "").strip()
    if not reason:
        raise StateConflict("请填写申诉理由，否则人工复核无从判断")
    if target_type not in {"asset", "input", "episode"}:
        raise StateConflict("申诉对象类型不合法")

    if target_type == "asset":
        if not target_id:
            raise StateConflict("申诉具体素材时必须指定素材 id")
        asset = session.get(MediaAsset, target_id)
        if asset is None or asset.episode_id != episode.id:
            raise NotFound("素材不存在或不属于当前剧集")
        # 没被拦截的素材没有申诉的必要，放任会产生一堆无意义工单
        if asset.safety_status != "BLOCKED":
            raise StateConflict("该素材未被拦截，无需申诉")

    # 同一目标只允许一条待审申诉，避免重复提交刷屏
    for existing in list_appeals(session, episode.id):
        if (
            existing.status == APPEAL_PENDING
            and existing.target_type == target_type
            and existing.target_id == target_id
        ):
            raise StateConflict("该对象已有待复核的申诉，请等待处理结果")

    appeal = SafetyAppeal(
        episode_id=episode.id,
        project_id=episode.project_id,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        detail=(detail or "").strip(),
        status=APPEAL_PENDING,
    )
    session.add(appeal)
    session.flush()
    events.publish(
        session,
        "safety.appeal_submitted",
        {"appeal_id": appeal.id, "target_type": target_type, "target_id": target_id},
        episode_id=episode.id,
    )
    return appeal


def resolve_appeal(
    session: Session,
    *,
    appeal: Any,
    status: str,
    resolution_note: str = "",
) -> Any:
    """人工复核结论。只有 APPROVED 会解除对应素材的导出阻断。"""
    if status not in APPEAL_STATUSES:
        raise StateConflict(f"申诉状态只能是 {'/'.join(APPEAL_STATUSES)}")
    appeal.status = status
    appeal.resolution_note = (resolution_note or "").strip()
    session.flush()
    events.publish(
        session,
        "safety.appeal_resolved",
        {"appeal_id": appeal.id, "status": status, "target_id": appeal.target_id},
        episode_id=appeal.episode_id,
    )
    return appeal
