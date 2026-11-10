"""任务执行器：job handler 注册表，worker 与工作流内联执行共用同一套代码。"""

from __future__ import annotations

import datetime as dt
import json
import math
import traceback
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode, RenderError
from app.core.security import FREE_TEXT_KEYS, is_truncated, redact_payload
from app.db.models import (
    AssetStatus,
    CharacterAsset,
    Episode,
    EpisodeStage,
    ExportRecord,
    Job,
    JobStatus,
    MediaAsset,
    Project,
    ProviderCall,
    Shot,
    StoryDocument,
)
from app.providers.base import ProviderErrorCode
from app.services import cost as cost_service
from app.services import degraded
from app.services import events, jobs as jobs_service, media as media_service
from app.services import provider_log

Handler = Callable[[Session, Job], dict[str, Any]]

_HANDLERS: dict[str, Handler] = {}

#: §12 降级：任务类型 → 外部依赖类型。用于在连续失败时暂停该类**新任务**。
#: 未列出的任务（剧本重生成、字幕、素材包等）不参与熔断。
JOB_TYPE_KIND: dict[str, str] = {
    "STORY_GENERATION": degraded.KIND_TEXT,
    "CHARACTER_ASSET_GENERATION": degraded.KIND_IMAGE,
    "IMAGE_GENERATION": degraded.KIND_IMAGE,
    "VIDEO_GENERATION": degraded.KIND_VIDEO,
    "TTS_GENERATION": degraded.KIND_SPEECH,
    "BGM_GENERATION": degraded.KIND_SPEECH,
    "RENDER_PREVIEW": degraded.KIND_STORAGE,
    "RENDER_EXPORT": degraded.KIND_STORAGE,
}

#: 触发暂停的错误码：都是「供应商/存储侧」的问题，不是用户输入问题。
#: 前四个来自 `ProviderErrorCode`（供应商把异常映射成这些码），最后一个来自 `ErrorCode`。
_DEGRADE_ERROR_CODES = {
    ProviderErrorCode.UNAVAILABLE.value,
    ProviderErrorCode.UNKNOWN.value,
    ProviderErrorCode.TIMEOUT.value,
    ProviderErrorCode.RATE_LIMITED.value,
    ErrorCode.STORAGE_ERROR,
}


def handler(job_type: str):
    def decorator(fn: Handler) -> Handler:
        _HANDLERS[job_type] = fn
        return fn

    return decorator


def registered_types() -> list[str]:
    return sorted(_HANDLERS)


def _episode(session: Session, job: Job) -> Episode:
    if not job.episode_id:
        raise AppError("任务缺少 episode_id")
    episode = session.get(Episode, job.episode_id)
    if episode is None:
        raise AppError(f"剧集不存在：{job.episode_id}", code=ErrorCode.NOT_FOUND)
    return episode


def _shots(session: Session, episode_id: str, shot_nos: list[int] | None) -> list[Shot]:
    stmt = select(Shot).where(Shot.episode_id == episode_id).order_by(Shot.shot_no)
    if shot_nos:
        stmt = stmt.where(Shot.shot_no.in_(shot_nos))
    return list(session.scalars(stmt))


def _note_degrade(session: Session, job: Job, exc: AppError) -> None:
    """§12：任务最终失败且错误来自供应商/存储侧时，累计失败并可能暂停该类新任务。

    只对「可重试类」错误计数——用户输入非法、安全拦截这类问题不是依赖故障，
    计进去会让正常使用误触发熔断。
    """
    kind = JOB_TYPE_KIND.get(job.type)
    if kind is None:
        return
    if exc.code not in _DEGRADE_ERROR_CODES:
        return
    triggered = degraded.note_failure(kind, f"{job.type} 失败：{exc.message}")
    if triggered:
        events.publish(
            session,
            event_type="degraded.paused",
            episode_id=job.episode_id,
            project_id=job.project_id,
            payload={
                "kind": kind,
                "label": triggered["label"],
                "reason": triggered["reason"],
                "still_available": triggered["still_available"],
                "retry_after_seconds": triggered["ttl_seconds"],
            },
        )


