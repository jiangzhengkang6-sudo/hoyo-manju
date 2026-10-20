"""§12 异常与降级：依赖暂停与自动恢复。

PRD §12 有两行要求「暂停新任务、但已成功资源仍可用」：

- 视频供应商不可用 → 暂停视频任务，允许完成剧本、图片和素材包
- 对象存储暂时不可用 → 暂停新媒体任务，保留数据库任务状态并自动恢复

实现方式：一个进程内的轻量熔断表。某类依赖连续失败到阈值就 `pause`，
新建该类任务时 `ensure_available()` 抛 `ServiceDegraded`（503），
到 TTL 后自动放行重试（成功即 `resume`）。**不删除任何已有数据**，
已完成的素材、任务记录、导出文件一律照常可用。

多进程部署时这张表是各进程独立的（本项目的 worker 与 API 同进程内嵌，
见 `app.main.lifespan`），这一点在 docstring 里说明，不假装是分布式状态。
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from app.core.errors import ServiceDegraded

#: 依赖类型
KIND_VIDEO = "video"
KIND_IMAGE = "image"
KIND_SPEECH = "speech"
KIND_TEXT = "text"
KIND_STORAGE = "storage"

KIND_LABELS: dict[str, str] = {
    KIND_VIDEO: "视频供应商",
    KIND_IMAGE: "图像供应商",
    KIND_SPEECH: "语音供应商",
    KIND_TEXT: "文本供应商",
    KIND_STORAGE: "对象存储",
}

#: 各类依赖被暂停后，哪些能力仍然可用（用于给用户可执行的提示）
STILL_AVAILABLE: dict[str, list[str]] = {
    KIND_VIDEO: ["剧本编辑", "分镜画面生成与确认", "音频与字幕", "预览合成", "素材包导出"],
    KIND_IMAGE: ["剧本编辑", "已确认画面", "预览合成", "素材包导出"],
    KIND_SPEECH: ["剧本编辑", "画面与视频", "手动上传音频", "跳过配音后预览"],
    KIND_TEXT: ["手动编写剧本", "已生成剧本的后续阶段"],
    KIND_STORAGE: ["已有素材的预览与导出", "数据库中的任务记录"],
}

#: 连续失败到该次数即暂停
FAILURE_THRESHOLD = 3

#: 暂停后自动放行的等待时间（秒）；到点放行一次探测性重试
DEFAULT_TTL_SECONDS = 120

_lock = threading.Lock()
_paused: dict[str, dict[str, Any]] = {}
_failures: dict[str, int] = {}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def pause(kind: str, reason: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any]:
    """暂停某类依赖的新任务。重复调用会刷新原因与 TTL。"""
    with _lock:
        entry = {
            "kind": kind,
            "label": KIND_LABELS.get(kind, kind),
            "reason": reason,
            "since": _now().isoformat(),
            "until": (_now() + dt.timedelta(seconds=ttl_seconds)).isoformat(),
            "ttl_seconds": ttl_seconds,
            "still_available": STILL_AVAILABLE.get(kind, []),
        }
        _paused[kind] = entry
        _failures[kind] = 0
        return dict(entry)


def resume(kind: str) -> None:
    """解除暂停（成功一次或管理员手动恢复）。"""
    with _lock:
        _paused.pop(kind, None)
        _failures[kind] = 0


def note_failure(kind: str, reason: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict[str, Any] | None:
    """记录一次失败；达到阈值自动暂停。返回暂停信息（未触发则 None）。"""
    with _lock:
        _failures[kind] = _failures.get(kind, 0) + 1
        triggered = _failures[kind] >= FAILURE_THRESHOLD
    if triggered:
        return pause(kind, reason, ttl_seconds=ttl_seconds)
    return None


def note_success(kind: str) -> None:
    """一次成功即清零失败计数并解除暂停（自愈）。"""
    resume(kind)


def state(kind: str) -> dict[str, Any] | None:
    """当前暂停状态；已过 TTL 则返回 None（并顺手清理）。"""
    with _lock:
        entry = _paused.get(kind)
        if entry is None:
            return None
        try:
            until = dt.datetime.fromisoformat(entry["until"])
        except (KeyError, ValueError):
            _paused.pop(kind, None)
            return None
        if _now() >= until:
            _paused.pop(kind, None)
            return None
        return dict(entry)


def is_paused(kind: str) -> bool:
    return state(kind) is not None


def ensure_available(kind: str, *, action: str = "新建任务") -> None:
    """§12：暂停期间阻断**新任务**，并给出「什么仍然可用」的可执行提示。"""
    entry = state(kind)
    if entry is None:
        return
    still = "、".join(entry["still_available"]) or "已有内容"
    raise ServiceDegraded(
        f"{entry['label']}暂时不可用，已暂停{action}。"
        f"原因：{entry['reason']}。仍可继续：{still}。"
        f"系统将在 {entry['ttl_seconds']} 秒后自动重试。",
        details={
            "kind": kind,
            "label": entry["label"],
            "reason": entry["reason"],
            "since": entry["since"],
            "retry_after_seconds": entry["ttl_seconds"],
            "still_available": entry["still_available"],
        },
    )


def snapshot() -> dict[str, Any]:
    """给前端/运维看的整体降级视图。"""
    kinds = [KIND_VIDEO, KIND_IMAGE, KIND_SPEECH, KIND_TEXT, KIND_STORAGE]
    active = {k: s for k in kinds if (s := state(k)) is not None}
    return {
        "degraded": bool(active),
        "paused": active,
        "threshold": FAILURE_THRESHOLD,
        "default_ttl_seconds": DEFAULT_TTL_SECONDS,
        "labels": KIND_LABELS,
    }


def reset_all() -> None:
    """仅用于测试与运维手动清空。"""
    with _lock:
        _paused.clear()
        _failures.clear()


def failure_count(kind: str) -> int:
    with _lock:
        return _failures.get(kind, 0)
