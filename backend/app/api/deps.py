"""API 依赖与通用工具。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.db.models import (
    Approval,
    CharacterAsset,
    Episode,
    MediaAsset,
    Project,
    Shot,
    StyleAsset,
    Voice,
)
from app.db.session import get_session
from app.services import cost as cost_service
from app.services import media as media_service

SessionDep = Annotated[Session, Depends(get_session)]


def get_project(session: SessionDep, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise NotFound(f"项目不存在：{project_id}")
    return project


def get_episode(session: SessionDep, episode_id: str) -> Episode:
    episode = session.get(Episode, episode_id)
    if episode is None:
        raise NotFound(f"剧集不存在：{episode_id}")
    return episode


def get_shot(session: SessionDep, shot_id: str) -> Shot:
    shot = session.get(Shot, shot_id)
    if shot is None:
        raise NotFound(f"分镜不存在：{shot_id}")
    return shot


def get_character(session: SessionDep, character_id: str) -> CharacterAsset:
    character = session.get(CharacterAsset, character_id)
    if character is None:
        raise NotFound(f"角色不存在：{character_id}")
    return character


def get_style(session: SessionDep, style_id: str) -> StyleAsset:
    style = session.get(StyleAsset, style_id)
    if style is None:
        raise NotFound(f"画风不存在：{style_id}")
    return style


def shots_of(session: Session, episode_id: str) -> list[Shot]:
    return list(
        session.scalars(select(Shot).where(Shot.episode_id == episode_id).order_by(Shot.shot_no))
    )


def project_of(session: Session, episode: Episode) -> Project:
    project = session.get(Project, episode.project_id)
    if project is None:
        raise NotFound("剧集所属项目不存在")
    return project


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #

def project_dict(session: Session, project: Project) -> dict[str, Any]:
    from sqlalchemy import func

    episode_count = int(
        session.scalar(select(func.count(Episode.id)).where(Episode.project_id == project.id)) or 0
    )
    return {
        "id": project.id,
        "name": project.name,
        "genre": project.genre,
        "aspect_ratio": project.aspect_ratio,
        "default_language": project.default_language,
        "default_style_id": project.default_style_id,
        "default_voice_speed": project.default_voice_speed,
        "status": project.status,
        "episode_count": episode_count,
        "total_cost_cents": cost_service.project_total(session, project.id),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def episode_dict(session: Session, episode: Episode) -> dict[str, Any]:
    from app.agents import shot as shot_agent
    from app.workflow import engine as workflow_engine

    run = workflow_engine.get_run(session, episode.id)
    # §10.1：STALE 是「受影响资源待用户决定」的提示，前端据此给出重新生成入口
    stale_shots = shot_agent.stale_shot_count(session, episode.id)
    stale_reasons = sorted(
        {
            s.stale_reason
            for s in session.scalars(
                select(Shot).where(Shot.episode_id == episode.id, Shot.stale.is_(True))
            )
            if s.stale_reason
        }
    )
    return {
        "id": episode.id,
        "project_id": episode.project_id,
        "title": episode.title,
        "episode_no": episode.episode_no,
        "target_duration_ms": episode.target_duration_ms,
        "stage": episode.stage,
        "stage_before_block": episode.stage_before_block,
        "blocked_reason": episode.blocked_reason,
        "stale_shot_count": stale_shots,
        "stale_reasons": stale_reasons,
        "version": episode.version,
        "status": episode.status,
        "total_cost_cents": cost_service.episode_total(session, episode.id),
        "workflow_status": run.status if run else None,
        "pending_gate": run.pending_gate if run else None,
        "created_at": episode.created_at,
        "updated_at": episode.updated_at,
    }


def style_dict(style: StyleAsset, *, project: Project | None = None) -> dict[str, Any]:
    # FR-022：前端需要知道哪一个是项目默认画风（此前后端不返回，界面无法标记）
    is_default = bool(
        project is not None and getattr(project, "default_style_id", None) == style.id
    )
    return {
        "id": style.id,
        "project_id": style.project_id,
        "name": style.name,
        "description": style.description,
        "palette": style.palette,
        "camera_style": style.camera_style,
        "lighting_style": style.lighting_style,
        "negative_prompt": style.negative_prompt,
        "version": style.version,
        "status": style.status,
        "is_default": is_default,
        "created_at": style.created_at,
    }


#: 角色卡 spec_json 里需要拍平到顶层的字段（前端按顶层字段读写，见 PRD FR-020）
CHARACTER_SPEC_FIELDS = (
    "identity",
    "age_range",
    "appearance",
    "outfit",
    "personality",
    "speech_style",
    "forbidden_changes",
)


def character_dict(session: Session, character: CharacterAsset) -> dict[str, Any]:
    from app.agents import character as character_agent

    spec = dict(character.spec_json or {})
    return {
        "id": character.id,
        "project_id": character.project_id,
        # 为空 = 项目级公共角色；有值 = 只属于这一集的卡
        "episode_id": character.episode_id,
        "name": character.name,
        # spec_json 同时保留（兼容旧调用方），并把字段拍平到顶层供前端读写；
        # 不拍平会导致前端读到空值、回写空串从而清空角色设定。
        # 下划线开头的是内部标记（来源/手改），不返回给前端。
        "spec_json": character_agent.public_spec(spec),
        **{field: spec.get(field) for field in CHARACTER_SPEC_FIELDS},
        # 来源与手改标记：前端据此显示「剧本抓取」角标，并提示「同步不会覆盖你的修改」
        "source": "script" if spec.get(character_agent.MARK_SOURCE_DOC) else "manual",
        "source_script_version": spec.get(character_agent.MARK_SOURCE_VERSION),
        "user_edited": bool(spec.get(character_agent.MARK_USER_EDITED)),
        "primary_media_id": character.primary_media_id,
        "primary_media_url": (
            media_service.media_url(session.get(MediaAsset, character.primary_media_id))
            if character.primary_media_id and session.get(MediaAsset, character.primary_media_id)
            else None
        ),
        "voice_id": character.voice_id,
        # FR-051：语速随角色一起暴露，前端据此渲染控件
        "voice_speed": character.voice_speed,
        "version": character.version,
        "status": character.status,
        "is_main": character.is_main,
        "consistency": character_agent.consistency_params(character),
        "created_at": character.created_at,
        "updated_at": character.updated_at,
    }


def voice_dict(voice: Voice) -> dict[str, Any]:
    return {
        "id": voice.id,
        "project_id": voice.project_id,
        "name": voice.name,
        "provider": voice.provider,
        "provider_voice_id": voice.provider_voice_id,
        "gender": voice.gender,
        "description": voice.description,
        "preview_media_id": voice.preview_media_id,
        "preview_url": (
            f"/api/v1/media/{voice.preview_media_id}/content" if voice.preview_media_id else None
        ),
    }


def approval_dict(approval: Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "episode_id": approval.episode_id,
        "stage": approval.stage,
        "target_type": approval.target_type,
        "target_id": approval.target_id,
        "decision": approval.decision,
        "actor_id": approval.actor_id,
        "asset_version": approval.asset_version,
        "candidate_media_id": approval.candidate_media_id,
        "note": approval.note,
        "payload": approval.payload,
        "created_at": approval.created_at,
    }


def record_approval(
    session: Session,
    *,
    episode_id: str,
    stage: str,
    target_type: str,
    target_id: str,
    decision: str = "APPROVE",
    asset_version: int | None = None,
    candidate_media_id: str | None = None,
    note: str = "",
    payload: dict[str, Any] | None = None,
) -> Approval:
    approval = Approval(
        episode_id=episode_id,
        stage=stage,
        target_type=target_type,
        target_id=target_id,
        decision=decision,
        asset_version=asset_version,
        candidate_media_id=candidate_media_id,
        note=note,
        payload=payload or {},
    )
    session.add(approval)
    session.flush()
    return approval


def pagination(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> tuple[int, int]:
    return page, page_size


PageDep = Annotated[tuple[int, int], Depends(pagination)]
