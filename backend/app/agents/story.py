"""StoryAgent：故事结构、人物表、场景表、剧情节拍与分镜初稿（FR-011/012/013）。"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.errors import ProviderError, SchemaValidationFailed
from app.db.models import Episode
from app.providers.base import ProviderKind, TextRequest
from app.providers.registry import resolve
from app.schemas import ScriptModel

SYSTEM_PROMPT = (
    "你是资深漫剧编剧与分镜师。只输出一个 JSON 对象，不要任何解释文字。"
    "输出必须严格符合用户给出的 JSON Schema，字段名与枚举值不得改动。"
)

#: FR-010「长文本」判定阈值（字符）。超过它前端应提示成本会随之上升。
LONG_TEXT_THRESHOLD = 500

SCRIPT_INSTRUCTIONS = """请基于以下故事设定，产出一集竖屏漫剧（9:16）的完整结构化剧本。

要求：
1. story_bible：logline、theme、tone、setting、characters（至少 2 个主要角色，含 name/identity/age_range/appearance/outfit/personality/speech_style/forbidden_changes）、scenes（至少 3 个场景）、conflict（goal/obstacle/stakes）、relationships。
2. beat_sheet：3~5 个剧情节拍，每个含 beat_no/name/summary/scene_no/shots。
3. shots：{shot_count} 个分镜，镜号从 1 连续递增。每个分镜必须包含：
   shot_no, duration_ms, scene_no, characters, action, camera_movement, composition,
   emotion, dialogue（数组，元素含 character/text/emotion）, narration, sfx,
   image_prompt, negative_prompt, video_params（含 duration_ms、motion_strength、
   camera_movement 三个字段）, required。
4. camera_movement 只能取：固定/推/拉/摇/移/跟/升降；composition 只能取：特写/近景/中景/全景/远景/过肩/俯拍/仰拍。
5. 单镜 duration_ms 在 {dmin}~{dmax} 毫秒之间，全片总时长不超过 {total} 毫秒。
6. image_prompt 必须包含：画风、场景、时间、构图、出场角色外观特征、动作，并适配竖屏 9:16。
7. 角色名必须与 story_bible.characters 中的 name 完全一致。

故事设定：
{source_text}