def _progress(session: Session, job: Job):
    def cb(pct: int, message: str = "") -> None:
        jobs_service.set_progress(session, job, pct, message)
        session.commit()

    return cb


def _render_cost_cents(manifest: dict[str, Any] | None) -> int:
    """§13.1：合成按输出时长（秒）× 单价结算，口径与 `edit_agent.estimate_render` 一致。

    注意 manifest 里的键是 `duration_ms`（不是 `total_duration_ms`）——
    取错键会静默按 0 结算，等于合成永远不花钱。
    """
    duration_ms = int((manifest or {}).get("duration_ms") or 0)
    if duration_ms <= 0:
        return 0
    return math.ceil(duration_ms / 1000) * int(settings.render_price_per_second_cents)


# --------------------------------------------------------------------------- #
# 剧本
# --------------------------------------------------------------------------- #

@handler("STORY_GENERATION")
def handle_story_generation(session: Session, job: Job) -> dict[str, Any]:
    from app.agents import story as story_agent

    episode = _episode(session, job)
    payload = job.input_json or {}
    source_text = payload.get("source_text", "")
    # FR-093：任务输入里的故事正文在入库时已按 strict 等级截断，此处回退到
    # StoryDocument 中的全文（正文的权威存储），保证剧本生成仍基于完整故事文本。
    if is_truncated(source_text):
        document = session.scalar(
            select(StoryDocument)
            .where(StoryDocument.episode_id == episode.id)
            .order_by(StoryDocument.version.desc())
            .limit(1)
        )
        if document and document.source_text:
            source_text = document.source_text
    shot_count = int(payload.get("shot_count") or settings.min_shots)
    project = session.get(Project, episode.project_id)

    jobs_service.set_progress(session, job, 10, "调用 StoryAgent")
    session.commit()

    script_model, meta, provider_result = story_agent.generate_script(
        session,
        source_text=source_text,
        shot_count=shot_count,
        genre=project.genre if project else "都市",
    )
    script = script_model.model_dump(mode="json")

    version = int(
        session.scalar(
            select(StoryDocument.version)
            .where(StoryDocument.episode_id == episode.id)
            .order_by(StoryDocument.version.desc())
            .limit(1)
        )
        or 0
    ) + 1
    document = StoryDocument(
        episode_id=episode.id,
        version=version,
        input_type=payload.get("input_type", "idea"),
        source_text=source_text,
        story_json=script,
        analysis_json=payload.get("analysis", {}),
        status="DRAFT",
    )
    session.add(document)
    session.flush()

    from app.agents import shot as shot_agent

    shots = shot_agent.sync_shots_from_script(session, episode, script)

    # 统一走 versions 服务，保证 input_digest 与其它实体一致
    # （手写 EntityVersion 会漏掉指纹，导致版本记录无法判断内容是否真的变了）
    from app.services import versions as versions_service

    versions_service.record(
        session,
        episode_id=episode.id,
        entity_type=versions_service.TARGET_SCRIPT,
        entity_id=document.id,
        payload=script,
        note="StoryAgent 生成",
        version=version,
        force=True,
    )
    episode.stage = EpisodeStage.SCRIPT_REVIEW.value
    session.flush()

    # 剧本一生成就把人物表抓成角色卡：用户停在「故事与剧本」页也能直接去
    # 「角色与画风」改设定，不必先把工作流跑到角色节点（那时角色页才是空的）。
    from app.agents import character as character_agent

    synced = character_agent.sync_characters_from_script(
        session, project, script, document=document, episode_id=episode.id
    )

    return {
        "story_document_id": document.id,
        "version": version,
        "shot_count": len(shots),
        "characters_synced": {
            "created": len(synced["created"]),
            "updated": len(synced["updated"]),
            "total": len(synced["characters"]),
            "names": [c.name for c in synced["characters"]],
        },
        "provider_calls": [
            {
                "provider": job.provider,
                "model": job.model,
                "request_id": provider_result.request_id,
                "usage": provider_result.usage,
                "cost_cents": provider_result.cost_cents,
                "estimated_cost_cents": provider_result.estimated_cost_cents,
                "billing_unit": provider_result.billing_unit,
                "price_version": provider_result.price_version,
                "raw_summary": provider_result.raw_summary,
                "status": "SUCCESS",
            }
        ],
        "meta": meta,
    }


