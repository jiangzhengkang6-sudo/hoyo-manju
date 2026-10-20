"""工作流服务：启动、门控暂停、恢复、重试、回退（需求文档 §6.3 / §9.3）。"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import CheckpointStale, NotFound, StateConflict
from app.core.security import FREE_TEXT_KEYS, new_id, redact_payload
from app.db.models import (
    Episode,
    EpisodeStage,
    StoryDocument,
    WorkflowNodeLog,
    WorkflowRun,
    WorkflowStatus,
)
from app.services import events
from app.workflow import fallback
from app.workflow.graph import build_graph, engine_name, graph_version
from app.workflow.state import GATE_NODES, NODE_LABELS, NODE_SEQUENCE, WorkflowState

RUNNING_STATUSES = {WorkflowStatus.RUNNING.value, WorkflowStatus.WAITING_CONFIRMATION.value}


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #

def get_run(session: Session, episode_id: str) -> WorkflowRun | None:
    return session.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.episode_id == episode_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    )


def require_run(session: Session, episode_id: str) -> WorkflowRun:
    run = get_run(session, episode_id)
    if run is None:
        raise NotFound("该剧集尚未启动工作流")
    return run


def create_run(session: Session, episode: Episode) -> WorkflowRun:
    run = WorkflowRun(
        id=new_id("wfr"),
        episode_id=episode.id,
        graph_version=graph_version(),
        status=WorkflowStatus.RUNNING.value,
        current_node="initialize",
        engine=engine_name(),
        state_json={},
    )
    session.add(run)
    session.flush()
    return run


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #

def _initial_state(session: Session, run: WorkflowRun, episode: Episode) -> WorkflowState:
    document = session.scalar(
        select(StoryDocument)
        .where(StoryDocument.episode_id == episode.id)
        .order_by(StoryDocument.version.desc())
        .limit(1)
    )
    return {
        "episode_id": episode.id,
        "project_id": episode.project_id,
        "workflow_run_id": run.id,
        "source_text": document.source_text if document else "",
        "shot_count": settings.min_shots,
        "decisions": {},
        "warnings": [],
        "errors": [],
    }


def _invoke_langgraph(
    session: Session, run: WorkflowRun, payload: Any, *, resume: bool
) -> tuple[WorkflowState, dict[str, Any] | None, str | None]:
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command

    graph = build_graph()
    config = {"configurable": {"thread_id": run.id}}
    try:
        graph.invoke(Command(resume=payload) if resume else payload, config)
    except GraphInterrupt:
        pass
    snapshot = graph.get_state(config)
    values: WorkflowState = dict(snapshot.values or {})  # type: ignore[assignment]
    checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
    gate_payload: dict[str, Any] | None = None
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            gate_payload = item.value if hasattr(item, "value") else item
    return values, gate_payload, checkpoint_id


def _invoke_fallback(
    session: Session, run: WorkflowRun, payload: Any, *, resume: bool
) -> tuple[WorkflowState, dict[str, Any] | None, str | None]:
    state: WorkflowState = dict(run.state_json or {})  # type: ignore[assignment]
    if not state:
        episode = session.get(Episode, run.episode_id)
        state = _initial_state(session, run, episode)
    if resume:
        start = fallback.node_index(run.pending_gate or "initialize")
        state, gate = fallback.run_from(state, start, resume_value=payload)
    else:
        state, gate = fallback.run_from(state, 0)
    checkpoint_id = f"fb-{int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)}"
    return state, gate, checkpoint_id


def _invoke(
    session: Session, run: WorkflowRun, payload: Any, *, resume: bool
) -> tuple[WorkflowState, dict[str, Any] | None, str | None]:
    session.commit()
    if engine_name() == "langgraph":
        try:
            return _invoke_langgraph(session, run, payload, resume=resume)
        except ImportError:
            return _invoke_fallback(session, run, payload, resume=resume)
    return _invoke_fallback(session, run, payload, resume=resume)


def _apply_result(
    session: Session,
    run: WorkflowRun,
    values: WorkflowState,
    gate: dict[str, Any] | None,
    checkpoint_id: str | None,
) -> WorkflowRun:
    session.expire_all()
    run = session.get(WorkflowRun, run.id)
    run.state_json = json.loads(json.dumps(values, ensure_ascii=False, default=str))
    run.checkpoint_id = checkpoint_id
    run.checkpoint_ref = f"{run.id}:{checkpoint_id}" if checkpoint_id else None

    if gate:
        gate_name = gate.get("gate")
        run.pending_gate = gate_name
        run.current_node = gate_name or run.current_node
        run.status = WorkflowStatus.WAITING_CONFIRMATION.value
        events.publish(
            session,
            events.WORKFLOW_GATE_WAITING,
            {
                "workflow_run_id": run.id,
                "gate": gate_name,
                "checkpoint_id": checkpoint_id,
                "payload": gate.get("payload", {}),
                "episode_id": run.episode_id,
            },
            episode_id=run.episode_id,
            project_id=values.get("project_id"),
        )
    else:
        last = values.get("last_node") or run.current_node
        run.current_node = last
        run.pending_gate = None
        if last == "completed":
            run.status = WorkflowStatus.COMPLETED.value
        elif run.status not in {WorkflowStatus.FAILED.value, WorkflowStatus.BLOCKED.value}:
            run.status = WorkflowStatus.RUNNING.value
        events.publish(
            session,
            events.WORKFLOW_NODE_CHANGED,
            {"workflow_run_id": run.id, "node": last, "status": run.status,
             "episode_id": run.episode_id},
            episode_id=run.episode_id,
            project_id=values.get("project_id"),
        )
    session.flush()
    return run


def start(
    session: Session,
    episode: Episode,
    *,
    source_text: str = "",
    shot_count: int | None = None,
) -> WorkflowRun:
    run = create_run(session, episode)
    state = _initial_state(session, run, episode)
    if source_text:
        state["source_text"] = source_text
    if shot_count:
        state["shot_count"] = shot_count
    values, gate, checkpoint_id = _invoke(session, run, state, resume=False)
    return _apply_result(session, run, values, gate, checkpoint_id)


def restore_stage_after_unblock(session: Session, episode: Episode) -> bool:
    """§10.1：解除阻塞后回到原阶段。

    进入 `BLOCKED` 时会把当时的阶段记在 `stage_before_block`；这里在用户
    重新推进流程（恢复门控 / 重试节点）时把它恢复回去，并清空阻塞信息。
    返回是否真的发生了恢复。
    """
    if episode is None or episode.stage != EpisodeStage.BLOCKED.value:
        return False
    target = (episode.stage_before_block or "").strip()
    # 没记录或记录本身是 BLOCKED 时无法推断，退回 DRAFT 让用户从头推进
    if not target or target == EpisodeStage.BLOCKED.value:
        target = EpisodeStage.DRAFT.value
    episode.stage = target
    episode.stage_before_block = ""
    episode.blocked_reason = ""
    session.flush()
    return True


def resume(
    session: Session,
    run: WorkflowRun,
    *,
    gate: str,
    decision: str,
    payload: dict[str, Any] | None = None,
    checkpoint_id: str | None = None,
    target_version: int | None = None,
) -> WorkflowRun:
    if run.status == WorkflowStatus.COMPLETED.value:
        raise StateConflict("工作流已完成，无法继续恢复")
    if run.pending_gate and gate != run.pending_gate:
        raise StateConflict(
            f"门控不匹配：当前等待 {run.pending_gate}，收到 {gate}",
            details={"pending_gate": run.pending_gate},
        )
    if run.pending_gate is None and run.status == WorkflowStatus.RUNNING.value:
        raise StateConflict("工作流未处于门控等待状态")

    if checkpoint_id and run.checkpoint_id and checkpoint_id != run.checkpoint_id:
        raise CheckpointStale(
            "checkpoint 已过期：请刷新后基于最新状态重新确认",
            details={"expected": run.checkpoint_id, "received": checkpoint_id},
        )

    if target_version is not None:
        latest = session.scalar(
            select(StoryDocument.version)
            .where(StoryDocument.episode_id == run.episode_id)
            .order_by(StoryDocument.version.desc())
            .limit(1)
        )
        if latest is not None and target_version != latest:
            raise CheckpointStale(
                "输入版本已变化，拒绝基于旧 checkpoint 恢复；请重新计算影响范围",
                details={"target_version": target_version, "current_version": latest},
            )

    value = {"decision": decision, "payload": payload or {}, "gate": gate}
    values, next_gate, new_checkpoint = _invoke(session, run, value, resume=True)
    result = _apply_result(session, run, values, next_gate, new_checkpoint)
    # §10.1：阻塞解除后回到原阶段（节点执行成功才恢复，失败时仍保持 BLOCKED）
    episode = session.get(Episode, run.episode_id)
    if episode is not None and run.status != WorkflowStatus.BLOCKED.value:
        restore_stage_after_unblock(session, episode)
    return result


def retry_node(session: Session, run: WorkflowRun, node: str, payload: dict[str, Any] | None = None) -> WorkflowRun:
    """重跑失败节点：把工作流回退到该节点重新执行。"""
    if node not in NODE_SEQUENCE:
        raise NotFound(f"未知节点：{node}")
    session.add(
        WorkflowNodeLog(
            workflow_run_id=run.id,
            episode_id=run.episode_id,
            node=node,
            status="RETRY",
            message="用户手动重试节点",
            # FR-093 / §11.1：运行日志 detail 入库前脱敏
            detail=redact_payload(payload or {}, truncate_keys=FREE_TEXT_KEYS),
        )
    )
    run.status = WorkflowStatus.RUNNING.value
    run.pending_gate = None
    session.flush()

    state: WorkflowState = dict(run.state_json or {})  # type: ignore[assignment]
    index = fallback.node_index(node)
    values, gate = fallback.run_from(state, index)
    result = _apply_result(session, run, values, gate, f"retry-{index}")
    # §10.1：手动重试也是「解除阻塞」的一种，同样回到原阶段
    episode = session.get(Episode, run.episode_id)
    if episode is not None and run.status != WorkflowStatus.BLOCKED.value:
        restore_stage_after_unblock(session, episode)
    return result


def rollback(session: Session, run: WorkflowRun, to_node: str) -> WorkflowRun:
    if to_node not in NODE_SEQUENCE:
        raise NotFound(f"未知节点：{to_node}")
    index = fallback.node_index(to_node)
    state: WorkflowState = dict(run.state_json or {})  # type: ignore[assignment]
    state["decisions"] = {
        k: v for k, v in (state.get("decisions") or {}).items()
        if fallback.node_index(k) < index
    }
    run.status = WorkflowStatus.RUNNING.value
    run.pending_gate = None
    run.state_json = state
    session.flush()
    values, gate = fallback.run_from(state, index)
    result = _apply_result(session, run, values, gate, f"rollback-{index}")
    # §10.1：回滚同样是「解除阻塞」的一种，同样回到原阶段
    episode = session.get(Episode, run.episode_id)
    if episode is not None and run.status != WorkflowStatus.BLOCKED.value:
        restore_stage_after_unblock(session, episode)
    return result


# --------------------------------------------------------------------------- #
# 视图
# --------------------------------------------------------------------------- #

def snapshot(session: Session, run: WorkflowRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "episode_id": run.episode_id,
        "graph_version": run.graph_version,
        "engine": run.engine,
        "status": run.status,
        "current_node": run.current_node,
        "pending_gate": run.pending_gate,
        "checkpoint_id": run.checkpoint_id,
        "checkpoint_ref": run.checkpoint_ref,
        "state": run.state_json,
        "error_message": run.error_message,
        "updated_at": run.updated_at,
    }


def graph_overview(session: Session, run: WorkflowRun | None) -> list[dict[str, Any]]:
    done_until = -1
    if run is not None and run.current_node in NODE_SEQUENCE:
        done_until = NODE_SEQUENCE.index(run.current_node)
    nodes = []
    for idx, name in enumerate(NODE_SEQUENCE):
        if run is None:
            status = "PENDING"
        elif run.status == WorkflowStatus.COMPLETED.value:
            status = "DONE"
        elif idx < done_until:
            status = "DONE"
        elif idx == done_until:
            status = "WAITING" if run.pending_gate == name else "RUNNING"
        else:
            status = "PENDING"
        nodes.append(
            {
                "node": name,
                "label": NODE_LABELS.get(name, name),
                "is_gate": name in GATE_NODES,
                "status": status,
                "index": idx,
            }
        )
    return nodes


def node_logs(session: Session, run: WorkflowRun, limit: int = 200) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(WorkflowNodeLog)
        .where(WorkflowNodeLog.workflow_run_id == run.id)
        .order_by(WorkflowNodeLog.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": r.id,
            "node": r.node,
            "status": r.status,
            "duration_ms": r.duration_ms,
            "message": r.message,
            "detail": r.detail,
            "created_at": r.created_at,
        }
        for r in rows
    ]
