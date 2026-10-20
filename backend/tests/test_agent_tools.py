"""§6.6 智能体工具调用边界：白名单必须完整，越权必须失败。"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from app.agents import tools as agent_tools

AGENTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "agents"

#: 允许出现在 agent 源码里的 SQLAlchemy 语句构造器（只读/写入结构化结果）
_ALLOWED_SQL_VERBS = {"select", "insert", "update", "add", "flush", "delete"}


def test_all_seven_capabilities_are_declared() -> None:
    """PRD §6.6 原文列了七类能力，一类都不能少。"""
    assert set(agent_tools.ALLOWED_TOOLS) == {
        "query_project_context",
        "read_asset",
        "create_generation_job",
        "get_job_status",
        "save_structured_result",
        "request_safety_check",
        "generate_preview",
    }


def test_every_capability_has_a_real_implementation() -> None:
    """白名单不能只是文档——每一项都要能解析到真实函数。"""
    for spec in agent_tools.ALLOWED_TOOLS.values():
        assert spec.implementations, f"{spec.name} 没有实现落点"
        for path in spec.implementations:
            assert callable(agent_tools.resolve(path)), f"{path} 不可调用"


def test_assert_tool_allowed_passes_for_whitelisted() -> None:
    spec = agent_tools.assert_tool_allowed(agent_tools.TOOL_SAFETY_CHECK)
    assert spec.read_only is True


def test_assert_tool_allowed_rejects_unknown() -> None:
    with pytest.raises(agent_tools.ToolNotAllowed) as excinfo:
        agent_tools.assert_tool_allowed("run_arbitrary_sql")
    err = excinfo.value
    assert err.status_code == 403
    assert err.code == "TOOL_NOT_ALLOWED"
    assert "run_arbitrary_sql" in err.details["requested"] or err.details["requested"]
    assert "delete_asset" in err.details["forbidden"]


def test_forbidden_list_matches_prd() -> None:
    assert set(agent_tools.FORBIDDEN_TOOLS) == {
        "run_arbitrary_sql",
        "delete_asset",
        "adjust_quota",
        "change_permissions",
    }


def test_read_only_tools_are_declared_read_only() -> None:
    """纯读取的能力必须标成只读，避免实现里偷偷写库。"""
    for name in (
        agent_tools.TOOL_PROJECT_CONTEXT,
        agent_tools.TOOL_READ_ASSET,
        agent_tools.TOOL_JOB_STATUS,
        agent_tools.TOOL_SAFETY_CHECK,
    ):
        assert agent_tools.ALLOWED_TOOLS[name].read_only is True


def test_describe_exposes_labels_in_chinese() -> None:
    body = agent_tools.describe()
    labels = [item["label"] for item in body["allowed"]]
    assert "查询项目上下文" in labels
    assert "请求安全检查" in labels
    assert "生成预览" in labels
    assert len(body["forbidden"]) == 4


def test_agent_endpoint_exposes_whitelist(client: TestClient) -> None:
    body = client.get("/api/v1/agent/tools").json()
    assert len(body["allowed"]) == 7
    assert len(body["forbidden"]) == 4


# --------------------------------------------------------------------------- #
# 静态扫描：确认 agent 源码里没有越权操作
# --------------------------------------------------------------------------- #

def _agent_sources() -> list[tuple[str, str]]:
    out = []
    for path in sorted(AGENTS_DIR.glob("*.py")):
        if path.name in {"__init__.py", "tools.py"}:
            continue
        out.append((path.name, path.read_text(encoding="utf-8")))
    return out


def test_agents_never_execute_raw_sql() -> None:
    """禁止 `session.execute(text(...))` / `executescript` / 裸字符串 SQL。

    注意用「双词」模式匹配：单看 `TRUNCATE` 会误伤 Python 里叫 `_TRUNCATE`
    的常量/函数（本项目 `story.py` 就有一个）。
    """
    banned_patterns = (
        r"DROP\s+TABLE",
        r"DELETE\s+FROM",
        r"ALTER\s+TABLE",
        r"TRUNCATE\s+TABLE",
        r"executescript",
    )
    for name, source in _agent_sources():
        for pattern in banned_patterns:
            assert not re.search(pattern, source, re.IGNORECASE), (
                f"{name} 出现疑似裸 SQL：{pattern}"
            )
        assert "sqlalchemy.text(" not in source, f"{name} 使用 text() 执行裸 SQL"


def test_agents_never_delete_media_assets() -> None:
    """禁止在 agent 里删除素材（可以标记，不可以删除）。"""
    for name, source in _agent_sources():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                attr = getattr(func, "attr", None)
                # session.delete(asset) / os.remove(...) 之类
                if attr == "delete":
                    args = [getattr(a, "id", "") for a in node.args]
                    assert not any("asset" in str(a).lower() for a in args), (
                        f"{name} 直接删除了素材"
                    )
                if attr in {"remove", "unlink", "rmtree"}:
                    raise AssertionError(f"{name} 出现文件删除调用：{attr}")


def test_agents_never_touch_quota_or_permissions() -> None:
    """禁止 agent 改配额或用户权限。"""
    banned_attrs = {"set_quota", "update_quota", "adjust_quota", "grant", "revoke"}
    for name, source in _agent_sources():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in banned_attrs, f"{name} 触碰了 {node.attr}"


def test_agents_do_not_write_quota_table_directly() -> None:
    """配额只能通过 cost_service 走预留/结算，agent 不该直接改 Quota 行。"""
    for name, source in _agent_sources():
        assert "Quota(" not in source, f"{name} 直接构造了配额对象"
        assert ".quota_cents" not in source, f"{name} 直接改了配额字段"