@handler("SCRIPT_REGENERATE")
def handle_script_regenerate(session: Session, job: Job) -> dict[str, Any]:
    from app.agents import shot as shot_agent
    from app.agents import story as story_agent

    episode = _episode(session, job)
    payload = job.input_json or {}
    shot_nos = [int(n) for n in payload.get("shot_nos", [])]
    # FR-014：用户明确勾选「重新规划后续镜头」时才连带重排后续分镜
    replan_downstream = bool(payload.get("replan_downstream", False))
    document = session.scalar(
        select(StoryDocument)
        .where(StoryDocument.episode_id == episode.id)
        .order_by(StoryDocument.version.desc())
        .limit(1)
    )
    if document is None:
        raise AppError("尚未生成剧本，无法局部重生成")

    jobs_service.set_progress(session, job, 20, "局部重生成分镜")
    session.commit()

    updated, changed_nos = story_agent.regenerate_shots(
        session,
        script=document.story_json,
        shot_nos=shot_nos,
        instruction=payload.get("instruction", ""),
        replan_downstream=replan_downstream,
    )
    if not changed_nos:
        # 模型返回内容与现状一致：不落新版本，避免版本膨胀（FR-004）
        return {
            "story_document_id": document.id,
            "version": document.version,
            "changed_shot_nos": [],
            "no_change": True,
        }

    version = document.version + 1
    new_doc = StoryDocument(
        episode_id=episode.id,
        version=version,
        input_type=document.input_type,
        source_text=document.source_text,
        story_json=updated,
        analysis_json=document.analysis_json,
        status="DRAFT",
    )
    session.add(new_doc)
    session.flush()
    shots = shot_agent.sync_shots_from_script(session, episode, updated)
    # FR-004：被改动的分镜，其已确认画面/视频标记为「可能过期」，不能静默复用
    changed = set(changed_nos)
    for shot in shots:
        if shot.shot_no in changed:
            shot.stale = True
            shot.stale_reason = "分镜已局部重生成，需重新确认画面"
    session.flush()
    return {
        "story_document_id": new_doc.id,
        "version": version,
        "changed_shot_nos": sorted(changed),
        "shot_count": len(shots),
    }


# --------------------------------------------------------------------------- #
# 角色 / 图像
# --------------------------------------------------------------------------- #

@handler("CHARACTER_ASSET_GENERATION")
def handle_character_assets(session: Session, job: Job) -> dict[str, Any]:
    from app.agents import character as character_agent

    payload = job.input_json or {}
    character = session.get(CharacterAsset, payload.get("character_id"))
    if character is None:
        raise AppError("角色不存在", code=ErrorCode.NOT_FOUND)
    project = session.get(Project, character.project_id)
    style = character_agent.project_style(session, project)
    assets, cost_cents, cancelled = character_agent.generate_assets(
        session,
        character=character,
        asset_kinds=payload.get("asset_kinds") or ["setting", "front"],
        count_per_kind=int(payload.get("count_per_kind") or 2),
        project=project,
        style=style,
        progress_cb=lambda pct: (jobs_service.set_progress(session, job, pct), session.commit()),
        should_cancel=jobs_service.cancel_probe(job.id),
    )
    return {
        "media_ids": [a.id for a in assets],
        "count": len(assets),
        "character_id": character.id,
        "cost_cents": cost_cents,
        "cancelled": cancelled,
    }


