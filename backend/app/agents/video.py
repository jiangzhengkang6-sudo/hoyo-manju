"""VideoAgent：视频片段生成与时长校验（FR-MVP-040~043）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

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


def _frame_refs(session: Session, asset: MediaAsset) -> tuple[str, str]:
    """首帧的两种引用方式：公网 URL（远端存储）与本地路径（本地存储）。

    本地存储的 `media_url()` 是**相对签名路径**（`/api/v1/storage/signed/...`），
    外部网关根本访问不到；直接塞给供应商等于没传首帧。这里只把真正的
    http(s) 地址当 URL，其余交给 `image_path` 由供应商上传换公网地址。
    """
    url = media_service.media_url(asset)
    if not url.startswith(("http://", "https://")):
        url = ""
    return url, _local_path(session, asset)


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


def frame_media_id(session: Session, shot: Shot) -> str | None:
    """图生视频的首帧：已确认画面 → 已选中候选 → 任意未驳回候选图。

    口径统一在这里，避免估算、能力校验、真正生成三处各判一次。
    「有画面」的判定要宽松：用户在分镜页看到候选图就算有画面，
    不该因为没点「选中」就把整次估价拦下来；真正生成时按同样顺序取帧。
    """
    if shot.confirmed_media_id:
        return shot.confirmed_media_id
    if shot.selected_media_id:
        return shot.selected_media_id
    from sqlalchemy import select

    candidate = session.scalar(
        select(MediaAsset)
        .where(
            MediaAsset.owner_type == "shot",
            MediaAsset.owner_id == shot.id,
            MediaAsset.role == "shot_image",
            MediaAsset.status != "REJECTED",
        )
        .order_by(MediaAsset.created_at)
    )
    return candidate.id if candidate else None


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
    should_cancel: Callable[[], bool] | None = None,
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
    cost_cents = 0
    cancelled = False

    for shot in shots:
        # 视频单条要跑 1~3 分钟，取消只能在「下一条」生效；
        # 已经提交的那条会照常出片、照常计费。
        if should_cancel and should_cancel():
            cancelled = True
            break
        # 出片严格：首帧必须是**已确认**的画面（估价可以宽松，花钱不行）
        frame_id = shot.confirmed_media_id
        if not frame_id:
            failures.append(
                {
                    "shot_no": shot.shot_no,
                    "code": "STATE_CONFLICT",
                    "message": "该分镜尚未确认画面，不能进入视频生成（先在分镜页点「确认画面」）",
                }
            )
            done += 1
            continue
        image = session.get(MediaAsset, frame_id)
        if image is None:
            failures.append({"shot_no": shot.shot_no, "code": "NOT_FOUND", "message": "首帧图已丢失"})
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
        frame_url, frame_path = _frame_refs(session, image)
        result = run_coro(
            provider.generate(
                VideoRequest(
                    prompt=prompt,
                    image_uri=frame_url,
                    image_media_id=image.id,
                    image_path=frame_path,
                    duration_ms=merged["duration_ms"],
                    motion_strength=merged["motion_strength"],
                    camera_movement=merged["camera_movement"],
                    width=width,
                    height=height,
                    metadata={
                        "shot_no": shot.shot_no,
                        "simulate": (params or {}).get("simulate"),
                        "first_frame_path": frame_path,
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
        # 视频按秒计费，单条成本可能几百上千分；不回传结算就只能按预留兜底
        cost_cents += int(result.cost_cents or 0)
        done += 1
        if progress_cb:
            progress_cb(int(done / total * 100))

    session.flush()
    return {
        "media_ids": created,
        "failures": failures,
        "count": len(created),
        "cost_cents": cost_cents,
        "cancelled": cancelled,
    }


def confirm(session: Session, shot: Shot, media_id: str) -> Shot:
    asset = media_service.get_media(session, media_id)
    if asset.owner_id != shot.id or asset.kind != "video":
        raise StateConflict("该视频不属于此分镜")
    shot.video_media_id = media_id
    shot.video_status = "CONFIRMED"
    session.flush()
    return shot


def fetch_from_provider(
    session: Session, episode: Episode, shot: Shot, task_id: str
) -> tuple[MediaAsset, Any]:
    """把供应商已生成、但本地没拉回来的片段取回并挂到该镜头。

    不提交新任务、不产生新费用；纯粹是「把已经付过钱的东西拿回来」。
    上游成片只保留 24 小时，过期后 `fetch_result` 会明确报「链接已过期」。
    """
    provider = resolve(ProviderKind.VIDEO, session)
    fetch = getattr(provider, "fetch_result", None)
    if fetch is None:
        raise StateConflict("当前视频供应商不支持按任务号补拉，请改用「上传片段」")
    result = run_coro(fetch(task_id))
    if not result.ok:
        raise ProviderError(
            result.error_message or "补拉供应商片段失败",
            details={
                "task_id": task_id,
                "code": result.error_code,
                "retryable": result.retryable,
            },
        )
    if not result.artifacts:
        raise StateConflict("供应商任务没有返回任何产物")
    artifact = result.artifacts[0]
    asset = media_service.save_artifact(
        session,
        artifact,
        kind="video",
        role="shot_video",
        owner_type="shot",
        owner_id=shot.id,
        episode_id=episode.id,
        project_id=episode.project_id,
        status=AssetStatus.CONFIRMED.value,
        source="provider_fetch",
        metadata={
            "provider_task_id": task_id,
            "shot_no": shot.shot_no,
            "fetched": True,
        },
    )
    if not asset.duration_ms:
        probed = media_service.probe_duration_ms(asset)
        if probed:
            asset.duration_ms = probed
    confirm(session, shot, asset.id)
    session.flush()
    return asset, result


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
