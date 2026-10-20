"""内置降级工作流引擎（未安装 langgraph 时使用）。

与 LangGraph 版本保持同样的节点序列、门控中断与状态持久化语义。
"""

from __future__ import annotations

from typing import Any

from app.workflow import nodes as node_module
from app.workflow.state import NODE_SEQUENCE, WorkflowState

_state = {"pending": None, "resume": None, "armed": False}


class GateInterrupt(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("gate", "gate"))
        self.payload = payload


def _interrupt(payload: dict[str, Any]) -> Any:
    if _state["resume"] is not None:
        value = _state["resume"]
        _state["resume"] = None
        return value
    _state["pending"] = payload
    raise GateInterrupt(payload)


def install() -> None:
    if not _state["armed"]:
        node_module.interrupt = _interrupt  # type: ignore[assignment]
        _state["armed"] = True


def _merge(state: WorkflowState, update: dict[str, Any] | None) -> WorkflowState:
    if not update:
        return state
    merged = dict(state)
    for key, value in update.items():
        if key in {"errors", "warnings"} and isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = [*merged[key], *value]
        else:
            merged[key] = value
    return merged  # type: ignore[return-value]


def run_from(
    state: WorkflowState,
    start_index: int,
    *,
    resume_value: Any = None,
) -> tuple[WorkflowState, dict[str, Any] | None]:
    """从指定节点开始执行，遇到门控中断即返回。"""
    install()
    _state["pending"] = None
    _state["resume"] = resume_value
    current = dict(state)
    for name in NODE_SEQUENCE[start_index:]:
        fn = getattr(node_module, f"node_{name}")
        try:
            update = fn(current)  # type: ignore[arg-type]
        except GateInterrupt as exc:
            return current, exc.payload
        current = _merge(current, update)
    return current, None


def node_index(name: str) -> int:
    return NODE_SEQUENCE.index(name)