@handler("IMAGE_GENERATION")
def handle_image_generation(session: Session, job: Job) -> dict[str, Any]:
    from app.agents import character as character_agent
    from app.agents import image as image_agent

    episode = _episode(session, job)
    payload = job.input_json or {}
    shot_nos = payload.get("shot_nos")
    shots = _shots(session, episode.id, shot_nos)
    if not shots:
        raise AppError("没有匹配的分镜", code=ErrorCode.NOT_FOUND)

    problems = character_agent.ensure_character_refs(session, episode, shots)
    # FR-023：角色未建档与缺已确认参考图都属于阻断条件（提交接口已拦一遍，
    # 这里是任务执行时的兜底，防止绕过接口直接入队）。
    blocking = [
        p for p in problems
        if p["reason"] in {"MISSING_CHARACTER", "MISSING_REFERENCE"}
    ]
    if blocking and not payload.get("allow_missing_reference"):
        raise AppError(
            "角色资产缺失，已阻断图像生成（FR-023）",
            code=ErrorCode.STATE_CONFLICT,
            details={"problems": problems},
        )

    result = image_agent.generate_for_shots(
        session,
        episode=episode,
        shots=shots,
        candidates_per_shot=int(payload.get("candidates_per_shot") or settings.candidates_per_shot_default),
        progress_cb=lambda pct: (jobs_service.set_progress(session, job, pct), session.commit()),
        should_cancel=jobs_service.cancel_probe(job.id),
    )
    # 中途取消不是失败：已生成的候选保留，剩下的不再花钱
    if result.get("cancelled"):
        result["note"] = "已按请求停止：已生成的候选保留，后续镜头未提交"
    elif result["failures"] and not result["media_ids"]:
        first = result["failures"][0]
        raise AppError(
            first.get("message") or "图像生成全部失败",
            code=first.get("code") or ErrorCode.PROVIDER_ERROR,
            details={"failures": result["failures"]},
            retryable=bool(first.get("retryable")),
        )
    episode.stage = EpisodeStage.SHOT_REVIEW.value
    return {**result, "problems": problems}


@handler("VIDEO_GENERATION")
def handle_video_generation(session: Session, job: Job) -> dict[str, Any]:
    from app.agents import video as video_agent

    episode = _episode(session, job)
    payload = job.input_json or {}
    shots = _shots(session, episode.id, payload.get("shot_nos"))
    pending = [s for s in shots if not s.confirmed_media_id and s.required]
    if pending:
        raise AppError(
            "存在未确认画面的必做镜头，无法进入视频生成",
            code=ErrorCode.STATE_CONFLICT,
            details={"shot_nos": [s.shot_no for s in pending]},
        )
    result = video_agent.generate_for_shots(
        session,
        episode=episode,
        shots=shots,
        params=payload.get("params") or {},
        progress_cb=lambda pct: (jobs_service.set_progress(session, job, pct), session.commit()),
        should_cancel=jobs_service.cancel_probe(job.id),
    )
    if result.get("cancelled"):
        result["note"] = "已按请求停止：已出片的镜头保留，后续镜头未提交"
    elif result["failures"] and not result["media_ids"]:
        first = result["failures"][0]
        raise AppError(
            first.get("message") or "视频生成全部失败",
            code=first.get("code") or ErrorCode.PROVIDER_ERROR,
            details={"failures": result["failures"]},
            retryable=bool(first.get("retryable")),
        )
    episode.stage = EpisodeStage.VIDEO_REVIEW.value
    return result


@handler("TTS_GENERATION")
def handle_tts(session: Session, job: Job) -> dict[str, Any]:
    from app.agents import audio as audio_agent

    episode = _episode(session, job)
    payload = job.input_json or {}
    shots = _shots(session, episode.id, payload.get("shot_nos"))
    result = audio_agent.synthesize_for_shots(
        session,
        episode=episode,
        shots=shots,
        progress_cb=lambda pct: (jobs_service.set_progress(session, job, pct), session.commit()),
        only_character_id=payload.get("character_id") or None,
    )
    if result["failures"] and not result["media_ids"]:
        first = result["failures"][0]
        raise AppError(
            first.get("message") or "TTS 生成全部失败",
            code=first.get("code") or ErrorCode.PROVIDER_ERROR,
            details={"failures": result["failures"], "skipped": result["skipped"]},
            retryable=bool(first.get("retryable")),
        )
    episode.stage = EpisodeStage.AUDIO_REVIEW.value
    return result


