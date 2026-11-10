"""已提交给供应商、但本地没拿到产物的任务：在有效期内自动补拉。

为什么需要这一层：
- 供应商的任务一旦提交，扣费就发生了；本地因为网络/超时判失败时，钱已经花了。
- 成片在供应商源站只保留 24 小时，过期后谁也取不回——所以补救窗口就是这 24 小时。
- 用户不该盯着页面手动点「按任务号拉回」；worker 每隔一段时间自动试一次即可。

状态全部记在 `job.error_detail["recover"]` 里（JSON 字段），不新增表：
`{attempts, last_at, last_error, recovered_at, media_id}`。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError
from app.db.models import (
    AssetStatus,
    Episode,
    Job,
    JobStatus,
    MediaAsset,
    ProviderCall,
    Shot,
)
from app.providers.base import ProviderKind
from app.providers.registry import resolve
from app.services import cost as cost_service
from app.services import media as media_service

#: 补拉窗口：供应商成片只保留 24 小时，留 10 分钟余量
RECOVER_WINDOW_HOURS = 24
#: 单个任务最多试几次（每次间隔 5 分钟，24 小时内足够覆盖网络恢复）
MAX_ATTEMPTS = 6
RETRY_INTERVAL_SECONDS = 300

_KIND_BY_JOB_TYPE = {
    "VIDEO_GENERATION": ProviderKind.VIDEO,
    "IMAGE_GENERATION": ProviderKind.IMAGE,
}


def _recover_state(job: Job) -> dict[str, Any]:
    detail = job.error_detail if isinstance(job.error_detail, dict) else {}
    state = detail.get("recover")
    return state if isinstance(state, dict) else {}


def _write_state(job: Job, patch: dict[str, Any]) -> None:
    detail = dict(job.error_detail or {})
    state = dict(detail.get("recover") or {})
    state.update(patch)
    detail["recover"] = state
    job.error_detail = detail


def _task_id_for(session: Session, job: Job) -> str:
    row = session.scalar(
        select(ProviderCall.provider_job_id)
        .where(ProviderCall.job_id == job.id, ProviderCall.provider_job_id != "")
        .order_by(ProviderCall.id.desc())
        .limit(1)
    )
    return str(row or "")


def _shot_for(session: Session, job: Job) -> Shot | None:
    """任务输入里的镜号 → 镜头。补拉回来的片段只挂第一镜（一次生成就是一镜）。"""
    payload = job.input_json if isinstance(job.input_json, dict) else {}
    nos = payload.get("shot_nos") or []
    if not nos:
        return None
    return session.scalar(
        select(Shot).where(Shot.episode_id == job.episode_id, Shot.shot_no == int(nos[0]))
    )


def is_recoverable(job: Job, now: dt.datetime | None = None) -> bool:
    """还能不能试：失败、24 小时内、没补回来、重试次数没超。"""
    if job.status != JobStatus.FAILED.value:
        return False
    state = _recover_state(job)
    if state.get("recovered_at"):
        return False
    if int(state.get("attempts") or 0) >= MAX_ATTEMPTS:
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    finished = job.finished_at
    if finished is not None:
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=dt.timezone.utc)
        if (now - finished).total_seconds() > RECOVER_WINDOW_HOURS * 3600:
            return False
    last_at = state.get("last_at")
    if isinstance(last_at, str):
        try:
            last = dt.datetime.fromisoformat(last_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=dt.timezone.utc)
            if (now - last).total_seconds() < RETRY_INTERVAL_SECONDS:
                return False
        except ValueError:
            pass
    return True


def recover_one(session: Session, job: Job) -> dict[str, Any]:
    """对单个失败任务尝试补拉，返回结果摘要（不改 job.status，只补产物与账）。"""
    state = _recover_state(job)
    attempts = int(state.get("attempts") or 0) + 1
    task_id = _task_id_for(session, job)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    def fail(message: str) -> dict[str, Any]:
        _write_state(job, {"attempts": attempts, "last_at": now_iso, "last_error": message})
        return {"job_id": job.id, "ok": False, "error": message}

    if not task_id:
        return fail("任务没有记录供应商编号，无法补拉")
    kind = _KIND_BY_JOB_TYPE.get(str(job.type))
    if kind is None:
        return fail(f"任务类型 {job.type} 不支持补拉")
    provider = resolve(kind, session)
    fetch = getattr(provider, "fetch_result", None)
    if fetch is None:
        return fail("当前供应商不支持按任务号补拉")
    shot = _shot_for(session, job)
    if shot is None:
        return fail("找不到对应镜头（分镜可能已被删除）")

    from app.agents.story import run_coro

    result = run_coro(fetch(task_id))
    if not result.ok:
        return fail(result.error_message or "补拉失败")
    if not result.artifacts:
        return fail("供应商任务没有产物")

    artifact = result.artifacts[0]
    asset = media_service.save_artifact(
        session,
        artifact,
        kind="video" if kind is ProviderKind.VIDEO else "image",
        role="shot_video" if kind is ProviderKind.VIDEO else "shot_image",
        owner_type="shot",
        owner_id=shot.id,
        episode_id=shot.episode_id,
        project_id=job.project_id,
        status=AssetStatus.CONFIRMED.value,
        source="provider_recover",
        metadata={"provider_task_id": task_id, "recovered_job_id": job.id},
    )
    if kind is ProviderKind.VIDEO:
        if not asset.duration_ms:
            probed = media_service.probe_duration_ms(asset)
            if probed:
                asset.duration_ms = probed
        shot.video_media_id = asset.id
        shot.video_status = "CONFIRMED"
    else:
        shot.confirmed_media_id = asset.id
        shot.image_status = "CONFIRMED"

    # 供应商确实计费了，账本要如实补记（只补没结算过的）
    billed = 0
    usd = float((result.raw_summary or {}).get("billing_usd") or 0)
    if usd > 0 and not job.cost_settled_cents:
        billed = int(round(usd * settings.usd_cny_rate * 100))
        if billed > 0:
            cost_service.settle(
                session,
                job=job,
                actual_cents=billed,
                note=(
                    f"补记：{task_id} 供应商已计费，产物由后台自动补拉取回"
                    f"（原任务标记失败时曾全额释放预留）"
                ),
            )

    _write_state(
        job,
        {
            "attempts": attempts,
            "last_at": now_iso,
            "recovered_at": now_iso,
            "media_id": asset.id,
            "last_error": "",
        },
    )
    job.result_ref = {
        **(job.result_ref or {}),
        "recovered": True,
        "media_ids": [asset.id],
        "provider_task_id": task_id,
    }
    session.flush()
    return {
        "job_id": job.id,
        "ok": True,
        "media_id": asset.id,
        "shot_no": shot.shot_no,
        "billed_cents": billed,
    }


def candidates(session: Session, *, limit: int = 5) -> list[Job]:
    """待补拉的失败任务（最近 24 小时内、有网关编号的优先）。"""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=RECOVER_WINDOW_HOURS)
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.FAILED.value)
        .where(Job.finished_at >= cutoff)
        .where(Job.type.in_(list(_KIND_BY_JOB_TYPE.keys())))
        .order_by(Job.finished_at.desc())
        .limit(limit * 3)
    )
    picked: list[Job] = []
    for job in session.scalars(stmt):
        if is_recoverable(job) and _task_id_for(session, job):
            picked.append(job)
        if len(picked) >= limit:
            break
    return picked


def sweep(session: Session, *, limit: int = 5) -> list[dict[str, Any]]:
    """worker 定时调用：把能补的任务都试一遍。异常不外抛，避免拖垮 tick。"""
    results: list[dict[str, Any]] = []
    for job in candidates(session, limit=limit):
        try:
            results.append(recover_one(session, job))
        except AppError as exc:
            _write_state(
                job,
                {
                    "attempts": int(_recover_state(job).get("attempts") or 0) + 1,
                    "last_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "last_error": exc.message,
                },
            )
            results.append({"job_id": job.id, "ok": False, "error": exc.message})
        except Exception as exc:  # noqa: BLE001 - 单个任务失败不能影响其它任务
            _write_state(
                job,
                {
                    "attempts": int(_recover_state(job).get("attempts") or 0) + 1,
                    "last_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "last_error": f"{type(exc).__name__}: {exc}"[:300],
                },
            )
            results.append({"job_id": job.id, "ok": False, "error": str(exc)})
    return results