JSON Schema：
{schema}
"""


def script_json_schema() -> dict[str, Any]:
    return ScriptModel.model_json_schema()


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text
        text = text.lstrip("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _validate(payload: dict[str, Any]) -> ScriptModel:
    return ScriptModel.model_validate(payload)


def build_script_prompt(*, source_text: str, shot_count: int = 6) -> str:
    """构造剧本生成提示词。

    单独抽出来是为了让「预估成本」与「真实调用」用**同一份**提示词——
    FR-010 要求长文本入口显示预计生成成本，若两处各拼一份提示词，
    预估就会与实扣金额对不上。
    """
    from app.core.config import settings

    return SCRIPT_INSTRUCTIONS.format(
        shot_count=shot_count,
        source_text=source_text or "（用户未提供，请自行创作一个 500 字以内的都市悬疑故事）",
        schema=json.dumps(script_json_schema(), ensure_ascii=False)[:6000],
        dmin=settings.shot_duration_ms_min,
        dmax=settings.shot_duration_ms_max,
        total=settings.episode_duration_ms_max,
    )


def estimate_script(
    session: Session,
    *,
    source_text: str,
    shot_count: int = 6,
    genre: str = "都市",
) -> dict[str, Any]:
    """FR-010：长文本入口的字符数与预计生成成本。

    成本按供应商声明的 `unit_price_cents` 计；供应商未声明单价时返回 0，
    并显式标记 `declared=False`，避免前端把 0 当成「免费」。
    """
    provider = resolve(ProviderKind.TEXT, session)
    prompt = build_script_prompt(source_text=source_text, shot_count=shot_count)
    request = TextRequest(
        prompt=prompt,
        system=SYSTEM_PROMPT,
        json_schema=script_json_schema(),
        metadata={"task": "script", "source_text": source_text, "genre": genre},
    )
    estimate = provider.estimate_cost(request)
    caps = provider.capabilities()
    unit_price = int(getattr(caps, "unit_price_cents", 0) or 0)
    return {
        "source_chars": len(source_text or ""),
        "prompt_chars": len(prompt),
        "system_chars": len(SYSTEM_PROMPT),
        "billing_chars": len(prompt) + len(SYSTEM_PROMPT),
        "estimate_cents": estimate.estimate_cents,
        "billing_unit": estimate.billing_unit,
        "unit_price_cents": unit_price,
        "declared": unit_price > 0,
        "provider": estimate.provider,
        "model": estimate.model,
        "shot_count": shot_count,
        "estimated_wait_seconds": caps.estimated_latency_seconds,
        # 长文本提醒：超过阈值时前端应提示「输入较长，成本随之上升」
        "long_text": len(source_text or "") >= LONG_TEXT_THRESHOLD,
        "long_text_threshold": LONG_TEXT_THRESHOLD,
    }


def generate_script(
    session: Session,
    *,
    source_text: str,
    shot_count: int = 6,
    genre: str = "都市",
    raw_only: bool = False,
) -> tuple[ScriptModel, dict[str, Any], Any]:
    """生成剧本。LLM 输出不符合 Schema 时自动修复一次（需求 §11）。"""
    provider = resolve(ProviderKind.TEXT, session)

    prompt = build_script_prompt(source_text=source_text, shot_count=shot_count)
    attempts: list[dict[str, Any]] = []
    last_error = ""
    payload: dict[str, Any] | None = None
    result = None

    for attempt in range(2):
        result = await_result(
            provider,
            TextRequest(
                prompt=prompt if attempt == 0 else _repair_prompt(prompt, last_error, payload),
                system=SYSTEM_PROMPT,
                json_schema=script_json_schema(),
                temperature=0.7 if attempt == 0 else 0.2,
                metadata={
                    "task": "script",
                    "source_text": source_text,
                    "shot_count": shot_count,
                    "genre": genre,
                },
            ),
        )
        attempts.append(
            {
                "attempt": attempt + 1,
                "ok": result.ok,
                "request_id": result.request_id,
                "raw_summary": result.raw_summary,
            }
        )
        if not result.ok:
            raise ProviderError(
                result.error_message or "文本供应商调用失败",
                code=result.error_code or "PROVIDER_ERROR",
                details={"attempts": attempts},
                retryable=result.retryable,
            )
        payload = result.data
        try:
            model = _validate(payload)
            return model, {"attempts": attempts, "raw_summary": result.raw_summary}, result
        except ValidationError as exc:
            last_error = _format_errors(exc)
            if attempt == 0:
                continue
            raise SchemaValidationFailed(
                f"剧本 Schema 校验失败（已自动修复一次）：{last_error}",
                details={
                    "errors": exc.errors(include_url=False)[:10],
                    "raw_summary": result.raw_summary,
                    "raw_payload": _truncate(payload),
                },
            ) from exc

    raise SchemaValidationFailed("剧本生成失败：未获得合法 JSON")


def _repair_prompt(prompt: str, error: str, payload: dict[str, Any] | None) -> str:
    return (
        f"{prompt}\n\n上一次输出未通过 Schema 校验，错误如下：\n{error}\n"
        f"上一次输出摘要：{json.dumps(_truncate(payload or {}), ensure_ascii=False)[:2000]}\n"
        "请严格修正后重新输出完整 JSON。"
    )


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors(include_url=False)[:8]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        lines.append(f"{loc}: {err.get('msg')}")
    return "；".join(lines)


def _truncate(payload: dict[str, Any], limit: int = 4000) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) <= limit:
        return payload
    return {"_truncated": True, "_preview": raw[:limit]}


def analyze_story(session: Session, episode: Episode, source_text: str) -> dict[str, Any]:
    """FR-011 故事分析：主题、人物关系、场景清单、剧情节拍、冲突目标。

    模型输出经 `StoryAnalysis` 归一（缺项补默认、字符串转列表）；
    模型不可用或输出为空时，从已有剧本的 story_bible 派生一份兜底分析，
    保证分析面板不会整块空白。
    """
    from app.schemas import StoryAnalysis

    provider = resolve(ProviderKind.TEXT, session)
    result = await_result(
        provider,
        TextRequest(
            prompt=f"分析以下故事设定，输出主题、人物关系、场景清单、剧情节拍、冲突目标。\n\n{source_text}",
            system=SYSTEM_PROMPT,
            json_schema={"type": "object"},
            metadata={"task": "story_analysis", "source_text": source_text},
        ),
    )
    data = result.data if result.ok and isinstance(result.data, dict) else {}
    analysis = StoryAnalysis.model_validate(data) if data else StoryAnalysis()
    analysis.raw_summary = {
        "provider": getattr(provider, "id", ""),
        "ok": bool(result.ok),
        **({"error": result.error} if not result.ok and result.error else {}),
    }
    if not data:
        # 兜底：用已有剧本的 story_bible 派生主题/角色/场景/节拍
        analysis = _analysis_from_script(session, episode, analysis)
    return analysis.model_dump(mode="json")


def _analysis_from_script(
    session: Session, episode: Episode, analysis: "Any"
) -> "Any":
    """从最新剧本的 story_bible / beat_sheet 派生分析结果（模型不可用时的降级）。"""
    from app.db.models import StoryDocument

    document = session.scalar(
        select(StoryDocument)
        .where(StoryDocument.episode_id == episode.id)
        .order_by(StoryDocument.version.desc())
        .limit(1)
    )
    script = (document.story_json if document else None) or {}
    bible = script.get("story_bible") or {}
    if not bible:
        return analysis
    analysis.theme = analysis.theme or str(bible.get("theme") or "")
    if not analysis.characters:
        analysis.characters = [
            c.get("name", "") for c in (bible.get("characters") or []) if c.get("name")
        ]
    if not analysis.scenes:
        analysis.scenes = [
            s.get("location", "") for s in (bible.get("scenes") or []) if s.get("location")
        ]
    if not analysis.relationships:
        analysis.relationships = list(bible.get("relationships") or [])
    if not analysis.beats:
        analysis.beats = [
            b.get("name", "") for b in (script.get("beat_sheet") or []) if b.get("name")
        ]
    conflict = bible.get("conflict") or {}
    if conflict and not any(getattr(analysis.conflict, k) for k in ("goal", "obstacle", "stakes")):
        analysis.conflict = conflict
    analysis.raw_summary = {**analysis.raw_summary, "derived_from_script": True}
    return analysis


def _parse_shot_patches(data: Any, target_nos: list[int]) -> dict[int, dict[str, Any]]:
    """把模型返回归一成 `{shot_no: patch}`。

    容忍四种常见形态：`{"shots": [...]}`、裸列表、`{"1": {...}}` 键值表，
    以及只返回一个 patch 对象（此时套用到全部目标分镜）。
    """
    items: list[dict[str, Any]] = []
    if isinstance(data, list):
        items = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        nested = data.get("shots") or data.get("items") or data.get("results")
        if isinstance(nested, list):
            items = [x for x in nested if isinstance(x, dict)]
        else:
            # {"1": {...}, "2": {...}}
            keyed = {k: v for k, v in data.items() if str(k).isdigit() and isinstance(v, dict)}
            if keyed:
                items = [{"shot_no": int(k), **v} for k, v in keyed.items()]
            elif any(k in data for k in ("action", "image_prompt", "negative_prompt",
                                         "camera_movement", "composition")):
                # 单个 patch：套用到全部目标分镜
                return {no: data for no in target_nos}

    patches: dict[int, dict[str, Any]] = {}
    for item in items:
        try:
            no = int(item.get("shot_no"))
        except (TypeError, ValueError):
            continue
        if no in target_nos:
            patches[no] = item
    if not patches and items:
        # 模型没给镜号：按顺序对应目标分镜
        for no, item in zip(target_nos, items):
            patches[no] = item
    return patches


def regenerate_shots(
    session: Session,
    *,
    script: dict[str, Any],
    shot_nos: list[int],
    instruction: str = "",
    replan_downstream: bool = False,
) -> tuple[dict[str, Any], list[int]]:
    """FR-014 局部重生成。

    - 默认只改选中的分镜，未选中分镜原样保留；
    - `replan_downstream=True`（用户明确选择「重新规划后续镜头」）时，从最早
      选中的镜号起，后续所有分镜一并重新规划；
    - 返回 `(新剧本, 实际被改动的镜号列表)`。
    """
    provider = resolve(ProviderKind.TEXT, session)
    shots = script.get("shots", [])
    selected = sorted({int(n) for n in shot_nos})
    if not selected:
        raise SchemaValidationFailed("未指定要重生成的分镜")
    if replan_downstream:
        floor = min(selected)
        targets = [s for s in shots if int(s.get("shot_no", 0)) >= floor]
    else:
        targets = [s for s in shots if int(s.get("shot_no", 0)) in set(selected)]
    if not targets:
        raise SchemaValidationFailed(f"未找到要重生成的分镜：{shot_nos}")
    target_nos = [int(s["shot_no"]) for s in targets]

    if replan_downstream:
        task_line = (
            "请重写以下分镜的动作、画面提示词、负面提示词、镜头运动与构图。"
            "这是「重新规划后续镜头」：请让后续分镜在情节上承接前文，"
            "但每个分镜都要各自给出完整结果（用 shots 数组逐条返回，带 shot_no）。"
            "保持镜号、时长、出场角色、台词不变。"
        )
    else:
        task_line = (
            "请重写以下分镜的动作、画面提示词、负面提示词、镜头运动与构图，"
            "保持镜号、时长、出场角色、台词不变。"
            "每个分镜各自给出完整结果（用 shots 数组逐条返回，带 shot_no）。"
        )

    result = await_result(
        provider,
        TextRequest(
            prompt=(
                f"{task_line}"
                f"\n额外要求：{instruction or '提升画面张力与电影感'}"
                f"\n分镜：{json.dumps(targets, ensure_ascii=False)}"
            ),
            system=SYSTEM_PROMPT,
            json_schema={"type": "object"},
            temperature=0.8,
            metadata={
                "task": "shot_rewrite",
                "replan_downstream": replan_downstream,
                "shot_nos": target_nos,
                "action": targets[0].get("action", ""),
                "image_prompt": targets[0].get("image_prompt", ""),
                "negative_prompt": targets[0].get("negative_prompt", ""),
                "camera_movement": targets[0].get("camera_movement", "固定"),
                "composition": targets[0].get("composition", "中景"),
            },
        ),
    )
    if not result.ok:
        raise ProviderError(result.error_message or "分镜重生成失败",
                            code=result.error_code or "PROVIDER_ERROR")

    patches = _parse_shot_patches(result.data, target_nos)
    updated = json.loads(json.dumps(script))
    changed: list[int] = []
    for shot in updated.get("shots", []):
        no = int(shot.get("shot_no", 0))
        patch = patches.get(no)
        if not patch:
            continue
        touched = False
        for field in ("action", "image_prompt", "negative_prompt",
                      "camera_movement", "composition"):
            value = patch.get(field)
            if value and value != shot.get(field):
                shot[field] = value
                touched = True
        if touched:
            changed.append(no)
    # 模型返回的内容与现状一致时不算失败，但要如实报告「没有分镜被改动」，
    # 由调用方决定是否真的落一个新版本（避免无意义的版本膨胀）。
    return updated, changed


def _suggest_one(
    session: Session,
    *,
    payload: dict[str, Any],
    bible: dict[str, Any],
    beats: list[Any],
    prev_shot: dict[str, Any] | None,
    next_shot: dict[str, Any] | None,
    instruction: str = "",
) -> tuple[dict[str, Any], int]:
    """单镜建议的公共实现：输入「这一镜的现状 + 上下文」，返回归一后的建议字段。

    只产出草稿、不落库；两条入口（剧本页 JSON、分镜画面页 Shot 行）共用同一套
    prompt 与归一逻辑，避免两边各写一份、结果不一致。
    """
    from app.core.config import settings
    from app.schemas import (
        CAMERA_ALIASES,
        CAMERA_MOVEMENTS,
        COMPOSITION_ALIASES,
        COMPOSITIONS,
        _match_known_name,
        normalize_enum,
    )

    shot_no = int(payload.get("shot_no") or 0)
    names = {
        str(c.get("name") or "").strip()
        for c in (bible.get("characters") or [])
        if isinstance(c, dict)
    }
    names.discard("")
    beat = next(
        (b for b in beats if shot_no in {int(x) for x in (b.get("shots") or [])}),
        None,
    )

    def _brief(shot: dict[str, Any] | None) -> str:
        if not shot:
            return "（无）"
        return (
            f"#{shot.get('shot_no')} {shot.get('action') or ''}"
            f"（{shot.get('composition') or ''}/{shot.get('camera_movement') or ''}，"
            f"{shot.get('duration_ms') or 0}ms）"
        )

    prompt = f"""你在为一集竖屏漫剧（9:16）写**第 {shot_no} 个分镜**。请根据大纲与前后镜头，让这一镜服务剧情。

