"""LangGraph 主图组装与 checkpointer（需求文档 §6.2 / §6.3 / §9.3）。

若环境未安装 langgraph，会自动降级到内置的等价状态机引擎（``fallback``），
节点函数、门控语义与状态持久化保持一致。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.workflow import nodes as node_module
from app.workflow.state import GRAPH_VERSION, NODE_SEQUENCE, WorkflowState

_conn: sqlite3.Connection | None = None
_graph: Any = None
_lock = threading.Lock()


def langgraph_available() -> bool:
    try:
        import langgraph  # noqa: F401
        from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: F401
        from langgraph.graph import StateGraph  # noqa: F401
    except Exception:
        return False
    return True


def _checkpointer() -> Any:
    global _conn
    from langgraph.checkpoint.sqlite import SqliteSaver

    if _conn is None:
        path = Path(settings.checkpoint_db)
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=30000")
    saver = SqliteSaver(_conn)
    try:
        saver.setup()
    except Exception:
        pass
    return saver


def build_graph() -> Any:
    global _graph
    with _lock:
        if _graph is not None:
            return _graph
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(WorkflowState)
        for name in NODE_SEQUENCE:
            graph.add_node(name, getattr(node_module, f"node_{name}"))
        graph.add_edge(START, NODE_SEQUENCE[0])
        for current, nxt in zip(NODE_SEQUENCE, NODE_SEQUENCE[1:]):
            graph.add_edge(current, nxt)
        graph.add_edge(NODE_SEQUENCE[-1], END)
        _graph = graph.compile(checkpointer=_checkpointer())
        return _graph


def engine_name() -> str:
    if settings.enable_langgraph and langgraph_available():
        return "langgraph"
    return "fallback"


def reset_graph() -> None:
    """测试用：重置编译缓存。"""
    global _graph, _conn
    with _lock:
        _graph = None
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None


def graph_version() -> str:
    return GRAPH_VERSION
