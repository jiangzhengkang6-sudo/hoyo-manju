"""工作流启动、门控恢复、重试与回退。"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import SessionDep, get_episode, record_approval
from app.core.errors import NotFound
from app.db.models import Episode, StoryDocument, WorkflowStatus
from app.schemas import WorkflowResume, WorkflowRetry, WorkflowRollback, WorkflowStart
from app.workflow import engine as workflow_engine
from app.workflow.state import GATE_NODES, NODE_LABELS, NODE_SEQUENCE

router = APIRouter(tags=["workflow"])


@router.post("/episodes/{episode_id}/workflow/start")
def start_workflow(episode_id: str, payload: WorkflowStart, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    existing = workflow_engine.get_run(session, episode.id)
    if existing and existing.status in {
        WorkflowStatus.RUNNING.value,
        WorkflowStatus.WAITING_CONFIRMATION.value,
    }:
        return workflow_engine.snapshot(session, existing)

    document = session.scalar(
        select(StoryDocument)
        .where(StoryDocument.episode_id == episode.id)
        .order_by(StoryDocument.version.desc())
        .limit(1)
    )
    run = workflow_engine.start(
        session,
        episode,
        source_text=document.source_text if document else "",
    )
    session.flush()
    return workflow_engine.snapshot(session, run)


@router.get("/episodes/{episode_id}/workflow")
def read_workflow(episode_id: str, session: SessionDep) -> dict | None:
    """当前 WorkflowRun；尚未启动时返回 null（契约见 docs/API.md §9）。

    节点清单请走 `/workflow/graph`。
    """
    episode = get_episode(session, episode_id=episode_id)
    run = workflow_engine.get_run(session, episode.id)
    if run is None:
        return None
    return workflow_engine.snapshot(session, run)


@router.post("/episodes/{episode_id}/workflow/resume")
def resume_workflow(episode_id: str, payload: WorkflowResume, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    run = workflow_engine.require_run(session, episode.id)
    if payload.workflow_run_id != run.id:
        raise NotFound(f"工作流实例不匹配：{payload.workflow_run_id}")
    # §12-8「输出内容被拦截 → 禁止确认和导出」：确认动作走的就是门控审批，
    # 所以闸门必须落在 approve 上，否则用户能一路确认到导出阶段才被拒。
    if str(payload.decision or "approve").lower() in {"approve", "approved"}:
        from app.agents import safety as safety_agent

        safety_agent.assert_export_allowed(session, episode)
    # 人工门控决策留痕：每个 gate 一条审批记录，便于审计与回溯（需求 §12.3）
    record_approval(
        session,
        episode_id=episode.id,
        stage=payload.gate,
        target_type="workflow_gate",
        target_id=run.id,
        decision=str(payload.decision or "approve").upper(),
        note=f"人工门控：{payload.gate}",
        payload={
            "checkpoint_id": payload.checkpoint_id,
            "target_version": payload.target_version,
            "payload": payload.payload or {},
        },
    )
    run = workflow_engine.resume(
        session,
        run,
        gate=payload.gate,
        decision=payload.decision,
        payload=payload.payload,
        checkpoint_id=payload.checkpoint_id,
        target_version=payload.target_version,
    )
    session.flush()
    return workflow_engine.snapshot(session, run)


@router.post("/episodes/{episode_id}/workflow/retry")
def retry_node(episode_id: str, payload: WorkflowRetry, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    run = workflow_engine.require_run(session, episode.id)
    run = workflow_engine.retry_node(session, run, payload.node, payload.payload)
    session.flush()
    return workflow_engine.snapshot(session, run)


@router.post("/episodes/{episode_id}/workflow/rollback")
def rollback_workflow(episode_id: str, payload: WorkflowRollback, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    run = workflow_engine.require_run(session, episode.id)
    run = workflow_engine.rollback(session, run, payload.to_node)
    session.flush()
    return workflow_engine.snapshot(session, run)


@router.get("/episodes/{episode_id}/workflow/logs")
def workflow_logs(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    run = workflow_engine.get_run(session, episode.id)
    if run is None:
        return {"items": []}
    return {"items": workflow_engine.node_logs(session, run)}


@router.get("/episodes/{episode_id}/workflow/graph")
def workflow_graph(episode_id: str, session: SessionDep) -> dict:
    episode = get_episode(session, episode_id=episode_id)
    run = workflow_engine.get_run(session, episode.id)
    return {
        "nodes": workflow_engine.graph_overview(session, run),
        "sequence": NODE_SEQUENCE,
        "gates": GATE_NODES,
        "labels": NODE_LABELS,
    }
