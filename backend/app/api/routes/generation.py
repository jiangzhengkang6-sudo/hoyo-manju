"""视频、音频与字幕相关接口。"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.agents import audio as audio_agent
from app.agents import edit as edit_agent
from app.agents import video as video_agent
from app.api.deps import SessionDep, get_episode, get_shot, record_approval, shots_of
from app.core.config import settings
from app.core.errors import AppError, Conflict, ErrorCode, NotFound, StateConflict
from app.db.models import AudioTrack, EpisodeStage, MediaAsset, Project, Shot, SubtitleCue
from app.schemas import (
    ManualAudioIn,
    RenderPreviewIn,
    SkipDialogueIn,
    TrackCreate,
    TTSGenerate,
    VideoConfirmIn,
    VideoEstimateIn,
    VideoGenerateIn,
    VideoSkipIn,
)
from app.services import audit as audit_service
from app.services import cost as cost_service
from app.services import degraded as degraded_service
from app.services import jobs as jobs_service
from app.services import media as media_service

router = APIRouter(tags=["generation"])


def _shot_by_no(session, episode_id: str, shot_no: int):
    """按镜号取镜头；不存在就直接 404，别让后续逻辑拿着 None 继续跑。"""
    for shot in shots_of(session, episode_id):
        if shot.shot_no == shot_no:
            return shot
    raise NotFound(f"镜头不存在：第 {shot_no} 镜")


# --------------------------------------------------------------------------- #
# 视频
# --------------------------------------------------------------------------- #

def _validate_video_capabilities(session, shots) -> None:
    """FR-091：提交前按视频供应商的能力声明校验时长/画幅/图生视频支持。

    逐镜校验（每镜的时长来自剧本，可能各不相同），任一不满足即整体阻断，
    避免「一半生成成功、一半失败」还要用户自己找原因。
    """
    from app.providers import registry
    from app.providers.base import ProviderKind
    from app.services import capabilities as caps_service

    caps = registry.resolve(ProviderKind.VIDEO, session).capabilities()
    for shot in shots:
        merged = video_agent.resolve_shot_params(shot, None)
        caps_service.validate_video_request(
            caps,
            duration_ms=int(merged["duration_ms"]),
            width=video_agent.DEFAULT_VIDEO_WIDTH,
            height=video_agent.DEFAULT_VIDEO_HEIGHT,
            aspect_ratio=video_agent.DEFAULT_ASPECT_RATIO,
            has_image=bool(shot.confirmed_media_id),
        )


@router.get("/episodes/{episode_id}/video/params")
def read_video_params(episode_id: str, session: SessionDep) -> dict:
    """FR-040：返回画幅比例与逐镜视频参数（含可选值与范围）。"""
    episode = get_episode(session, episode_id=episode_id)
    shots = shots_of(session, episode.id)
    project = session.get(Project, episode.project_id)
    width, height = video_agent.episode_resolution(session, episode)
    return {
        "episode_id": episode.id,
        "aspect_ratio": getattr(project, "aspect_ratio", None),
        "width": width,
        "height": height,
        "aspect_presets": [
            {"value": value, "width": size[0], "height": size[1]}
            for value, size in video_agent.ASPECT_PRESETS.items()
        ],
        "duration_ms_min": settings.shot_duration_ms_min,
        "duration_ms_max": settings.shot_duration_ms_max,
        "motion_strength_min": 0.0,
        "motion_strength_max": 1.0,
        "shots": [
            {
                "shot_id": shot.id,
                "shot_no": shot.shot_no,
                **video_agent.resolve_shot_params(shot),
            }
            for shot in shots
        ],
    }


@router.put("/episodes/{episode_id}/video/params")
def update_video_params(episode_id: str, payload: dict, session: SessionDep) -> dict:
    """FR-040：持久化画幅比例与逐镜视频参数。

    `shot_nos` 为空表示应用到全部镜头。参数写进 `shot_json.video_params`，
    与 `resolve_shot_params()` 读取的位置一致，因此生成时会真正生效。
    画幅比例落在**项目**上（`Project.aspect_ratio`），全剧集共享。
    """
    episode = get_episode(session, episode_id=episode_id)
    project = session.get(Project, episode.project_id)
    changed = 0
    if payload.get("aspect_ratio"):
        ratio = str(payload["aspect_ratio"]).strip()
        if ratio not in video_agent.ASPECT_PRESETS:
            raise StateConflict(
                f"不支持的画幅比例：{ratio}",
                details={"allowed": list(video_agent.ASPECT_PRESETS)},
            )
        if project is not None:
            project.aspect_ratio = ratio

    shot_nos = payload.get("shot_nos")
    shots = shots_of(session, episode.id)
    if shot_nos:
        wanted = {int(n) for n in shot_nos}
        shots = [s for s in shots if s.shot_no in wanted]

    patch: dict[str, Any] = {}
    if payload.get("motion_strength") is not None:
        strength = float(payload["motion_strength"])
        patch["motion_strength"] = max(0.0, min(1.0, strength))
    if payload.get("duration_ms") is not None:
        duration = int(payload["duration_ms"])
        patch["duration_ms"] = max(
            settings.shot_duration_ms_min,
            min(duration, settings.shot_duration_ms_max),
        )
    if payload.get("camera_movement"):
        patch["camera_movement"] = str(payload["camera_movement"])

    if patch:
        for shot in shots:
            data = dict(shot.shot_json or {})
            vp = dict(data.get("video_params") or {})
            vp.update(patch)
            data["video_params"] = vp
            shot.shot_json = data
            if "duration_ms" in patch:
                shot.duration_ms = int(patch["duration_ms"])
            changed += 1
    session.flush()
    width, height = video_agent.episode_resolution(session, episode)
    return {
        "episode_id": episode.id,
        "aspect_ratio": getattr(project, "aspect_ratio", None),
        "width": width,
        "height": height,
        "updated_shots": changed,
        "applied": patch,
    }


@router.post("/episodes/{episode_id}/video/estimate")
def estimate_video(episode_id: str, payload: VideoEstimateIn, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    shots = shots_of(session, episode.id)
    if payload.shot_nos:
        shots = [s for s in shots if s.shot_no in set(payload.shot_nos)]
    shots = [s for s in shots if s.required]
    pending = [s.shot_no for s in shots if not s.confirmed_media_id]
    if pending:
        raise StateConflict(
            "存在未确认画面的镜头，无法预估视频成本",
            details={"pending_shot_nos": pending},
        )
    _validate_video_capabilities(session, shots)
    quote = video_agent.estimate(session, episode=episode, shots=shots, params=payload.params)
    quote["quote_id"] = f"quote_{uuid.uuid4().hex[:12]}"
    quote["shot_nos"] = [s.shot_no for s in shots]
    quote["expires_at"] = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)
    ).isoformat()
    quote["shot_confirmation"] = [
        {
            "shot_no": s.shot_no,
            "confirmed_media_id": s.confirmed_media_id,
            "duration_ms": s.duration_ms,
        }
        for s in shots
    ]
    return quote


@router.post("/episodes/{episode_id}/video/generate")
def generate_video(episode_id: str, payload: VideoGenerateIn, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    # §12：视频供应商被熔断时只暂停**视频**任务；剧本/画面/素材包照常可用
    degraded_service.ensure_available(degraded_service.KIND_VIDEO, action="视频生成")
    if not payload.confirm:
        raise Conflict("视频生成必须先确认成本（FR-MVP-041）")
    shots = shots_of(session, episode.id)
    if payload.shot_nos:
        shots = [s for s in shots if s.shot_no in set(payload.shot_nos)]
    shots = [s for s in shots if s.required]
    pending = [s.shot_no for s in shots if not s.confirmed_media_id]
    if pending:
        raise StateConflict(
            "存在未确认画面的镜头，不能进入视频生成",
            details={"pending_shot_nos": pending},
        )
    _validate_video_capabilities(session, shots)
    quote = video_agent.estimate(session, episode=episode, shots=shots, params=payload.params)
    key = payload.idempotency_key or jobs_service.build_idempotency_key(
        episode.id, "VIDEO_GENERATION", payload.quote_id, ",".join(str(s.shot_no) for s in shots)
    )
    job = jobs_service.create_job(
        session,
        type="VIDEO_GENERATION",
        idempotency_key=key,
        episode_id=episode.id,
        project_id=episode.project_id,
        stage="video_generation",
        input_json={"shot_nos": [s.shot_no for s in shots], "params": payload.params},
        input_summary={
            "shot_count": len(shots),
            "estimate_cents": quote["estimate_cents"],
            "provider": quote["provider"],
        },
        provider=quote["provider"],
        model=str(quote.get("model", "")),
        reserve_cents=quote["estimate_cents"],
        max_attempts=2,
    )
    episode.stage = EpisodeStage.VIDEO_REVIEW.value
    session.flush()
    return {"job": jobs_service.to_dict(job), "estimate": quote}


@router.get("/shots/{shot_id}/video")
def read_shot_video(shot_id: str, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    jobs = jobs_service.list_jobs(
        session, episode_id=shot.episode_id, type="VIDEO_GENERATION", page=1, page_size=20
    )
    return {
        "shot_no": shot.shot_no,
        "video_status": shot.video_status,
        "video_media_id": shot.video_media_id,
        "skipped": shot.video_status == "SKIPPED",
        "fallback": (shot.shot_json or {}).get("video_fallback"),
        "history": video_agent.video_history(session, shot),
        "jobs": [jobs_service.to_dict(j) for j in jobs["items"]],
    }


@router.post("/shots/{shot_id}/video/confirm")
def confirm_video(shot_id: str, payload: VideoConfirmIn, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    video_agent.confirm(session, shot, payload.media_id)
    record_approval(
        session,
        episode_id=shot.episode_id,
        stage="gate_video_confirmation",
        target_type="shot_video",
        target_id=shot.id,
        asset_version=shot.current_version,
        candidate_media_id=payload.media_id,
        payload={"shot_no": shot.shot_no},
    )
    session.flush()
    return {"shot_no": shot.shot_no, "video_status": shot.video_status,
            "video_media_id": shot.video_media_id}


@router.post("/shots/{shot_id}/video/skip")
def skip_video(shot_id: str, payload: VideoSkipIn, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    video_agent.skip(session, shot, payload.fallback, payload.reason)
    record_approval(
        session,
        episode_id=shot.episode_id,
        stage="gate_video_confirmation",
        target_type="shot_video",
        target_id=shot.id,
        decision="SKIP",
        note=payload.reason,
        payload={"fallback": payload.fallback, "shot_no": shot.shot_no},
    )
    session.flush()
    return {
        "shot_no": shot.shot_no,
        "video_status": shot.video_status,
        "note": "该镜头将使用静态图替代" if payload.fallback == "static_image" else "该镜头将使用空镜替代",
    }


@router.post("/shots/{shot_id}/video/retry")
def retry_video(shot_id: str, session: SessionDep) -> dict:
    shot = get_shot(session, shot_id=shot_id)
    if not shot.confirmed_media_id:
        raise StateConflict("该镜头尚未确认画面，无法重试视频生成")
    job = jobs_service.create_job(
        session,
        type="VIDEO_GENERATION",
        idempotency_key=jobs_service.build_windowed_key(
            shot.id, "VIDEO_GENERATION", shot.current_version
        ),
        episode_id=shot.episode_id,
        stage="video_generation",
        input_json={"shot_nos": [shot.shot_no]},
        input_summary={"shot_no": shot.shot_no, "retry": True},
        max_attempts=2,
    )
    session.flush()
    return jobs_service.to_dict(job)


# --------------------------------------------------------------------------- #
# 音频
# --------------------------------------------------------------------------- #

@router.get("/episodes/{episode_id}/tts/estimate")
def estimate_tts(
    episode_id: str,
    session: SessionDep,
    shot_nos: str | None = None,
    character_id: str | None = None,
) -> dict:
    """TTS 成本预估 + FR-050 前置条件（前端据此禁用按钮并给出可执行提示）。"""
    episode = get_episode(session, episode_id=episode_id)
    shots = shots_of(session, episode.id)
    if shot_nos:
        wanted = {int(part) for part in shot_nos.split(",") if part.strip().lstrip("-").isdigit()}
        shots = [s for s in shots if s.shot_no in wanted]
    if character_id:
        shots = [
            s
            for s in shots
            if any(
                (line.get("character_id") or "") == character_id
                for line in (s.shot_json or {}).get("dialogue") or []
            )
        ]
    quote = audio_agent.estimate(session, shots=shots)
    return {"episode_id": episode.id, "shot_count": len(shots), **quote}


@router.post("/episodes/{episode_id}/tts/generate")
def generate_tts(episode_id: str, payload: TTSGenerate, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    # §12：语音供应商不可用时暂停 TTS，但允许用户上传音频或跳过配音后继续预览
    degraded_service.ensure_available(degraded_service.KIND_SPEECH, action="语音合成")
    shots = shots_of(session, episode.id)
    if payload.shot_nos:
        shots = [s for s in shots if s.shot_no in set(payload.shot_nos)]
    # FR-010 / FR-042 / §12-6：TTS 也要预估并预留配额，不能只在视频上做
    quote = audio_agent.estimate(session, shots=shots)
    # FR-050：每个有台词的角色必须已绑定音色。不满足就整体拒绝 ——
    # 不留任务、不占配额，而不是把这几句台词静默跳过（成片会缺声音，用户难发现）。
    if not quote["can_generate"]:
        missing = quote["blocking_problems"]
        names = "、".join(str(item["character"]) for item in missing)
        raise StateConflict(
            f"以下角色尚未绑定音色，请先在「角色」页绑定：{names}",
            code="MISSING_VOICE",
            details={"blocking_problems": missing, "episode_id": episode.id},
        )
    job = jobs_service.create_job(
        session,
        type="TTS_GENERATION",
        idempotency_key=jobs_service.build_windowed_key(
            episode.id, "TTS_GENERATION",
            ",".join(str(s.shot_no) for s in shots),
            payload.character_id,
        ),
        episode_id=episode.id,
        project_id=episode.project_id,
        stage="audio_generation",
        input_json={"shot_nos": [s.shot_no for s in shots], "character_id": payload.character_id},
        input_summary={
            "shot_count": len(shots),
            "estimate_cents": quote["estimate_cents"],
            "provider": quote["provider"],
        },
        provider=quote["provider"],
        model=str(quote.get("model", "")),
        reserve_cents=quote["estimate_cents"],
        max_attempts=2,
    )
    episode.stage = EpisodeStage.AUDIO_REVIEW.value
    session.flush()
    return {**jobs_service.to_dict(job), "estimate": quote}


@router.get("/episodes/{episode_id}/tts")
def list_tts(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    tracks = audio_agent.list_tracks(session, episode.id, kind="dialogue")
    return {
        "items": [audio_agent.track_to_dict(session, t) for t in tracks],
        "stale_count": len([t for t in tracks if t.stale]),
        "skipped_count": len([t for t in tracks if t.source == audio_agent.SOURCE_SKIPPED]),
        "manual_count": len([t for t in tracks if t.source == audio_agent.SOURCE_MANUAL]),
    }


@router.get("/episodes/{episode_id}/tts/lines")
def list_tts_lines(episode_id: str, session: SessionDep) -> dict:
    """§12-4：逐句台词的状态表（ready / missing / stale / skipped）。

    前端靠它决定「哪几句需要给『上传音频 / 跳过』入口」——只给真正缺音频的行，
    已完成的句子不该再出现降级入口。
    """
    episode = get_episode(session, episode_id=episode_id)
    shots = shots_of(session, episode.id)
    items = audio_agent.dialogue_lines(session, episode, shots)
    return {"items": items, "counts": audio_agent.dialogue_progress(session, episode, shots)}


@router.post("/episodes/{episode_id}/tts/manual", status_code=201)
def attach_manual_tts(episode_id: str, payload: ManualAudioIn, session: SessionDep) -> dict:
    """§12-4：TTS 失败时上传音频顶替该句台词，之后可继续预览/导出。"""
    episode = get_episode(session, episode_id=episode_id)
    shot = _shot_by_no(session, episode.id, payload.shot_no)
    track = audio_agent.attach_manual_audio(
        session,
        episode=episode,
        shot=shot,
        line_index=payload.line_index,
        media_id=payload.media_id,
    )
    session.flush()
    return audio_agent.track_to_dict(session, track)


@router.post("/episodes/{episode_id}/tts/skip")
def skip_tts(episode_id: str, payload: SkipDialogueIn, session: SessionDep) -> dict:
    """§12-4：跳过这些镜头的配音，让预览与导出可以继续。"""
    episode = get_episode(session, episode_id=episode_id)
    skipped: list[str] = []
    for shot_no in payload.shot_nos:
        shot = _shot_by_no(session, episode.id, shot_no)
        for index, line in enumerate((shot.shot_json or {}).get("dialogue") or []):
            if not (line.get("text") or "").strip():
                continue
            track = audio_agent.skip_dialogue(
                session, episode=episode, shot=shot, line_index=index, reason=payload.reason
            )
            skipped.append(track.id)
    session.flush()
    shots = shots_of(session, episode.id)
    return {
        "skipped_track_ids": skipped,
        "counts": audio_agent.dialogue_progress(session, episode, shots),
    }


@router.get("/episodes/{episode_id}/bgm")
def list_bgm(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    tracks = [
        t for t in audio_agent.list_tracks(session, episode.id) if t.kind != "dialogue"
    ]
    return {"items": [audio_agent.track_to_dict(session, t) for t in tracks]}


@router.post("/episodes/{episode_id}/bgm", status_code=201)
def create_bgm(episode_id: str, payload: TrackCreate, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    track = audio_agent.add_track(
        session,
        episode=episode,
        media_id=payload.media_id,
        kind=payload.kind,
        start_ms=payload.start_ms,
        end_ms=payload.end_ms,
        volume_db=payload.volume_db,
        fade_in_ms=payload.fade_in_ms,
        fade_out_ms=payload.fade_out_ms,
        rights_declared=payload.rights_declared,
    )
    return audio_agent.track_to_dict(session, track)


@router.delete("/episodes/{episode_id}/bgm/{track_id}")
def delete_bgm(episode_id: str, track_id: str, session: SessionDep) -> dict:
    track = session.get(AudioTrack, track_id)
    if track is None or track.episode_id != episode_id:
        raise NotFound("音轨不存在")
    # §11.1-3：删除音轨留痕（先把字段取出来，删除后对象不可再用）
    kind = track.kind
    media_id = track.media_id
    session.delete(track)
    audit_service.record(
        session,
        action=audit_service.ACTION_DELETE,
        target_type=audit_service.TARGET_MEDIA,
        target_id=track_id,
        episode_id=episode_id,
        detail={"track_kind": kind, "media_id": media_id},
    )
    session.flush()
    return {"deleted": track_id}


# --------------------------------------------------------------------------- #
# 字幕
# --------------------------------------------------------------------------- #

@router.post("/episodes/{episode_id}/subtitles/generate")
def generate_subtitles(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    job = jobs_service.create_job(
        session,
        type="SUBTITLE_GENERATION",
        idempotency_key=jobs_service.build_windowed_key(
            episode.id, "SUBTITLE_GENERATION"
        ),
        episode_id=episode.id,
        project_id=episode.project_id,
        stage="edit_agent",
        input_json={},
        input_summary={"scope": "episode"},
    )
    session.flush()
    return jobs_service.to_dict(job)


@router.get("/episodes/{episode_id}/subtitles")
def list_subtitles(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    cues = session.scalars(
        select(SubtitleCue)
        .where(SubtitleCue.episode_id == episode.id)
        .order_by(SubtitleCue.index_no)
    )
    return {
        "items": [
            {
                "id": c.id,
                "index": c.index_no,
                "start_ms": c.start_ms,
                "end_ms": c.end_ms,
                "text": c.text,
                "speaker": c.speaker,
                "edited": c.edited,
                "shot_id": c.shot_id,
            }
            for c in cues
        ]
    }


@router.get("/episodes/{episode_id}/subtitles/export")
def export_subtitles(
    episode_id: str,
    session: SessionDep,
    format: str = Query("srt", pattern="^(srt|vtt)$"),
) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    return {
        "format": format,
        "content": edit_agent.render_subtitles(session, episode, format),
        "filename": f"{episode.title}.{format}",
    }


@router.patch("/episodes/{episode_id}/subtitles/{cue_id}")
def update_subtitle(episode_id: str, cue_id: str, payload: dict, session: SessionDep) -> dict:
    """FR-053：人工校正单条字幕。

    只提交要改的字段。时间轴必须 `start_ms < end_ms` —— 否则会产出在
    播放器里不显示（或倒放）的字幕，且渲染时 FFmpeg 的 `subtitles` 滤镜会直接跳过该条，
    成片与字幕文件对不上。这里在写入前拦住。
    """
    cue = session.get(SubtitleCue, cue_id)
    if cue is None or cue.episode_id != episode_id:
        raise NotFound("字幕条目不存在")

    # 先算出「改完之后」的时间轴，再一起校验，避免只改一端时漏判
    start = cue.start_ms
    end = cue.end_ms
    for field in ("text", "start_ms", "end_ms", "speaker"):
        if field not in payload or payload[field] is None:
            continue
        value = payload[field]
        if field in ("start_ms", "end_ms"):
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise AppError(
                    f"字幕 {field} 必须是整数毫秒",
                    code=ErrorCode.VALIDATION_ERROR,
                    status_code=422,
                    details={"field": field, "received": payload[field]},
                ) from exc
            if value < 0:
                raise AppError(
                    f"字幕 {field} 不能为负",
                    code=ErrorCode.VALIDATION_ERROR,
                    status_code=422,
                    details={"field": field, "received": value},
                )
        if field == "start_ms":
            start = value
        elif field == "end_ms":
            end = value
        else:
            setattr(cue, field, value)

    if end <= start:
        raise AppError(
            "字幕结束时间必须大于开始时间",
            code=ErrorCode.VALIDATION_ERROR,
            status_code=422,
            details={"start_ms": start, "end_ms": end},
        )

    cue.start_ms = start
    cue.end_ms = end
    cue.edited = True
    session.flush()
    return {
        "id": cue.id,
        "index": cue.index_no,
        "start_ms": cue.start_ms,
        "end_ms": cue.end_ms,
        "text": cue.text,
        "speaker": cue.speaker,
        "edited": cue.edited,
    }
