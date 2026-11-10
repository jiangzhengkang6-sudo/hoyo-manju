"""LangGraph 工作流状态定义（需求文档 §6.2）。"""

from __future__ import annotations

from typing import Any, TypedDict

GRAPH_VERSION = "mvp-1"

NODE_SEQUENCE = [
    "initialize",
    "safety_check_input",
    "story_agent",
    "validate_script",
    "gate_script_confirmation",
    "character_agent",
    "gate_character_confirmation",
    "shot_agent",
    "image_generation",
    "gate_shot_confirmation",
    "cost_estimate",
    "gate_video_cost_confirmation",
    "video_generation",
    "gate_video_confirmation",
    "audio_agent",
    "audio_generation",
    "edit_agent",
    "render_preview",
    "gate_export_confirmation",
    "export",
    "completed",
]

GATE_NODES = [
    "gate_script_confirmation",
    "gate_character_confirmation",
    "gate_shot_confirmation",
    "gate_video_cost_confirmation",
    "gate_video_confirmation",
    "gate_export_confirmation",
]

NODE_LABELS = {
    "initialize": "初始化",
    "safety_check_input": "输入安全检查",
    "story_agent": "剧本生成",
    "validate_script": "剧本校验",
    "gate_script_confirmation": "剧本确认门控",
    "character_agent": "角色与画风",
    "gate_character_confirmation": "角色确认门控",
    "shot_agent": "分镜与提示词",
    "image_generation": "候选图生成",
    "gate_shot_confirmation": "画面确认门控",
    "cost_estimate": "视频成本预估",
    "gate_video_cost_confirmation": "视频成本确认门控",
    "video_generation": "视频片段生成",
    "gate_video_confirmation": "视频确认门控",
    "audio_agent": "音频规划",
    "audio_generation": "TTS 与 BGM",
    "edit_agent": "字幕与时间线",
    "render_preview": "预览合成",
    "gate_export_confirmation": "导出确认门控",
    "export": "正式导出",
    "completed": "完成",
}


class WorkflowState(TypedDict, total=False):
    episode_id: str
    project_id: str
    workflow_run_id: str
    source_text: str
    shot_count: int
    #: 启动时已存在可用剧本 → `story_agent` 只复用、不重新调用模型
    reuse_script: bool
    story_document_id: str
    script_version: int
    script_valid: bool
    script_errors: list[dict[str, Any]]
    character_ready: bool
    confirmed_shots: list[int]
    video_quote: dict[str, Any]
    render: dict[str, Any]
    export: dict[str, Any]
    decisions: dict[str, Any]
    pending_gate: str
    last_node: str
    warnings: list[str]
    errors: list[dict[str, Any]]
    safety: dict[str, Any]
    stopped: bool
