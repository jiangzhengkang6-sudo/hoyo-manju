"""工作区与个人空间（PRD §3.1「账号与个人工作区」/ §5.1-1 登录与工作区页）。

PRD §3.2 明确 V1 排除「多租户企业权限」，因此这里不做真实鉴权，
只把当前本地创作者的工作区画像、配额与项目级资产库暴露出来，
让前端有一个「进入工作区」的落地页，而不是直接掉进项目列表。
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import SessionDep, style_dict
from app.core.config import LOCAL_USER_ID, LOCAL_WORKSPACE_ID, settings
from app.core.errors import NotFound
from app.db.models import (
    CharacterAsset,
    Episode,
    Job,
    MediaAsset,
    Project,
    StyleAsset,
    User,
)
from app.services import cost as cost_service

router = APIRouter(tags=["workspace"])


def _get_user(session: SessionDep) -> User:
    user = session.get(User, LOCAL_USER_ID)
    if user is None:
        raise NotFound("本地用户不存在，请重启服务以重新初始化")
    return user


@router.get("/workspace")
def read_workspace(session: SessionDep) -> dict:
    """当前工作区画像 + 概览计数 + 配额快照（前端登录/工作区页的单一入口）。"""
    user = _get_user(session)
    quota = cost_service.quota_snapshot(session)

    def count(model) -> int:
        return int(session.scalar(select(func.count(model.id))) or 0)

    active_projects = int(
        session.scalar(
            select(func.count(Project.id)).where(Project.status == "ACTIVE")
        )
        or 0
    )
    archived_projects = int(
        session.scalar(
            select(func.count(Project.id)).where(Project.status == "ARCHIVED")
        )
        or 0
    )
    return {
        "user": {
            "id": user.id,
            "name": user.name,
            "workspace_id": user.workspace_id,
        },
        "workspace_id": user.workspace_id,
        "quota": quota,
        "counts": {
            "projects": count(Project),
            "active_projects": active_projects,
            "archived_projects": archived_projects,
            "episodes": count(Episode),
            "jobs": count(Job),
            "media": count(MediaAsset),
            "characters": count(CharacterAsset),
            "styles": count(StyleAsset),
        },
        # 默认值，前端建项目表单可以直接拿来当初始值
        "defaults": {
            "language": "zh-CN",
            "aspect_ratio": "9:16",
        },
        "providers": {
            "mock_only": _is_mock_only(),
            "configured": _configured_providers(),
        },
    }


@router.patch("/workspace")
def update_workspace(payload: dict, session: SessionDep) -> dict:
    """更新创作者显示名。配额调整走 `PUT /quota`（会写审计），不在这里。"""
    user = _get_user(session)
    name = str(payload.get("name") or "").strip()
    if name:
        if len(name) > 120:
            from app.core.errors import StateConflict

            raise StateConflict("显示名过长（最多 120 字）")
        user.name = name
    session.flush()
    return {"id": user.id, "name": user.name, "workspace_id": user.workspace_id}


@router.get("/workspace/assets")
def workspace_assets(
    session: SessionDep,
    kind: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
) -> dict:
    """项目级资产库（PRD §5.1-6）：跨项目汇总角色与画风，可按项目过滤。

    `kind` 取 `character` / `style`；缺省返回两类。`q` 为名称模糊匹配。
    """
    want_character = kind in (None, "", "character", "all")
    want_style = kind in (None, "", "style", "all")
    projects = {
        p.id: p.name for p in session.scalars(select(Project))
    }

    characters: list[dict] = []
    if want_character:
        stmt = select(CharacterAsset).order_by(CharacterAsset.created_at.desc())
        if project_id:
            stmt = stmt.where(CharacterAsset.project_id == project_id)
        if q:
            stmt = stmt.where(CharacterAsset.name.like(f"%{q}%"))
        for row in session.scalars(stmt):
            spec = row.spec_json or {}
            characters.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "project_id": row.project_id,
                    "project_name": projects.get(row.project_id),
                    "status": row.status,
                    "identity": spec.get("identity"),
                    "appearance": spec.get("appearance"),
                    "primary_media_id": row.primary_media_id,
                    "voice_id": getattr(row, "voice_id", None),
                    "created_at": row.created_at,
                }
            )

    styles: list[dict] = []
    if want_style:
        stmt = select(StyleAsset).order_by(StyleAsset.created_at.desc())
        if project_id:
            stmt = stmt.where(StyleAsset.project_id == project_id)
        if q:
            stmt = stmt.where(StyleAsset.name.like(f"%{q}%"))
        for row in session.scalars(stmt):
            project = session.get(Project, row.project_id)
            styles.append(style_dict(row, project=project))

    return {
        "characters": characters,
        "styles": styles,
        "projects": [
            {"id": pid, "name": name} for pid, name in sorted(projects.items())
        ],
        "total": len(characters) + len(styles),
    }


def _is_mock_only() -> bool:
    """是否一个真实供应商密钥都没配（前端据此提示「当前为演示模式」）。"""
    return not any(
        [
            settings.ark_api_key,
            settings.kling_api_key,
            settings.minimax_api_key,
            settings.openai_api_key,
        ]
    )


def _configured_providers() -> list[str]:
    names: list[str] = []
    if settings.ark_api_key:
        names.append("ark")
    if settings.kling_api_key:
        names.append("kling")
    if settings.minimax_api_key:
        names.append("minimax")
    if settings.openai_api_key:
        names.append("openai")
    return names


__all__ = ["router", "LOCAL_WORKSPACE_ID"]
