"""§6.6 智能体工具调用边界（白名单）。

PRD §6.6 规定智能体可调用的工具必须是白名单，且明确列出七类能力：

> 查询项目上下文、读取资产、创建生成任务、获取任务状态、保存结构化结果、
> 请求安全检查、生成预览。

并规定智能体**不能**：直接执行任意 SQL、删除素材、修改配额、改变用户权限。

本项目的智能体是硬编码的 Python 函数（不是 LLM tool-calling），所以「白名单」
在这里的落地方式是：把七类能力显式声明成一张表，逐项指向真实的函数，
再用 `assert_tool_allowed()` 作为唯一的守卫入口；任何不在表里的能力一律拒绝。
`tests/test_agent_tools.py` 还会静态扫描 agent 源码，确认没有越权操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.errors import AppError

# --------------------------------------------------------------------------- #
# 七类允许的能力（PRD §6.6 原文顺序）
# --------------------------------------------------------------------------- #

TOOL_PROJECT_CONTEXT = "query_project_context"
TOOL_READ_ASSET = "read_asset"
TOOL_CREATE_JOB = "create_generation_job"
TOOL_JOB_STATUS = "get_job_status"
TOOL_SAVE_RESULT = "save_structured_result"
TOOL_SAFETY_CHECK = "request_safety_check"
TOOL_PREVIEW = "generate_preview"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    label: str
    description: str
    #: 该能力在代码里的落点（模块.函数 形式，便于人工核对与测试校验）
    implementations: tuple[str, ...] = field(default_factory=tuple)
    #: 是否只读。只读能力不得产生副作用。
    read_only: bool = False


ALLOWED_TOOLS: dict[str, ToolSpec] = {
    TOOL_PROJECT_CONTEXT: ToolSpec(
        name=TOOL_PROJECT_CONTEXT,
        label="查询项目上下文",
        description="读取项目/剧集/画风等上下文，用于构造提示词与校验前提。",
        implementations=(
            "app.agents.character.project_style",
            "app.agents.edit.require_episode",
            "app.agents.edit.get_style",
        ),
        read_only=True,
    ),
    TOOL_READ_ASSET: ToolSpec(
        name=TOOL_READ_ASSET,
        label="读取资产",
        description="读取角色参考图、分镜候选图、音轨、字幕等既有素材。",
        implementations=(
            "app.agents.character.list_assets",
            "app.agents.image.candidates",
            "app.agents.image.usable_media_ids",
            "app.agents.audio.list_tracks",
            "app.agents.video.video_history",
        ),
        read_only=True,
    ),
    TOOL_CREATE_JOB: ToolSpec(
        name=TOOL_CREATE_JOB,
        label="创建生成任务",
        description="创建生成任务并预留配额；任务的执行与结算由 executor 负责。",
        implementations=(
            "app.services.jobs.create_job",
            "app.services.cost.reserve",
        ),
    ),
    TOOL_JOB_STATUS: ToolSpec(
        name=TOOL_JOB_STATUS,
        label="获取任务状态",
        description="查询任务状态与进度，用于决定是否继续下游节点。",
        implementations=(
            "app.services.jobs.to_dict",
            "app.services.jobs.set_progress",
        ),
        read_only=True,
    ),
    TOOL_SAVE_RESULT: ToolSpec(
        name=TOOL_SAVE_RESULT,
        label="保存结构化结果",
        description="把结构化结果写入数据库（剧本、分镜、角色设定等）。",
        implementations=(
            "app.agents.shot.sync_shots_from_script",
            "app.agents.character.sync_characters_from_script",
            "app.agents.edit.generate_subtitles",
        ),
    ),
    TOOL_SAFETY_CHECK: ToolSpec(
        name=TOOL_SAFETY_CHECK,
        label="请求安全检查",
        description="对输入/输出做安全与合规检查；检查不通过时不得继续。",
        implementations=(
            "app.agents.safety.check_input",
            "app.agents.safety.assert_input_allowed",
            "app.agents.safety.check_output",
        ),
        read_only=True,
    ),
    TOOL_PREVIEW: ToolSpec(
        name=TOOL_PREVIEW,
        label="生成预览",
        description="生成预览时间线/字幕/素材包，供用户确认后再正式导出。",
        implementations=(
            "app.agents.edit.build_timeline",
            "app.agents.edit.render_subtitles",
            "app.agents.edit.build_package",
        ),
    ),
}

# --------------------------------------------------------------------------- #
# 明确禁止的能力（PRD §6.6 原文）
# --------------------------------------------------------------------------- #

FORBIDDEN_TOOLS: dict[str, str] = {
    "run_arbitrary_sql": "禁止直接执行任意 SQL",
    "delete_asset": "禁止删除素材",
    "adjust_quota": "禁止修改配额",
    "change_permissions": "禁止改变用户权限",
}


class ToolNotAllowed(AppError):
    """智能体尝试调用白名单外的能力。"""

    status_code = 403
    code = "TOOL_NOT_ALLOWED"


def assert_tool_allowed(name: str) -> ToolSpec:
    """唯一的能力守卫入口：不在白名单内一律拒绝。

    调用方应当在实际执行前先调用它，这样「越权」会在动作发生之前失败，
    而不是事后审计才发现。
    """
    spec = ALLOWED_TOOLS.get(name)
    if spec is None:
        raise ToolNotAllowed(
            f"智能体不允许调用能力「{name}」；§6.6 只允许七类白名单能力",
            details={
                "requested": name,
                "allowed": sorted(ALLOWED_TOOLS),
                "forbidden": FORBIDDEN_TOOLS,
            },
        )
    return spec


def describe() -> dict[str, Any]:
    """给前端「设置 / 智能体边界」页与审计用的可读清单。"""
    return {
        "allowed": [
            {
                "name": spec.name,
                "label": spec.label,
                "description": spec.description,
                "read_only": spec.read_only,
                "implementations": list(spec.implementations),
            }
            for spec in ALLOWED_TOOLS.values()
        ],
        "forbidden": [
            {"name": name, "reason": reason} for name, reason in FORBIDDEN_TOOLS.items()
        ],
    }


def resolve(path: str) -> Callable[..., Any]:
    """把 `模块.函数` 解析成真实可调用对象，供测试与自检使用。"""
    module_name, _, attr = path.rpartition(".")
    from importlib import import_module

    module = import_module(module_name)
    return getattr(module, attr)
