"""成本、配额、供应商设置、价格表与安全检查。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import SessionDep
from app.core.config import LOCAL_USER_ID, settings
from app.core.errors import NotFound
from app.core.security import (
    encrypt_secret,
    mask_secret,
)
from app.db.models import PriceBook, ProviderSetting, User
from app.providers.http_providers import PRICE_TABLE
from app.providers.registry import describe_all
from app.schemas import AppealResolveIn, ProvidersUpdateIn, QuotaAdjustIn, SafetyCheckIn
from app.services import audit as audit_service
from app.services import cost as cost_service
from app.services import safety as safety_service
from app.services import uploads

router = APIRouter(tags=["admin"])

PROVIDER_KINDS = ["text", "image", "video", "speech"]


@router.get("/cost/ledger")
def ledger(
    session: SessionDep,
    episode_id: str | None = None,
    project_id: str | None = None,
    kind: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> dict:
    return cost_service.ledger_page(
        session,
        episode_id=episode_id,
        project_id=project_id,
        kind=kind,
        page=page,
        page_size=page_size,
    )


@router.get("/cost/summary")
def cost_summary(
    session: SessionDep,
    episode_id: str | None = None,
    project_id: str | None = None,
) -> dict:
    return cost_service.summary(session, episode_id=episode_id, project_id=project_id)


@router.get("/episodes/{episode_id}/cost")
def episode_cost(episode_id: str, session: SessionDep) -> dict:
    return cost_service.summary(session, episode_id=episode_id)


@router.get("/projects/{project_id}/cost")
def project_cost(project_id: str, session: SessionDep) -> dict:
    return cost_service.summary(session, project_id=project_id)


@router.get("/quota")
def quota(session: SessionDep) -> dict:
    snapshot = cost_service.quota_snapshot(session)
    # §11.3：存储配额与成本配额一并回传，前端「配额」卡片一次拿全
    snapshot["storage"] = uploads.upload_quota_snapshot(session)
    return snapshot


@router.put("/quota")
def adjust_quota(payload: QuotaAdjustIn, session: SessionDep) -> dict:
    """§11.1-3：配额调整必须写入审计日志。

    配额上限的**生效值**仍是 `settings.quota_limit_cents`（成本模块的唯一口径），
    这里同步落到 `User.quota_limit_cents` 作为账号侧持久记录，并写审计。
    """
    before_cents = int(settings.quota_limit_cents)
    after_cents = int(payload.limit_cents)
    settings.quota_limit_cents = after_cents

    user = session.get(User, LOCAL_USER_ID)
    if user is None:
        user = User(id=LOCAL_USER_ID, name="本地创作者", quota_limit_cents=after_cents)
        session.add(user)
    else:
        user.quota_limit_cents = after_cents

    audit_service.record(
        session,
        action=audit_service.ACTION_QUOTA_ADJUST,
        target_type=audit_service.TARGET_QUOTA,
        target_id=LOCAL_USER_ID,
        detail={
            "before_cents": before_cents,
            "after_cents": after_cents,
            "delta_cents": after_cents - before_cents,
            "note": payload.note,
        },
    )
    session.flush()
    snapshot = cost_service.quota_snapshot(session)
    snapshot["storage"] = uploads.upload_quota_snapshot(session)
    return snapshot


@router.get("/settings/providers")
def read_providers(session: SessionDep) -> dict:
    rows = {r.kind: r for r in session.scalars(select(ProviderSetting))}
    items = []
    for kind in PROVIDER_KINDS:
        row = rows.get(kind)
        items.append(
            {
                "kind": kind,
                "provider": row.provider if row else getattr(settings, f"{kind}_provider"),
                "model": row.model if row else "",
                "base_url": row.base_url if row else "",
                "configured": bool(row and row.api_key_encrypted),
                "masked": row.api_key_masked if row else "",
                "is_primary": row.is_primary if row else True,
                # FR-090 备用供应商（密钥同样只回传掩码）
                "fallback_provider": row.fallback_provider if row else "",
                "fallback_model": row.fallback_model if row else "",
                "fallback_base_url": row.fallback_base_url if row else "",
                "fallback_configured": bool(row and row.fallback_api_key_encrypted),
                "fallback_masked": row.fallback_api_key_masked if row else "",
            }
        )
    return {"items": items, "available": describe_all()}


@router.put("/settings/providers")
def update_providers(payload: ProvidersUpdateIn, session: SessionDep) -> dict:
    updated = []
    for kind in PROVIDER_KINDS:
        config = getattr(payload, kind)
        if config is None:
            continue
        row = session.scalar(select(ProviderSetting).where(ProviderSetting.kind == kind))
        if row is None:
            row = ProviderSetting(kind=kind)
            session.add(row)
        # §11.1-3：先记录变更前的取值，用于生成「哪些字段变了」的审计 detail
        before = {
            "provider": row.provider or "",
            "model": row.model or "",
            "base_url": row.base_url or "",
            "fallback_provider": row.fallback_provider or "",
            "fallback_model": row.fallback_model or "",
            "fallback_base_url": row.fallback_base_url or "",
        }
        row.provider = config.provider
        if config.model:
            row.model = config.model
        if config.base_url:
            row.base_url = config.base_url
        if config.api_key:
            row.api_key_encrypted = encrypt_secret(config.api_key)
            row.api_key_masked = mask_secret(config.api_key)
        row.is_primary = True
        # FR-090 备用供应商：provider 留空即表示清除备用配置
        row.fallback_provider = config.fallback_provider
        if config.fallback_model:
            row.fallback_model = config.fallback_model
        if config.fallback_base_url:
            row.fallback_base_url = config.fallback_base_url
        if config.fallback_api_key:
            row.fallback_api_key_encrypted = encrypt_secret(config.fallback_api_key)
            row.fallback_api_key_masked = mask_secret(config.fallback_api_key)
        if not config.fallback_provider:
            row.fallback_model = ""
            row.fallback_base_url = ""
            row.fallback_api_key_encrypted = ""
            row.fallback_api_key_masked = ""
        updated.append(kind)
        after = {
            "provider": row.provider or "",
            "model": row.model or "",
            "base_url": row.base_url or "",
            "fallback_provider": row.fallback_provider or "",
            "fallback_model": row.fallback_model or "",
            "fallback_base_url": row.fallback_base_url or "",
        }
        changed_fields = sorted(k for k, v in after.items() if before.get(k) != v)
        if config.api_key:
            changed_fields.append("api_key")
        if config.fallback_api_key:
            changed_fields.append("fallback_api_key")
        # §11.1-3：供应商变更留痕。detail 只写字段名与布尔标记，
        # 明文密钥在源头就不写进来（FR-093 脱敏只是兜底）。
        audit_service.record(
            session,
            action=audit_service.ACTION_PROVIDER_CHANGE,
            target_type=audit_service.TARGET_PROVIDER,
            target_id=row.id,
            detail={
                "kind": kind,
                "provider": config.provider,
                "changed_fields": changed_fields,
                "before": before,
                "after": after,
                "api_key_updated": bool(config.api_key),
                "fallback_provider": config.fallback_provider,
                "fallback_api_key_updated": bool(config.fallback_api_key),
            },
        )
    session.flush()
    return read_providers(session)


@router.get("/settings/pricing")
def pricing(session: SessionDep) -> dict:
    rows = list(session.scalars(select(PriceBook)))
    items = [
        {
            "id": r.id,
            "provider": r.provider,
            "kind": r.kind,
            "model": r.model,
            "price_version": r.price_version,
            "billing_unit": r.billing_unit,
            "unit_price_cents": r.unit_price_cents,
            "currency": r.currency,
            "note": r.note,
        }
        for r in rows
    ]
    if not items:
        items = [
            {
                "provider": key.split(":")[0],
                "model": key.split(":", 1)[1],
                "billing_unit": unit,
                "unit_price_cents": price,
                "price_version": "v1",
                "currency": "CNY",
                "kind": "builtin",
                "note": "内置价格表（仅估算用）",
            }
            for key, table in PRICE_TABLE.items()
            for unit, price in table.items()
        ]
    return {"items": items, "price_version": "v1", "currency": "CNY"}


@router.post("/safety/check")
def safety_check(payload: SafetyCheckIn) -> dict:
    return safety_service.check_text(payload.text, payload.scope)


@router.get("/appeals")
def list_all_appeals(
    session: SessionDep,
    status: str | None = None,
    episode_id: str | None = None,
    limit: int = Query(100, le=500),
) -> dict:
    """§12-8 人工复核队列：按状态/剧集筛选待处理申诉。"""
    from app.agents import safety as safety_agent
    from app.db.models import SafetyAppeal

    stmt = select(SafetyAppeal).order_by(SafetyAppeal.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(SafetyAppeal.status == status.upper())
    if episode_id:
        stmt = stmt.where(SafetyAppeal.episode_id == episode_id)
    rows = list(session.scalars(stmt))
    return {"items": [safety_agent.appeal_to_dict(a) for a in rows]}


@router.post("/appeals/{appeal_id}/resolve")
def resolve_appeal(appeal_id: str, payload: AppealResolveIn, session: SessionDep) -> dict:
    """§12-8：人工复核结论。只有 `APPROVED` 会解除对应素材的导出阻断。"""
    from app.agents import safety as safety_agent
    from app.db.models import SafetyAppeal
    from app.services import audit as audit_service

    appeal = session.get(SafetyAppeal, appeal_id)
    if appeal is None:
        raise NotFound(f"申诉不存在：{appeal_id}")
    safety_agent.resolve_appeal(
        session,
        appeal=appeal,
        status=payload.status,
        resolution_note=payload.resolution_note,
    )
    # 申诉结论会改变「能否导出」，属于必须留痕的治理动作
    audit_service.record(
        session,
        action="SAFETY_APPEAL_RESOLVE",
        target_type="safety_appeal",
        target_id=appeal.id,
        project_id=appeal.project_id,
        episode_id=appeal.episode_id,
        detail={
            "status": payload.status,
            "target_type": appeal.target_type,
            "target_id": appeal.target_id,
            "note": payload.resolution_note,
        },
    )
    session.flush()
    return safety_agent.appeal_to_dict(appeal)


@router.get("/audit")
@router.get("/audit-logs")
def audit_logs(
    session: SessionDep,
    project_id: str | None = None,
    episode_id: str | None = None,
    target_type: str | None = None,
    action: str | None = None,
    limit: int = Query(100, le=500),
) -> dict:
    """§11.1-3 审计流水查询：在既有 `/audit` 上扩展筛选维度，不新增重复实现。

    `GET /audit-logs` 是同一处理函数的别名路由（PRD §11.1-3 的对外口径）；
    响应结构保持原样（`items` + 原有字段），只是新增可选过滤条件。
    """
    items = audit_service.history(
        session,
        project_id=project_id,
        episode_id=episode_id,
        target_type=target_type,
        action=action,
        limit=limit,
    )
    return {
        "items": [
            {
                "id": r["id"],
                "action": r["action"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "actor_id": "local-user",
                "project_id": r["project_id"],
                "episode_id": r["episode_id"],
                "detail": r["detail"],
                "created_at": r["created_at"],
            }
            for r in items
        ]
    }


@router.get("/stats/workspace")
def workspace_stats(session: SessionDep) -> dict:
    from sqlalchemy import func

    from app.db.models import Episode, Job, MediaAsset, Project

    return {
        "projects": int(session.scalar(select(func.count(Project.id))) or 0),
        "episodes": int(session.scalar(select(func.count(Episode.id))) or 0),
        "jobs": int(session.scalar(select(func.count(Job.id))) or 0),
        "media": int(session.scalar(select(func.count(MediaAsset.id))) or 0),
        "quota": cost_service.quota_snapshot(session),
    }


# --------------------------------------------------------------------------- #
# §11.2-2 备份
# --------------------------------------------------------------------------- #

@router.get("/backups")
def list_backups() -> dict:
    from app.services import backup as backup_service

    return {
        "items": backup_service.list_backups(),
        "dir": str(settings.backup_dir),
        "keep": settings.backup_keep,
        "enabled": settings.backup_enabled,
    }


@router.post("/backups")
def create_backup() -> dict:
    """手动触发一次备份（每日自动备份见 `app/worker/scheduler.py`）。"""
    from app.services import backup as backup_service

    return backup_service.run_backup(reason="manual")


@router.post("/backups/restore")
def restore_backup(payload: dict) -> dict:
    """从备份恢复。默认拒绝在服务运行中执行——详见 `backup.restore` 的说明。"""
    from app.services import backup as backup_service

    source = str(payload.get("path") or "")
    if not source:
        raise NotFound("缺少 path 参数：请传备份文件的绝对路径")
    return backup_service.restore(source, force=bool(payload.get("force")))


# --------------------------------------------------------------------------- #
# §6.6 智能体工具白名单
# --------------------------------------------------------------------------- #

@router.get("/agent/tools")
def agent_tools() -> dict:
    """§6.6：智能体可调用的能力白名单与明确禁止的能力。"""
    from app.agents import tools as agent_tools_service

    return agent_tools_service.describe()


# --------------------------------------------------------------------------- #
# §12 降级状态
# --------------------------------------------------------------------------- #
@router.get("/degraded")
def degraded_state() -> dict:
    """§12：查看当前被熔断的依赖（视频/图像/语音/文本/对象存储）。"""
    from app.services import degraded as degraded_service

    return degraded_service.snapshot()


@router.post("/degraded/{kind}/resume")
def resume_degraded(kind: str, session: SessionDep) -> dict:
    """手动解除某类依赖的暂停（例如供应商已恢复，不必等 TTL）。"""
    from app.services import degraded as degraded_service

    if kind not in degraded_service.KIND_LABELS:
        raise NotFound(
            f"未知的依赖类型：{kind}",
            details={"available": sorted(degraded_service.KIND_LABELS)},
        )
    was = degraded_service.state(kind)
    degraded_service.resume(kind)
    audit_service.record(
        session,
        action="DEGRADED_RESUME",
        target_type="provider",
        target_id=kind,
        detail={"kind": kind, "was_paused": was is not None},
    )
    session.flush()
    return {"kind": kind, "resumed": was is not None, "state": degraded_service.snapshot()}
