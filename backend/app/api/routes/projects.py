"""项目、剧集、画风、角色、音色相关接口。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.agents import character as character_agent
from app.agents import audio as audio_agent
from app.agents import shot as shot_agent
from app.api.deps import (
    SessionDep,
    approval_dict,
    character_dict,
    episode_dict,
    get_character,
    get_episode,
    get_project,
    get_style,
    project_dict,
    record_approval,
    shots_of,
    style_dict,
    voice_dict,
)
from app.core.config import LOCAL_USER_ID
from app.core.errors import Conflict, NotFound, StateConflict
from app.db.models import (
    Approval,
    AssetStatus,
    AudioTrack,
    AuditLog,
    CharacterAsset,
    Episode,
    EpisodeStage,
    Job,
    MediaAsset,
    Project,
    Shot,
    ShotCharacterRef,
    StoryDocument,
    StyleAsset,
    Voice,
)
from app.schemas import (
    CharacterConfirm,
    CharacterCreate,
    CharacterGenerate,
    CharacterReplacePrimary,
    CharacterUpdate,
    EpisodeCopy,
    EpisodeCreate,
    EpisodeUpdate,
    ProjectCreate,
    ProjectUpdate,
    StyleIn,
    StyleUpdate,
    VoiceBind,
    VoiceCreate,
)
from app.services import audit as audit_service
from app.services import events
from app.services import jobs as jobs_service
from app.services import versions as versions_service

router = APIRouter(tags=["projects"])


def _sync_stale_stages(session: SessionDep, episode_ids: set[str]) -> None:
    """§10.1：上游变更后把受影响的剧集阶段标记为 `STALE`。

    只标记，不删除任何下游资源；用户可在界面里选择重新生成。
    """
    from app.db.models import Episode

    for episode_id in {eid for eid in episode_ids if eid}:
        episode = session.get(Episode, episode_id)
        if episode is not None:
            shot_agent.sync_episode_stale_stage(session, episode)


def audit(
    session: SessionDep,
    *,
    action: str,
    target_type: str,
    target_id: str,
    project_id: str | None = None,
    episode_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """§11.1-3：本文件内的历史调用点保持不变，实现下沉到统一审计服务。

    签名与行为（含 detail 脱敏口径）与原实现完全一致。
    """
    audit_service.record(
        session,
        action=action,
        target_type=target_type,
        target_id=target_id,
        project_id=project_id,
        episode_id=episode_id,
        detail=detail,
    )


# --------------------------------------------------------------------------- #
# 项目
# --------------------------------------------------------------------------- #

@router.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, session: SessionDep) -> dict:
    exists = session.scalar(
        select(Project).where(Project.name == payload.name, Project.user_id == LOCAL_USER_ID)
    )
    if exists:
        raise Conflict(f"项目名称已存在：{payload.name}")
    project = Project(
        user_id=LOCAL_USER_ID,
        name=payload.name,
        genre=payload.genre,
        aspect_ratio=payload.aspect_ratio,
        default_language=payload.default_language,
    )
    session.add(project)
    session.flush()

    style_payload = payload.style or StyleIn(
        name=f"{payload.genre}默认画风",
        description=f"{payload.genre}题材，电影感分镜，统一色调",
        palette=["#1b1f2a", "#e05d75", "#f0b429", "#4cc9b0"],
        camera_style="中近景为主，适度留白，竖屏 9:16",
        lighting_style="低调光，边缘轮廓光，冷暖对比",
        negative_prompt="低清，模糊，多余肢体，文字水印",
    )
    style = StyleAsset(
        project_id=project.id,
        name=style_payload.name,
        description=style_payload.description,
        palette=style_payload.palette,
        camera_style=style_payload.camera_style,
        lighting_style=style_payload.lighting_style,
        negative_prompt=style_payload.negative_prompt,
    )
    session.add(style)
    session.flush()
    project.default_style_id = style.id
    audit(session, action="project.create", target_type="project", target_id=project.id,
          project_id=project.id, detail={"name": project.name})
    session.flush()
    return project_dict(session, project)


@router.get("/projects")
def list_projects(
    session: SessionDep,
    include_archived: bool = Query(False),
) -> dict:
    stmt = select(Project).order_by(Project.created_at.desc())
    if not include_archived:
        stmt = stmt.where(Project.status == "ACTIVE")
    return {"items": [project_dict(session, p) for p in session.scalars(stmt)]}


@router.get("/projects/{project_id}")
def read_project(project_id: str, session: SessionDep) -> dict:
    return project_dict(session, get_project(session, project_id=project_id))


@router.patch("/projects/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate, session: SessionDep) -> dict:
    project = get_project(session, project_id=project_id)
    if payload.name and payload.name != project.name:
        # 与创建时的口径保持一致：唯一性限定在同一用户工作区内（PRD FR-001）
        dup = session.scalar(
            select(Project).where(
                Project.name == payload.name,
                Project.user_id == project.user_id,
                Project.id != project.id,
            )
        )
        if dup:
            raise Conflict(f"项目名称已存在：{payload.name}")
        project.name = payload.name
    for field in ("genre", "aspect_ratio", "default_language", "default_style_id"):
        value = getattr(payload, field)
        if value is not None:
            setattr(project, field, value)
    session.flush()
    return project_dict(session, project)


@router.post("/projects/{project_id}/archive")
def archive_project(project_id: str, session: SessionDep) -> dict:
    project = get_project(session, project_id=project_id)
    project.status = "ARCHIVED"
    audit(session, action="project.archive", target_type="project", target_id=project.id,
          project_id=project.id)
    session.flush()
    return project_dict(session, project)


@router.post("/projects/{project_id}/restore")
def restore_project(project_id: str, session: SessionDep) -> dict:
    project = get_project(session, project_id=project_id)
    project.status = "ACTIVE"
    audit(session, action="project.restore", target_type="project", target_id=project.id,
          project_id=project.id)
    session.flush()
    return project_dict(session, project)


@router.get("/projects/{project_id}/audit")
def project_audit(project_id: str, session: SessionDep, limit: int = Query(100, le=500)) -> dict:
    get_project(session, project_id=project_id)
    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.project_id == project_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    return {
        "items": [
            {
                "id": r.id,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "actor_id": r.actor_id,
                "detail": r.detail,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


# --------------------------------------------------------------------------- #
# 剧集
# --------------------------------------------------------------------------- #

@router.post("/projects/{project_id}/episodes", status_code=201)
def create_episode(project_id: str, payload: EpisodeCreate, session: SessionDep) -> dict:
    project = get_project(session, project_id=project_id)
    episode_no = payload.episode_no
    if episode_no is None:
        episode_no = int(
            session.scalar(
                select(Episode.episode_no)
                .where(Episode.project_id == project.id)
                .order_by(Episode.episode_no.desc())
                .limit(1)
            )
            or 0
        ) + 1
    else:
        dup = session.scalar(
            select(Episode).where(
                Episode.project_id == project.id, Episode.episode_no == episode_no
            )
        )
        if dup:
            raise Conflict(f"第 {episode_no} 集已存在")
    episode = Episode(
        project_id=project.id,
        title=payload.title,
        episode_no=episode_no,
        target_duration_ms=payload.target_duration_ms,
        stage=EpisodeStage.DRAFT.value,
    )
    session.add(episode)
    session.flush()
    return episode_dict(session, episode)


@router.get("/projects/{project_id}/episodes")
def list_episodes(
    project_id: str,
    session: SessionDep,
    include_archived: bool = Query(False),
) -> dict:
    get_project(session, project_id=project_id)
    stmt = select(Episode).where(Episode.project_id == project_id).order_by(Episode.episode_no)
    if not include_archived:
        stmt = stmt.where(Episode.status == "ACTIVE")
    return {"items": [episode_dict(session, e) for e in session.scalars(stmt)]}


@router.get("/episodes/{episode_id}")
def read_episode(episode_id: str, session: SessionDep) -> dict:
    return episode_dict(session, get_episode(session, episode_id=episode_id))


@router.patch("/episodes/{episode_id}")
def update_episode(episode_id: str, payload: EpisodeUpdate, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    if payload.title:
        episode.title = payload.title
    if payload.target_duration_ms:
        episode.target_duration_ms = payload.target_duration_ms
    session.flush()
    return episode_dict(session, episode)


@router.post("/episodes/{episode_id}/copy", status_code=201)
def copy_episode(episode_id: str, payload: EpisodeCopy, session: SessionDep) -> dict:
    """复制剧集：复制剧本与资产引用，不复制进行中任务（FR-MVP-002）。"""
    source = get_episode(session, episode_id=episode_id)
    episode_no = int(
        session.scalar(
            select(Episode.episode_no)
            .where(Episode.project_id == source.project_id)
            .order_by(Episode.episode_no.desc())
            .limit(1)
        )
        or 0
    ) + 1
    clone = Episode(
        project_id=source.project_id,
        title=payload.title or f"{source.title} 副本",
        episode_no=episode_no,
        target_duration_ms=source.target_duration_ms,
        stage=source.stage,
        version=1,
    )
    session.add(clone)
    session.flush()

    for document in session.scalars(
        select(StoryDocument).where(StoryDocument.episode_id == source.id)
    ):
        session.add(
            StoryDocument(
                episode_id=clone.id,
                version=document.version,
                input_type=document.input_type,
                source_text=document.source_text,
                story_json=document.story_json,
                analysis_json=document.analysis_json,
                status=document.status,
            )
        )
    for shot in shots_of(session, source.id):
        new_shot = Shot(
            episode_id=clone.id,
            shot_no=shot.shot_no,
            duration_ms=shot.duration_ms,
            shot_json=shot.shot_json,
            stage=clone.stage,
            current_version=shot.current_version,
            required=shot.required,
            selected_media_id=shot.selected_media_id,
            confirmed_media_id=shot.confirmed_media_id,
            confirmed_at=shot.confirmed_at,
            image_status=shot.image_status,
            video_status=shot.video_status,
            video_media_id=shot.video_media_id,
            audio_status=shot.audio_status,
        )
        session.add(new_shot)
        session.flush()
        for ref in session.scalars(
            select(ShotCharacterRef).where(ShotCharacterRef.shot_id == shot.id)
        ):
            session.add(
                ShotCharacterRef(
                    shot_id=new_shot.id,
                    character_id=ref.character_id,
                    character_version=ref.character_version,
                    media_id=ref.media_id,
                )
            )
    session.flush()
    return episode_dict(session, clone)


@router.post("/episodes/{episode_id}/archive")
def archive_episode(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    episode.status = "ARCHIVED"
    # §11.1-3：归档必须留痕
    audit(
        session,
        action=audit_service.ACTION_ARCHIVE,
        target_type=audit_service.TARGET_EPISODE,
        target_id=episode.id,
        project_id=episode.project_id,
        episode_id=episode.id,
        detail={"title": episode.title, "status": "ARCHIVED"},
    )
    session.flush()
    return episode_dict(session, episode)


@router.post("/episodes/{episode_id}/restore")
def restore_episode(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    episode.status = "ACTIVE"
    audit(
        session,
        action=audit_service.ACTION_UPDATE,
        target_type=audit_service.TARGET_EPISODE,
        target_id=episode.id,
        project_id=episode.project_id,
        episode_id=episode.id,
        detail={"status": "ACTIVE"},
    )
    session.flush()
    return episode_dict(session, episode)


@router.get("/episodes/{episode_id}/overview")
def episode_overview(episode_id: str, session: SessionDep) -> dict:
    from app.agents import edit as edit_agent
    from app.agents import safety as safety_agent
    from app.services import media as media_service
    from app.workflow import engine as workflow_engine

    episode = get_episode(session, episode_id=episode_id)
    shots = shots_of(session, episode.id)
    run = workflow_engine.get_run(session, episode.id)
    validation = edit_agent.validate_timeline(session, episode)
    return {
        "episode": episode_dict(session, episode),
        "workflow": workflow_engine.snapshot(session, run) if run else None,
        "graph": workflow_engine.graph_overview(session, run),
        "job_stats": jobs_service.stats(session, episode.id),
        "media_counts": media_service.count_by_kind(session, episode.id),
        "shot_stats": {
            "total": len(shots),
            "confirmed": len([s for s in shots if s.confirmed_media_id]),
            "video_confirmed": len([s for s in shots if s.video_status == "CONFIRMED"]),
            "video_skipped": len([s for s in shots if s.video_status == "SKIPPED"]),
            "stale": len([s for s in shots if s.stale]),
        },
        "timeline": {
            "total_duration_ms": validation["timeline"]["total_duration_ms"],
            "valid": validation["valid"],
            "error_count": len(validation["errors"]),
            "warning_count": len(validation["warnings"]),
        },
        "safety": safety_agent.episode_risk_summary(session, episode),
    }


@router.get("/episodes/{episode_id}/approvals")
def list_approvals(episode_id: str, session: SessionDep) -> dict:
    get_episode(session, episode_id=episode_id)
    rows = session.scalars(
        select(Approval)
        .where(Approval.episode_id == episode_id)
        .order_by(Approval.created_at.desc())
    )
    return {"items": [approval_dict(a) for a in rows]}


@router.get("/episodes/{episode_id}/safety")
def episode_safety(episode_id: str, session: SessionDep) -> dict:
    from app.agents import safety as safety_agent

    episode = get_episode(session, episode_id=episode_id)
    return safety_agent.episode_risk_summary(session, episode)


# --------------------------------------------------------------------------- #
# 画风
# --------------------------------------------------------------------------- #

@router.get("/projects/{project_id}/styles")
def list_styles(project_id: str, session: SessionDep) -> dict:
    project = get_project(session, project_id=project_id)
    rows = session.scalars(
        select(StyleAsset).where(StyleAsset.project_id == project_id).order_by(StyleAsset.created_at)
    )
    return {"items": [style_dict(s, project=project) for s in rows]}


@router.post("/projects/{project_id}/styles", status_code=201)
def create_style(project_id: str, payload: StyleIn, session: SessionDep) -> dict:
    project = get_project(session, project_id=project_id)
    style = StyleAsset(
        project_id=project_id,
        name=payload.name,
        description=payload.description,
        palette=payload.palette,
        camera_style=payload.camera_style,
        lighting_style=payload.lighting_style,
        negative_prompt=payload.negative_prompt,
    )
    session.add(style)
    session.flush()
    return style_dict(style, project=project)


@router.patch("/styles/{style_id}")
def update_style(style_id: str, payload: StyleUpdate, session: SessionDep) -> dict:
    """画风局部更新。未提供的字段一律不动。

    这里必须用 `StyleUpdate` 而不是 `StyleIn`：后者是创建用的，字段带非 None 默认值，
    而下面的判空是 `value is not None` —— 用它会「只改描述就把 name 重置成默认画风、palette 清空」。
    """
    style = get_style(session, style_id=style_id)
    changed = False
    for field in ("name", "description", "palette", "camera_style", "lighting_style",
                  "negative_prompt"):
        value = getattr(payload, field)
        if value is not None and getattr(style, field) != value:
            setattr(style, field, value)
            changed = True
    if changed:
        style.version = int(style.version or 1) + 1
    session.flush()
    return style_dict(style, project=session.get(Project, style.project_id))


@router.post("/styles/{style_id}/apply")
def apply_style(style_id: str, session: SessionDep) -> dict:
    style = get_style(session, style_id=style_id)
    project = get_project(session, project_id=style.project_id)
    project.default_style_id = style.id
    shots = session.scalars(
        select(Shot)
        .join(Episode, Episode.id == Shot.episode_id)
        .where(Episode.project_id == project.id)
    )
    affected = 0
    for shot in shots:
        shot.stale = True
        shot.stale_reason = "画风已切换，建议重新生成画面"
        affected += 1
    session.flush()
    # §10.1：受影响的剧集阶段标记为 STALE（不删除下游资源，由用户决定是否重生成）
    _sync_stale_stages(session, {s.episode_id for s in shots})
    return {"project_id": project.id, "style_id": style.id, "affected_shots": affected}


# --------------------------------------------------------------------------- #
# 角色
# --------------------------------------------------------------------------- #

@router.get("/projects/{project_id}/characters")
def list_characters(
    project_id: str,
    session: SessionDep,
    include_archived: bool = Query(False),
) -> dict:
    """项目角色列表。

    `DELETE /characters/{id}` 是**软归档**（只把 status 置为 ARCHIVED），所以这里默认
    过滤掉已归档的角色 —— 否则前端「归档」后一刷新角色又回来了，看起来像没生效。
    需要看历史角色时传 `include_archived=true`（与 `GET /projects` 同一套约定）。
    """
    get_project(session, project_id=project_id)
    stmt = (
        select(CharacterAsset)
        .where(CharacterAsset.project_id == project_id)
        .order_by(CharacterAsset.created_at)
    )
    if not include_archived:
        stmt = stmt.where(CharacterAsset.status != AssetStatus.ARCHIVED.value)
    return {"items": [character_dict(session, c) for c in session.scalars(stmt)]}


@router.post("/projects/{project_id}/characters", status_code=201)
def create_character(project_id: str, payload: CharacterCreate, session: SessionDep) -> dict:
    project = get_project(session, project_id=project_id)
    dup = session.scalar(
        select(CharacterAsset).where(
            CharacterAsset.project_id == project.id, CharacterAsset.name == payload.name
        )
    )
    if dup:
        raise Conflict(f"角色已存在：{payload.name}")
    character = CharacterAsset(
        project_id=project.id,
        name=payload.name,
        spec_json={
            "identity": payload.identity,
            "age_range": payload.age_range,
            "appearance": payload.appearance,
            "outfit": payload.outfit,
            "personality": payload.personality,
            "speech_style": payload.speech_style,
            "forbidden_changes": payload.forbidden_changes,
        },
        is_main=payload.is_main,
        status=AssetStatus.DRAFT.value,
    )
    session.add(character)
    session.flush()
    return character_dict(session, character)


@router.get("/characters/{character_id}")
def read_character(character_id: str, session: SessionDep) -> dict:
    return character_dict(session, get_character(session, character_id=character_id))


@router.patch("/characters/{character_id}")
def update_character(character_id: str, payload: CharacterUpdate, session: SessionDep) -> dict:
    character = get_character(session, character_id=character_id)
    spec = dict(character.spec_json or {})
    changed = False
    for field in ("identity", "age_range", "appearance", "outfit", "personality",
                  "speech_style", "forbidden_changes"):
        value = getattr(payload, field)
        if value is not None and spec.get(field) != value:
            spec[field] = value
            changed = True
    character.spec_json = spec
    if payload.is_main is not None:
        character.is_main = payload.is_main
    if payload.voice_id is not None:
        character.voice_id = payload.voice_id
    if payload.voice_speed is not None:
        # FR-051：改语速会让已生成的台词音频不再匹配，需重跑配音
        character.voice_speed = audio_agent.clamp_speed(payload.voice_speed)
        for track in session.scalars(
            select(AudioTrack).where(
                AudioTrack.character_id == character.id, AudioTrack.kind == "dialogue"
            )
        ):
            track.stale = True
            track.stale_reason = "角色语速已变更，需重新生成配音"
    if changed:
        character.version = int(character.version or 1) + 1
        character.status = AssetStatus.DRAFT.value
        # FR-004：角色描述留版本，便于回溯「这版成片用的是哪版设定」
        versions_service.record(
            session,
            entity_type=versions_service.TARGET_CHARACTER,
            entity_id=character.id,
            payload=spec,
            note="角色描述更新",
        )
        shots = session.scalars(
            select(Shot)
            .join(ShotCharacterRef, ShotCharacterRef.shot_id == Shot.id)
            .where(ShotCharacterRef.character_id == character.id)
        )
        for shot in shots:
            shot.stale = True
            shot.stale_reason = f"角色「{character.name}」设定已更新"
        session.flush()
        _sync_stale_stages(session, {s.episode_id for s in shots})
    session.flush()
    return character_dict(session, character)


@router.delete("/characters/{character_id}")
def archive_character(character_id: str, session: SessionDep) -> dict:
    character = get_character(session, character_id=character_id)
    character.status = AssetStatus.ARCHIVED.value
    # §11.1-3：删除角色必须留痕
    audit(
        session,
        action=audit_service.ACTION_DELETE,
        target_type=audit_service.TARGET_CHARACTER,
        target_id=character.id,
        project_id=character.project_id,
        detail={"name": character.name, "status": character.status},
    )
    session.flush()
    return character_dict(session, character)


@router.post("/characters/{character_id}/generate")
def generate_character_assets(
    character_id: str, payload: CharacterGenerate, session: SessionDep
) -> dict:
    character = get_character(session, character_id=character_id)
    job = jobs_service.create_job(
        session,
        type="CHARACTER_ASSET_GENERATION",
        idempotency_key=jobs_service.build_idempotency_key(
            character.id, "character_assets", character.version,
            ",".join(sorted(payload.asset_kinds)), payload.count_per_kind,
            int(dt.datetime.now(dt.timezone.utc).timestamp() // 5),
        ),
        episode_id=None,
        project_id=character.project_id,
        stage="character_agent",
        input_json={
            "character_id": character.id,
            "asset_kinds": list(payload.asset_kinds),
            "count_per_kind": payload.count_per_kind,
        },
        input_summary={"character": character.name, "kinds": list(payload.asset_kinds)},
        max_attempts=2,
    )
    session.flush()
    return jobs_service.to_dict(job)


@router.get("/characters/{character_id}/assets")
def character_assets(character_id: str, session: SessionDep) -> dict:
    from app.services import media as media_service

    character = get_character(session, character_id=character_id)
    items = [
        media_service.to_dict(a)
        for a in media_service.list_media(session, owner_id=character.id, limit=200)
    ]
    return {"items": items, "primary_media_id": character.primary_media_id}


@router.post("/characters/{character_id}/confirm")
def confirm_character(character_id: str, payload: CharacterConfirm, session: SessionDep) -> dict:
    character = get_character(session, character_id=character_id)
    character_agent.confirm_primary(session, character, payload.media_id)
    episode_ids = [
        e.id
        for e in session.scalars(select(Episode).where(Episode.project_id == character.project_id))
    ]
    for episode_id in episode_ids:
        record_approval(
            session,
            episode_id=episode_id,
            stage="gate_character_confirmation",
            target_type="character",
            target_id=character.id,
            asset_version=character.version,
            candidate_media_id=payload.media_id,
        )
    session.flush()
    return character_dict(session, character)


@router.post("/characters/{character_id}/replace-primary")
def replace_primary(
    character_id: str, payload: CharacterReplacePrimary, session: SessionDep
) -> dict:
    character = get_character(session, character_id=character_id)
    affected = character_agent.affected_shots(session, character)
    character_agent.confirm_primary(session, character, payload.media_id)
    character_agent.bump_version(session, character, "替换主参考图")
    if payload.impact == "mark_stale":
        for item in affected:
            shot = session.get(Shot, item["shot_id"])
            if shot:
                shot.stale = True
                shot.stale_reason = f"角色「{character.name}」主参考图已替换"
        session.flush()
        # §10.1：受影响的剧集阶段标记为 STALE
        _sync_stale_stages(
            session,
            {
                item["episode_id"]
                for item in affected
                if item.get("episode_id")
            }
            or {
                s.episode_id
                for s in (session.get(Shot, item["shot_id"]) for item in affected)
                if s is not None
            },
        )
    session.flush()
    return {
        "character": character_dict(session, character),
        "impact": payload.impact,
        "affected_shots": affected,
    }


# --------------------------------------------------------------------------- #
# 音色
# --------------------------------------------------------------------------- #

@router.get("/projects/{project_id}/voices")
def list_voices(project_id: str, session: SessionDep) -> dict:
    project = get_project(session, project_id=project_id)
    voices = audio_agent.ensure_default_voices(session, project)
    return {"items": [voice_dict(v) for v in voices]}


@router.post("/projects/{project_id}/voices", status_code=201)
def create_voice(project_id: str, payload: VoiceCreate, session: SessionDep) -> dict:
    get_project(session, project_id=project_id)
    voice = Voice(
        project_id=project_id,
        name=payload.name,
        provider=payload.provider,
        provider_voice_id=payload.provider_voice_id,
        gender=payload.gender,
        description=payload.description,
    )
    session.add(voice)
    session.flush()
    return voice_dict(voice)


@router.post("/characters/{character_id}/voice")
def bind_voice(character_id: str, payload: VoiceBind, session: SessionDep) -> dict:
    character = get_character(session, character_id=character_id)
    # FR-051：绑定音色时可一并设置语速；换音色/改语速都会让已有配音过期
    previous_voice = character.voice_id
    previous_speed = character.voice_speed
    audio_agent.bind_voice(session, character, payload.voice_id, payload.speed)
    if previous_voice != character.voice_id or previous_speed != character.voice_speed:
        for track in session.scalars(
            select(AudioTrack).where(
                AudioTrack.character_id == character.id, AudioTrack.kind == "dialogue"
            )
        ):
            track.stale = True
            track.stale_reason = "角色音色或语速已变更，需重新生成配音"
    session.flush()
    return character_dict(session, character)


@router.get("/voices/{voice_id}/preview")
def preview_voice(voice_id: str, session: SessionDep) -> dict:
    from app.agents.story import run_coro
    from app.providers.base import ProviderKind, SpeechRequest
    from app.providers.registry import resolve
    from app.services import media as media_service

    voice = session.get(Voice, voice_id)
    if voice is None:
        raise NotFound("音色不存在")
    if voice.preview_media_id:
        asset = session.get(MediaAsset, voice.preview_media_id)
        if asset:
            return {"media_id": asset.id, "url": media_service.media_url(asset)}
    provider = resolve(ProviderKind.SPEECH, session)
    result = run_coro(
        provider.synthesize(
            SpeechRequest(text="这是一段音色试听示例，用来确认角色的声音。", voice_id=voice.provider_voice_id)
        )
    )
    if not result.ok or not result.artifacts:
        raise StateConflict(result.error_message or "音色试听生成失败")
    asset = media_service.save_artifact(
        session,
        result.artifacts[0],
        kind="audio",
        role="voice_preview",
        owner_type="voice",
        owner_id=voice.id,
        project_id=voice.project_id,
        status=AssetStatus.CONFIRMED.value,
        metadata={"voice_id": voice.id, "provider": provider.id},
    )
    voice.preview_media_id = asset.id
    session.flush()
    return {"media_id": asset.id, "url": media_service.media_url(asset)}
