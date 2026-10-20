"""成本与配额：预估、预留、结算、释放、流水（需求文档 §12）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import QuotaExceeded
from app.db.models import CostLedger, Job, JobStatus, Project, Episode

KIND_RESERVE = "RESERVE"
KIND_SETTLE = "SETTLE"
KIND_RELEASE = "RELEASE"


def _sum(session: Session, kind: str, **filters: Any) -> int:
    stmt = select(func.coalesce(func.sum(CostLedger.amount_cents), 0)).where(CostLedger.kind == kind)
    for field, value in filters.items():
        if value is not None:
            stmt = stmt.where(getattr(CostLedger, field) == value)
    return int(session.scalar(stmt) or 0)


def quota_snapshot(session: Session, *, limit_cents: int | None = None) -> dict[str, int]:
    limit = settings.quota_limit_cents if limit_cents is None else limit_cents
    used = _sum(session, KIND_SETTLE)
    reserved = _sum(session, KIND_RESERVE) + _sum(session, KIND_RELEASE)
    remaining = limit - used - reserved
    running = int(
        session.scalar(
            select(func.count(Job.id)).where(
                Job.status.in_([JobStatus.RUNNING.value, JobStatus.QUEUED.value,
                                JobStatus.RETRYING.value, JobStatus.CREATED.value])
            )
        )
        or 0
    )
    return {
        "limit_cents": limit,
        "used_cents": used,
        "reserved_cents": reserved,
        "remaining_cents": remaining,
        "concurrent_limit": settings.max_concurrent_jobs,
        "running_jobs": running,
    }


def check_quota(session: Session, need_cents: int) -> None:
    snap = quota_snapshot(session)
    if need_cents > snap["remaining_cents"]:
        raise QuotaExceeded(
            f"配额不足：本次需要 {need_cents} 分，剩余 {snap['remaining_cents']} 分。"
            "已成功生成的素材仍可预览与导出。",
            details={"need_cents": need_cents, **snap},
        )


def reserve(
    session: Session,
    *,
    job: Job,
    cents: int,
    stage: str = "",
    note: str = "",
    enforce: bool = True,
) -> None:
    if cents <= 0:
        return
    if enforce:
        check_quota(session, cents)
    job.cost_reserved_cents = cents
    session.add(
        CostLedger(
            project_id=job.project_id,
            episode_id=job.episode_id,
            job_id=job.id,
            amount_cents=cents,
            kind=KIND_RESERVE,
            stage=stage or job.stage or job.type,
            note=note or f"预留：{job.type}",
        )
    )


def settle(
    session: Session,
    *,
    job: Job,
    actual_cents: int,
    stage: str = "",
    note: str = "",
) -> None:
    """成功后按实际消耗结算，并全额释放该任务的预留。

    预留只是临时占用额度：结算时写入 SETTLE(+实际消耗) 并 RELEASE(-预留)，
    这样 `used = ΣSETTLE`、`reserved = ΣRESERVE + ΣRELEASE` 两条口径始终自洽。
    """
    reserved = job.cost_reserved_cents or 0
    job.cost_settled_cents = actual_cents
    if actual_cents > 0:
        session.add(
            CostLedger(
                project_id=job.project_id,
                episode_id=job.episode_id,
                job_id=job.id,
                amount_cents=actual_cents,
                kind=KIND_SETTLE,
                stage=stage or job.stage or job.type,
                note=note or f"结算：{job.type}",
            )
        )
    if reserved > 0:
        session.add(
            CostLedger(
                project_id=job.project_id,
                episode_id=job.episode_id,
                job_id=job.id,
                amount_cents=-reserved,
                kind=KIND_RELEASE,
                stage=stage or job.stage or job.type,
                note="结算后释放预留",
            )
        )
    job.cost_reserved_cents = 0


def release(session: Session, *, job: Job, reason: str = "任务失败或取消，释放预留") -> None:
    reserved = job.cost_reserved_cents or 0
    if reserved <= 0:
        return
    session.add(
        CostLedger(
            project_id=job.project_id,
            episode_id=job.episode_id,
            job_id=job.id,
            amount_cents=-reserved,
            kind=KIND_RELEASE,
            stage=job.stage or job.type,
            note=reason,
        )
    )
    job.cost_reserved_cents = 0


def ledger_page(
    session: Session,
    *,
    episode_id: str | None = None,
    project_id: str | None = None,
    kind: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    stmt = select(CostLedger).order_by(CostLedger.created_at.desc())
    count_stmt = select(func.count(CostLedger.id))
    for stmt_ref in (stmt, count_stmt):
        if episode_id:
            stmt_ref = stmt_ref.where(CostLedger.episode_id == episode_id)
        if project_id:
            stmt_ref = stmt_ref.where(CostLedger.project_id == project_id)
        if kind:
            stmt_ref = stmt_ref.where(CostLedger.kind == kind)
        if stmt_ref is stmt:
            stmt = stmt_ref
        else:
            count_stmt = stmt_ref
    total = int(session.scalar(count_stmt) or 0)
    rows = list(session.scalars(stmt.offset((page - 1) * page_size).limit(page_size)))
    return {
        "items": [
            {
                "id": r.id,
                "job_id": r.job_id,
                "episode_id": r.episode_id,
                "project_id": r.project_id,
                "amount_cents": r.amount_cents,
                "kind": r.kind,
                "stage": r.stage,
                "currency": r.currency,
                "note": r.note,
                "created_at": r.created_at,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _group(session: Session, column, **filters: Any) -> list[dict[str, Any]]:
    stmt = (
        select(column, func.coalesce(func.sum(CostLedger.amount_cents), 0))
        .where(CostLedger.kind == KIND_SETTLE)
        .group_by(column)
    )
    for field, value in filters.items():
        if value is not None:
            stmt = stmt.where(getattr(CostLedger, field) == value)
    return [
        {column.key: key, "cents": int(total)}
        for key, total in session.execute(stmt).all()
        if key is not None
    ]


def summary(
    session: Session,
    *,
    episode_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if episode_id:
        filters["episode_id"] = episode_id
    if project_id:
        filters["project_id"] = project_id
    total = _sum(session, KIND_SETTLE, **filters)
    snap = quota_snapshot(session)
    by_stage = _group(session, CostLedger.stage, **filters)
    by_job_type_rows = session.execute(
        select(Job.type, func.coalesce(func.sum(Job.cost_settled_cents), 0))
        .where(Job.cost_settled_cents > 0)
        .group_by(Job.type)
    ).all()
    return {
        "total_cents": total,
        "by_stage": by_stage,
        "by_kind": [{"kind": k, "cents": int(v)} for k, v in by_job_type_rows],
        "quota": snap,
        "remaining_quota_cents": snap["remaining_cents"],
    }


def project_total(session: Session, project_id: str) -> int:
    return _sum(session, KIND_SETTLE, project_id=project_id)


def episode_total(session: Session, episode_id: str) -> int:
    return _sum(session, KIND_SETTLE, episode_id=episode_id)


def ensure_owner_scope(session: Session, episode_id: str | None) -> tuple[str | None, str | None]:
    """由 episode 反查 project，用于成本归集。"""
    if not episode_id:
        return None, None
    episode = session.get(Episode, episode_id)
    if episode is None:
        return None, None
    project = session.get(Project, episode.project_id)
    return (project.id if project else None), episode.id