【故事】
logline：{bible.get('logline') or ''}
主题：{bible.get('theme') or ''}｜基调：{bible.get('tone') or ''}｜场景：{bible.get('setting') or ''}
主要人物：{"; ".join(f"{c.get('name')}（{c.get('identity') or ''}）" for c in (bible.get('characters') or [])[:6] if isinstance(c, dict))}

【所在情节点】
{("- " + str(beat.get('name')) + "：" + str(beat.get('summary'))) if beat else "（未归属具体节拍）"}

【前后镜头】
上一镜：{_brief(prev_shot)}
下一镜：{_brief(next_shot)}

【当前这一镜的现状】
{json.dumps(payload, ensure_ascii=False)[:1200]}

【额外要求】
{instruction or "让动作、台词与画面提示词更贴合剧情，信息密度高一些"}

只输出一个 JSON 对象（不要数组、不要解释），字段：
- action：本镜画面动作描述（1~2 句，写清谁在做什么、环境如何）
- dialogue：台词数组，元素 {{"character": 人物表里的名字, "text": 台词, "emotion": 情绪词}}；没有台词就给 []
- narration：旁白（没有就留空字符串）
- composition：只能取 {"/".join(COMPOSITIONS)}
- camera_movement：只能取 {"/".join(CAMERA_MOVEMENTS)}
- duration_ms：{settings.shot_duration_ms_min}~{settings.shot_duration_ms_max} 之间的整数
- characters：本镜出场角色名数组，必须来自人物表
- image_prompt：画面提示词，含画风/场景/时间/构图/角色外观/动作，并说明竖屏 9:16
- negative_prompt：需要避免的内容
"""

    provider = resolve(ProviderKind.TEXT, session)
    result = await_result(
        provider,
        TextRequest(
            prompt=prompt,
            system=SYSTEM_PROMPT,
            json_schema={"type": "object"},
            temperature=0.75,
            metadata={
                "task": "shot_suggest",
                "shot_no": shot_no,
                "action": str(payload.get("action") or ""),
                "image_prompt": str(payload.get("image_prompt") or ""),
                "composition": str(payload.get("composition") or ""),
                "camera_movement": str(payload.get("camera_movement") or ""),
            },
        ),
    )
    if not result.ok:
        raise ProviderError(
            result.error_message or "分镜建议生成失败",
            code=result.error_code or "PROVIDER_ERROR",
        )
    data = result.data if isinstance(result.data, dict) else {}
    if not data:
        raise ProviderError("模型没有返回可用的分镜内容，请重试")

    dialogue: list[dict[str, str]] = []
    for line in data.get("dialogue") or []:
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "").strip()
        who = _match_known_name(str(line.get("character") or ""), names) if names else None
        if text and who:
            dialogue.append(
                {"character": who, "text": text, "emotion": str(line.get("emotion") or "").strip()}
            )

    characters: list[str] = []
    for raw in data.get("characters") or []:
        who = _match_known_name(str(raw), names) if names else str(raw).strip()
        if who and who not in characters:
            characters.append(who)
    if not characters:
        # 模型没给（或给的名字都不在人物表里）就沿用原镜头的角色，避免"生成一次把角色清空"
        characters = [str(n) for n in (payload.get("characters") or []) if str(n).strip()]
    if not characters:
        characters = list(dict.fromkeys(line["character"] for line in dialogue))

    try:
        duration = int(data.get("duration_ms") or payload.get("duration_ms") or 4000)
    except (TypeError, ValueError):
        duration = 4000
    duration = max(settings.shot_duration_ms_min, min(settings.shot_duration_ms_max, duration))

    suggestion = {
        "duration_ms": duration,
        "composition": normalize_enum(
            data.get("composition"),
            COMPOSITIONS,
            COMPOSITION_ALIASES,
            str(payload.get("composition") or "中景"),
        ),
        "camera_movement": normalize_enum(
            data.get("camera_movement"),
            CAMERA_MOVEMENTS,
            CAMERA_ALIASES,
            str(payload.get("camera_movement") or "固定"),
        ),
        "characters": characters,
        "action": str(data.get("action") or "").strip(),
        "dialogue": dialogue,
        "narration": str(data.get("narration") or "").strip(),
        "emotion": str(data.get("emotion") or payload.get("emotion") or "").strip(),
        "image_prompt": str(data.get("image_prompt") or "").strip(),
        "negative_prompt": str(
            data.get("negative_prompt") or payload.get("negative_prompt") or ""
        ).strip(),
    }
    return suggestion, int(result.cost_cents or 0)


def suggest_shot(
    session: Session,
    *,
    script: dict[str, Any],
    shot_no: int,
    instruction: str = "",
) -> tuple[dict[str, Any], int]:
    """剧本页（JSON 草稿）入口：按大纲为单个分镜生成可编辑草稿（不落库）。"""
    shots = script.get("shots") or []
    target = next(
        (s for s in shots if int(s.get("shot_no") or 0) == int(shot_no)), None
    )
    if target is None:
        raise SchemaValidationFailed(f"未找到分镜 {shot_no}")
    index = shots.index(target)
    return _suggest_one(
        session,
        payload=target,
        bible=script.get("story_bible") or {},
        beats=script.get("beat_sheet") or [],
        prev_shot=shots[index - 1] if index > 0 else None,
        next_shot=shots[index + 1] if index + 1 < len(shots) else None,
        instruction=instruction,
    )


def suggest_shot_for_row(
    session: Session,
    *,
    episode: Episode,
    shot: Any,
    instruction: str = "",
) -> tuple[dict[str, Any], int]:
    """分镜画面页入口：以 `Shot` 行（用户正在编辑的那份）为准。

    剧情上下文（人物表、节拍表）取自最新剧本；镜头现状取自 Shot 行，
    这样即使用户先改过这一镜，AI 也是基于「屏幕上这份」给建议。
    """
    from sqlalchemy import select

    from app.db.models import Shot as ShotModel
    from app.db.models import StoryDocument

    document = session.scalar(
        select(StoryDocument)
        .where(StoryDocument.episode_id == episode.id)
        .order_by(StoryDocument.version.desc())
    )
    script = (document.story_json if document else {}) or {}
    rows = list(
        session.scalars(
            select(ShotModel)
            .where(ShotModel.episode_id == episode.id)
            .order_by(ShotModel.shot_no)
        )
    )

    def _flat(row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {**(row.shot_json or {}), "shot_no": row.shot_no, "duration_ms": row.duration_ms}

    payload = _flat(shot) or {}
    index = next((i for i, row in enumerate(rows) if row.id == shot.id), -1)
    return _suggest_one(
        session,
        payload=payload,
        bible=script.get("story_bible") or {},
        beats=script.get("beat_sheet") or [],
        prev_shot=_flat(rows[index - 1]) if index > 0 else None,
        next_shot=_flat(rows[index + 1]) if 0 <= index < len(rows) - 1 else None,
        instruction=instruction,
    )


def run_coro(coro: Any) -> Any:
    """在同步上下文中执行异步适配器调用。

    worker 在线程池中执行 job handler，因此绝大多数场景可以直接 ``asyncio.run``；
    若当前线程已有事件循环（例如 async 路由里直接调用），则转到独立线程执行。
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    from app.services import provider_log

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(provider_log.run_with, provider_log.current(), asyncio.run, coro).result()


def await_result(provider: Any, request: TextRequest) -> Any:
    return run_coro(provider.generate_json(request))