@handler("BGM_GENERATION")
def handle_bgm(session: Session, job: Job) -> dict[str, Any]:
    """生成一轨氛围音 / BGM 并挂到时间线（分层方案：不与台词混在一起）。"""
    from app.agents import audio as audio_agent

    episode = _episode(session, job)
    payload = job.input_json or {}
    return audio_agent.generate_ambience(
        session,
        episode=episode,
        prompt=str(payload.get("prompt") or ""),
        seconds=payload.get("seconds"),
        shot_no=payload.get("shot_no"),
        shot_from=payload.get("shot_from"),
        shot_to=payload.get("shot_to"),
        volume_db=payload.get("volume_db"),
        fade_in_ms=int(payload.get("fade_in_ms") or 800),
        fade_out_ms=int(payload.get("fade_out_ms") or 1500),
        progress_cb=lambda pct: (jobs_service.set_progress(session, job, pct), session.commit()),
    )


@handler("SUBTITLE_GENERATION")
def handle_subtitles(session: Session, job: Job) -> dict[str, Any]:
    from app.agents import edit as edit_agent

    episode = _episode(session, job)
    cues = edit_agent.generate_subtitles(session, episode)
    return {"cue_count": len(cues)}


@handler("RENDER_PREVIEW")
def handle_render_preview(session: Session, job: Job) -> dict[str, Any]:
    from app.services import render as render_service

    episode = _episode(session, job)
    payload = job.input_json or {}
    record = render_service.create_export_record(
        session, episode, kind="preview", preset="preview", job_id=job.id
    )
    session.commit()
    try:
        asset, manifest = render_service.render_episode(
            session,
            episode,
            kind="preview",
            preset="preview",
            watermark=bool(payload.get("watermark", True)),
            only_ready=bool(payload.get("partial", False)),
            progress_cb=_progress(session, job),
        )
    except RenderError as exc:
        record.status = "FAILED"
        record.error_code = exc.code
        record.error_message = exc.message
        record.error_detail = jobs_service.jsonable(exc.details)
        session.flush()
        raise
    record.status = "SUCCEEDED"
    record.media_id = asset.id
    record.manifest = manifest
    episode.stage = EpisodeStage.PREVIEW_READY.value
    session.flush()
    cost = _render_cost_cents(manifest)
    return {
        "media_id": asset.id,
        "export_id": record.id,
        "manifest": manifest,
        "cost_cents": cost,
    }


@handler("RENDER_EXPORT")
def handle_render_export(session: Session, job: Job) -> dict[str, Any]:
    from app.services import render as render_service

    episode = _episode(session, job)
    payload = job.input_json or {}
    preset = payload.get("preset", "720p_9x16")
    # FR-062：水印是可配置项，正式导出也要尊重调用方传入的开关
    # （AIGC 标识始终烧录，不受该开关影响）
    watermark = bool(payload.get("watermark", False))
    record = render_service.create_export_record(
        session, episode, kind="final", preset=preset, job_id=job.id
    )
    session.commit()
    try:
        asset, manifest = render_service.render_episode(
            session,
            episode,
            kind="final",
            preset=preset,
            watermark=watermark,
            progress_cb=_progress(session, job),
        )
    except RenderError as exc:
        record.status = "FAILED"
        record.error_code = exc.code
        record.error_message = exc.message
        record.error_detail = jobs_service.jsonable(exc.details)
        session.flush()
        raise
    record.status = "SUCCEEDED"
    record.media_id = asset.id
    record.manifest = manifest
    episode.stage = EpisodeStage.EXPORTED.value
    session.flush()
    cost = _render_cost_cents(manifest)
    return {
        "media_id": asset.id,
        "export_id": record.id,
        "manifest": manifest,
        "cost_cents": cost,
    }


@handler("EXPORT_PACKAGE")
def handle_export_package(session: Session, job: Job) -> dict[str, Any]:
    from app.agents import edit as edit_agent

    episode = _episode(session, job)
    content, manifest = edit_agent.build_package(session, episode)
    asset = media_service.save_media(
        session,
        content=content,
        kind="archive",
        role="package",
        filename=f"episode_{episode.episode_no:02d}_package.zip",
        mime_type="application/zip",
        owner_type="episode",
        owner_id=episode.id,
        episode_id=episode.id,
        project_id=episode.project_id,
        status=AssetStatus.CONFIRMED.value,
        metadata={"kind": "package", "aigc": True},
    )
    record = ExportRecord(
        episode_id=episode.id,
        kind="package",
        preset="zip",
        status="SUCCEEDED",
        job_id=job.id,
        media_id=asset.id,
        manifest={"shots": len(manifest.get("shots", [])), "aigc": manifest.get("aigc")},
    )
    session.add(record)
    session.flush()
    return {"media_id": asset.id, "export_id": record.id, "size_bytes": asset.size_bytes}


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #

