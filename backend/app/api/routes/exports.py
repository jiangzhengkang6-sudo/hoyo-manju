"""时间线校验、自动合成、预览与导出（FR-060~064）。"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from fastapi.responses import Response, RedirectResponse
from sqlalchemy import select

from app.agents import edit as edit_agent
from app.agents import safety as safety_agent
from app.api.deps import SessionDep, get_episode
from app.core.errors import NotFound
from app.db.models import Episode, EpisodeStage, ExportRecord, MediaAsset
from app.schemas import AppealCreateIn, RenderExportIn, RenderPreviewIn
from app.services import audit as audit_service
from app.services import degraded as degraded_service
from app.services import jobs as jobs_service
from app.services import media as media_service
from app.services import versions as versions_service

router = APIRouter(tags=["export"])


def _timeline_snapshot(timeline: dict) -> dict:
    """时间线快照：去掉每条音轨的逐条明细噪声，只保留可复现的关键结构。

    完整时间线动辄上千行，全部存进版本表既浪费又难比对；
    这里保留尺寸/时长/每镜的媒体与转场，足以回答「当时合成的是什么」。
    """
    return {
        "aspect_ratio": timeline.get("aspect_ratio"),
        "width": timeline.get("width"),
        "height": timeline.get("height"),
        "total_duration_ms": timeline.get("total_duration_ms"),
        "shots": [
            {
                "shot_no": entry.get("shot_no"),
                "media_id": entry.get("media_id"),
                "kind": entry.get("kind"),
                "start_ms": entry.get("start_ms"),
                "duration_ms": entry.get("duration_ms"),
                "transition": (entry.get("transition") or {}).get("type"),
            }
            for entry in timeline.get("tracks", {}).get("video", [])
        ],
        "dialogue_count": len(timeline.get("tracks", {}).get("dialogue", [])),
        "bgm_count": len(timeline.get("tracks", {}).get("bgm", [])),
        "subtitle_count": len(timeline.get("tracks", {}).get("subtitles", [])),
    }


@router.get("/episodes/{episode_id}/timeline")
def read_timeline(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    return edit_agent.build_timeline(session, episode)


@router.get("/episodes/{episode_id}/timeline/validate")
def validate_timeline(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    result = edit_agent.validate_timeline(session, episode)
    return {
        "valid": result["valid"],
        "errors": result["errors"],
        "warnings": result["warnings"],
        "total_duration_ms": result["timeline"]["total_duration_ms"],
        "shot_count": len(result["timeline"]["tracks"]["video"]),
    }


@router.get("/episodes/{episode_id}/export-gate")
def read_export_gate(episode_id: str, session: SessionDep) -> dict:
    """§12-8：导出闸门状态 + 已提交的申诉。

    前端用它决定「能否确认/导出」，并在被拦截时展示原因与申诉入口 ——
    否则用户只会看到一个 422，不知道该改什么、也不知道能申诉。
    """
    episode = get_episode(session, episode_id=episode_id)
    return safety_agent.export_gate(session, episode)


@router.post("/episodes/{episode_id}/appeals", status_code=201)
def create_appeal(episode_id: str, payload: AppealCreateIn, session: SessionDep) -> dict:
    """§12-8 申诉入口。"""
    episode = get_episode(session, episode_id=episode_id)
    appeal = safety_agent.submit_appeal(
        session,
        episode=episode,
        target_type=payload.target_type,
        target_id=payload.target_id,
        reason=payload.reason,
        detail=payload.detail,
    )
    session.flush()
    return safety_agent.appeal_to_dict(appeal)


@router.get("/episodes/{episode_id}/appeals")
def list_episode_appeals(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    items = [safety_agent.appeal_to_dict(a) for a in safety_agent.list_appeals(session, episode.id)]
    return {
        "items": items,
        "pending": len([a for a in items if a["status"] == "PENDING"]),
        "approved": len([a for a in items if a["status"] == "APPROVED"]),
        "rejected": len([a for a in items if a["status"] == "REJECTED"]),
    }


@router.post("/episodes/{episode_id}/render/preview")
def render_preview(episode_id: str, payload: RenderPreviewIn, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    # §12-9：对象存储不可用时暂停新媒体任务，但已有素材的预览与导出不受影响
    degraded_service.ensure_available(degraded_service.KIND_STORAGE, action="预览合成")
    # §12-8：被拦截的内容禁止预览/确认/导出（申诉经人工批准后才放行）
    safety_agent.assert_export_allowed(session, episode)
    validation = edit_agent.validate_timeline(session, episode, only_ready=payload.partial)
    if not validation["valid"]:
        from app.core.errors import RenderError

        raise RenderError(
            "时间线校验未通过，无法生成预览",
            details={"errors": validation["errors"], "warnings": validation["warnings"]},
        )
    # FR-010 / FR-042 / §12-6：合成也占配额，不足则在建任务前阻断
    quote = edit_agent.estimate_render(
        session, episode, kind="preview", only_ready=payload.partial
    )
    job = jobs_service.create_job(
        session,
        type="RENDER_PREVIEW",
        idempotency_key=jobs_service.build_idempotency_key(
            episode.id, "RENDER_PREVIEW", episode.version,
            int(dt.datetime.now(dt.timezone.utc).timestamp() // 5),
        ),
        episode_id=episode.id,
        project_id=episode.project_id,
        stage="render_preview",
        input_json={"watermark": payload.watermark, "partial": payload.partial},
        input_summary={
            "kind": "preview",
            "watermark": payload.watermark,
            "estimate_cents": quote["estimate_cents"],
        },
        provider=quote["provider"],
        model=quote["model"],
        reserve_cents=quote["estimate_cents"],
    )
    episode.stage = EpisodeStage.RENDERING.value
    # §11.1-3：导出/合成本身也要留痕（含格式与成本预估）
    audit_service.record(
        session,
        action=audit_service.ACTION_RENDER,
        target_type=audit_service.TARGET_EXPORT,
        target_id=job.id,
        project_id=episode.project_id,
        episode_id=episode.id,
        detail={
            "kind": "preview",
            "job_type": job.type,
            "watermark": payload.watermark,
            "estimate_cents": quote["estimate_cents"],
        },
    )
    session.flush()
    return {**jobs_service.to_dict(job), "estimate": quote}


@router.post("/episodes/{episode_id}/render/export")
def render_export(episode_id: str, payload: RenderExportIn, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    # §12-9：同上——暂停新媒体任务，已有资源照常可导出
    degraded_service.ensure_available(degraded_service.KIND_STORAGE, action="正式导出")
    # §12-8：被拦截的内容禁止正式导出
    safety_agent.assert_export_allowed(session, episode)
    validation = edit_agent.validate_timeline(session, episode)
    if not validation["valid"]:
        from app.core.errors import RenderError

        raise RenderError(
            "时间线校验未通过，无法正式导出",
            details={"errors": validation["errors"], "warnings": validation["warnings"]},
        )
    # FR-010 / FR-042 / §12-6：合成也占配额，不足则在建任务前阻断
    quote = edit_agent.estimate_render(session, episode, kind="final")
    job = jobs_service.create_job(
        session,
        type="RENDER_EXPORT",
        idempotency_key=jobs_service.build_idempotency_key(
            episode.id, "RENDER_EXPORT", episode.version, payload.preset,
            int(dt.datetime.now(dt.timezone.utc).timestamp() // 5),
        ),
        episode_id=episode.id,
        project_id=episode.project_id,
        stage="export",
        input_json={"preset": payload.preset, "watermark": payload.watermark},
        input_summary={"preset": payload.preset, "estimate_cents": quote["estimate_cents"]},
        provider=quote["provider"],
        model=quote["model"],
        reserve_cents=quote["estimate_cents"],
    )
    episode.stage = EpisodeStage.RENDERING.value
    # FR-004：时间线留版本，便于回溯「这份成片当时的时间线长什么样」。
    # 内容没变时不会产生新版本（versions.record 按指纹去重）。
    versions_service.record(
        session,
        entity_type=versions_service.TARGET_TIMELINE,
        entity_id=episode.id,
        episode_id=episode.id,
        payload=_timeline_snapshot(validation["timeline"]),
        note=f"正式导出 {payload.preset}",
    )
    # §11.1-3：正式导出留痕，detail 记录预设（格式）与成本预估
    audit_service.record(
        session,
        action=audit_service.ACTION_EXPORT,
        target_type=audit_service.TARGET_EXPORT,
        target_id=job.id,
        project_id=episode.project_id,
        episode_id=episode.id,
        detail={
            "kind": "final",
            "preset": payload.preset,
            "watermark": payload.watermark,
            "job_type": job.type,
            "estimate_cents": quote["estimate_cents"],
        },
    )
    session.flush()
    return {**jobs_service.to_dict(job), "estimate": quote}


@router.post("/episodes/{episode_id}/export/package")
def export_package(episode_id: str, session: SessionDep) -> dict:
    """ZIP 素材包。不依赖渲染任务成功（需求 §10.2）。"""
    episode = get_episode(session, episode_id=episode_id)
    # §12-8：素材包同样是「导出」，被拦截时一并禁止
    safety_agent.assert_export_allowed(session, episode)
    job = jobs_service.create_job(
        session,
        type="EXPORT_PACKAGE",
        idempotency_key=jobs_service.build_idempotency_key(
            episode.id, "EXPORT_PACKAGE",
            int(dt.datetime.now(dt.timezone.utc).timestamp() // 5),
        ),
        episode_id=episode.id,
        project_id=episode.project_id,
        stage="export",
        input_json={},
        input_summary={"kind": "package"},
    )
    # §11.1-3：素材包下载同样属于「导出」
    audit_service.record(
        session,
        action=audit_service.ACTION_EXPORT,
        target_type=audit_service.TARGET_EXPORT,
        target_id=job.id,
        project_id=episode.project_id,
        episode_id=episode.id,
        detail={"kind": "package", "format": "zip", "job_type": job.type},
    )
    session.flush()
    return jobs_service.to_dict(job)


@router.get("/episodes/{episode_id}/exports")
def list_exports(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    rows = session.scalars(
        select(ExportRecord)
        .where(ExportRecord.episode_id == episode.id)
        .order_by(ExportRecord.created_at.desc())
    )
    return {"items": [_export_dict(session, r) for r in rows]}


@router.get("/exports/{export_id}")
def read_export(export_id: str, session: SessionDep) -> dict:
    record = session.get(ExportRecord, export_id)
    if record is None:
        raise NotFound(f"导出记录不存在：{export_id}")
    return _export_dict(session, record)


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, session: SessionDep) -> RedirectResponse:
    record = session.get(ExportRecord, export_id)
    if record is None or not record.media_id:
        raise NotFound("导出文件不存在或尚未完成")
    asset = session.get(MediaAsset, record.media_id)
    if asset is None:
        raise NotFound("导出文件已丢失")
    # §11.1-3：导出产物下载留痕（真正的字节由 /media/{id}/content 回，那里再记一条）
    episode = session.get(Episode, record.episode_id) if record.episode_id else None
    audit_service.record(
        session,
        action=audit_service.ACTION_DOWNLOAD,
        target_type=audit_service.TARGET_EXPORT,
        target_id=record.id,
        project_id=episode.project_id if episode else None,
        episode_id=record.episode_id,
        detail={"kind": record.kind, "media_id": asset.id, "preset": record.preset},
    )
    session.flush()
    return RedirectResponse(url=f"/api/v1/media/{asset.id}/content", status_code=307)


def _export_dict(session: SessionDep, record: ExportRecord) -> dict:
    asset = session.get(MediaAsset, record.media_id) if record.media_id else None
    return {
        "id": record.id,
        "episode_id": record.episode_id,
        "kind": record.kind,
        "status": record.status,
        "preset": record.preset,
        "job_id": record.job_id,
        "media_id": record.media_id,
        "media": media_service.to_dict(asset) if asset else None,
        "url": media_service.media_url(asset) if asset else None,
        "download_url": f"/api/v1/exports/{record.id}/download" if asset else None,
        "watermarked": record.watermarked,
        "error_code": record.error_code,
        "error_message": record.error_message,
        "error_detail": record.error_detail,
        "manifest": record.manifest,
        "created_at": record.created_at,
    }


@router.get("/episodes/{episode_id}/package")
def latest_package(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    record = session.scalar(
        select(ExportRecord)
        .where(ExportRecord.episode_id == episode.id, ExportRecord.kind == "package")
        .order_by(ExportRecord.created_at.desc())
        .limit(1)
    )
    if record is None:
        raise NotFound("尚未生成素材包")
    return _export_dict(session, record)
