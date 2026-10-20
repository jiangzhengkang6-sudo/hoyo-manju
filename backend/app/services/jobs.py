"""任务中心：幂等创建、租约锁、状态机、重试、取消（需求文档 §5.8 / §8.4 / §9.2）。"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import Conflict, NotFound, StateConflict
from app.core.security import FREE_TEXT_KEYS, new_id, redact_payload
from app.db.models import Job, JobStatus, TERMINAL_JOB_STATUSES, utcnow
from app.services import cost as cost_service
from app.services import events

JobType = str


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def build_idempotency_key(*parts: Any) -> str:
    return ":".join(str(p) for p in parts if p is not None)


def idempotency_bucket(window_seconds: int | None = None) -> int:
    """把当前时间压进时间桶，用于「短时间内的重复点击」去重。

    窗口外的请求会得到不同的桶，因此仍可正常发起一次新的生成。
    """
    window = int(window_seconds or settings.idempotency_window_seconds or 5)
    return int(_now().timestamp()) // max(1, window)


def build_windowed_key(*parts: Any, window_seconds: int | None = None) -> str:
    """生成类接口的幂等键：业务维度 + 时间桶。"""
    return build_idempotency_key(*parts, idempotency_bucket(window_seconds))


def find_by_idempotency(session: Session, key: str) -> Job | None:
    return session.scalar(select(Job).where(Job.idempotency_key == key))


def create_job(
    session: Session,
    *,
    type: str,
    idempotency_key: str,
    episode_id: str | None = None,
    project_id: str | None = None,
    workflow_run_id: str | None = None,
    stage: str = "",
    input_json: dict[str, Any] | None = None,
    input_summary: dict[str, Any] | None = None,
    provider: str = "",
    model: str = "",
    max_attempts: int | None = None,
    parent_job_id: str | None = None,
    reserve_cents: int = 0,
    auto_start: bool = True,
    on_replay: Literal["return", "error"] = "return",
) -> Job:
    """创建任务。命中幂等键时默认返回既有任务，保证不重复扣费。"""
    existing = find_by_idempotency(session, idempotency_key)
    if existing is not None:
        if on_replay == "error" and existing.status not in TERMINAL_JOB_STATUSES:
            raise Conflict(
                f"相同幂等键的任务已在执行中：{existing.id}",
                code="IDEMPOTENCY_REPLAY",
                details={"job_id": existing.id, "status": existing.status},
            )
        return existing

    if episode_id and not project_id:
        project_id, _ = cost_service.ensure_owner_scope(session, episode_id)

    job = Job(
        id=new_id("job"),
        workflow_run_id=workflow_run_id,
        episode_id=episode_id,
        project_id=project_id,
        type=type,
        status=JobStatus.QUEUED.value if auto_start else JobStatus.CREATED.value,
        stage=stage or type.lower(),
        idempotency_key=idempotency_key,
        # FR-093 / §11.1：任务输入在入库前脱敏。自由文本字段（故事正文、提示词、
        # 台词、备注等）按 strict 截断，结构化小字段（shot_nos、preset 等）原样保留。
        input_json=redact_payload(input_json or {}, truncate_keys=FREE_TEXT_KEYS),
        input_summary=redact_payload(input_summary or {}, truncate_keys=FREE_TEXT_KEYS),
        provider=provider,
        model=model,
        max_attempts=max_attempts if max_attempts is not None else 1 + settings.auto_retry_limit,
        parent_job_id=parent_job_id,
    )
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        again = find_by_idempotency(session, idempotency_key)
        if again is None:
            raise
        return again

    if reserve_cents > 0:
        cost_service.reserve(session, job=job, cents=reserve_cents, stage=job.stage)

    events.publish(
        session,
        events.JOB_CREATED,
        {
            "job_id": job.id,
            "type": job.type,
            "status": job.status,
            "episode_id": job.episode_id,
            "project_id": job.project_id,
            "stage": job.stage,
        },
        episode_id=job.episode_id,
        project_id=job.project_id,
    )
    return job


def get_job(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise NotFound(f"任务不存在：{job_id}")
    return job


def list_jobs(
    session: Session,
    *,
    episode_id: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
    type: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    stmt = select(Job).order_by(Job.created_at.desc())
    count_stmt = select(func.count(Job.id))
    for field, value in (
        ("episode_id", episode_id),
        ("project_id", project_id),
        ("status", status),
        ("type", type),
    ):
        if value:
            stmt = stmt.where(getattr(Job, field) == value)
            count_stmt = count_stmt.where(getattr(Job, field) == value)
    total = int(session.scalar(count_stmt) or 0)
    rows = list(session.scalars(stmt.offset((page - 1) * page_size).limit(page_size)))
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


def set_status(
    session: Session,
    job: Job,
    status: str,
    *,
    progress: int | None = None,
    result_ref: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    error_detail: dict[str, Any] | None = None,
    publish: bool = True,
) -> Job:
    job.status = status
    if progress is not None:
        job.progress = max(0, min(100, progress))
    if result_ref is not None:
        job.result_ref = result_ref
    if error_code:
        job.error_code = error_code
    if error_message:
        job.error_message = error_message
    if error_detail is not None:
        job.error_detail = error_detail
    if status == JobStatus.RUNNING.value and job.started_at is None:
        job.started_at = _now()
    if status in TERMINAL_JOB_STATUSES:
        job.finished_at = _now()
        if job.started_at:
            job.duration_ms = int((job.finished_at - job.started_at).total_seconds() * 1000)
        job.lease_owner = None
        job.lease_expires_at = None
    if publish:
        mapping = {
            JobStatus.SUCCEEDED.value: events.JOB_SUCCEEDED,
            JobStatus.FAILED.value: events.JOB_FAILED,
            JobStatus.CANCELLED.value: events.JOB_CANCELLED,
        }
        events.publish(
            session,
            mapping.get(status, events.JOB_STATUS_CHANGED),
            {
                "job_id": job.id,
                "status": job.status,
                "progress": job.progress,
                "episode_id": job.episode_id,
                "project_id": job.project_id,
                "type": job.type,
                "stage": job.stage,
                "error_code": job.error_code,
                "error_message": job.error_message,
                "result_ref": job.result_ref,
            },
            episode_id=job.episode_id,
            project_id=job.project_id,
        )
    return job


def set_progress(session: Session, job: Job, progress: int, message: str = "") -> None:
    job.progress = max(0, min(100, progress))
    events.publish(
        session,
        events.JOB_PROGRESS,
        {
            "job_id": job.id,
            "status": job.status,
            "progress": job.progress,
            "message": message,
            "episode_id": job.episode_id,
        },
        episode_id=job.episode_id,
        project_id=job.project_id,
    )


# --------------------------------------------------------------------------- #
# 租约与调度
# --------------------------------------------------------------------------- #

def claim_job(session: Session, job_id: str, worker_id: str) -> bool:
    """用条件 UPDATE 抢占任务，保证重复消息只被一个 worker 执行。"""
    now = _now()
    stmt = (
        update(Job)
        .where(
            Job.id == job_id,
            Job.status.in_([JobStatus.CREATED.value, JobStatus.QUEUED.value,
                            JobStatus.RETRYING.value]),
            (Job.lease_expires_at.is_(None)) | (Job.lease_expires_at < now),
            # 自动重试退避未到期的任务不抢占
            (Job.next_attempt_at.is_(None)) | (Job.next_attempt_at <= now),
        )
        .values(
            status=JobStatus.RUNNING.value,
            lease_owner=worker_id,
            lease_expires_at=now + dt.timedelta(seconds=settings.worker_lease_seconds),
            started_at=func.coalesce(Job.started_at, now),
            attempt=Job.attempt,
            next_attempt_at=None,
        )
    )
    result = session.execute(stmt)
    session.flush()
    return bool(result.rowcount)


def renew_lease(session: Session, job: Job, worker_id: str) -> None:
    job.lease_owner = worker_id
    job.lease_expires_at = _now() + dt.timedelta(seconds=settings.worker_lease_seconds)
    session.flush()


def next_queued_job_ids(session: Session, limit: int = 10) -> list[str]:
    now = _now()
    stmt = (
        select(Job.id)
        .where(
            Job.status.in_([JobStatus.CREATED.value, JobStatus.QUEUED.value,
                            JobStatus.RETRYING.value]),
            (Job.lease_expires_at.is_(None)) | (Job.lease_expires_at < now),
            (Job.next_attempt_at.is_(None)) | (Job.next_attempt_at <= now),
        )
        .order_by(Job.created_at)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def schedule_auto_retry(
    session: Session,
    job: Job,
    *,
    retryable: bool,
    delay_seconds: int | None = None,
) -> bool:
    """FR-072：可重试的错误在还有剩余尝试次数时，退避后自动重入队。

    命中时任务置 `RETRYING` 并递增 `attempt`（同一任务记录，不覆盖原始错误信息——
    `error_code/error_message/error_detail` 保留本次失败原因，便于排查）。
    返回 True 表示已安排自动重试，调用方不应再把任务标成 FAILED。
    """
    if not retryable:
        return False
    if int(job.attempt or 1) >= int(job.max_attempts or 1):
        return False
    if job.cancel_requested:
        return False
    delay = (
        int(delay_seconds)
        if delay_seconds is not None
        else settings.retry_backoff_seconds * int(job.attempt or 1)
    )
    job.status = JobStatus.RETRYING.value
    job.attempt = int(job.attempt or 1) + 1
    job.next_attempt_at = _now() + dt.timedelta(seconds=max(0, delay))
    job.lease_owner = None
    job.lease_expires_at = None
    events.publish(
        session,
        events.JOB_STATUS_CHANGED,
        {
            "job_id": job.id,
            "status": job.status,
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
            "next_attempt_in_seconds": max(0, delay),
        },
        episode_id=job.episode_id,
        project_id=job.project_id,
    )
    session.flush()
    return True


def expire_stale_jobs(session: Session, *, ttl_seconds: int | None = None) -> int:
    """把长时间停留在非终态的任务标记为 `EXPIRED`（FR-070 的「已过期」）。

    覆盖「排队但一直没人执行」和「重试退避后长期无人接管」两种堆积情况；
    正在执行（RUNNING，持有租约）的任务不在此列，由租约回收处理。
    """
    ttl = int(ttl_seconds or settings.job_expire_seconds)
    if ttl <= 0:
        return 0
    deadline = _now() - dt.timedelta(seconds=ttl)
    stmt = (
        update(Job)
        .where(
            Job.status.in_([JobStatus.CREATED.value, JobStatus.QUEUED.value,
                            JobStatus.RETRYING.value]),
            Job.created_at < deadline,
        )
        .values(
            status=JobStatus.EXPIRED.value,
            finished_at=_now(),
            next_attempt_at=None,
            error_code="JOB_EXPIRED",
            error_message=f"任务超过 {ttl} 秒仍未完成，已标记为过期",
        )
    )
    result = session.execute(stmt)
    session.flush()
    return int(result.rowcount or 0)


def reap_expired_leases(session: Session) -> int:
    """worker 崩溃后回收租约过期的 RUNNING 任务。"""
    now = _now()
    stmt = (
        update(Job)
        .where(
            Job.status == JobStatus.RUNNING.value,
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at < now,
        )
        .values(status=JobStatus.QUEUED.value, lease_owner=None, lease_expires_at=None)
    )
    result = session.execute(stmt)
    session.flush()
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# 重试 / 取消
# --------------------------------------------------------------------------- #

def retry_job(session: Session, job: Job, *, reason: str = "") -> Job:
    if job.status not in TERMINAL_JOB_STATUSES:
        raise StateConflict(
            f"任务仍在执行中（{job.status}），请等待结束或先取消后再重试",
            details={"job_id": job.id, "status": job.status},
        )
    base_key = job.idempotency_key.split("#retry")[0]
    attempt = int(session.scalar(
        select(func.count(Job.id)).where(Job.idempotency_key.like(f"{base_key}#retry%"))
    ) or 0) + 1
    new_job = Job(
        id=new_id("job"),
        workflow_run_id=job.workflow_run_id,
        episode_id=job.episode_id,
        project_id=job.project_id,
        type=job.type,
        status=JobStatus.QUEUED.value,
        stage=job.stage,
        idempotency_key=f"{base_key}#retry{attempt}",
        input_json=job.input_json,
        input_summary=job.input_summary,
        provider=job.provider,
        model=job.model,
        max_attempts=job.max_attempts,
        parent_job_id=job.parent_job_id or job.id,
    )
    session.add(new_job)
    session.flush()
    if job.cost_reserved_cents:
        cost_service.reserve(session, job=new_job, cents=job.cost_reserved_cents,
                             stage=new_job.stage, note=f"重试预留（原任务 {job.id}）")
    events.publish(
        session,
        events.JOB_CREATED,
        {
            "job_id": new_job.id,
            "type": new_job.type,
            "status": new_job.status,
            "retry_of": job.id,
            "reason": reason,
            "episode_id": new_job.episode_id,
        },
        episode_id=new_job.episode_id,
        project_id=new_job.project_id,
    )
    return new_job


def cancel_job(session: Session, job: Job) -> dict[str, Any]:
    if job.status in TERMINAL_JOB_STATUSES:
        raise StateConflict(f"任务已处于终态，无法取消：{job.status}")
    if job.status in (JobStatus.CREATED.value, JobStatus.QUEUED.value, JobStatus.RETRYING.value):
        released = int(job.cost_reserved_cents or 0)
        cost_service.release(session, job=job, reason="取消排队任务，释放预留")
        set_status(session, job, JobStatus.CANCELLED.value)
        return {
            "status": job.status,
            "released_cents": released,
            "note": "排队任务已取消，配额已释放。" if released else "排队任务已取消。",
        }
    if job.status == JobStatus.RUNNING.value:
        job.cancel_requested = True
        set_status(session, job, JobStatus.CANCELLING.value)
        return {
            "status": job.status,
            "released_cents": 0,
            "note": "已停止等待，但供应商可能仍在计费；任务将在下一个检查点停止。",
        }
    raise StateConflict(f"当前状态不可取消：{job.status}")


def is_cancel_requested(session: Session, job_id: str) -> bool:
    session.expire_all()
    job = session.get(Job, job_id)
    return bool(job and (job.cancel_requested or job.status == JobStatus.CANCELLING.value))


def to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "workflow_run_id": job.workflow_run_id,
        "episode_id": job.episode_id,
        "project_id": job.project_id,
        "type": job.type,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        #: 自动重试的下次可执行时间（RETRYING 时非空，供任务中心展示倒计时）
        "next_attempt_at": job.next_attempt_at,
        "idempotency_key": job.idempotency_key,
        "input_summary": job.input_summary,
        "provider": job.provider,
        "model": job.model,
        "cost_reserved_cents": job.cost_reserved_cents,
        "cost_settled_cents": job.cost_settled_cents,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "error_detail": job.error_detail,
        "parent_job_id": job.parent_job_id,
        "result_ref": job.result_ref,
        "cancel_requested": job.cancel_requested,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "duration_ms": job.duration_ms,
    }


def stats(session: Session, episode_id: str) -> dict[str, int]:
    rows = session.execute(
        select(Job.status, func.count(Job.id)).where(Job.episode_id == episode_id).group_by(Job.status)
    ).all()
    return {status: int(count) for status, count in rows}


def updated_at_guard(job: Job) -> dt.datetime:
    return job.updated_at or utcnow()