def _submitted_job_ids(calls: list[dict[str, Any]]) -> list[str]:
    """本次尝试里「已经提交到供应商」的任务 id（去重保序）。"""
    seen: list[str] = []
    for call in calls:
        job_id = str(call.get("provider_job_id") or "").strip()
        if job_id and job_id not in seen:
            seen.append(job_id)
    return seen


def _release_reason(exc: AppError, calls: list[dict[str, Any]]) -> str:
    """失败释放的账本备注。

    任务已经提交到供应商、之后才失败时，网关侧很可能已经扣了费——
    本地额度全额退回是对的（我们确实没拿到产物），但账本要留下网关任务 id，
    否则用户只看到「失败」，而供应商后台显示「成功、已扣积分」，对不上账。
    """
    note = f"失败释放：{exc.message}"
    submitted = _submitted_job_ids(calls)
    if submitted:
        note += (
            f"（供应商任务 {'、'.join(submitted[:3])} 可能已计费，本地未扣；"
            "可在供应商控制台核对）"
        )
    return note


def _persist_calls(session: Session, job: Job, calls: list[dict[str, Any]]) -> None:
    """把供应商调用写入 ProviderCall，供任务详情与审计查看（需求 §9.2）。

    FR-093 / §11.1：入库前对用量与原始返回摘要脱敏（密钥打码、长正文截断），
    字段名与结构不变，只有值被脱敏。
    """
    for call in calls:
        session.add(
            ProviderCall(
                job_id=job.id,
                provider=call.get("provider") or job.provider,
                model=call.get("model") or job.model,
                kind=call.get("kind") or job.type,
                request_id=call.get("request_id", "") or "",
                provider_job_id=call.get("provider_job_id", "") or "",
                price_version=call.get("price_version", "v1") or "v1",
                billing_unit=call.get("billing_unit", "call") or "call",
                usage_json=redact_payload(
                    call.get("usage", {}) or {}, truncate_keys=FREE_TEXT_KEYS
                ),
                estimated_cost_cents=int(call.get("estimated_cost_cents", 0) or 0),
                cost_cents=int(call.get("cost_cents", 0) or 0),
                status=call.get("status", "SUCCESS") or "SUCCESS",
                raw_summary=redact_payload(
                    call.get("raw_summary", {}) or {}, truncate_keys=FREE_TEXT_KEYS
                ),
            )
        )


