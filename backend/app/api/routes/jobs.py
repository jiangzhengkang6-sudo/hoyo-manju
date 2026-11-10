"""任务中心与 SSE 事件流（FR-070~074）。"""

from __future__ import annotations

import json
import time
from typing import Iterator

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import SessionDep, get_episode
from app.core.config import settings
from app.core.errors import NotFound
from app.db.models import Job, ProviderCall, TERMINAL_JOB_STATUSES
from app.db.session import SessionLocal
from app.services import events
from app.services import jobs as jobs_service

router = APIRouter(tags=["jobs"])

HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 0.5


@router.get("/jobs")
def list_jobs(
    session: SessionDep,
    episode_id: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
    type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> dict:
    result = jobs_service.list_jobs(
        session,
        episode_id=episode_id,
        project_id=project_id,
        status=status,
        type=type,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [jobs_service.to_dict(j) for j in result["items"]],
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


@router.get("/jobs/{job_id}")
def read_job(job_id: str, session: SessionDep) -> dict:
    job = jobs_service.get_job(session, job_id)
    data = jobs_service.to_dict(job)
    data["input_json"] = job.input_json
    calls = session.scalars(
        select(ProviderCall).where(ProviderCall.job_id == job.id).order_by(ProviderCall.created_at)
    )
    data["provider_calls"] = [
        {
            "id": c.id,
            "provider": c.provider,
            "model": c.model,
            "request_id": c.request_id,
            "provider_job_id": c.provider_job_id,
            "price_version": c.price_version,
            "billing_unit": c.billing_unit,
            "usage": c.usage_json,
            "estimated_cost_cents": c.estimated_cost_cents,
            "cost_cents": c.cost_cents,
            "status": c.status,
            "error_code": c.error_code,
            "raw_summary": c.raw_summary,
            "created_at": c.created_at,
        }
        for c in calls
    ]
    if job.parent_job_id:
        parent = session.get(Job, job.parent_job_id)
        data["parent"] = jobs_service.to_dict(parent) if parent else None
    children = session.scalars(
        select(Job).where(Job.parent_job_id == job.id).order_by(Job.created_at)
    )
    data["retries"] = [jobs_service.to_dict(c) for c in children]
    return data


@router.get("/jobs/{job_id}/provider-calls")
def job_provider_calls(job_id: str, session: SessionDep) -> dict:
    jobs_service.get_job(session, job_id)
    rows = session.scalars(
        select(ProviderCall).where(ProviderCall.job_id == job_id).order_by(ProviderCall.created_at)
    )
    return {
        "items": [
            {
                "id": c.id,
                "provider": c.provider,
                "model": c.model,
                "request_id": c.request_id,
                "price_version": c.price_version,
                "billing_unit": c.billing_unit,
                "usage": c.usage_json,
                "estimated_cost_cents": c.estimated_cost_cents,
                "cost_cents": c.cost_cents,
                "status": c.status,
                "raw_summary": c.raw_summary,
                "created_at": c.created_at,
            }
            for c in rows
        ]
    }


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, session: SessionDep) -> dict:
    job = jobs_service.get_job(session, job_id)
    new_job = jobs_service.retry_job(session, job, reason="用户手动重试")
    session.flush()
    return {"job": jobs_service.to_dict(new_job), "retry_of": job.id}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, session: SessionDep) -> dict:
    job = jobs_service.get_job(session, job_id)
    result = jobs_service.cancel_job(session, job)
    session.flush()
    return {"job": jobs_service.to_dict(job), **result}


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #

def _sse_stream(
    request: Request,
    *,
    last_event_id: int,
    episode_id: str | None,
    project_id: str | None,
) -> Iterator[str]:
    cursor = last_event_id
    last_beat = time.time()
    while True:
        session = SessionLocal()
        try:
            rows = events.fetch_since(
                session, cursor, episode_id=episode_id, project_id=project_id, limit=200
            )
            payloads = [
                (
                    row.id,
                    row.event_type,
                    json.dumps(
                        {**(row.payload or {}), "episode_id": row.episode_id},
                        ensure_ascii=False,
                        default=str,
                    ),
                )
                for row in rows
            ]
        finally:
            session.close()

        for event_id, event_type, data in payloads:
            cursor = event_id
            yield f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"

        now = time.time()
        if now - last_beat >= HEARTBEAT_SECONDS:
            last_beat = now
            yield f": heartbeat {int(now)}\n\n"
        time.sleep(POLL_SECONDS)


def _last_id(last_event_id: int | None, header: str | None, episode_id: str | None) -> int:
    if last_event_id:
        return int(last_event_id)
    if header:
        try:
            return int(header)
        except ValueError:
            pass
    session = SessionLocal()
    try:
        latest = events.latest_id(session, episode_id=episode_id)
        return max(latest - 50, 0)
    finally:
        session.close()


@router.get("/episodes/{episode_id}/events")
def episode_events(
    episode_id: str,
    request: Request,
    session: SessionDep,
    last_event_id: int | None = Query(None),
    last_event_id_header: str | None = Header(None, alias="Last-Event-ID"),
) -> StreamingResponse:
    get_episode(session, episode_id=episode_id)
    cursor = _last_id(last_event_id, last_event_id_header, episode_id)
    return StreamingResponse(
        _sse_stream(request, last_event_id=cursor, episode_id=episode_id, project_id=None),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/events")
def workspace_events(
    request: Request,
    last_event_id: int | None = Query(None),
    last_event_id_header: str | None = Header(None, alias="Last-Event-ID"),
) -> StreamingResponse:
    cursor = _last_id(last_event_id, last_event_id_header, None)
    return StreamingResponse(
        _sse_stream(request, last_event_id=cursor, episode_id=None, project_id=None),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/events/history")
def event_history(
    session: SessionDep,
    episode_id: str | None = None,
    limit: int = Query(100, le=500),
) -> dict:
    latest = events.latest_id(session, episode_id=episode_id)
    rows = events.fetch_since(session, max(latest - limit, 0), episode_id=episode_id, limit=limit)
    return {
        "items": [
            {
                "id": r.id,
                "type": r.event_type,
                "payload": r.payload,
                "episode_id": r.episode_id,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }
