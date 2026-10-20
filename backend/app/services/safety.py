"""内容安全：输入检查、输出检查、AIGC 标识（FR-MVP-080/081/083）。"""

from __future__ import annotations

import re
from typing import Any

from app.core.config import settings

#: 高风险类别关键词（MVP 使用可配置词表，后续可替换为审核 API）
CATEGORY_RULES: dict[str, list[str]] = {
    "violence": ["血腥", "肢解", "虐杀", "屠杀"],
    "sexual": ["色情", "裸体", "性行为"],
    "minor_harm": ["未成年", "儿童色情", "幼女", "幼童"],
    "terror": ["恐怖主义", "爆炸物制作", "制毒", "炸弹配方"],
    "hate": ["种族灭绝", "极端仇恨"],
    "illegal": ["洗钱", "诈骗教程", "枪支买卖"],
}


def _hits(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for category, words in CATEGORY_RULES.items():
        hit = [w for w in words if w in text]
        if hit:
            found[category] = hit
    for term in settings.safety_block_terms:
        if term and term in text:
            found.setdefault("policy", []).append(term)
    return found


def check_text(text: str, scope: str = "input") -> dict[str, Any]:
    """返回 {allowed, risk_level, categories, suggestions}。

    命中任一风险类别即判定不通过（`allowed=False`），由调用方阻断或要求修改；
    其中涉及未成年人、恐怖主义等类别直接判为 high，其余为 medium。
    """
    text = text or ""
    hits = _hits(text)
    if not hits:
        return {
            "allowed": True,
            "risk_level": "low",
            "categories": [],
            "suggestions": [],
            "scope": scope,
        }

    severe = {"minor_harm", "terror"}
    risk = "high" if severe & set(hits) else "medium"
    suggestions: list[str] = []
    for category in hits:
        if category == "violence":
            suggestions.append("弱化暴力描写，改为暗示性表达（如「画面一暗」）")
        elif category == "sexual":
            suggestions.append("删除露骨描写，改为情感或氛围侧写")
        elif category == "minor_harm":
            suggestions.append("涉及未成年人的内容一律禁止，请调整角色年龄设定")
        elif category == "terror":
            suggestions.append("移除涉及危险物品制作的内容")
        elif category == "hate":
            suggestions.append("移除针对特定群体的极端表述")
        elif category == "illegal":
            suggestions.append("移除违法操作细节，改为剧情化处理")
        else:
            suggestions.append(f"请修改或删除命中内容：{'、'.join(hits[category])}")
    return {
        "allowed": False,
        "risk_level": risk,
        "categories": [
            {"category": c, "terms": terms} for c, terms in hits.items()
        ],
        "suggestions": suggestions,
        "scope": scope,
    }


def check_media_metadata(kind: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """输出检查：记录生成媒体的安全检查结果。"""
    meta = metadata or {}
    provider_flag = str(meta.get("safety_status", "")).upper()
    if provider_flag in {"BLOCKED", "RISK"}:
        return {
            "status": "BLOCKED",
            "risk_level": "high",
            "detail": {"reason": "供应商内容审核未通过", **meta},
        }
    return {"status": "PASSED", "risk_level": "low", "detail": {"checked": True}}


def aigc_label() -> dict[str, Any]:
    return {
        "label": settings.aigc_label_text,
        "generated_by_ai": True,
        "disclosure_required": True,
        "can_disable": False,
    }


def rights_payload(*, source: str, rights_declared: bool, note: str = "") -> dict[str, Any]:
    return {
        "source": source,
        "rights_declared": rights_declared,
        "note": note,
        "aigc": aigc_label(),
    }


def scrub_prompt(prompt: str) -> str:
    """提示词入库前的轻量清洗（去掉多余空白与换行炸弹）。"""
    return re.sub(r"\s{3,}", "  ", (prompt or "").strip())[:2000]


#: 需要做输入检查的自由文本字段（FR-080）。
#: 故事的正文与素材上传已有检查，这里补齐**台词与提示词**这两条此前完全没查的路径。
CHECKED_FIELDS: tuple[tuple[str, str], ...] = (
    ("dialogue", "台词"),
    ("image_prompt", "画面提示词"),
    ("negative_prompt", "负向提示词"),
    ("action", "画面动作"),
    ("narration", "旁白"),
)


def check_fields(
    fields: dict[str, Any],
    *,
    scope: str = "shot",
    only: tuple[tuple[str, str], ...] = CHECKED_FIELDS,
) -> dict[str, Any]:
    """批量检查若干自由文本字段（FR-080）。

    返回 `{allowed, scope, problems[], checked[]}`；`problems` 每项带
    `field`（字段名）、`label`（中文名）以及 `check_text` 的完整结果，
    便于接口直接把「哪一句、为什么、怎么改」交给前端展示。
    """
    problems: list[dict[str, Any]] = []
    checked: list[str] = []
    for field, label in only:
        value = fields.get(field)
        texts: list[str] = []
        if isinstance(value, str):
            texts = [value]
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict):
                    # 台词结构：{"character": ..., "text": ...}
                    for key in ("text", "line", "content"):
                        if isinstance(item.get(key), str):
                            texts.append(item[key])
                    # 角色名也可能被用作绕过手段，一并查
                    if isinstance(item.get("character"), str):
                        texts.append(item["character"])
        for text in texts:
            if not (text or "").strip():
                continue
            checked.append(field)
            result = check_text(text, scope=f"{scope}.{field}")
            if not result["allowed"]:
                problems.append({"field": field, "label": label, **result})

    return {
        "allowed": not problems,
        "scope": scope,
        "problems": problems,
        "checked": sorted(set(checked)),
    }


def ensure_fields_allowed(
    fields: dict[str, Any],
    *,
    scope: str = "shot",
    message: str = "",
) -> dict[str, Any]:
    """字段不通过时抛 `SafetyBlocked`；通过时返回检查结果。"""
    result = check_fields(fields, scope=scope)
    if not result["allowed"]:
        from app.core.errors import SafetyBlocked

        first = result["problems"][0]
        raise SafetyBlocked(
            message
            or f"{first['label']}未通过内容安全检查，请按建议修改后重试",
            details={
                "problems": result["problems"],
                "suggestions": first.get("suggestions", []),
                "risk_level": first.get("risk_level"),
            },
        )
    return result