def run_job(
    session: Session,
    job: Job,
    *,
    worker_id: str = "inline",
    allow_auto_retry: bool = True,
) -> Job:
    """执行一个任务；负责状态流转、成本预留/结算、供应商调用记录与错误记录。

    `allow_auto_retry=False` 用于工作流节点的内联执行：那条路径有自己的节点级
    重试/回滚机制（`workflow_engine.retry_node`），如果再安排一次队列重试，
    同一个任务会被两条路径重复执行。
    """
    started = dt.datetime.now(dt.timezone.utc)
    jobs_service.set_status(session, job, JobStatus.RUNNING.value, progress=1)
    session.commit()

    fn = _HANDLERS.get(job.type)
    if fn is None:
        jobs_service.set_status(
            session,
            job,
            JobStatus.FAILED.value,
            error_code=ErrorCode.INTERNAL_ERROR,
            error_message=f"未注册的任务类型：{job.type}",
        )
        session.commit()
        return job

    provider_log.start()
    calls: list[dict[str, Any]] = []
    try:
        try:
            result = fn(session, job) or {}
        finally:
            calls = provider_log.finish()
        session.flush()

        _persist_calls(session, job, [*(result.pop("provider_calls", []) or []), *calls])

        # 实际消耗以处理器回传为准（含 0：取消得太早、一张都没生成时就是 0）。
        # 不能用 `or` 兜底——0 会被当成「没给值」，于是把整笔预留都结算掉。
        settled = (
            int(result["cost_cents"])
            if "cost_cents" in result and result["cost_cents"] is not None
            else int(job.cost_reserved_cents or 0)
        )
        cost_service.settle(session, job=job, actual_cents=settled)

        if job.cancel_requested and result.get("cancelled") is True:
            # 只有「真的提前停下」才算取消；取消信号晚到、活已干完的按成功记账，
            # 否则会出现「产物都齐了却显示已取消」的怪状态。
            jobs_service.set_status(session, job, JobStatus.CANCELLED.value,
                                    result_ref=result)
        else:
            jobs_service.set_status(
                session, job, JobStatus.SUCCEEDED.value, progress=100, result_ref=result
            )
        session.commit()
    except AppError as exc:
        # 异常可能是在 flush/commit 途中抛的（例如结果里混进 datetime 导致
        # StatementError）。不先 rollback 的话 session 停在 PendingRollback 状态，
        # 后面写失败原因、安排重试都会再次抛错，任务就永远卡在 RUNNING。
        session.rollback()
        _persist_calls(session, job, calls)
        # FR-072 自动重试：可重试的错误且还有剩余次数时，退避后重入队。
        # 此时**不释放**配额预留——预留跨尝试保持，等任务最终终结时才释放，
        # 保证「预留→结算」口径不会被自动重试打断。
        #
        # 但「已经提交到供应商」的失败不能自动重试：任务在网关侧已经跑起来了，
        # 再走一遍就是重新下单、重新计费。这种情况交给用户决定（看控制台 / 手动重试）。
        submitted_ids = _submitted_job_ids(calls)
        if (
            allow_auto_retry
            and not submitted_ids
            and jobs_service.schedule_auto_retry(
                session, job, retryable=bool(exc.retryable)
            )
        ):
            job.error_code = exc.code
            job.error_message = exc.message
            job.error_detail = jobs_service.jsonable(exc.details or {})
            session.commit()
        else:
            cost_service.release(
                session, job=job, reason=_release_reason(exc, calls)
            )
            jobs_service.set_status(
                session,
                job,
                JobStatus.FAILED.value,
                error_code=exc.code,
                error_message=exc.message,
                error_detail=exc.details,
            )
            session.commit()
            # §12：任务最终失败且属于「供应商/存储侧」问题时，累计到阈值即暂停该类新任务
            _note_degrade(session, job, exc)
    except Exception as exc:  # noqa: BLE001 - 兜底，保留原始错误
        session.rollback()
        _persist_calls(session, job, calls)
        # 未预期异常同样给一次自动重试机会，避免瞬时故障直接判死
        if allow_auto_retry and jobs_service.schedule_auto_retry(session, job, retryable=True):
            job.error_code = ErrorCode.INTERNAL_ERROR
            job.error_message = f"{type(exc).__name__}: {exc}"
            job.error_detail = {"traceback": traceback.format_exc()[-2000:]}
            session.commit()
        else:
            cost_service.release(session, job=job, reason="异常释放")
            jobs_service.set_status(
                session,
                job,
                JobStatus.FAILED.value,
                error_code=ErrorCode.INTERNAL_ERROR,
                error_message=f"{type(exc).__name__}: {exc}",
                error_detail={"traceback": traceback.format_exc()[-2000:]},
            )
            session.commit()
    else:
        # §12：任务成功即自愈，清零该依赖的失败计数并解除暂停
        kind = JOB_TYPE_KIND.get(job.type)
        if kind:
            degraded.note_success(kind)

    job.duration_ms = int(
        (dt.datetime.now(dt.timezone.utc) - started).total_seconds() * 1000
    )
    session.commit()
    return job


def run_job_inline(session: Session, job: Job) -> Job:
    """工作流节点内联执行：不安排队列自动重试（由节点级重试/回滚负责）。"""
    return run_job(session, job, worker_id="inline", allow_auto_retry=False)


def serialize_input(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)[:2000]
