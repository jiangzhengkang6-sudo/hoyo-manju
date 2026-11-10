"""Pydantic v2 请求/响应模型与剧本 Schema（FR-013）。"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings

# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #

CameraMovement = Literal["固定", "推", "拉", "摇", "移", "跟", "升降"]
Composition = Literal["特写", "近景", "中景", "全景", "远景", "过肩", "俯拍", "仰拍"]

#: 规范值元组（供归一化使用；顺序即「长值优先」匹配顺序）
CAMERA_MOVEMENTS: tuple[str, ...] = ("升降", "固定", "推", "拉", "摇", "移", "跟")
COMPOSITIONS: tuple[str, ...] = (
    "特写", "近景", "中景", "全景", "远景", "过肩", "俯拍", "仰拍",
)

#: 大模型常见变体 → 规范值。真实 LLM 几乎不会严格照抄枚举，
#: 直接严格校验会让整条剧本生成失败（实测 DeepSeek 会输出「推镜头」「pan」等）。
CAMERA_ALIASES: dict[str, str] = {
    # 固定
    "静止": "固定", "不动": "固定", "定格": "固定", "锁镜": "固定",
    "固定镜头": "固定", "固定机位": "固定", "static": "固定",
    "fixed": "固定", "lock": "固定", "still": "固定",
    # 推
    "推镜": "推", "推近": "推", "前推": "推", "推进": "推", "推镜头": "推",
    "推轨": "推", "zoom in": "推", "push in": "推", "dolly in": "推",
    # 拉
    "拉远": "拉", "后拉": "拉", "拉开": "拉", "拉镜头": "拉", "拉背": "拉",
    "zoom out": "拉", "pull out": "拉", "dolly out": "拉",
    # 摇
    "摇镜": "摇", "摇摄": "摇", "横摇": "摇", "摇镜头": "摇", "甩镜": "摇",
    "pan": "摇", "pan left": "摇", "pan right": "摇",
    # 移
    "移镜": "移", "横移": "移", "平移": "移", "移镜头": "移", "侧移": "移",
    "track": "移", "tracking": "移", "truck": "移",
    # 跟
    "跟镜": "跟", "跟随": "跟", "跟拍": "跟", "跟镜头": "跟", "跟移": "跟",
    "follow": "跟", "following": "跟",
    # 升降
    "升": "升降", "降": "升降", "上升": "升降", "下降": "升降",
    "升降镜头": "升降", "摇臂": "升降", "crane": "升降",
    "boom": "升降", "crane up": "升降", "crane down": "升降",
}

COMPOSITION_ALIASES: dict[str, str] = {
    "大特写": "特写", "close-up": "特写", "closeup": "特写", "close up": "特写",
    "特写镜头": "特写",
    "中近景": "近景", "半身": "近景", "medium close": "近景",
    "中景镜头": "中景", "medium shot": "中景", "medium": "中景",
    "大全景": "全景", "广角": "全景", "wide": "全景", "full shot": "全景",
    "远景镜头": "远景", "long shot": "远景",
    "过肩镜头": "过肩", "over shoulder": "过肩", "over-the-shoulder": "过肩",
    "俯视": "俯拍", "俯角": "俯拍", "高角度": "俯拍", "high angle": "俯拍",
    "仰视": "仰角", "仰拍镜头": "仰拍", "低角度": "仰拍", "low angle": "仰拍",
}


def normalize_enum(
    value: Any,
    canon: tuple[str, ...],
    aliases: dict[str, str],
    default: str,
) -> str:
    """把大模型输出的枚举变体归一成规范值。

    依次尝试：精确匹配 → 去空白/标点后匹配 → 别名表 → 包含规范值 → 默认值。
    宁可归一化也不让整条剧本生成失败 —— 一个镜头运动写歪不该毁掉全部产出。
    """
    if value is None:
        return default
    raw = str(value).strip()
    if not raw:
        return default
    if raw in canon:
        return raw

    compact = re.sub(r"[\s\-_/·、，,。.（）()【】\[\]]+", "", raw)
    if compact in canon:
        return compact
    lowered = compact.lower()
    for key, target in aliases.items():
        if re.sub(r"[\s\-_/]+", "", key.lower()) in (lowered, compact.lower()):
            return target

    # 包含规范值：如「缓慢推近」含「推」、「固定机位」含「固定」
    for token in canon:
        if token in compact:
            return token
    # 英文别名做子串兜底
    for key, target in aliases.items():
        if re.sub(r"[\s\-_/]+", "", key.lower()) in lowered:
            return target
    return default


def _match_known_name(raw: str, names: set[str]) -> str | None:
    """把分镜里的角色引用宽松匹配到人物表的规范名。

    `「林晚（女主）」` → `林晚`；对不上就返回 None（调用方会丢弃该引用）。
    """
    if not names:
        return None
    cleaned = re.sub(r"[（(【\[].*?[）)】\]]", "", raw).strip()
    if cleaned in names:
        return cleaned
    candidates = [n for n in names if n and (n in cleaned or cleaned in n)]
    return max(candidates, key=len) if candidates else None

TimeOfDay = Literal["清晨", "白天", "黄昏", "夜晚", "深夜", "不限"]
AssetKind = Literal["setting", "front", "half_body", "expression"]
GateName = Literal[
    "gate_script_confirmation",
    "gate_character_confirmation",
    "gate_shot_confirmation",
    "gate_video_cost_confirmation",
    "gate_video_confirmation",
    "gate_export_confirmation",
]

CAMERA_MOVEMENTS = ["固定", "推", "拉", "摇", "移", "跟", "升降"]
COMPOSITIONS = ["特写", "近景", "中景", "全景", "远景", "过肩", "俯拍", "仰拍"]
TIMES_OF_DAY = ["清晨", "白天", "黄昏", "夜晚", "深夜", "不限"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# --------------------------------------------------------------------------- #
# 剧本结构化 Schema
# --------------------------------------------------------------------------- #

class DialogueLine(BaseModel):
    character: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=500)
    emotion: str = Field(default="", max_length=60)


class SceneItem(BaseModel):
    scene_no: int = Field(ge=1)
    location: str = Field(min_length=1, max_length=200)
    time_of_day: TimeOfDay = "白天"
    summary: str = Field(default="", max_length=1000)


class StoryCharacter(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    identity: str = Field(default="", max_length=200)
    age_range: str = Field(default="", max_length=40)
    appearance: str = Field(default="", max_length=1000)
    outfit: str = Field(default="", max_length=1000)
    personality: str = Field(default="", max_length=1000)
    speech_style: str = Field(default="", max_length=500)
    forbidden_changes: list[str] = Field(default_factory=list)
    is_main: bool = True


class ConflictInfo(BaseModel):
    goal: str = Field(default="", max_length=500)
    obstacle: str = Field(default="", max_length=500)
    stakes: str = Field(default="", max_length=500)


class Relationship(BaseModel):
    from_: str = Field(alias="from", min_length=1, max_length=60)
    to: str = Field(min_length=1, max_length=60)
    relation: str = Field(default="", max_length=120)

    model_config = ConfigDict(populate_by_name=True)


class StoryAnalysis(BaseModel):
    """FR-011 故事分析：主题、人物关系、场景清单、剧情节拍、冲突目标。

    模型输出不受控，这里做宽容归一：字符串可直接当列表用，缺项给默认值，
    保证分析面板始终有结构化内容可展示（而不是整块空白）。
    """

    theme: str = Field(default="", max_length=500)
    #: 出场人物名（供 UI 快速展示）
    characters: list[str] = Field(default_factory=list)
    #: 人物关系（PRD 明确要求的五项之一）
    relationships: list[Relationship] = Field(default_factory=list)
    #: 场景清单
    scenes: list[str] = Field(default_factory=list)
    #: 剧情节拍
    beats: list[str] = Field(default_factory=list)
    #: 冲突目标
    conflict: ConflictInfo = Field(default_factory=ConflictInfo)
    #: 模型原始输出摘要，便于排查
    raw_summary: dict[str, Any] = Field(default_factory=dict)

    @field_validator("characters", "scenes", "beats", mode="before")
    @classmethod
    def _coerce_str_list(cls, v: Any) -> Any:
        """把「单个字符串 / 对象列表 / None」统一成字符串列表。"""
        if v is None:
            return []
        if isinstance(v, str):
            text = v.strip()
            if not text:
                return []
            parts = [p.strip() for p in re.split(r"[、,，/|\n]+", text) if p.strip()]
            return parts or [text]
        if isinstance(v, list):
            out: list[str] = []
            for item in v:
                if item is None:
                    continue
                if isinstance(item, str):
                    if item.strip():
                        out.append(item.strip())
                elif isinstance(item, dict):
                    # {"name": "林晚"} / {"location": "咖啡馆"} / {"name":..., "summary":...}
                    label = (
                        item.get("name")
                        or item.get("location")
                        or item.get("title")
                        or item.get("summary")
                        or ""
                    )
                    if isinstance(label, str) and label.strip():
                        detail = item.get("summary") or item.get("relation") or ""
                        if detail and detail != label:
                            out.append(f"{label.strip()}（{str(detail).strip()}）")
                        else:
                            out.append(label.strip())
                else:
                    out.append(str(item))
            return out
        return v

    @model_validator(mode="after")
    def _fill_from_relationships(self) -> "StoryAnalysis":
        """人物关系里出现的人名，补进 characters，避免 UI「角色」为空。"""
        if not self.characters and self.relationships:
            names: list[str] = []
            for rel in self.relationships:
                for name in (rel.from_, rel.to):
                    if name and name not in names:
                        names.append(name)
            self.characters = names
        return self


class StoryBible(BaseModel):
    logline: str = Field(default="", max_length=500)
    theme: str = Field(default="", max_length=300)
    tone: str = Field(default="", max_length=200)
    setting: str = Field(default="", max_length=500)
    characters: list[StoryCharacter] = Field(default_factory=list)
    scenes: list[SceneItem] = Field(default_factory=list)
    conflict: ConflictInfo = Field(default_factory=ConflictInfo)
    relationships: list[Relationship] = Field(default_factory=list)


class BeatItem(BaseModel):
    beat_no: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=1000)
    scene_no: int = Field(default=1, ge=1)
    shots: list[int] = Field(default_factory=list)


class VideoParams(BaseModel):
    duration_ms: int = Field(default=4000)
    motion_strength: float = Field(default=0.4, ge=0.0, le=1.0)
    camera_movement: CameraMovement = "固定"

    @field_validator("camera_movement", mode="before")
    @classmethod
    def _normalize_camera(cls, v: Any) -> Any:
        return normalize_enum(v, CAMERA_MOVEMENTS, CAMERA_ALIASES, "固定")

    @field_validator("motion_strength", mode="before")
    @classmethod
    def _clamp_motion(cls, v: Any) -> Any:
        """大模型偶尔给 1.5 / 70 这类越界值，夹到 0~1 而不是整条失败。"""
        try:
            num = float(v)
        except (TypeError, ValueError):
            return v
        return max(0.0, min(1.0, num))


class TransitionSpec(BaseModel):
    """FR-061 转场：表示「从上一镜切入本镜」的方式与时长。

    首镜的转场会被忽略（`validate_timeline` 会给出提示）。
    """

    type: str = Field(default="none", max_length=32)
    duration_ms: int = Field(default=300, ge=0, le=10_000)


class ShotScript(BaseModel):
    shot_no: int = Field(ge=1)
    duration_ms: int
    scene_no: int = Field(default=1, ge=1)
    characters: list[str] = Field(default_factory=list)
    action: str = Field(min_length=1, max_length=1000)
    camera_movement: CameraMovement = "固定"
    composition: Composition = "中景"
    emotion: str = Field(default="", max_length=120)
    dialogue: list[DialogueLine] = Field(default_factory=list)
    narration: str = Field(default="", max_length=1000)
    sfx: list[str] = Field(default_factory=list)
    image_prompt: str = Field(min_length=1, max_length=2000)
    negative_prompt: str = Field(default="", max_length=1000)
    video_params: VideoParams = Field(default_factory=VideoParams)
    #: FR-061 转场（从上一镜切入本镜）；`none` 表示硬切
    transition: TransitionSpec | None = None
    required: bool = True

    @field_validator("camera_movement", mode="before")
    @classmethod
    def _normalize_camera(cls, v: Any) -> Any:
        """把「推镜头」「pan」「固定机位」等变体归一成规范枚举。

        真实 LLM 不会严格照抄枚举值；严格校验会让整条剧本生成失败。
        """
        return normalize_enum(v, CAMERA_MOVEMENTS, CAMERA_ALIASES, "固定")

    @field_validator("composition", mode="before")
    @classmethod
    def _normalize_composition(cls, v: Any) -> Any:
        return normalize_enum(v, COMPOSITIONS, COMPOSITION_ALIASES, "中景")

    @field_validator("duration_ms")
    @classmethod
    def _check_duration(cls, v: int) -> int:
        if v < settings.shot_duration_ms_min or v > settings.shot_duration_ms_max:
            raise ValueError(
                f"时长必须在 {settings.shot_duration_ms_min}~{settings.shot_duration_ms_max} ms 之间"
            )
        return v

    @model_validator(mode="after")
    def _sync_video_params(self) -> "ShotScript":
        if self.video_params.duration_ms != self.duration_ms:
            self.video_params.duration_ms = self.duration_ms
        if self.video_params.camera_movement != self.camera_movement:
            self.video_params.camera_movement = self.camera_movement
        return self


class ScriptModel(BaseModel):
    story_bible: StoryBible
    beat_sheet: list[BeatItem] = Field(default_factory=list)
    shots: list[ShotScript]

    @model_validator(mode="after")
    def _check_shots(self) -> "ScriptModel":
        if not self.shots:
            raise ValueError("分镜列表不能为空")
        nos = [s.shot_no for s in self.shots]
        if len(set(nos)) != len(nos):
            dup = sorted({n for n in nos if nos.count(n) > 1})
            raise ValueError(f"镜号重复：{dup}")
        if nos != sorted(nos):
            raise ValueError("分镜必须按镜号升序排列")
        total = sum(s.duration_ms for s in self.shots)
        if total > settings.episode_duration_ms_max:
            raise ValueError(
                f"全片总时长 {total} ms 超过上限 {settings.episode_duration_ms_max} ms"
            )
        names = {c.name for c in self.story_bible.characters}
        for shot in self.shots:
            # 真实 LLM 常把角色名写成「林晚（女主）」「记者林晚」，或干脆
            # 引用了人物表里没登记的人。以前这里直接抛错，会让整条剧本生成失败。
            # 现在做宽容匹配：能对上就纠正成规范名，对不上就丢弃该引用。
            resolved: list[str] = []
            for raw_name in shot.characters:
                name = str(raw_name).strip()
                if not name:
                    continue
                matched = name if name in names else _match_known_name(name, names)
                if matched and matched not in resolved:
                    resolved.append(matched)
            shot.characters = resolved
        return self

    @property
    def total_duration_ms(self) -> int:
        return sum(s.duration_ms for s in self.shots)


# --------------------------------------------------------------------------- #
# 项目 / 剧集
# --------------------------------------------------------------------------- #

class StyleIn(BaseModel):
    name: str = Field(default="默认画风", max_length=120)
    description: str = ""
    palette: list[str] = Field(default_factory=list)
    camera_style: str = ""
    lighting_style: str = ""
    negative_prompt: str = ""


class StyleUpdate(BaseModel):
    """画风局部更新（PATCH）。

    必须单独一个模型：`StyleIn` 是**创建**用的，字段都带非 None 默认值，
    直接拿它当 PATCH body，会让「只改描述」把 name 重置成「默认画风」、palette 清空 ——
    `update_style` 的判空是 `value is not None`，而这些默认值恰好不是 None，于是全被覆盖。
    """

    name: str | None = Field(default=None, max_length=120)
    description: str | None = None
    palette: list[str] | None = None
    camera_style: str | None = None
    lighting_style: str | None = None
    negative_prompt: str | None = None


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    genre: str = Field(default="都市", max_length=80)
    aspect_ratio: str = Field(default="9:16", max_length=16)
    default_language: str = Field(default="zh-CN", max_length=16)
    #: 画风既接受结构化的 StyleIn，也接受创建表单里的一行画风备注（纯字符串）。
    style: StyleIn | None = None

    @field_validator("style", mode="before")
    @classmethod
    def _coerce_style(cls, v: Any) -> Any:
        """前端「画风备注（可选）」是自由文本，这里转成 StyleIn，避免带画风创建直接 422。"""
        if isinstance(v, str):
            text = v.strip()
            if not text:
                return None
            return {"name": text[:120], "description": text}
        return v


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    genre: str | None = None
    aspect_ratio: str | None = None
    default_language: str | None = None
    default_style_id: str | None = None
    #: 统一语速：仅影响之后新建/同步出来的角色；已有角色要显式「应用到全部角色」
    default_voice_speed: float | None = Field(default=None, ge=0.5, le=2.0)


class VoiceSpeedIn(BaseModel):
    """把统一语速应用到项目下全部角色（本地生效，不调用模型）。"""

    speed: float = Field(ge=0.5, le=2.0)


class ProjectOut(ORMModel):
    id: str
    name: str
    genre: str
    aspect_ratio: str
    default_language: str
    default_style_id: str | None
    status: str
    created_at: dt.datetime
    updated_at: dt.datetime


class EpisodeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    episode_no: int | None = Field(default=None, ge=1)
    target_duration_ms: int = Field(default=60_000, ge=5_000)


class EpisodeUpdate(BaseModel):
    title: str | None = None
    target_duration_ms: int | None = Field(default=None, ge=5_000)


class EpisodeCopy(BaseModel):
    title: str | None = None


class EpisodeOut(ORMModel):
    id: str
    project_id: str
    title: str
    episode_no: int
    target_duration_ms: int
    stage: str
    version: int
    status: str
    blocked_reason: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


# --------------------------------------------------------------------------- #
# 故事 / 剧本
# --------------------------------------------------------------------------- #

class StoryInput(BaseModel):
    input_type: Literal["idea", "text", "manual"] = "idea"
    source_text: str = Field(default="", max_length=20000)
    #: FR-010：预估/生成剧本时的目标分镜数；不传则用项目默认值
    shot_count: int | None = Field(default=None, ge=1, le=60)

    @field_validator("source_text")
    @classmethod
    def _check_len(cls, v: str) -> str:
        if len(v) > settings.story_text_max_chars:
            raise ValueError(f"故事文本不得超过 {settings.story_text_max_chars} 字")
        return v


class ScriptSave(BaseModel):
    story_json: dict[str, Any]
    base_version: int | None = None
    note: str = ""


class ScriptValidate(BaseModel):
    story_json: dict[str, Any]


class ShotRegenerate(BaseModel):
    shot_nos: list[int] = Field(min_length=1)
    replan_downstream: bool = False
    instruction: str = ""


class ShotSuggestIn(BaseModel):
    """单镜「按大纲生成」的补充要求（可选）。"""

    instruction: str = Field(default="", max_length=300)


class BeatRegenerate(BaseModel):
    """FR-014：按剧情节拍局部重生成。

    节拍与分镜的对应关系来自剧本 `beat_sheet[].shots`；服务端据此换算成
    镜号列表，复用已验证的「分镜局部重生成」链路，不引入第二条生成路径。
    """

    beat_nos: list[int] = Field(min_length=1)
    replan_downstream: bool = False
    instruction: str = ""


# --------------------------------------------------------------------------- #
# 角色 / 画风
# --------------------------------------------------------------------------- #

class CharacterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    identity: str = ""
    age_range: str = ""
    appearance: str = ""
    outfit: str = ""
    personality: str = ""
    speech_style: str = ""
    forbidden_changes: list[str] = Field(default_factory=list)
    is_main: bool = True
    #: 归属剧集；为空表示项目级公共角色（所有剧集可见）
    episode_id: str | None = None


class CharacterUpdate(BaseModel):
    identity: str | None = None
    age_range: str | None = None
    appearance: str | None = None
    outfit: str | None = None
    personality: str | None = None
    speech_style: str | None = None
    forbidden_changes: list[str] | None = None
    is_main: bool | None = None
    voice_id: str | None = None
    # FR-051：语速倍率（0.5~2.0）
    voice_speed: float | None = Field(default=None, ge=0.5, le=2.0)


class CharacterGenerate(BaseModel):
    asset_kinds: list[AssetKind] = Field(default_factory=lambda: ["setting", "front"])
    count_per_kind: int = Field(default=2, ge=1, le=4)


class CharacterConfirm(BaseModel):
    media_id: str


class CharacterReplacePrimary(BaseModel):
    media_id: str
    impact: Literal["future_only", "mark_stale"] = "future_only"


# --------------------------------------------------------------------------- #
# 分镜 / 图像
# --------------------------------------------------------------------------- #

class ShotUpdate(BaseModel):
    duration_ms: int | None = None
    action: str | None = None
    camera_movement: CameraMovement | None = None
    composition: Composition | None = None
    emotion: str | None = None
    dialogue: list[DialogueLine] | None = None
    narration: str | None = None
    sfx: list[str] | None = None
    image_prompt: str | None = None
    negative_prompt: str | None = None
    characters: list[str] | None = None
    required: bool | None = None
    #: FR-023 用户显式确认「本镜无角色」后，引用完整性检查不再要求角色参考图
    no_character: bool | None = None
    #: FR-061 转场；传 `{"type": "none"}` 或 null 表示清除
    transition: TransitionSpec | None = None


class GenerateImages(BaseModel):
    shot_nos: list[int] | None = None
    candidates_per_shot: int = Field(default=settings.candidates_per_shot_default, ge=1, le=4)

    @field_validator("candidates_per_shot")
    @classmethod
    def _check(cls, v: int) -> int:
        if v > settings.candidates_per_shot_max:
            raise ValueError(f"单镜候选图最多 {settings.candidates_per_shot_max} 张")
        return v


class SelectCandidate(BaseModel):
    media_id: str


class RejectCandidate(BaseModel):
    media_id: str
    reason: str = ""


class ConfirmBatch(BaseModel):
    shot_nos: list[int] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# 视频
# --------------------------------------------------------------------------- #

class VideoEstimateIn(BaseModel):
    shot_nos: list[int] | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class VideoGenerateIn(BaseModel):
    shot_nos: list[int] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    quote_id: str
    idempotency_key: str | None = None
    confirm: bool = True


class VideoConfirmIn(BaseModel):
    media_id: str


class VideoFetchIn(BaseModel):
    """按供应商任务号补拉已生成的片段（不产生新费用）。"""

    task_id: str = Field(min_length=6, max_length=96)


class VideoSkipIn(BaseModel):
    reason: str = ""
    fallback: Literal["static_image", "blank"] = "static_image"


# --------------------------------------------------------------------------- #
# 音频 / 字幕
# --------------------------------------------------------------------------- #

class VoiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: str = "mock"
    provider_voice_id: str = ""
    gender: str = "female"
    description: str = ""


class VoiceBind(BaseModel):
    voice_id: str
    # FR-051：语速倍率，可选；不给则保持角色当前值
    speed: float | None = Field(default=None, ge=0.5, le=2.0)


class TTSGenerate(BaseModel):
    shot_nos: list[int] | None = None
    character_id: str | None = None


class TrackSpeedIn(BaseModel):
    """调整**已生成**音频的语速（本地变速，不调用模型）。"""

    speed: float = Field(ge=0.5, le=2.0)


class ManualAudioIn(BaseModel):
    """§12-4：TTS 失败时用上传音频顶替某句台词。"""

    shot_no: int
    #: 该镜内第几句台词（从 0 开始）
    line_index: int = Field(default=0, ge=0)
    media_id: str


class SkipDialogueIn(BaseModel):
    """§12-4：跳过配音后继续预览。

    `line_index` 给定时只跳过该镜的第几句台词（逐行「跳过」按钮），
    否则跳过这些镜头的全部台词（整段跳过）。
    """

    shot_nos: list[int] = Field(min_length=1)
    reason: str = ""
    line_index: int | None = Field(default=None, ge=0)


class AppealCreateIn(BaseModel):
    """§12-8：内容拦截申诉。"""

    target_type: Literal["asset", "input", "episode"] = "asset"
    target_id: str | None = None
    reason: str = Field(min_length=1, max_length=1000)
    detail: str = Field(default="", max_length=4000)


class AppealResolveIn(BaseModel):
    """§12-8：人工复核结论。"""

    status: Literal["PENDING", "APPROVED", "REJECTED"]
    resolution_note: str = Field(default="", max_length=2000)


class BGMGenerateIn(BaseModel):
    """氛围音 / BGM 生成：描述写场景，按生成时长（秒）计费。

    - 不传镜头：整集垫乐（kind=bgm），从 0 开始铺；
    - 传 `shot_no`：只覆盖该镜头（kind=ambience）；
    - 传 `shot_from` + `shot_to`：覆盖这段镜头（一段垫乐多个镜头共用），
      起点取首镜、长度铺到末镜结束。

    指定镜头时 `seconds` 会被忽略（按镜头实际长度裁剪）。
    """

    prompt: str = Field(min_length=2, max_length=1000)
    seconds: int = Field(default=30, ge=3, le=120)
    shot_no: int | None = Field(default=None, ge=1)
    shot_from: int | None = Field(default=None, ge=1)
    shot_to: int | None = Field(default=None, ge=1)
    volume_db: float = Field(default=-14.0, ge=-60, le=0)
    fade_in_ms: int = Field(default=800, ge=0, le=10000)
    fade_out_ms: int = Field(default=1500, ge=0, le=10000)


class TrackCreate(BaseModel):
    media_id: str
    kind: Literal["bgm", "ambience", "sfx"] = "bgm"
    start_ms: int = Field(default=0, ge=0)
    end_ms: int = Field(default=0, ge=0)
    volume_db: float = 0.0
    fade_in_ms: int = Field(default=0, ge=0)
    fade_out_ms: int = Field(default=0, ge=0)
    rights_declared: bool = False


# --------------------------------------------------------------------------- #
# 工作流
# --------------------------------------------------------------------------- #

class WorkflowStart(BaseModel):
    auto_run: bool = True
    #: 已有剧本时是否直接复用、不再调用模型生成。
    #: `None`（默认）表示「有剧本就复用」——用户在「故事与剧本」页已经生成过
    #: 剧本（可能还手改过）时，启动工作流不该再花一次钱把它重新生成一遍。
    reuse_script: bool | None = None


class WorkflowResume(BaseModel):
    workflow_run_id: str
    checkpoint_id: str | None = None
    gate: GateName
    decision: Literal["approve", "reject", "edit", "skip"] = "approve"
    payload: dict[str, Any] = Field(default_factory=dict)
    target_version: int | None = None


class WorkflowRetry(BaseModel):
    node: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowRollback(BaseModel):
    to_node: str


# --------------------------------------------------------------------------- #
# 任务 / 成本 / 导出 / 设置
# --------------------------------------------------------------------------- #

class JobOut(ORMModel):
    id: str
    workflow_run_id: str | None
    episode_id: str | None
    project_id: str | None
    type: str
    status: str
    stage: str
    progress: int
    attempt: int
    max_attempts: int
    idempotency_key: str
    input_summary: dict[str, Any]
    provider: str
    model: str
    cost_reserved_cents: int
    cost_settled_cents: int
    error_code: str | None
    error_message: str | None
    error_detail: dict[str, Any]
    parent_job_id: str | None
    result_ref: dict[str, Any]
    cancel_requested: bool
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    duration_ms: int | None


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int
    page: int
    page_size: int


class RenderPreviewIn(BaseModel):
    watermark: bool = True
    #: 部分预览：只合成「已就绪」的镜头（有画面/视频），先看单镜成片效果；
    #: 未就绪镜头从时间线剔除，字幕/BGM 按新起点重映射。正式导出仍必须完整。
    partial: bool = False


class RenderExportIn(BaseModel):
    #: `720p` / `1080p` 是画幅无关的档位（尺寸跟随项目画幅比例）；
    #: `*_9x16` 为历史 id，仍接受，但尺寸同样跟随项目比例，避免横屏项目被强行出竖屏。
    preset: Literal["720p_9x16", "1080p_9x16", "720p", "1080p"] = "720p_9x16"
    watermark: bool = False


class ProviderConfigIn(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    # FR-090：备用供应商（留空表示不配置备用）
    fallback_provider: str = ""
    fallback_model: str = ""
    fallback_base_url: str = ""
    fallback_api_key: str | None = None


class ProvidersUpdateIn(BaseModel):
    text: ProviderConfigIn | None = None
    image: ProviderConfigIn | None = None
    video: ProviderConfigIn | None = None
    speech: ProviderConfigIn | None = None


class SafetyCheckIn(BaseModel):
    text: str
    scope: str = "input"


class QuotaAdjustIn(BaseModel):
    """§11.1-3：配额调整入参。`limit_cents` 为调整后的配额上限（单位：分）。"""

    limit_cents: int = Field(ge=0)
    note: str = ""


class MediaRightsIn(BaseModel):
    rights_declared: bool
    source_note: str = ""
