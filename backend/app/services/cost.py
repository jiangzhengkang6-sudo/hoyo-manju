"""成本与配额：预估、预留、结算、释放、流水（需求文档 §12）。"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import QuotaExceeded
from app.db.models import CostLedger, Job, JobStatus, Project, Episode

logger = logging.getLogger("hoyomanju.cost")

KIND_RESERVE = "RESERVE"
KIND_SETTLE = "SETTLE"
KIND_RELEASE = "RELEASE"


def _sum(session: Session, kind: str, **filters: Any) -> int:
    stmt = select(func.coalesce(func.sum(CostLedger.amount_cents), 0)).where(CostLedger.kind == kind)
    for field, value in filters.items():
        if value is not None:
            stmt = stmt.where(getattr(CostLedger, field) == value)
    return int(session.scalar(stmt) or 0)


def cents_per_credit() -> float:
    """1 个供应商积分折合多少分（人民币）。

    ToAPIs 实测口径：1 积分 = $0.005（32.9652 积分 = $0.164826），
    按 USD_CNY_RATE 折算后约 3.6 分。内部账本仍用「分」记（整数不丢精度），
    只在展示层换算成积分，让界面数字和供应商后台对得上。
    """
    return max(settings.credit_usd_value * settings.usd_cny_rate * 100, 0.0001)


def to_credits(cents: int | float) -> float:
    """分 → 积分（保留 2 位小数，和供应商后台的显示精度一致）。"""
    return round(float(cents) / cents_per_credit(), 2)


def quota_snapshot(session: Session, *, limit_cents: int | None = None) -> dict[str, Any]:
    limit = settings.quota_limit_cents if limit_cents is None else limit_cents
    # 优先用「积分」配额：用户按供应商的充值套餐填，界面才不会出现
    # 「¥200 但不知道还能生成几次」这种对不上的数字。
    if limit_cents is None and settings.quota_limit_credits > 0:
        limit = int(round(settings.quota_limit_credits * cents_per_credit()))
    used = _sum(session, KIND_SETTLE)
    # 预留只可能 >= 0：负数是历史重复释放留下的脏数据，
    # 展示时夹到 0，别让它把「剩余额度」凭空放大。
    raw_reserved = _sum(session, KIND_RESERVE) + _sum(session, KIND_RELEASE)
    reserved = max(0, raw_reserved)
    if raw_reserved < 0:
        # 夹 0 只是不让用户看到负数，偏差仍在；不记日志的话，等下一次有并发任务时
        # 这份负值会把可用额度算多（例如真冻结 100、账上 -50，就只有 50 被算进去）。
        logger.warning(
            "成本台账异常：冻结额度为负（%s 分），疑似某笔预留被重复释放", raw_reserved
        )
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
    rate = cents_per_credit()
    return {
        "limit_cents": limit,
        "used_cents": used,
        "reserved_cents": reserved,
        "remaining_cents": remaining,
        # 供应商口径（积分）：界面上以它为主，人民币作为辅助
        "limit_credits": round(limit / rate, 2),
        "used_credits": round(used / rate, 2),
        "reserved_credits": round(reserved / rate, 2),
        "remaining_credits": round(remaining / rate, 2),
        "cents_per_credit": round(rate, 4),
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


def outstanding_reserved(session: Session, job_id: str) -> int:
    """该任务**当前仍未结清**的预留（台账口径，不看模型字段）。

    为什么以台账为准：`job.cost_reserved_cents` 是内存里的一个数字，
    任务被重跑/跨会话复用时很容易拿到旧值，于是同一笔预留被释放两次，
    `reserved` 变成负数（配额被凭空放大）。台账是唯一事实来源。
    """
    # 会话关闭了 autoflush，必须先落库再聚合，否则看不到刚写入的那一条
    session.flush()
    total = session.scalar(
        select(func.coalesce(func.sum(CostLedger.amount_cents), 0)).where(
            CostLedger.job_id == job_id,
            CostLedger.kind.in_([KIND_RESERVE, KIND_RELEASE]),
        )
    )
    return max(0, int(total or 0))


def _settled_times(session: Session, job_id: str) -> int:
    session.flush()
    return int(
        session.scalar(
            select(func.count(CostLedger.id)).where(
                CostLedger.job_id == job_id, CostLedger.kind == KIND_SETTLE
            )
        )
        or 0
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
    """成功后按实际消耗结算，并释放该任务尚未结清的预留。

    幂等：同一个任务被重复执行（工作流节点重试、worker 重复领取）时，
    只记一次 SETTLE，避免「扣两次费」。
    """
    if _settled_times(session, job.id):
        job.cost_settled_cents = int(job.cost_settled_cents or 0) or actual_cents
        job.cost_reserved_cents = outstanding_reserved(session, job.id)
        return
    reserved = outstanding_reserved(session, job.id)
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
    reserved = outstanding_reserved(session, job.id)
    if reserved <= 0:
        job.cost_reserved_cents = 0
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
