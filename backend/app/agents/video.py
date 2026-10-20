"""VideoAgent：视频片段生成与时长校验（FR-MVP-040~043）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.agents.story import run_coro
from app.core.config import settings
from app.core.errors import ProviderError, StateConflict
from app.db.models import AssetStatus, Episode, MediaAsset, Project, Shot
from app.providers.base import CostEstimate, ProviderKind, VideoRequest
from app.providers.registry import resolve
from app.services import media as media_service

#: 漫剧默认竖屏尺寸（FR-091 会拿它与供应商能力声明比对，因此集中定义）
DEFAULT_VIDEO_WIDTH = 1080
DEFAULT_VIDEO_HEIGHT = 1920
DEFAULT_ASPECT_RATIO = "9:16"

#: FR-040：画幅比例 → 输出分辨率。短边基准为 1080，保证各比例的像素质感一致。
#: 此前 `aspect_ratio` 只存在库里、从未参与出片，所有剧集都被写死成 1080×1920。
ASPECT_PRESETS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
    "21:9": (2560, 1080),
}


def resolve_resolution(aspect_ratio: str | None) -> tuple[int, int]:
    """按画幅比例给出 (width, height)；未知比例回落到默认竖屏。"""
    key = (aspect_ratio or DEFAULT_ASPECT_RATIO).strip()
    return ASPECT_PRESETS.get(key, (DEFAULT_VIDEO_WIDTH, DEFAULT_VIDEO_HEIGHT))


def episode_resolution(session: Session, episode: Episode) -> tuple[int, int]:
    """FR-040：画幅比例存在**项目**上（`Project.aspect_ratio`），逐集继承。"""
    project = session.get(Project, episode.project_id) if episode else None
    return resolve_resolution(project.aspect_ratio if project else None)


def _local_path(session: Session, asset: MediaAsset) -> str:
    """返回本地存储中的真实文件路径，供本地视频合成读取首帧。"""
    try:
        path = media_service.local_path(asset)
    except Exception:  # noqa: BLE001 - 远端存储时无本地路径
        return ""
    return str(path) if path and Path(path).exists() else ""


def resolve_shot_params(shot: Shot, params: dict[str, Any] | None = None) -> dict[str, Any]:
    data = shot.shot_json or {}
    vp = dict(data.get("video_params") or {})
    merged = {
        "duration_ms": int(vp.get("duration_ms") or shot.duration_ms),
        "motion_strength": float(vp.get("motion_strength", 0.4)),
        "camera_movement": vp.get("camera_movement") or data.get("camera_movement", "固定"),
    }
    for key, value in (params or {}).items():
        if value is not None:
            merged[key] = value
    merged["duration_ms"] = max(
        settings.shot_duration_ms_min, min(int(merged["duration_ms"]), settings.shot_duration_ms_max)
    )
    return merged


def clamp_to_provider(params: dict[str, Any], capabilities: Any) -> dict[str, Any]:
    """按供应商能力限制可选项（FR-MVP-040）。"""
    out = dict(params)
    out["duration_ms"] = max(
        capabilities.duration_ms_min, min(int(out["duration_ms"]), capabilities.duration_ms_max)
    )
    return out


def estimate(
    session: Session,
    *,
    episode: Episode,
    shots: list[Shot],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider = resolve(ProviderKind.VIDEO, session)
    caps = provider.capabilities()
    # FR-040：按项目画幅比例决定输出分辨率（此前写死 1080×1920）
    width, height = episode_resolution(session, episode)
    per_shot: list[dict[str, Any]] = []
    total = 0
    wait = 0
    for shot in shots:
        merged = clamp_to_provider(resolve_shot_params(shot, params), caps)
        estimate_result: CostEstimate = run_coro(
            provider.estimate_cost(
                VideoRequest(
                    prompt=(shot.shot_json or {}).get("image_prompt", ""),
                    duration_ms=merged["duration_ms"],
                    motion_strength=merged["motion_strength"],
                    camera_movement=merged["camera_movement"],
                    width=width,
                    height=height,
                )
            )
        )
        total += estimate_result.estimate_cents
        wait += estimate_result.estimated_wait_seconds
        per_shot.append(
            {
                "shot_id": shot.id,
                "shot_no": shot.shot_no,
                "duration_ms": merged["duration_ms"],
                "estimate_cents": estimate_result.estimate_cents,
                "breakdown": estimate_result.breakdown,
            }
        )
    from app.services import cost as cost_service

    snap = cost_service.quota_snapshot(session)
    return {
        "provider": provider.id,
        "model": getattr(provider, "model", provider.id),
        "estimate_cents": total,
        "quota_after_cents": snap["remaining_cents"] - total,
        "estimated_wait_seconds": wait,
        "per_shot": per_shot,
        "billing_unit": caps.billing_unit,
        "params": params or {},
    }


def generate_for_shots(
    session: Session,
    *,
    episode: Episode,
    shots: list[Shot],
    params: dict[str, Any] | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    project = session.get(Project, episode.project_id)
    provider = resolve(ProviderKind.VIDEO, session)
    caps = provider.capabilities()
    # FR-040：按项目画幅比例决定输出分辨率（此前写死 1080×1920）
    width, height = episode_resolution(session, episode)
    created: list[str] = []
    failures: list[dict[str, Any]] = []
    total = max(1, len(shots))
    done = 0

    for shot in shots:
        if not shot.confirmed_media_id:
            failures.append(
                {
                    "shot_no": shot.shot_no,
                    "code": "STATE_CONFLICT",
                    "message": "该分镜尚未确认画面，不能进入视频生成",
                }
            )
            done += 1
            continue
        image = session.get(MediaAsset, shot.confirmed_media_id)
        if image is None:
            failures.append({"shot_no": shot.shot_no, "code": "NOT_FOUND", "message": "确认图已丢失"})
            done += 1
            continue
        merged = clamp_to_provider(resolve_shot_params(shot, params), caps)
        data = shot.shot_json or {}
        prompt = "，".join(
            p
            for p in [
                data.get("action", ""),
                data.get("image_prompt", ""),
                f"镜头运动：{merged['camera_movement']}",
                f"运动强度：{merged['motion_strength']}",
            ]
            if p
        )
        result = run_coro(
            provider.generate(
                VideoRequest(
                    prompt=prompt,
                    image_uri=media_service.media_url(image),
                    image_media_id=image.id,
                    duration_ms=merged["duration_ms"],
                    motion_strength=merged["motion_strength"],
                    camera_movement=merged["camera_movement"],
                    width=width,
                    height=height,
                    metadata={
                        "shot_no": shot.shot_no,
                        "simulate": (params or {}).get("simulate"),
                        "first_frame_path": _local_path(session, image),
                    },
                )
            )
        )
        if not result.ok:
            shot.video_status = "FAILED"
            failures.append(
                {
                    "shot_no": shot.shot_no,
                    "code": result.error_code,
                    "message": result.error_message,
                    "retryable": result.retryable,
                }
            )
            done += 1
            if progress_cb:
                progress_cb(int(done / total * 100))
            continue
        for artifact in result.artifacts:
            asset = media_service.save_artifact(
                session,
                artifact,
                kind="video",
                role="shot_video",
                owner_type="shot",
                owner_id=shot.id,
                episode_id=episode.id,
                project_id=project.id,
                status=AssetStatus.CANDIDATE.value,
                version=shot.current_version,
                metadata={
                    "shot_id": shot.id,
                    "shot_no": shot.shot_no,
                    "provider": provider.id,
                    "prompt": prompt,
                    "duration_ms": merged["duration_ms"],
                    "source_image_id": image.id,
                },
            )
            created.append(asset.id)
            shot.video_media_id = asset.id
            shot.video_status = "CANDIDATES_READY"
        done += 1
        if progress_cb:
            progress_cb(int(done / total * 100))

    session.flush()
    return {"media_ids": created, "failures": failures, "count": len(created)}


def confirm(session: Session, shot: Shot, media_id: str) -> Shot:
    asset = media_service.get_media(session, media_id)
    if asset.owner_id != shot.id or asset.kind != "video":
        raise StateConflict("该视频不属于此分镜")
    shot.video_media_id = media_id
    shot.video_status = "CONFIRMED"
    session.flush()
    return shot


def skip(session: Session, shot: Shot, fallback: str, reason: str = "") -> Shot:
    shot.video_status = "SKIPPED"
    shot.shot_json = {
        **(shot.shot_json or {}),
        "video_skipped": True,
        "video_fallback": fallback,
        "video_skip_reason": reason,
    }
    session.flush()
    return shot


def validate_duration(shot: Shot, actual_ms: int | None) -> list[str]:
    """校验生成片段时长与剧本时长的一致性。"""
    warnings: list[str] = []
    if actual_ms is None:
        warnings.append(f"镜 {shot.shot_no}：未能读取生成片段时长")
        return warnings
    if abs(actual_ms - shot.duration_ms) > 500:
        warnings.append(
            f"镜 {shot.shot_no}：生成片段 {actual_ms}ms 与剧本 {shot.duration_ms}ms 不一致，合成时将按剧本时长裁剪"
        )
    return warnings


def video_history(session: Session, shot: Shot) -> list[dict[str, Any]]:
    items = []
    for asset in media_service.list_media(session, owner_id=shot.id, kind="video", limit=50):
        data = media_service.to_dict(asset)
        data["media_id"] = asset.id
        data["confirmed"] = shot.video_media_id == asset.id
        items.append(data)
    return items


def snapshot(shot: Shot) -> str:
    return json.dumps(shot.shot_json, ensure_ascii=False, sort_keys=True)
