"""工作流节点实现（需求文档 §6.2 主图节点）。"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError
from app.core.security import FREE_TEXT_KEYS, redact, redact_payload
from app.db.models import (
    Episode,
    EpisodeStage,
    Job,
    JobStatus,
    MediaAsset,
    Project,
    Shot,
    StoryDocument,
    WorkflowNodeLog,
    WorkflowRun,
    WorkflowStatus,
)
from app.services import events
from app.services import executor as executor_service
from app.services import jobs as jobs_service
from app.db.session import session_scope
from app.workflow.state import WorkflowState


def _log(
    session,
    run: WorkflowRun,
    node: str,
    status: str,
    started: float,
    message: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        WorkflowNodeLog(
            workflow_run_id=run.id,
            episode_id=run.episode_id,
            node=node,
            status=status,
            duration_ms=int((time.time() - started) * 1000),
            # FR-093 / §11.1：运行日志写入前脱敏。message 只打码密钥/手机号/邮箱
            # （不截断，保留可排查性）；detail 里的自由文本字段按 strict 截断。
            message=redact(message[:2000]),
            detail=redact_payload(detail or {}, truncate_keys=FREE_TEXT_KEYS),
        )
    )


def _run_job(
    session,
    run: WorkflowRun,
    *,
    job_type: str,
    key_suffix: str,
    input_json: dict[str, Any],
    input_summary: dict[str, Any],
    reserve_cents: int = 0,
    max_attempts: int = 2,
) -> Job:
    """在工作流节点内创建任务并内联执行（与 worker 共用同一套 handler）。"""
    episode = session.get(Episode, run.episode_id)
    job = jobs_service.create_job(
        session,
        type=job_type,
        idempotency_key=jobs_service.build_idempotency_key(
            run.episode_id, job_type, run.id, key_suffix
        ),
        episode_id=run.episode_id,
        project_id=episode.project_id if episode else None,
        workflow_run_id=run.id,
        stage=job_type.lower(),
        input_json=input_json,
        input_summary=input_summary,
        max_attempts=max_attempts,
        reserve_cents=reserve_cents,
    )
    session.commit()
    if job.status in {JobStatus.SUCCEEDED.value, JobStatus.RUNNING.value}:
        return job
    executor_service.run_job_inline(session, job)
    return job


def _gate_payload(state: WorkflowState, gate: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflow_run_id": state.get("workflow_run_id", ""),
        "episode_id": state.get("episode_id", ""),
        "gate": gate,
        "payload": payload,
        "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #

def node_initialize(state: WorkflowState) -> dict[str, Any]:
    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, state["episode_id"])
        _log(session, run, "initialize", "SUCCESS", started, "工作流初始化")
        run.current_node = "initialize"
        run.status = WorkflowStatus.RUNNING.value
        session.flush()
        return {
            "project_id": episode.project_id,
            "last_node": "initialize",
            "decisions": state.get("decisions", {}),
            "errors": [],
            "warnings": [],
        }


def node_safety_check_input(state: WorkflowState) -> dict[str, Any]:
    from app.agents import safety as safety_agent

    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, state["episode_id"])
        document = session.scalar(
            select(StoryDocument)
            .where(StoryDocument.episode_id == episode.id)
            .order_by(StoryDocument.version.desc())
            .limit(1)
        )
        text = state.get("source_text") or (document.source_text if document else "")
        result = safety_agent.check_input(session, text, episode_id=episode.id)
        if not result["allowed"]:
            episode.stage = EpisodeStage.BLOCKED.value
            episode.stage_before_block = EpisodeStage.SCRIPT_REVIEW.value
            episode.blocked_reason = "输入内容未通过安全检查"
            run.status = WorkflowStatus.BLOCKED.value
            _log(session, run, "safety_check_input", "BLOCKED", started,
                 "输入内容未通过安全检查", result)
            session.flush()
            raise AppError(
                "输入内容未通过安全检查，请按建议修改后重试",
                code="SAFETY_BLOCKED",
                details=result,
            )
        _log(session, run, "safety_check_input", "SUCCESS", started, "输入安全检查通过")
        return {"safety": result, "last_node": "safety_check_input", "source_text": text}


def node_story_agent(state: WorkflowState) -> dict[str, Any]:
    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, state["episode_id"])
        episode.stage = EpisodeStage.SCRIPT_REVIEW.value
        job = _run_job(
            session,
            run,
            job_type="STORY_GENERATION",
            key_suffix=f"script-v{state.get('script_version', 0) + 1}",
            input_json={
                "source_text": state.get("source_text", ""),
                "shot_count": state.get("shot_count") or settings.min_shots,
                "input_type": "idea",
            },
            input_summary={"source_chars": len(state.get("source_text", ""))},
        )
        if job.status != JobStatus.SUCCEEDED.value:
            _log(session, run, "story_agent", "FAILED", started, job.error_message or "")
            raise AppError(job.error_message or "剧本生成失败",
                           code=job.error_code or "PROVIDER_ERROR",
                           details=job.error_detail)
        _log(session, run, "story_agent", "SUCCESS", started, "剧本生成完成", job.result_ref)
        return {
            "last_node": "story_agent",
            "story_document_id": job.result_ref.get("story_document_id", ""),
            "script_version": job.result_ref.get("version", 1),
        }


def node_validate_script(state: WorkflowState) -> dict[str, Any]:
    from app.schemas import ScriptModel
    from pydantic import ValidationError

    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        document = session.scalar(
            select(StoryDocument)
            .where(StoryDocument.episode_id == run.episode_id)
            .order_by(StoryDocument.version.desc())
            .limit(1)
        )
        if document is None:
            raise AppError("剧本不存在，无法校验", code="STATE_CONFLICT")
        try:
            ScriptModel.model_validate(document.story_json)
        except ValidationError as exc:
            errors = [
                {"loc": list(e.get("loc", ())), "msg": e.get("msg")}
                for e in exc.errors(include_url=False)[:20]
            ]
            document.status = "INVALID"
            run.status = WorkflowStatus.BLOCKED.value
            _log(session, run, "validate_script", "FAILED", started, "Schema 校验未通过",
                 {"errors": errors})
            session.flush()
            raise AppError(
                "剧本 Schema 校验未通过，无法进入下游",
                code="SCHEMA_VALIDATION_FAILED",
                details={"errors": errors},
            ) from exc
        document.status = "CONFIRMED"
        _log(session, run, "validate_script", "SUCCESS", started, "剧本 Schema 校验通过")
        return {"last_node": "validate_script", "script_valid": True, "script_errors": []}


def node_gate_script_confirmation(state: WorkflowState) -> dict[str, Any]:
    decision = interrupt(
        _gate_payload(state, "gate_script_confirmation",
            {
                "story_document_id": state.get("story_document_id"),
                "script_version": state.get("script_version"),
                "hint": "请确认或编辑剧本后继续",
            },
        )
    )
    decisions = dict(state.get("decisions", {}))
    decisions["gate_script_confirmation"] = decision
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, run.episode_id)
        if (decision or {}).get("decision") == "reject":
            run.status = WorkflowStatus.BLOCKED.value
            episode.stage = EpisodeStage.BLOCKED.value
            episode.stage_before_block = EpisodeStage.SCRIPT_REVIEW.value
            episode.blocked_reason = "用户驳回了剧本"
            session.flush()
            raise AppError("用户驳回剧本，工作流已暂停", code="STATE_CONFLICT")
        episode.stage = EpisodeStage.ASSET_REVIEW.value
        events.publish(session, events.WORKFLOW_RESUMED,
                       {"workflow_run_id": run.id, "gate": "gate_script_confirmation",
                        "decision": (decision or {}).get("decision")},
                       episode_id=run.episode_id, project_id=episode.project_id)
        run.pending_gate = None
        run.status = WorkflowStatus.RUNNING.value
        session.flush()
    return {"last_node": "gate_script_confirmation", "decisions": decisions}


def node_character_agent(state: WorkflowState) -> dict[str, Any]:
    from app.agents import character as character_agent

    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, run.episode_id)
        project = session.get(Project, episode.project_id)

        document = session.scalar(
            select(StoryDocument)
            .where(StoryDocument.episode_id == episode.id)
            .order_by(StoryDocument.version.desc())
            .limit(1)
        )
        if document is None:
            raise AppError("剧本不存在，无法生成角色", code="STATE_CONFLICT")

        characters = character_agent.sync_characters_from_script(session, project, document.story_json)
        session.flush()

        pending = [c for c in characters if c.is_main and not c.primary_media_id]
        generated: list[str] = []
        for character in pending[:3]:
            job = _run_job(
                session,
                run,
                job_type="CHARACTER_ASSET_GENERATION",
                key_suffix=f"character-{character.id}-v{character.version}",
                input_json={
                    "character_id": character.id,
                    "asset_kinds": ["setting", "front"],
                    "count_per_kind": 2,
                },
                input_summary={"character": character.name},
            )
            if job.status == JobStatus.SUCCEEDED.value:
                generated.extend(job.result_ref.get("media_ids", []))

        episode.stage = EpisodeStage.ASSET_REVIEW.value
        _log(session, run, "character_agent", "SUCCESS", started,
             f"角色 {len(characters)} 个，生成参考图 {len(generated)} 张")
        return {
            "last_node": "character_agent",
            "character_ready": any(c.primary_media_id for c in characters if c.is_main),
            "generated_character_media": generated,
        }


def node_gate_character_confirmation(state: WorkflowState) -> dict[str, Any]:
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        from app.db.models import CharacterAsset

        episode = session.get(Episode, run.episode_id)
        characters = list(
            session.scalars(
                select(CharacterAsset).where(CharacterAsset.project_id == episode.project_id)
            )
        )
        gate_payload = {
            "characters": [
                {"id": c.id, "name": c.name, "primary_media_id": c.primary_media_id,
                 "version": c.version, "is_main": c.is_main}
                for c in characters
            ],
            "hint": "请为每个主要角色确认参考图后继续",
        }

    decision = interrupt(_gate_payload(state, "gate_character_confirmation", gate_payload))
    decisions = dict(state.get("decisions", {}))
    decisions["gate_character_confirmation"] = decision
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        from app.db.models import CharacterAsset

        episode = session.get(Episode, run.episode_id)
        characters = list(
            session.scalars(
                select(CharacterAsset).where(CharacterAsset.project_id == episode.project_id)
            )
        )
        missing = [c.name for c in characters if c.is_main and not c.primary_media_id]
        if (decision or {}).get("decision") == "reject":
            run.status = WorkflowStatus.BLOCKED.value
            episode.stage = EpisodeStage.BLOCKED.value
            episode.stage_before_block = EpisodeStage.ASSET_REVIEW.value
            episode.blocked_reason = "用户驳回了角色资产"
            session.flush()
            raise AppError("用户驳回角色资产，工作流已暂停", code="STATE_CONFLICT")
        if missing and (decision or {}).get("decision") != "skip":
            run.status = WorkflowStatus.BLOCKED.value
            episode.stage = EpisodeStage.BLOCKED.value
            episode.stage_before_block = EpisodeStage.ASSET_REVIEW.value
            episode.blocked_reason = "主要角色缺少已确认参考图"
            session.flush()
            raise AppError(
                "以下主要角色缺少已确认参考图：" + "、".join(missing),
                code="STATE_CONFLICT",
                details={"missing_characters": missing},
            )
        episode.stage = EpisodeStage.SHOT_REVIEW.value
        run.pending_gate = None
        run.status = WorkflowStatus.RUNNING.value
        events.publish(session, events.WORKFLOW_RESUMED,
                       {"workflow_run_id": run.id, "gate": "gate_character_confirmation"},
                       episode_id=run.episode_id, project_id=episode.project_id)
        session.flush()
    return {"last_node": "gate_character_confirmation", "decisions": decisions}


def node_shot_agent(state: WorkflowState) -> dict[str, Any]:
    from app.agents import shot as shot_agent

    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, run.episode_id)
        shots = list(
            session.scalars(
                select(Shot).where(Shot.episode_id == episode.id).order_by(Shot.shot_no)
            )
        )
        _log(session, run, "shot_agent", "SUCCESS", started, f"分镜就绪：{len(shots)} 个")
        return {
            "last_node": "shot_agent",
            "shot_nos": [s.shot_no for s in shots],
        }


def node_image_generation(state: WorkflowState) -> dict[str, Any]:
    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        job = _run_job(
            session,
            run,
            job_type="IMAGE_GENERATION",
            key_suffix="images",
            input_json={"candidates_per_shot": settings.candidates_per_shot_default},
            input_summary={"candidates_per_shot": settings.candidates_per_shot_default},
        )
        if job.status != JobStatus.SUCCEEDED.value:
            _log(session, run, "image_generation", "FAILED", started, job.error_message or "")
            raise AppError(job.error_message or "候选图生成失败",
                           code=job.error_code or "PROVIDER_ERROR",
                           details=job.error_detail)
        _log(session, run, "image_generation", "SUCCESS", started,
             f"生成候选图 {job.result_ref.get('count', 0)} 张")
        return {
            "last_node": "image_generation",
            "generated_images": job.result_ref.get("media_ids", []),
        }


def node_gate_shot_confirmation(state: WorkflowState) -> dict[str, Any]:
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        shots = list(
            session.scalars(
                select(Shot).where(Shot.episode_id == run.episode_id).order_by(Shot.shot_no)
            )
        )
        pending = [s.shot_no for s in shots if s.required and not s.confirmed_media_id]
        gate_payload = {
            "shots": [
                {
                    "shot_no": s.shot_no,
                    "confirmed": bool(s.confirmed_media_id),
                    "selected_media_id": s.selected_media_id,
                    "candidates": len(
                        [
                            a
                            for a in session.scalars(
                                select(MediaAsset).where(
                                    MediaAsset.owner_id == s.id, MediaAsset.kind == "image"
                                )
                            )
                        ]
                    ),
                }
                for s in shots
            ],
            "pending_shot_nos": pending,
            "hint": "请逐镜选图并确认画面",
        }
    decision = interrupt(_gate_payload(state, "gate_shot_confirmation", gate_payload))
    decisions = dict(state.get("decisions", {}))
    decisions["gate_shot_confirmation"] = decision

    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, run.episode_id)
        shots = list(
            session.scalars(
                select(Shot).where(Shot.episode_id == run.episode_id).order_by(Shot.shot_no)
            )
        )
        pending = [s.shot_no for s in shots if s.required and not s.confirmed_media_id]
        if pending and (decision or {}).get("decision") != "skip":
            run.status = WorkflowStatus.BLOCKED.value
            session.flush()
            raise AppError(
                "仍有必做镜头未确认画面，无法进入视频生成",
                code="STATE_CONFLICT",
                details={"pending_shot_nos": pending},
            )
        episode.stage = EpisodeStage.VIDEO_REVIEW.value
        run.pending_gate = None
        run.status = WorkflowStatus.RUNNING.value
        events.publish(session, events.WORKFLOW_RESUMED,
                       {"workflow_run_id": run.id, "gate": "gate_shot_confirmation"},
                       episode_id=run.episode_id, project_id=episode.project_id)
        session.flush()
        return {
            "last_node": "gate_shot_confirmation",
            "decisions": decisions,
            "confirmed_shots": [s.shot_no for s in shots if s.confirmed_media_id],
        }


def node_cost_estimate(state: WorkflowState) -> dict[str, Any]:
    from app.agents import video as video_agent

    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, run.episode_id)
        shots = [
            s
            for s in session.scalars(
                select(Shot).where(Shot.episode_id == episode.id).order_by(Shot.shot_no)
            )
            if s.required and s.confirmed_media_id
        ]
        quote = video_agent.estimate(session, episode=episode, shots=shots)
        _log(session, run, "cost_estimate", "SUCCESS", started,
             f"预估 {quote['estimate_cents']} 分", quote)
        return {"last_node": "cost_estimate", "video_quote": quote}


def node_gate_video_cost_confirmation(state: WorkflowState) -> dict[str, Any]:
    quote = state.get("video_quote", {})
    decision = interrupt(
        _gate_payload(state, "gate_video_cost_confirmation",
            {
                "estimate_cents": quote.get("estimate_cents", 0),
                "quota_after_cents": quote.get("quota_after_cents", 0),
                "estimated_wait_seconds": quote.get("estimated_wait_seconds", 0),
                "provider": quote.get("provider"),
                "model": quote.get("model"),
                "per_shot": quote.get("per_shot", []),
                "hint": "确认成本后才会扣除预留配额",
            },
        )
    )
    decisions = dict(state.get("decisions", {}))
    decisions["gate_video_cost_confirmation"] = decision
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, run.episode_id)
        if (decision or {}).get("decision") != "approve":
            episode.stage = EpisodeStage.BLOCKED.value
            episode.stage_before_block = EpisodeStage.VIDEO_REVIEW.value
            episode.blocked_reason = "用户未确认视频成本"
            run.status = WorkflowStatus.BLOCKED.value
            session.flush()
            raise AppError("用户未确认视频成本，视频阶段已暂停", code="STATE_CONFLICT")
        run.pending_gate = None
        run.status = WorkflowStatus.RUNNING.value
        events.publish(session, events.WORKFLOW_RESUMED,
                       {"workflow_run_id": run.id, "gate": "gate_video_cost_confirmation"},
                       episode_id=run.episode_id, project_id=episode.project_id)
        session.flush()
    return {"last_node": "gate_video_cost_confirmation", "decisions": decisions}


def node_video_generation(state: WorkflowState) -> dict[str, Any]:
    started = time.time()
    quote = state.get("video_quote", {})
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        job = _run_job(
            session,
            run,
            job_type="VIDEO_GENERATION",
            key_suffix="videos",
            input_json={"params": quote.get("params", {})},
            input_summary={"estimate_cents": quote.get("estimate_cents", 0)},
            reserve_cents=int(quote.get("estimate_cents", 0)),
        )
        if job.status != JobStatus.SUCCEEDED.value:
            _log(session, run, "video_generation", "FAILED", started, job.error_message or "")
            raise AppError(job.error_message or "视频生成失败",
                           code=job.error_code or "PROVIDER_ERROR",
                           details=job.error_detail)
        _log(session, run, "video_generation", "SUCCESS", started,
             f"生成片段 {job.result_ref.get('count', 0)} 个")
        return {"last_node": "video_generation", "generated_videos": job.result_ref.get("media_ids", [])}


def node_gate_video_confirmation(state: WorkflowState) -> dict[str, Any]:
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        shots = list(
            session.scalars(
                select(Shot).where(Shot.episode_id == run.episode_id).order_by(Shot.shot_no)
            )
        )
        gate_payload = {
            "shots": [
                {
                    "shot_no": s.shot_no,
                    "video_status": s.video_status,
                    "video_media_id": s.video_media_id,
                    "skipped": s.video_status == "SKIPPED",
                }
                for s in shots
                if s.required
            ],
            "hint": "可对片段执行确认、重试或跳过（跳过将用静态图或空镜替代）",
        }
    decision = interrupt(_gate_payload(state, "gate_video_confirmation", gate_payload))
    decisions = dict(state.get("decisions", {}))
    decisions["gate_video_confirmation"] = decision
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, run.episode_id)
        episode.stage = EpisodeStage.AUDIO_REVIEW.value
        run.pending_gate = None
        run.status = WorkflowStatus.RUNNING.value
        events.publish(session, events.WORKFLOW_RESUMED,
                       {"workflow_run_id": run.id, "gate": "gate_video_confirmation"},
                       episode_id=run.episode_id, project_id=episode.project_id)
        session.flush()
    return {"last_node": "gate_video_confirmation", "decisions": decisions}


def node_audio_agent(state: WorkflowState) -> dict[str, Any]:
    from app.agents import audio as audio_agent

    started = time.time()
    with session_scope() as session:
        from app.db.models import CharacterAsset

        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, run.episode_id)
        project = session.get(Project, episode.project_id)
        voices = audio_agent.ensure_default_voices(session, project)
        # FR-050：最小闭环下为「未显式绑定音色」的角色补一个默认音色，
        # 否则 TTS 会因前置条件被拒、整条链路卡住。用户可在「角色」页随时改绑。
        # 按 (created_at, id) 排序保证同一项目每次跑得到的分配结果一致 ——
        # 只按 created_at 排序在时间戳相同时顺序不稳定，会导致两次运行配音不同。
        characters = list(
            session.scalars(
                select(CharacterAsset)
                .where(CharacterAsset.project_id == project.id)
                .order_by(CharacterAsset.created_at, CharacterAsset.id)
            )
        )
        assigned = 0
        for index, character in enumerate(characters):
            if character.voice_id:
                continue
            candidate = voices[index % len(voices)] if voices else None
            if candidate is None:
                break
            character.voice_id = candidate.id
            assigned += 1
        _log(session, run, "audio_agent", "SUCCESS", started,
             f"可用音色 {len(voices)} 个，自动分配 {assigned} 个角色")
        return {"last_node": "audio_agent", "assigned_voices": assigned}


def node_audio_generation(state: WorkflowState) -> dict[str, Any]:
    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        job = _run_job(
            session,
            run,
            job_type="TTS_GENERATION",
            key_suffix="tts",
            input_json={},
            input_summary={"scope": "episode"},
        )
        if job.status != JobStatus.SUCCEEDED.value:
            _log(session, run, "audio_generation", "DEGRADED", started, job.error_message or "")
            # 异常降级：TTS 失败允许跳过配音继续预览（需求 §11）
            return {
                "last_node": "audio_generation",
                "warnings": [*(state.get("warnings") or []),
                             f"TTS 失败已降级继续：{job.error_message}"],
            }
        _log(session, run, "audio_generation", "SUCCESS", started,
             f"生成台词音频 {job.result_ref.get('count', 0)} 条")
        return {"last_node": "audio_generation"}


def node_edit_agent(state: WorkflowState) -> dict[str, Any]:
    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        job = _run_job(
            session,
            run,
            job_type="SUBTITLE_GENERATION",
            key_suffix="subtitles",
            input_json={},
            input_summary={"scope": "episode"},
        )
        _log(session, run, "edit_agent", "SUCCESS", started, "字幕与时间线就绪")
        return {"last_node": "edit_agent", "subtitles": job.result_ref.get("cue_count", 0)}


def node_render_preview(state: WorkflowState) -> dict[str, Any]:
    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        job = _run_job(
            session,
            run,
            job_type="RENDER_PREVIEW",
            key_suffix="preview",
            input_json={"watermark": True},
            input_summary={"kind": "preview"},
        )
        if job.status != JobStatus.SUCCEEDED.value:
            _log(session, run, "render_preview", "FAILED", started, job.error_message or "",
                 job.error_detail)
            # 合成失败不阻断素材包交付（需求 §10.2）
            return {
                "last_node": "render_preview",
                "render": {"ok": False, "error": job.error_message,
                           "detail": job.error_detail, "job_id": job.id},
                "warnings": [*(state.get("warnings") or []),
                             "自动合成失败，仍可导出素材包"],
            }
        _log(session, run, "render_preview", "SUCCESS", started, "预览合成完成", job.result_ref)
        return {"last_node": "render_preview",
                "render": {"ok": True, **job.result_ref}}


def node_gate_export_confirmation(state: WorkflowState) -> dict[str, Any]:
    render = state.get("render") or {}
    decision = interrupt(
        _gate_payload(state, "gate_export_confirmation",
            {
                "preview_media_id": render.get("media_id"),
                "render_ok": render.get("ok", False),
                "render_error": render.get("error"),
                "hint": "预览通过后执行正式导出；合成失败时仍可导出素材包",
            },
        )
    )
    decisions = dict(state.get("decisions", {}))
    decisions["gate_export_confirmation"] = decision
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        run.pending_gate = None
        run.status = WorkflowStatus.RUNNING.value
        session.flush()
    return {"last_node": "gate_export_confirmation", "decisions": decisions}


def node_export(state: WorkflowState) -> dict[str, Any]:
    started = time.time()
    decision = (state.get("decisions") or {}).get("gate_export_confirmation") or {}
    payload = decision.get("payload") or {}
    preset = payload.get("preset", "720p_9x16")
    want_package = payload.get("package", True)

    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, run.episode_id)
        result: dict[str, Any] = {}
        warnings = list(state.get("warnings") or [])

        if (state.get("render") or {}).get("ok"):
            job = _run_job(
                session,
                run,
                job_type="RENDER_EXPORT",
                key_suffix=f"export-{preset}",
                input_json={"preset": preset},
                input_summary={"preset": preset},
            )
            if job.status == JobStatus.SUCCEEDED.value:
                result["export"] = job.result_ref
            else:
                warnings.append(f"正式导出失败：{job.error_message}")
                result["export"] = {"ok": False, "error": job.error_message,
                                    "detail": job.error_detail}
        else:
            warnings.append("未生成预览，跳过正式导出，仅交付素材包")

        if want_package:
            package_job = _run_job(
                session,
                run,
                job_type="EXPORT_PACKAGE",
                key_suffix="package",
                input_json={},
                input_summary={"kind": "package"},
            )
            if package_job.status == JobStatus.SUCCEEDED.value:
                result["package"] = package_job.result_ref
            else:
                warnings.append(f"素材包导出失败：{package_job.error_message}")

        _log(session, run, "export", "SUCCESS", started, "导出完成", result)
        return {"last_node": "export", "export": result, "warnings": warnings}


def node_completed(state: WorkflowState) -> dict[str, Any]:
    started = time.time()
    with session_scope() as session:
        run = session.get(WorkflowRun, state["workflow_run_id"])
        episode = session.get(Episode, run.episode_id)
        run.status = WorkflowStatus.COMPLETED.value
        run.current_node = "completed"
        run.pending_gate = None
        if (state.get("export") or {}).get("export"):
            episode.stage = EpisodeStage.EXPORTED.value
        _log(session, run, "completed", "SUCCESS", started, "工作流完成")
        events.publish(session, events.WORKFLOW_NODE_CHANGED,
                       {"workflow_run_id": run.id, "node": "completed", "status": "COMPLETED"},
                       episode_id=run.episode_id, project_id=episode.project_id)
        session.flush()
    return {"last_node": "completed"}


