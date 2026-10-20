"""健康检查与前端可读的配置/能力清单。"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import SessionDep
from app.agents.edit import ASPECT_VALUES, resolution_for, transition_options
from app.core.config import settings
from app.providers.mock import ffmpeg_bin
from app.providers.registry import describe_all
from app.services import cost as cost_service
from app.services.storage import storage_health
from app.workflow.graph import engine_name, graph_version

router = APIRouter(tags=["health"])


@router.get("/health")
def health(session: SessionDep) -> dict:
    db_ok = True
    db_error = ""
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = str(exc)
    binary = ffmpeg_bin()
    return {
        "status": "ok" if db_ok else "degraded",
        "version": settings.version,
        "env": settings.env,
        "db": {"ok": db_ok, "url": settings.database_url.split("://")[0], "error": db_error},
        "storage": storage_health(),
        "ffmpeg": {"available": bool(binary), "path": binary or ""},
        "queue": {
            "backend": "redis" if settings.redis_url else "database",
            "concurrency": settings.max_concurrent_jobs,
            "worker_concurrency": settings.worker_concurrency,
        },
        "graph_engine": engine_name(),
        "graph_version": graph_version(),
        "providers": {
            "text": settings.text_provider,
            "image": settings.image_provider,
            "video": settings.video_provider,
            "speech": settings.speech_provider,
        },
        "quota": cost_service.quota_snapshot(session),
    }


@router.get("/config/limits")
def limits() -> dict:
    """前端不得写死任何配额数值，统一从这里读取。"""
    return {
        "max_concurrent_jobs": settings.max_concurrent_jobs,
        "candidates_per_shot_default": settings.candidates_per_shot_default,
        "candidates_per_shot_max": settings.candidates_per_shot_max,
        "shot_duration_ms_min": settings.shot_duration_ms_min,
        "shot_duration_ms_max": settings.shot_duration_ms_max,
        "episode_duration_ms_max": settings.episode_duration_ms_max,
        "story_text_max_chars": settings.story_text_max_chars,
        "min_shots": settings.min_shots,
        "preview_bitrate_kbps": settings.preview_bitrate_kbps,
        "export_presets": [
            # `*_9x16` 为历史 id（尺寸现在跟随项目画幅比例，见 FR-040/FR-060）
            {"id": "720p_9x16", "label": "720p 竖屏 9:16", "tier": "720p", "width": 720, "height": 1280},
            {"id": "1080p_9x16", "label": "1080p 竖屏 9:16", "tier": "1080p", "width": 1080, "height": 1920},
            # 画幅无关档位：实际像素由项目画幅比例决定，前端应优先使用这两个
            {"id": "720p", "label": "720p（跟随项目画幅）", "tier": "720p", "width": 720, "height": 1280},
            {"id": "1080p", "label": "1080p（跟随项目画幅）", "tier": "1080p", "width": 1080, "height": 1920},
        ],
        "aspect_ratios": list(ASPECT_VALUES),
        # 各画幅比例下 720p / 1080p 的实际像素，前端可直接展示，不必自己算
        "resolutions_by_aspect": {
            ratio: {
                "720p": list(resolution_for(ratio, "720p")),
                "1080p": list(resolution_for(ratio, "1080p")),
                "preview": list(resolution_for(ratio, "preview")),
            }
            for ratio in ASPECT_VALUES
        },
        "default_language": "zh-CN",
        "languages": [
            {"id": "zh-CN", "label": "简体中文"},
            {"id": "zh-TW", "label": "繁體中文"},
            {"id": "en-US", "label": "English"},
        ],
        "genres": ["都市", "悬疑", "玄幻", "校园", "科幻", "古风", "治愈"],
        "camera_movements": ["固定", "推", "拉", "摇", "移", "跟", "升降"],
        "compositions": ["特写", "近景", "中景", "全景", "远景", "过肩", "俯拍", "仰拍"],
        # FR-061 转场：前端下拉与时长上限都从这里读，避免前端写死
        "transition_types": transition_options(),
        "transition_default_ms": settings.transition_default_ms,
        "transition_max_ms": settings.transition_max_ms,
        "asset_kinds": [
            {"id": "setting", "label": "角色设定图"},
            {"id": "front", "label": "正面全身图"},
            {"id": "half_body", "label": "半身图"},
            {"id": "expression", "label": "表情图"},
        ],
        "signed_url_ttl_seconds": settings.signed_url_ttl_seconds,
        "require_video_cost_confirmation": settings.require_video_cost_confirmation,
        "aigc_label": settings.aigc_label_text,
        # §11.3 上传限制：前端据此在选文件时即提示，而不是等服务端报错
        "upload_max_bytes": settings.upload_max_bytes,
        "upload_workspace_quota_bytes": settings.upload_workspace_quota_bytes,
        "upload_allowed_mimes": list(settings.upload_allowed_mimes),
    }


@router.get("/config/providers")
def providers() -> dict:
    return {"items": describe_all()}


@router.get("/config/job-types")
def job_types() -> dict:
    from app.services.executor import registered_types

    return {"items": registered_types()}
