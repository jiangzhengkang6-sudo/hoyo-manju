"""项目、剧集、画风、角色、音色相关接口。"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import delete, func, select

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
from app.core.errors import Conflict, NotFound, ProviderError, StateConflict
from app.db.models import (
    Approval,
    AssetStatus,
    AudioTrack,
    AuditLog,
    CharacterAsset,
    EntityVersion,
    Episode,
    EpisodeStage,
    EventLog,
    ExportRecord,
    Job,
    JobStatus,
    MediaAsset,
    Project,
    ProviderCall,
    SafetyAppeal,
    Shot,
    ShotCharacterRef,
    StoryDocument,
    StyleAsset,
    SubtitleCue,
    Voice,
    WorkflowNodeLog,
    WorkflowRun,
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
    VoiceSpeedIn,
    VoiceCreate,
)
from app.services import audit as audit_service
from app.services import events
from app.services import jobs as jobs_service
from app.services import versions as versions_service
from app.services.storage import get_storage

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
    if payload.default_voice_speed is not None:
        # 只改「以后新建角色」的初始值；已存在的角色要显式应用（见 /voice-speed）
        project.default_voice_speed = audio_agent.clamp_speed(payload.default_voice_speed)
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
    """复制剧集：复制剧本与资产引用，不复制进行中任务（FR-002）。"""
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


logger = logging.getLogger("hoyomanju")

#: 删除剧集时要先清掉的「孙表」（外键指向剧集子表，不能直接 CASCADE）
_EPISODE_CHILD_MODELS = (
    StoryDocument,
    Shot,
    CharacterAsset,
    AudioTrack,
    SubtitleCue,
    ExportRecord,
    Approval,
    SafetyAppeal,
    WorkflowRun,
)


@router.delete("/episodes/{episode_id}")
def delete_episode(episode_id: str, session: SessionDep) -> dict:
    """**彻底删除**剧集及其全部内容（不可恢复）。

    与「归档」（`status=ARCHIVED`，可恢复）不同，这里会连同剧本、分镜、角色卡、
    音轨、字幕、媒体文件、任务、工作流运行、审批与申诉一起删除。

    两条安全线：
    1. 还有进行中的任务时拒绝（409），避免 worker 正在写的行被删掉；
    2. 成本流水与审计日志**保留**（财务与合规要求），只是不再挂在剧集上。
    """
    episode = get_episode(session, episode_id=episode_id)
    active_statuses = (
        JobStatus.CREATED.value,
        JobStatus.QUEUED.value,
        JobStatus.RUNNING.value,
        JobStatus.RETRYING.value,
        JobStatus.CANCELLING.value,
    )
    active = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.episode_id == episode.id, Job.status.in_(active_statuses))
    )
    if active:
        raise StateConflict("该剧集还有进行中的任务，请先取消或等任务结束再删除")

    shot_ids = list(session.scalars(select(Shot.id).where(Shot.episode_id == episode.id)))
    run_ids = list(
        session.scalars(select(WorkflowRun.id).where(WorkflowRun.episode_id == episode.id))
    )
    job_ids = list(session.scalars(select(Job.id).where(Job.episode_id == episode.id)))
    document_ids = list(
        session.scalars(select(StoryDocument.id).where(StoryDocument.episode_id == episode.id))
    )
    media = list(session.scalars(select(MediaAsset).where(MediaAsset.episode_id == episode.id)))

    # 1) 先删「孙表」：分镜→角色引用、工作流节点日志、供应商调用记录
    if shot_ids:
        session.execute(delete(ShotCharacterRef).where(ShotCharacterRef.shot_id.in_(shot_ids)))
    if run_ids:
        session.execute(
            delete(WorkflowNodeLog).where(WorkflowNodeLog.workflow_run_id.in_(run_ids))
        )
    if job_ids:
        session.execute(delete(ProviderCall).where(ProviderCall.job_id.in_(job_ids)))

    # 2) 再删剧集直属子表（有外键指向 episodes）
    for model in _EPISODE_CHILD_MODELS:
        session.execute(delete(model).where(model.episode_id == episode.id))

    # 3) 无外键但属于剧集运行态的数据
    session.execute(delete(Job).where(Job.episode_id == episode.id))
    session.execute(delete(EventLog).where(EventLog.episode_id == episode.id))
    session.execute(delete(MediaAsset).where(MediaAsset.episode_id == episode.id))
    entity_ids = [*shot_ids, *document_ids]
    if entity_ids:
        session.execute(delete(EntityVersion).where(EntityVersion.entity_id.in_(entity_ids)))

    # 4) 媒体文件（尽力而为：文件删不掉也要让数据行消失，避免列表里点开是坏图）
    storage = get_storage()
    removed_files = 0
    for asset in media:
        try:
            storage.delete(asset.storage_key)
            removed_files += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除媒体文件失败（忽略）：%s %s", asset.storage_key, exc)

    title = episode.title
    session.delete(episode)
    session.flush()

    # 5) 审计留痕放在最后：即使剧集没了，也要能查到「谁删了什么」
    audit(
        session,
        action=audit_service.ACTION_DELETE,
        target_type=audit_service.TARGET_EPISODE,
        target_id=episode_id,
        project_id=episode.project_id,
        episode_id=episode_id,
        detail={"title": title, "media_files": removed_files, "shots": len(shot_ids)},
    )
    session.flush()
    return {"deleted": True, "episode_id": episode_id, "media_files": removed_files}


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
    episode_id: str | None = Query(None),
    include_all: bool = Query(False),
) -> dict:
    """项目角色列表。

    `DELETE /characters/{id}` 是**软归档**（只把 status 置为 ARCHIVED），所以这里默认
    过滤掉已归档的角色 —— 否则前端「归档」后一刷新角色又回来了，看起来像没生效。
    需要看历史角色时传 `include_archived=true`（与 `GET /projects` 同一套约定）。

    角色卡是**项目级资产库**，但一个项目下可能有多集完全不相干的故事
    （例如 EP02 异世界、EP03 刑侦）。带 `episode_id` 时只返回「本集剧本人物表
    + 分镜里出现过 + 手工新增」的角色，避免角色页混进别的剧集的角色；
    `include_all=true` 可看全项目。
    """
    get_project(session, project_id=project_id)
    stmt = (
        select(CharacterAsset)
        .where(CharacterAsset.project_id == project_id)
        .order_by(CharacterAsset.created_at)
    )
    if not include_archived:
        stmt = stmt.where(CharacterAsset.status != AssetStatus.ARCHIVED.value)
    characters = list(session.scalars(stmt))
    hidden = 0
    if episode_id and not include_all:
        get_episode(session, episode_id=episode_id)  # 不存在时直接 404
        own = [c for c in characters if c.episode_id == episode_id]
        others = [c for c in characters if c.episode_id not in (None, episode_id)]
        shared = [c for c in characters if c.episode_id is None]
        # 本集已有专属卡时，同名的项目级公共卡不再重复显示
        shadowed = {c.name for c in own}
        characters = own + [c for c in shared if c.name not in shadowed]
        hidden = len(others)
    return {
        "items": [character_dict(session, c) for c in characters],
        "total": len(characters),
        # 被「按剧集过滤」挡掉的其他剧集角色数，前端用它提示「还有 N 个角色在别的剧集」
        "hidden": hidden,
    }


@router.post("/projects/{project_id}/characters", status_code=201)
def create_character(project_id: str, payload: CharacterCreate, session: SessionDep) -> dict:
    project = get_project(session, project_id=project_id)
    episode_id = payload.episode_id
    if episode_id:
        episode = get_episode(session, episode_id=episode_id)
        if episode.project_id != project.id:
            raise Conflict("剧集不属于当前项目")
    # 手工新增：默认是项目级公共角色（episode_id 为空）；从某一集页面新增时带上剧集，
    # 避免同名角色在别的剧集里也冒出来。
    dup = session.scalar(
        select(CharacterAsset).where(
            CharacterAsset.project_id == project.id,
            CharacterAsset.name == payload.name,
            CharacterAsset.episode_id == episode_id,
        )
    )
    if dup:
        raise Conflict(f"角色已存在：{payload.name}")
    character = CharacterAsset(
        project_id=project.id,
        episode_id=episode_id,
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
        # FR-051：初始语速跟随项目「统一语速」，避免每个角色都要手动设一遍
        voice_speed=float(project.default_voice_speed or 1.0),
    )
    session.add(character)
    session.flush()
    return character_dict(session, character)


@router.post("/episodes/{episode_id}/characters/sync")
def sync_characters_from_story(
    episode_id: str,
    session: SessionDep,
    force: bool = Query(False),
) -> dict:
    """从该剧集的最新剧本抓取角色，生成/补全角色卡（幂等，可反复调用）。

    剧本生成时后端已经自动同步过一次；这个接口给「改了剧本想重新抓」「角色页
    数据看着不对想刷新」提供显式入口。默认**不覆盖**用户手改过的字段，
    `force=true` 才会用剧本人物表整体覆盖。
    """
    episode = get_episode(session, episode_id=episode_id)
    document = session.scalar(
        select(StoryDocument)
        .where(StoryDocument.episode_id == episode.id)
        .order_by(StoryDocument.version.desc())
        .limit(1)
    )
    if document is None:
        raise NotFound("该剧集尚未生成剧本，无法抓取角色")
    project = get_project(session, project_id=episode.project_id)
    result = character_agent.sync_characters_from_script(
        session,
        project,
        document.story_json or {},
        document=document,
        episode_id=episode.id,
        force=force,
    )
    characters = result["characters"]
    return {
        "episode_id": episode.id,
        "script_version": document.version,
        "created": len(result["created"]),
        "updated": len(result["updated"]),
        "kept": len(result["kept"]),
        "total": len(characters),
        "names": [c.name for c in characters],
        "items": [character_dict(session, c) for c in characters],
    }


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
    if changed:
        # 手动改过的角色不再被「剧本同步」覆盖（用户改的东西优先），
        # 需要「用剧本覆盖」时走 sync 接口的 force=true。
        spec[character_agent.MARK_USER_EDITED] = True
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
            payload=character_agent.public_spec(spec),
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
    items = []
    # 已驳回的素材不进列表：它们可能正是「同名文件被覆盖」留下的历史记录，
    # 展示出来会让用户看到张冠李戴的图。
    for asset in media_service.list_media(
        session, owner_id=character.id, limit=200, include_rejected=False
    ):
        row = media_service.to_dict(asset)
        # 前端契约（types/index.ts CharacterAsset）用 `media_id`；媒体行本身是 `id`。
        # 不补这个别名，前端 `asset.media_id` 就是 undefined，点「设为主图」会提交空 body → 422。
        row["media_id"] = asset.id
        items.append(row)
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


@router.post("/projects/{project_id}/voice-speed")
def apply_project_voice_speed(
    project_id: str, payload: VoiceSpeedIn, session: SessionDep
) -> dict:
    """把统一语速应用到项目下**全部角色**，并把已有配音标记为过期。

    只改角色设定，不重新合成音频；用户在音频页点「生成全部台词」即可按新语速重跑。
    """
    project = get_project(session, project_id=project_id)
    speed = audio_agent.clamp_speed(payload.speed)
    project.default_voice_speed = speed
    updated: list[str] = []
    retimed: list[str] = []
    for character in session.scalars(
        select(CharacterAsset).where(CharacterAsset.project_id == project.id)
    ):
        if character.status == AssetStatus.ARCHIVED.value:
            continue
        if abs(float(character.voice_speed or 1.0) - speed) < 1e-6:
            continue
        character.voice_speed = speed
        updated.append(character.id)
        for track in session.scalars(
            select(AudioTrack).where(
                AudioTrack.character_id == character.id, AudioTrack.kind == "dialogue"
            )
        ):
            # 已有音频就地变速（本地 FFmpeg、0 费用），不用重跑模型；
            # 失败才退回「标记过期，等用户重新生成」。
            episode = session.get(Episode, track.episode_id) if track.episode_id else None
            if episode is not None and track.media_id:
                try:
                    audio_agent.retime_track(
                        session, episode=episode, track=track, speed=speed
                    )
                    retimed.append(track.id)
                    continue
                except Exception:  # noqa: BLE001 - 单条失败不应中断整体
                    pass
            track.stale = True
            track.stale_reason = "统一语速已变更，需重新生成配音"
    session.flush()
    return {
        "default_voice_speed": speed,
        "updated_characters": updated,
        "retimed_tracks": retimed,
        "count": len(updated),
        "retimed": len(retimed),
    }


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
    provider = resolve(ProviderKind.SPEECH, session)
    # 供应商切换后旧音色 id 对面不认（会静默变成默认女声），顺手对齐并作废旧试听
    audio_agent.align_voice_to_provider(session, voice, provider.id)
    if voice.preview_media_id:
        asset = session.get(MediaAsset, voice.preview_media_id)
        if asset:
            return {"media_id": asset.id, "url": media_service.media_url(asset)}
    result = run_coro(
        provider.synthesize(
            SpeechRequest(
                text="这是一段音色试听示例，用来确认角色的声音。",
                voice_id=voice.provider_voice_id,
                style_prompt=audio_agent.voice_style_prompt(voice),
            )
        )
    )
    if not result.ok or not result.artifacts:
        # 音色试听失败是**供应商侧**的问题（网络/额度/产物下载），不是「当前状态不允许」，
        # 之前一律抛 StateConflict(409)，前端只能看到「当前状态不允许该操作」，误导排查。
        raise ProviderError(
            result.error_message or "音色试听生成失败",
            details={"provider": provider.id, "voice_id": voice.id},
            retryable=True,
        )
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
