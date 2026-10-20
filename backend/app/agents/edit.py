"""EditAgent：时间线、混音、字幕、合成与素材包（FR-MVP-060~064）。"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import NotFound, RenderError
from app.db.models import (
    AssetStatus,
    AudioTrack,
    CharacterAsset,
    Episode,
    ExportRecord,
    MediaAsset,
    Project,
    Shot,
    StoryDocument,
    StyleAsset,
    SubtitleCue,
)
from app.services import media as media_service
from app.services import safety

RESOLUTIONS = {
    "preview": (540, 960),
    "720p_9x16": (720, 1280),
    "1080p_9x16": (1080, 1920),
}

#: 画幅比例 → (宽, 高) 的比值。与 `video_agent.ASPECT_PRESETS` 保持一致。
ASPECT_VALUES: dict[str, tuple[int, int]] = {
    "9:16": (9, 16),
    "16:9": (16, 9),
    "1:1": (1, 1),
    "4:3": (4, 3),
    "3:4": (3, 4),
    "21:9": (21, 9),
}

#: 画质档位 → 短边像素。`720p_9x16` 这类旧 id 只当档位看，真实尺寸由项目画幅比例决定。
QUALITY_SHORT_SIDE: dict[str, int] = {"preview": 540, "720p": 720, "1080p": 1080}

#: 旧 preset id → 画质档位（历史数据与前端枚举都还在用这些 id）
PRESET_TO_TIER: dict[str, str] = {
    "720p_9x16": "720p",
    "1080p_9x16": "1080p",
    "720p": "720p",
    "1080p": "1080p",
}


def _even(value: float) -> int:
    """H.264 要求宽高为偶数，否则 ffmpeg 直接报错。"""
    rounded = int(round(value))
    return rounded - (rounded % 2)


def _aspect_value(aspect_ratio: str | None) -> tuple[int, int] | None:
    """把 "16:9" 解析成 (16, 9)；非法或缺失返回 None。"""
    if not aspect_ratio:
        return None
    return ASPECT_VALUES.get(str(aspect_ratio).strip())


def resolution_for(aspect_ratio: str | None, preset: str = "720p") -> tuple[int, int]:
    """FR-060/FR-063：按**项目画幅比例**与画质档位算出输出分辨率。

    此前无论项目设置成什么比例，合成都固定输出 720×1280 竖屏；
    而 FR-040 已经让出图出片跟随项目比例，合成却不变，会造成画面被拉伸。
    """
    short = QUALITY_SHORT_SIDE.get(preset, 720)
    ratio_w, ratio_h = ASPECT_VALUES.get((aspect_ratio or "").strip(), (9, 16))
    if ratio_w <= ratio_h:
        # 竖屏或方形：宽是短边
        return short, _even(short * ratio_h / ratio_w)
    # 横屏：高是短边
    return _even(short * ratio_w / ratio_h), short


def preset_dimensions(preset: str, aspect_ratio: str | None = None) -> tuple[int, int]:
    """兼容入口：旧 preset id 也能拿到正确尺寸（尺寸跟随 `aspect_ratio`）。"""
    tier = PRESET_TO_TIER.get(preset)
    if tier is None:
        tier = "preview" if preset == "preview" else "720p"
    return resolution_for(aspect_ratio, tier)

#: FR-061 转场白名单：`id` 同时是镜头剧本里的取值，`xfade` 是 ffmpeg 的 transition 名。
#: 只暴露在 ffmpeg 7.x 上稳定的那一批，避免用户选了某些版本才有的效果。
TRANSITION_TYPES: dict[str, dict[str, str]] = {
    "none": {"label": "无（硬切）", "xfade": ""},
    "fade": {"label": "淡入淡出", "xfade": "fade"},
    "fadeblack": {"label": "黑场过渡", "xfade": "fadeblack"},
    "fadewhite": {"label": "白场过渡", "xfade": "fadewhite"},
    "dissolve": {"label": "叠化", "xfade": "dissolve"},
    "wipeleft": {"label": "左擦除", "xfade": "wipeleft"},
    "wiperight": {"label": "右擦除", "xfade": "wiperight"},
    "slideleft": {"label": "左滑入", "xfade": "slideleft"},
    "slideright": {"label": "右滑入", "xfade": "slideright"},
    "circleopen": {"label": "圆形展开", "xfade": "circleopen"},
    "hblur": {"label": "模糊过渡", "xfade": "hblur"},
    "pixelize": {"label": "像素化", "xfade": "pixelize"},
}

DEFAULT_TRANSITION_TYPE = "fade"


def transition_options() -> list[dict[str, str]]:
    """给前端下拉用的转场清单（顺序即展示顺序）。"""
    return [
        {"id": key, "label": value["label"]}
        for key, value in TRANSITION_TYPES.items()
        if key != "none"
    ]


def parse_transition(raw: Any) -> dict[str, Any] | None:
    """把镜头剧本里的 `transition` 归一成 `{type, duration_ms, xfade}`。

    容忍三种写法：`"fade"`、`{"type": "fade", "duration_ms": 300}`、`None`。
    `none` / 空值一律视为「无转场」，返回 None。
    """
    if raw in (None, "", "none", {}, []):
        return None
    if isinstance(raw, str):
        type_id = raw.strip()
        duration_ms = settings.transition_default_ms
    elif isinstance(raw, dict):
        type_id = str(raw.get("type") or raw.get("name") or "").strip()
        duration_ms = raw.get("duration_ms")
        try:
            duration_ms = int(duration_ms) if duration_ms is not None else settings.transition_default_ms
        except (TypeError, ValueError):
            duration_ms = settings.transition_default_ms
    else:
        return None
    if not type_id or type_id == "none":
        return None
    spec = TRANSITION_TYPES.get(type_id)
    if spec is None or not spec["xfade"]:
        # 未知类型原样带出，交由 validate_timeline 报可定位的错误
        return {"type": type_id, "duration_ms": duration_ms, "xfade": "", "known": False}
    return {"type": type_id, "duration_ms": duration_ms, "xfade": spec["xfade"], "known": True}


def _shot_transition(shot: Shot) -> dict[str, Any] | None:
    return parse_transition((shot.shot_json or {}).get("transition"))


def estimate_render(session: Session, episode: Episode, *, kind: str = "preview") -> dict[str, Any]:
    """合成成本预估（FR-010 / §12-6）。

    合成本身不调用外部供应商，但占用算力配额：按输出时长（秒）× 单价计价，
    这样「配额不足时阻断新任务」对预览与正式导出同样成立。
    """
    import math

    timeline = build_timeline(session, episode)
    seconds = max(timeline["total_duration_ms"], 0) / 1000.0
    unit = int(settings.render_price_per_second_cents)
    quantity = math.ceil(seconds) if seconds else 0
    total = max(0, quantity * unit)
    from app.services import cost as cost_service

    snap = cost_service.quota_snapshot(session)
    return {
        "provider": "local",
        "model": "ffmpeg",
        "kind": kind,
        "estimate_cents": total,
        "quota_after_cents": snap["remaining_cents"] - total,
        "remaining_cents": snap["remaining_cents"],
        "duration_ms": timeline["total_duration_ms"],
        "billing_unit": "second",
        "unit_price_cents": unit,
        "transition_count": sum(
            1 for e in timeline["tracks"]["video"] if e.get("transition")
        ),
    }


def _ordered_shots(session: Session, episode_id: str) -> list[Shot]:
    return list(
        session.scalars(
            select(Shot).where(Shot.episode_id == episode_id).order_by(Shot.shot_no)
        )
    )


def build_timeline(session: Session, episode: Episode) -> dict[str, Any]:
    project = session.get(Project, episode.project_id)
    shots = _ordered_shots(session, episode.id)
    video_track: list[dict[str, Any]] = []
    cursor = 0
    for index, shot in enumerate(s for s in shots if s.required):
        # FR-061：转场表示「从上一镜切入本镜」的时长，会与上一镜重叠，
        # 因此本镜起点前移，全片总时长相应缩短。音轨起点全部以本镜 start_ms 为基准，
        # 所以音画在加入转场后依然对齐。
        transition = _shot_transition(shot) if index > 0 else None
        overlap = int(transition["duration_ms"]) if transition else 0
        start = max(0, cursor - overlap)
        entry: dict[str, Any] = {
            "shot_id": shot.id,
            "shot_no": shot.shot_no,
            "start_ms": start,
            "duration_ms": shot.duration_ms,
            "end_ms": start + shot.duration_ms,
            "kind": "video",
            "media_id": None,
            "skipped": False,
            "transition": transition,
            "transition_ms": overlap,
        }
        if shot.video_status == "CONFIRMED" and shot.video_media_id:
            entry["media_id"] = shot.video_media_id
        elif shot.confirmed_media_id:
            entry["kind"] = "image"
            entry["media_id"] = shot.confirmed_media_id
        else:
            entry["kind"] = "placeholder"
        if shot.video_status == "SKIPPED":
            entry["skipped"] = True
            entry["kind"] = "image" if shot.confirmed_media_id else "placeholder"
        video_track.append(entry)
        cursor = entry["end_ms"]

    dialogue: list[dict[str, Any]] = []
    for track in session.scalars(
        select(AudioTrack)
        .where(AudioTrack.episode_id == episode.id, AudioTrack.kind == "dialogue")
        .order_by(AudioTrack.start_ms)
    ):
        shot = session.get(Shot, track.shot_id) if track.shot_id else None
        base = 0
        if shot is not None:
            for entry in video_track:
                if entry["shot_id"] == shot.id:
                    base = entry["start_ms"]
                    break
        asset = session.get(MediaAsset, track.media_id) if track.media_id else None
        duration = (asset.duration_ms if asset and asset.duration_ms else (track.end_ms - track.start_ms))
        dialogue.append(
            {
                "track_id": track.id,
                "shot_id": track.shot_id,
                "shot_no": shot.shot_no if shot else None,
                "media_id": track.media_id,
                "character_id": track.character_id,
                "text": track.text,
                "start_ms": base + track.start_ms,
                "end_ms": base + track.start_ms + max(duration, 0),
                "stale": track.stale,
            }
        )

    bgm: list[dict[str, Any]] = []
    for track in session.scalars(
        select(AudioTrack).where(
            AudioTrack.episode_id == episode.id, AudioTrack.kind.in_(["bgm", "ambience", "sfx"])
        )
    ):
        bgm.append(
            {
                "track_id": track.id,
                "kind": track.kind,
                "media_id": track.media_id,
                "start_ms": track.start_ms,
                "end_ms": track.end_ms,
                "volume_db": track.volume_db,
                "fade_in_ms": track.fade_in_ms,
                "fade_out_ms": track.fade_out_ms,
                "rights_declared": track.rights_declared,
            }
        )

    subtitles = [
        {
            "index": cue.index_no,
            "start_ms": cue.start_ms,
            "end_ms": cue.end_ms,
            "text": cue.text,
            "speaker": cue.speaker,
            "edited": cue.edited,
        }
        for cue in session.scalars(
            select(SubtitleCue)
            .where(SubtitleCue.episode_id == episode.id)
            .order_by(SubtitleCue.index_no)
        )
    ]

    # FR-060/FR-063：时间线尺寸跟随项目画幅比例，不再写死竖屏 720×1280
    width, height = resolution_for(project.aspect_ratio if project else None, "720p")
    return {
        "episode_id": episode.id,
        "project_id": episode.project_id,
        "aspect_ratio": project.aspect_ratio if project else "9:16",
        "width": width,
        "height": height,
        "total_duration_ms": cursor,
        "target_duration_ms": episode.target_duration_ms,
        "tracks": {
            "video": video_track,
            "dialogue": dialogue,
            "bgm": bgm,
            "subtitles": subtitles,
        },
    }


def validate_timeline(session: Session, episode: Episode) -> dict[str, Any]:
    """合成前校验，错误可定位到镜号与音轨（FR-MVP-060）。"""
    timeline = build_timeline(session, episode)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not timeline["tracks"]["video"]:
        errors.append({"code": "NO_SHOTS", "message": "时间线为空，没有可合成的分镜", "shot_no": None, "track": "video"})

    # FR-060：镜头顺序校验。镜号必须唯一且为正，否则「镜 N」定位会指到两个镜头。
    all_shots = _ordered_shots(session, episode.id)
    seen: dict[int, int] = {}
    for shot in all_shots:
        seen[shot.shot_no] = seen.get(shot.shot_no, 0) + 1
    for shot_no, count in sorted(seen.items()):
        if shot_no <= 0:
            errors.append({
                "code": "INVALID_SHOT_NO", "shot_no": shot_no,
                "message": f"镜号非法（{shot_no}），必须为正整数", "track": "video",
            })
        elif count > 1:
            errors.append({
                "code": "DUPLICATE_SHOT_NO", "shot_no": shot_no,
                "message": f"镜号 {shot_no} 重复出现 {count} 次，无法确定合成顺序", "track": "video",
            })
    expected = 1
    for shot_no in sorted(seen):
        if shot_no > 0 and shot_no != expected:
            warnings.append({
                "code": "SHOT_NO_GAP", "shot_no": shot_no,
                "message": f"镜号不连续：缺少镜 {expected}", "track": "video",
            })
            expected = shot_no
        expected += 1

    # FR-060：画幅一致性校验。若项目画幅比例在出图/出片之后被改过，
    # 源素材比例会与时间线不一致，合成时会被拉伸变形——必须提前报出来。
    timeline_ratio = _aspect_value(timeline.get("aspect_ratio"))
    if timeline_ratio is not None:
        expected_ratio = timeline_ratio[0] / timeline_ratio[1]
        for entry in timeline["tracks"]["video"]:
            if not entry["media_id"]:
                continue
            asset = session.get(MediaAsset, entry["media_id"])
            if asset is None or not asset.width or not asset.height:
                continue
            actual_ratio = asset.width / asset.height
            # 允许 2% 偏差（供应商返回的尺寸可能不是精确比例）
            if abs(actual_ratio - expected_ratio) / expected_ratio > 0.02:
                warnings.append({
                    "code": "ASPECT_MISMATCH", "shot_no": entry["shot_no"], "track": "video",
                    "message": (
                        f"镜 {entry['shot_no']} 的素材比例 {asset.width}×{asset.height} 与项目画幅 "
                        f"{timeline.get('aspect_ratio')} 不一致，合成后可能被拉伸；"
                        "建议按当前画幅重新生成"
                    ),
                })

    for index, entry in enumerate(timeline["tracks"]["video"]):
        if entry["duration_ms"] <= 0:
            errors.append({
                "code": "INVALID_DURATION", "shot_no": entry["shot_no"],
                "message": f"镜 {entry['shot_no']} 时长非法（{entry['duration_ms']}ms）", "track": "video",
            })
        if entry["kind"] == "placeholder":
            errors.append({
                "code": "MISSING_FRAME", "shot_no": entry["shot_no"],
                "message": f"镜 {entry['shot_no']} 既无确认画面也无视频，无法合成", "track": "video",
            })
        elif entry["kind"] == "image":
            warnings.append({
                "code": "STATIC_IMAGE", "shot_no": entry["shot_no"],
                "message": f"镜 {entry['shot_no']} 使用静态图（未生成视频或已跳过）", "track": "video",
            })
        if entry["media_id"]:
            asset = session.get(MediaAsset, entry["media_id"])
            if asset is None or asset.status == AssetStatus.REJECTED.value:
                errors.append({
                    "code": "MEDIA_UNUSABLE", "shot_no": entry["shot_no"],
                    "message": f"镜 {entry['shot_no']} 的素材缺失或质检未通过", "track": "video",
                })
            elif asset.safety_status == "BLOCKED":
                errors.append({
                    "code": "SAFETY_BLOCKED", "shot_no": entry["shot_no"],
                    "message": f"镜 {entry['shot_no']} 素材内容安全未通过，禁止导出", "track": "video",
                })

    for entry in timeline["tracks"]["dialogue"]:
        if entry["stale"]:
            warnings.append({
                "code": "STALE_AUDIO", "shot_no": entry["shot_no"],
                "message": f"镜 {entry['shot_no']} 的台词音频已过期（台词被修改）", "track": "dialogue",
            })
        if not entry["media_id"]:
            warnings.append({
                "code": "MISSING_AUDIO", "shot_no": entry["shot_no"],
                "message": f"镜 {entry['shot_no']} 缺少台词音频", "track": "dialogue",
            })

    for entry in timeline["tracks"]["bgm"]:
        if not entry["rights_declared"]:
            errors.append({
                "code": "RIGHTS_MISSING", "shot_no": None,
                "message": f"BGM/音效 {entry['track_id']} 未声明使用权，禁止正式导出", "track": "bgm",
            })
        # FR-060：BGM/音效必须真的有文件，否则合成时 ffmpeg 会直接失败
        if not entry["media_id"]:
            errors.append({
                "code": "BGM_MEDIA_MISSING", "shot_no": None,
                "message": f"BGM/音效 {entry['track_id']} 未绑定音频文件", "track": "bgm",
            })
        else:
            asset = session.get(MediaAsset, entry["media_id"])
            if asset is None or asset.status == AssetStatus.REJECTED.value:
                errors.append({
                    "code": "BGM_MEDIA_UNUSABLE", "shot_no": None,
                    "message": f"BGM/音效 {entry['track_id']} 的音频文件缺失或质检未通过",
                    "track": "bgm",
                })
            elif not media_service.exists(session, asset):
                errors.append({
                    "code": "BGM_FILE_MISSING", "shot_no": None,
                    "message": f"BGM/音效 {entry['track_id']} 的音频文件在存储中不存在",
                    "track": "bgm",
                })

    # FR-061：转场校验（类型白名单 + 时长上限 + 不得超过相邻两镜的一半）
    video_entries = timeline["tracks"]["video"]
    for index, entry in enumerate(video_entries):
        transition = entry.get("transition")
        if not transition:
            continue
        if index == 0:
            warnings.append({
                "code": "TRANSITION_IGNORED", "shot_no": entry["shot_no"],
                "message": f"镜 {entry['shot_no']} 是首镜，转场设置会被忽略", "track": "video",
            })
            continue
        if not transition.get("known", True):
            errors.append({
                "code": "TRANSITION_UNKNOWN", "shot_no": entry["shot_no"],
                "message": (
                    f"镜 {entry['shot_no']} 的转场类型「{transition['type']}」不受支持，"
                    f"可选：{'、'.join(k for k in TRANSITION_TYPES if k != 'none')}"
                ),
                "track": "video",
            })
            continue
        duration_ms = int(transition["duration_ms"])
        if duration_ms <= 0:
            errors.append({
                "code": "TRANSITION_INVALID_DURATION", "shot_no": entry["shot_no"],
                "message": f"镜 {entry['shot_no']} 的转场时长非法（{duration_ms}ms）", "track": "video",
            })
            continue
        if duration_ms > settings.transition_max_ms:
            errors.append({
                "code": "TRANSITION_TOO_LONG", "shot_no": entry["shot_no"],
                "message": (
                    f"镜 {entry['shot_no']} 的转场时长 {duration_ms}ms 超过上限 "
                    f"{settings.transition_max_ms}ms"
                ),
                "track": "video",
            })
            continue
        previous = video_entries[index - 1]
        limit = min(int(previous["duration_ms"]), int(entry["duration_ms"])) // 2
        if duration_ms > limit:
            errors.append({
                "code": "TRANSITION_TOO_LONG", "shot_no": entry["shot_no"],
                "message": (
                    f"镜 {entry['shot_no']} 的转场时长 {duration_ms}ms 超过相邻镜头允许的 "
                    f"上限 {limit}ms（不得超过两镜较短者的一半）"
                ),
                "track": "video",
            })

    # 转场会改变镜头起点，字幕若未重新生成就会与画面错位
    cue_by_shot: dict[str, list[int]] = {}
    for cue in timeline["tracks"]["subtitles"]:
        cue_by_shot.setdefault(cue.get("shot_id") or "", []).append(cue["start_ms"])
    for entry in video_entries:
        starts = cue_by_shot.get(entry["shot_id"] or "")
        if not starts:
            continue
        if min(abs(value - entry["start_ms"]) for value in starts) > 100:
            warnings.append({
                "code": "SUBTITLE_OFFSET", "shot_no": entry["shot_no"],
                "message": f"镜 {entry['shot_no']} 的字幕时间与时间线不一致，建议重新生成字幕",
                "track": "subtitles",
            })

    if timeline["total_duration_ms"] > settings.episode_duration_ms_max:
        errors.append({
            "code": "DURATION_EXCEEDED", "shot_no": None,
            "message": f"全片时长 {timeline['total_duration_ms']}ms 超过上限",
            "track": "video",
        })

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "timeline": timeline,
    }


# --------------------------------------------------------------------------- #
# 字幕
# --------------------------------------------------------------------------- #

def generate_subtitles(session: Session, episode: Episode) -> list[SubtitleCue]:
    """根据确认台词与实际音频生成可编辑字幕（FR-MVP-053）。

    字幕有两个来源，按优先级取：

    1. **已合成的台词音轨**（`AudioTrack.kind == "dialogue"`）—— 时间轴最准，
       因为它用的是真实音频时长。
    2. **分镜脚本里的台词 / 旁白** —— 配音**之前**也要能出字幕。

    第 2 条是必需的：否则会形成死结——不先跑 TTS 就没有 dialogue 轨，
    没有 dialogue 轨就生成不出字幕，而字幕本该在配音前就能校正。
    此时时间轴按镜头起点 + 台词序号在镜头时长内均分估算，TTS 跑完后
    重新生成字幕即可拿到精确时间。
    """
    timeline = build_timeline(session, episode)
    session.execute(delete(SubtitleCue).where(SubtitleCue.episode_id == episode.id))
    session.flush()
    cues: list[SubtitleCue] = []
    index = 1
    for entry in timeline["tracks"]["dialogue"]:
        if not entry["text"]:
            continue
        speaker = ""
        if entry["character_id"]:
            character = session.get(CharacterAsset, entry["character_id"])
            speaker = character.name if character else ""
        cue = SubtitleCue(
            episode_id=episode.id,
            shot_id=entry["shot_id"],
            index_no=index,
            start_ms=entry["start_ms"],
            end_ms=max(entry["end_ms"], entry["start_ms"] + 600),
            text=entry["text"],
            speaker=speaker,
        )
        session.add(cue)
        cues.append(cue)
        index += 1

    if not cues:
        # 还没有配音：退回分镜脚本，按镜头起点估算时间轴
        for shot in _ordered_shots(session, episode.id):
            if not shot.required:
                continue
            entry = next(
                (e for e in timeline["tracks"]["video"] if e["shot_id"] == shot.id), None
            )
            if entry is None:
                continue
            data = shot.shot_json or {}
            start = entry["start_ms"]
            span = max(int(entry["duration_ms"]), 600)

            lines = [
                line
                for line in (data.get("dialogue") or [])
                if isinstance(line, dict) and (line.get("text") or "").strip()
            ]
            if lines:
                # 多句台词在镜头时长内均分，保证首尾不越界
                step = max(span // len(lines), 600)
                for offset, line in enumerate(lines):
                    line_start = start + offset * step
                    line_end = start + (offset + 1) * step
                    # 最后一句拉到镜头末尾，避免越出本镜
                    if offset == len(lines) - 1:
                        line_end = start + span
                    session.add(
                        SubtitleCue(
                            episode_id=episode.id,
                            shot_id=shot.id,
                            index_no=index,
                            start_ms=line_start,
                            end_ms=max(line_end, line_start + 600),
                            text=str(line.get("text", "")).strip(),
                            speaker=str(line.get("character", "") or "").strip(),
                        )
                    )
                    index += 1
                continue

            narration = (data.get("narration") or "").strip()
            if narration:
                session.add(
                    SubtitleCue(
                        episode_id=episode.id,
                        shot_id=shot.id,
                        index_no=index,
                        start_ms=start,
                        end_ms=start + min(span, 4000),
                        text=narration,
                        speaker="旁白",
                    )
                )
                index += 1

        session.flush()
        cues = list(
            session.scalars(
                select(SubtitleCue)
                .where(SubtitleCue.episode_id == episode.id)
                .order_by(SubtitleCue.index_no)
            )
        )

    session.flush()
    return cues


def format_timestamp(ms: int, *, vtt: bool = False) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    sep = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def render_subtitles(session: Session, episode: Episode, fmt: str = "srt") -> str:
    cues = list(
        session.scalars(
            select(SubtitleCue)
            .where(SubtitleCue.episode_id == episode.id)
            .order_by(SubtitleCue.index_no)
        )
    )
    vtt = fmt.lower() == "vtt"
    lines: list[str] = ["WEBVTT", ""] if vtt else []
    for i, cue in enumerate(cues, start=1):
        if not vtt:
            lines.append(str(i))
        lines.append(f"{format_timestamp(cue.start_ms, vtt=vtt)} --> {format_timestamp(cue.end_ms, vtt=vtt)}")
        text = cue.text if not cue.speaker else f"{cue.speaker}：{cue.text}"
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def ass_subtitles(session: Session, episode: Episode, width: int, height: int) -> str:
    """生成 ASS 字幕（用于烧录，含 AIGC 标识）。"""
    cues = list(
        session.scalars(
            select(SubtitleCue)
            .where(SubtitleCue.episode_id == episode.id)
            .order_by(SubtitleCue.index_no)
        )
    )
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Microsoft YaHei,{max(int(height * 0.032), 20)},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,{int(height * 0.09)},1
Style: Label,Microsoft YaHei,{max(int(height * 0.022), 14)},&H00E6F2FF,&H000000FF,&H00000000,&HA0000000,0,0,0,0,100,100,0,0,1,1,0,7,24,24,24,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for cue in cues:
        text = cue.text.replace("\n", "\\N")
        if cue.speaker:
            text = f"{cue.speaker}：{text}"
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start_ms)},{_ass_time(cue.end_ms)},Main,,0,0,0,,{text}"
        )
    # AIGC 标识：不可关闭
    lines.append(
        f"Dialogue: 1,{_ass_time(0)},{_ass_time(10 * 3600 * 1000)},Label,,0,0,0,,"
        f"{settings.aigc_label_text}"
    )
    return "\n".join(lines)


def _ass_time(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


# --------------------------------------------------------------------------- #
# 素材包
# --------------------------------------------------------------------------- #

def build_package(session: Session, episode: Episode) -> tuple[bytes, dict[str, Any]]:
    """ZIP 素材包。不依赖渲染任务成功（需求 §10.2）。"""
    project = session.get(Project, episode.project_id)
    shots = _ordered_shots(session, episode.id)
    timeline = build_timeline(session, episode)
    story = session.scalar(
        select(StoryDocument)
        .where(StoryDocument.episode_id == episode.id)
        .order_by(StoryDocument.version.desc())
    )
    buffer = io.BytesIO()
    manifest: dict[str, Any] = {
        # 扁平字段便于外部系统直接读取
        "episode_id": episode.id,
        "episode_no": episode.episode_no,
        "episode_title": episode.title,
        "project_id": episode.project_id,
        "episode": {
            "id": episode.id,
            "title": episode.title,
            "episode_no": episode.episode_no,
            "stage": episode.stage,
            "version": episode.version,
            "target_duration_ms": episode.target_duration_ms,
        },
        "project": {
            "id": project.id if project else None,
            "name": project.name if project else None,
            "genre": project.genre if project else None,
            "aspect_ratio": project.aspect_ratio if project else "9:16",
        },
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "aigc": safety.aigc_label(),
        "shots": [],
        "audio": [],
        "exports": [],
    }

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        readme_text = _package_readme(episode, project)
        script_text = json.dumps(
            {
                "version": story.version if story else None,
                "status": story.status if story else None,
                "story_json": story.story_json if story else {},
            },
            ensure_ascii=False, indent=2,
        )
        rights_text = json.dumps(
            {
                "aigc": safety.aigc_label(),
                "items": _rights_items(session, episode),
            },
            ensure_ascii=False, indent=2,
        )
        srt_text = render_subtitles(session, episode, "srt")
        vtt_text = render_subtitles(session, episode, "vtt")
        text_files = {
            "README.txt": readme_text,
            "script.json": script_text,
            "rights.json": rights_text,
            "subtitles/episode.srt": srt_text,
            "subtitles/episode.vtt": vtt_text,
        }
        for name, payload in text_files.items():
            zf.writestr(name, payload)
        manifest["texts"] = [
            {
                "file": name,
                "kind": "text",
                "size_bytes": len(payload.encode("utf-8")),
                "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            }
            for name, payload in text_files.items()
        ]

        for shot in shots:
            no = f"{shot.shot_no:03d}"
            entry = {
                "shot_no": shot.shot_no,
                "duration_ms": shot.duration_ms,
                "image_status": shot.image_status,
                "video_status": shot.video_status,
                "audio_status": shot.audio_status,
                "stale": shot.stale,
                "shot_json": shot.shot_json,
                "images": [],
                "videos": [],
                "audio": [],
            }
            for asset in media_service.list_media(session, owner_id=shot.id, kind="image", limit=50):
                name = f"images/shot_{no}_v{asset.version}_{asset.id[-6:]}{Path(asset.filename).suffix or '.png'}"
                _write_asset(zf, asset, name)
                entry["images"].append(_asset_manifest(asset, name))
            for asset in media_service.list_media(session, owner_id=shot.id, kind="video", limit=20):
                name = f"videos/shot_{no}_{asset.id[-6:]}.mp4"
                _write_asset(zf, asset, name)
                entry["videos"].append(_asset_manifest(asset, name))
            for asset in media_service.list_media(session, owner_id=shot.id, kind="audio", limit=50):
                name = f"audio/shot_{no}_dialogue_{asset.id[-6:]}.mp3"
                _write_asset(zf, asset, name)
                entry["audio"].append(_asset_manifest(asset, name))
            manifest["shots"].append(entry)

        for track in session.scalars(
            select(AudioTrack).where(
                AudioTrack.episode_id == episode.id, AudioTrack.kind != "dialogue"
            )
        ):
            if not track.media_id:
                continue
            asset = session.get(MediaAsset, track.media_id)
            if asset is None:
                continue
            name = f"audio/{track.kind}_{asset.id[-6:]}{Path(asset.filename).suffix or '.mp3'}"
            _write_asset(zf, asset, name)
            manifest["audio"].append(
                {**_asset_manifest(asset, name), "kind": track.kind,
                 "start_ms": track.start_ms, "end_ms": track.end_ms,
                 "volume_db": track.volume_db, "rights_declared": track.rights_declared}
            )

        for record in session.scalars(
            select(ExportRecord).where(
                ExportRecord.episode_id == episode.id, ExportRecord.media_id.is_not(None)
            )
        ):
            asset = session.get(MediaAsset, record.media_id)
            if asset is None:
                continue
            name = f"exports/{record.kind}_{record.preset}_{asset.id[-6:]}.mp4"
            _write_asset(zf, asset, name)
            manifest["exports"].append(
                {**_asset_manifest(asset, name), "kind": record.kind, "preset": record.preset}
            )

        manifest["assets"] = [
            *manifest["texts"],
            *[a for entry in manifest["shots"]
              for a in (*entry["images"], *entry["videos"], *entry["audio"])],
            *manifest["audio"],
            *manifest["exports"],
        ]
        manifest["timeline"] = {
            "total_duration_ms": timeline["total_duration_ms"],
            "video_track": timeline["tracks"]["video"],
            "dialogue_track": timeline["tracks"]["dialogue"],
            "bgm_track": timeline["tracks"]["bgm"],
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return buffer.getvalue(), manifest


def _write_asset(zf: zipfile.ZipFile, asset: MediaAsset, name: str) -> None:
    try:
        content = media_service.read_media(asset)
    except Exception:
        return
    zf.writestr(name, content)


def _asset_manifest(asset: MediaAsset, name: str) -> dict[str, Any]:
    return {
        "media_id": asset.id,
        "file": name,
        "kind": asset.kind,
        "role": asset.role,
        "sha256": asset.sha256,
        "size_bytes": asset.size_bytes,
        "width": asset.width,
        "height": asset.height,
        "duration_ms": asset.duration_ms,
        "version": asset.version,
        "status": asset.status,
        "source": asset.source,
        "aigc_labeled": asset.aigc_labeled,
        "safety_status": asset.safety_status,
        "rights_declared": asset.rights_declared,
        "metadata": asset.metadata_json,
    }


def _rights_items(session: Session, episode: Episode) -> list[dict[str, Any]]:
    items = []
    for asset in media_service.list_media(session, episode_id=episode.id, limit=500):
        if asset.kind in {"audio"} or asset.source == "upload":
            items.append(
                {
                    "media_id": asset.id,
                    "kind": asset.kind,
                    "filename": asset.filename,
                    "source": asset.source,
                    "rights_declared": asset.rights_declared,
                    "rights_note": asset.rights_note,
                }
            )
    return items


def _package_readme(episode: Episode, project: Project | None) -> str:
    return (
        f"hoyo漫剧 素材包\n"
        f"================\n"
        f"项目：{project.name if project else '-'}\n"
        f"剧集：{episode.title}（第 {episode.episode_no} 集）\n"
        f"生成时间：{dt.datetime.now().isoformat(timespec='seconds')}\n\n"
        f"目录说明：\n"
        f"  manifest.json  素材清单（含每个素材的 sha256、尺寸、生成参数与安全检查结果）\n"
        f"  script.json    剧本 JSON（含版本）\n"
        f"  rights.json    权利与生成信息声明\n"
        f"  subtitles/     字幕（SRT / VTT）\n"
        f"  images/        分镜候选图与确认图（按镜号组织）\n"
        f"  videos/        分镜视频片段\n"
        f"  audio/         台词音频与 BGM/音效\n"
        f"  exports/       已生成的预览与正式成片\n\n"
        f"声明：{settings.aigc_label_text}。本素材包中的画面、视频与语音均由 AI 生成，"
        f"使用者需自行确认上传素材（文本、音乐、图片、声音）的使用权。\n"
    )


def timeline_to_json(timeline: dict[str, Any]) -> str:
    return json.dumps(timeline, ensure_ascii=False, indent=2)


def get_style(session: Session, project: Project | None) -> StyleAsset | None:
    if project is None or not project.default_style_id:
        return None
    return session.get(StyleAsset, project.default_style_id)


def require_episode(session: Session, episode_id: str) -> Episode:
    episode = session.get(Episode, episode_id)
    if episode is None:
        raise NotFound(f"剧集不存在：{episode_id}")
    return episode


def render_error(message: str, *, detail: dict[str, Any] | None = None) -> RenderError:
    return RenderError(message, details=detail or {})
