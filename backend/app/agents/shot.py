"""ShotAgent：拆解镜头、生成提示词与生成参数（FR-012/030/031）。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import CharacterAsset, Episode, EpisodeStage, Shot, StyleAsset
from app.services import safety

#: 参与「分镜是否真的变了」比较的字段（FR-004/FR-014）。
#: 未选中的分镜如果这些字段没变，就不应该被算作一个新版本。
_SHOT_COMPARE_FIELDS = (
    "action", "image_prompt", "negative_prompt", "camera_movement",
    "composition", "duration_ms", "dialogue", "characters", "required",
)


def shot_fingerprint(shot_json: dict[str, Any] | None) -> str:
    """分镜内容的稳定指纹，用于判断两次同步之间是否真的发生变化。"""
    payload = {
        field: (shot_json or {}).get(field)
        for field in _SHOT_COMPARE_FIELDS
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def build_image_prompt(
    shot: Shot,
    *,
    style: StyleAsset | None,
    characters: list[CharacterAsset],
    aspect_ratio: str | None = None,
) -> tuple[str, str]:
    """把画风与角色一致性信息自动注入分镜提示词（FR-022/FR-023）。"""
    data = shot.shot_json or {}
    parts: list[str] = []
    if style:
        parts += [style.description, style.camera_style, style.lighting_style]
        # FR-022：色板此前从未进入提示词（PRD 要求 5 项画风全部注入）
        palette = style.palette
        if isinstance(palette, dict):
            colors = [str(v).strip() for v in palette.values() if str(v or "").strip()]
            if colors:
                parts.append("配色：" + "、".join(colors))
        elif isinstance(palette, (list, tuple)):
            colors = [str(v).strip() for v in palette if str(v or "").strip()]
            if colors:
                parts.append("配色：" + "、".join(colors))
        elif isinstance(palette, str) and palette.strip():
            parts.append("配色：" + palette.strip())
    parts.append(f"{data.get('composition', '中景')}构图")
    parts.append(f"镜头运动：{data.get('camera_movement', '固定')}")
    parts.append(data.get("action", ""))
    parts.append(data.get("image_prompt", ""))
    for character in characters:
        spec = character.spec_json or {}
        anchor = "，".join(
            p for p in [spec.get("appearance", ""), spec.get("outfit", "")] if p
        )
        parts.append(f"{character.name}：{anchor}")
    parts.append("情绪：" + data.get("emotion", ""))
    # FR-040：构图提示随项目画幅比例走，不再写死竖屏
    ratio = (aspect_ratio or "").strip() or "9:16"
    orientation = "竖屏" if ratio == "9:16" else ("横屏" if ratio in {"16:9", "21:9"} else "方形" if ratio == "1:1" else "画面")
    parts.append(f"{orientation} {ratio}，电影感构图，画面无文字")

    negative = [data.get("negative_prompt", ""), "低清", "畸形手指", "多余肢体", "文字水印"]
    if style and style.negative_prompt:
        negative.append(style.negative_prompt)
    return safety.scrub_prompt("，".join(p for p in parts if p)), "，".join(
        p for p in negative if p
    )


def characters_for_shot(
    session: Session, shot: Shot, project_characters: dict[str, CharacterAsset]
) -> list[CharacterAsset]:
    names = list((shot.shot_json or {}).get("characters", []))
    return [project_characters[n] for n in names if n in project_characters]


def sync_shots_from_script(
    session: Session, episode: Episode, script: dict[str, Any]
) -> list[Shot]:
    """按剧本同步分镜表；已存在的镜号保留其生成状态与资产引用。"""
    existing = {
        s.shot_no: s
        for s in session.scalars(select(Shot).where(Shot.episode_id == episode.id))
    }
    result: list[Shot] = []
    seen: set[int] = set()
    for item in script.get("shots", []):
        no = int(item.get("shot_no", 0))
        if no <= 0 or no in seen:
            continue
        seen.add(no)
        shot = existing.get(no)
        if shot is None:
            shot = Shot(
                episode_id=episode.id,
                shot_no=no,
                duration_ms=int(item.get("duration_ms", 4000)),
                shot_json=item,
                required=bool(item.get("required", True)),
                stage=episode.stage,
                current_version=1,
            )
            session.add(shot)
            existing[no] = shot
        else:
            old = shot.shot_json or {}
            changed = shot_fingerprint(old) != shot_fingerprint(item)
            old_prompt = old.get("image_prompt")
            shot.shot_json = item
            shot.duration_ms = int(item.get("duration_ms", shot.duration_ms))
            shot.required = bool(item.get("required", shot.required))
            # FR-014：未选中的分镜内容没变就不该产生新版本（否则「局部重生成」
            # 会把整集分镜的版本号全部推高，下游无法分辨哪些真的被改过）。
            if changed:
                shot.current_version = int(shot.current_version or 1) + 1
                if old_prompt != item.get("image_prompt"):
                    mark_stale(shot, "分镜提示词已更新，需重新生成画面")
        result.append(shot)
    for no, shot in existing.items():
        if no not in seen:
            shot.required = False
            shot.stale = True
            shot.stale_reason = "剧本已移除该镜号"
    session.flush()
    return result


def mark_stale(shot: Shot, reason: str) -> None:
    shot.stale = True
    shot.stale_reason = reason


def clear_stale(shot: Shot) -> None:
    shot.stale = False
    shot.stale_reason = None


def stale_shot_count(session: Session, episode_id: str) -> int:
    """本集被标记为 STALE 的镜头数（§10.1：上游变更只标记，不强制删除）。"""
    return int(
        session.scalar(
            select(func.count(Shot.id)).where(
                Shot.episode_id == episode_id, Shot.stale.is_(True)
            )
        )
        or 0
    )


def sync_episode_stale_stage(session: Session, episode: Episode) -> bool:
    """§10.1：有受影响资源时把剧集阶段标记为 `STALE`，由用户决定是否重新生成。

    只在「已完成过的阶段」上生效（DRAFT / BLOCKED / STALE 不动），
    避免把还在初始状态的剧集也标脏。返回是否发生了阶段变更。
    """
    if episode is None:
        return False
    if episode.stage in (
        EpisodeStage.DRAFT.value,
        EpisodeStage.BLOCKED.value,
        EpisodeStage.STALE.value,
    ):
        return False
    if stale_shot_count(session, episode.id) <= 0:
        return False
    episode.stage = EpisodeStage.STALE.value
    session.flush()
    return True


def update_shot(session: Session, shot: Shot, patch: dict[str, Any]) -> Shot:
    data = dict(shot.shot_json or {})
    # FR-080：台词与提示词的入库路径此前完全没有安全检查，这里补齐。
    # 必须在写入 `data` **之前**执行，否则脏内容会先落库再被拒。
    safety.ensure_fields_allowed(
        {k: v for k, v in patch.items() if v is not None},
        scope="shot",
        message="台词或提示词未通过内容安全检查，请按建议修改后重试",
    )
    changed_prompt = False
    for field in (
        "action", "camera_movement", "composition", "emotion", "dialogue",
        "narration", "sfx", "image_prompt", "negative_prompt", "characters",
    ):
        if field in patch and patch[field] is not None:
            value = patch[field]
            if field == "dialogue" and value:
                value = [
                    d.model_dump() if hasattr(d, "model_dump") else d for d in value
                ]
            if field in {"image_prompt", "action", "composition", "characters"}:
                changed_prompt = changed_prompt or data.get(field) != value
            data[field] = value
    if patch.get("duration_ms"):
        data["duration_ms"] = int(patch["duration_ms"])
        shot.duration_ms = int(patch["duration_ms"])
        vp = dict(data.get("video_params") or {})
        vp["duration_ms"] = int(patch["duration_ms"])
        data["video_params"] = vp
    if patch.get("required") is not None:
        shot.required = bool(patch["required"])
        data["required"] = shot.required
    # FR-023：「本镜无角色」是用户的显式确认，用于放行引用完整性检查
    if patch.get("no_character") is not None:
        data["no_character"] = bool(patch["no_character"])
    # FR-061：转场只影响合成，不改变画面内容，因此单独处理——
    # 既不参与 `changed_prompt`（避免误标 stale），`none` 也允许清除设置。
    if "transition" in patch:
        raw = patch["transition"]
        if isinstance(raw, dict) and hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        if raw in (None, "", "none", {}, []):
            data.pop("transition", None)
        else:
            data["transition"] = raw
    shot.shot_json = data
    # 只有「改转场」这一种修改不产生新版本：它不影响任何画面/音频资产，
    # 抬高版本号会让下游把已确认的素材误判为过期。
    only_transition = set(patch) <= {"transition"}
    if not only_transition:
        shot.current_version = int(shot.current_version or 1) + 1
    if changed_prompt:
        mark_stale(shot, "分镜内容已修改，画面可能过期")
    session.flush()
    return shot


def to_dict(session: Session, shot: Shot, *, candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    data = dict(shot.shot_json or {})
    return {
        "id": shot.id,
        "episode_id": shot.episode_id,
        "shot_no": shot.shot_no,
        "duration_ms": shot.duration_ms,
        "shot_json": data,
        "stage": shot.stage,
        "current_version": shot.current_version,
        "selected_media_id": shot.selected_media_id,
        "confirmed_media_id": shot.confirmed_media_id,
        "confirmed_at": shot.confirmed_at,
        "image_status": shot.image_status,
        "video_status": shot.video_status,
        "video_media_id": shot.video_media_id,
        "audio_status": shot.audio_status,
        "stale": shot.stale,
        "stale_reason": shot.stale_reason,
        "required": shot.required,
        "candidates": candidates or [],
    }
