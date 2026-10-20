"""供应商调用记录：把一次任务内的供应商调用挂到 job 上（需求 §9.2 可观测性）。

实现方式：
- ``start()`` / ``finish()`` 在任务执行线程内建立调用收集器；
- ``registry.resolve`` 返回的供应商被 ``instrument()`` 包成透明代理，
  每次调用都会把结果摘要写进收集器；
- ``run_coro`` 若把协程丢到辅助线程执行，会显式把收集器带过去。
"""

from __future__ import annotations

import inspect
import threading
import time
from typing import Any

from app.core.security import FREE_TEXT_KEYS, redact_payload

_LOCAL = threading.local()

RECORDED_METHODS = {
    "generate_json",
    "generate",
    "synthesize",
    "submit",
    "get_result",
    "estimate_cost",
}


def current() -> list[dict[str, Any]] | None:
    return getattr(_LOCAL, "calls", None)


def bind(calls: list[dict[str, Any]] | None) -> None:
    _LOCAL.calls = calls


def start() -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    _LOCAL.calls = calls
    return calls


def finish() -> list[dict[str, Any]]:
    calls = list(getattr(_LOCAL, "calls", None) or [])
    _LOCAL.calls = None
    return calls


def record(call: dict[str, Any]) -> None:
    calls = getattr(_LOCAL, "calls", None)
    if calls is not None:
        calls.append(call)


def run_with(calls: list[dict[str, Any]] | None, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """在辅助线程里带着同一个收集器执行 ``fn``。"""
    previous = getattr(_LOCAL, "calls", None)
    _LOCAL.calls = calls
    try:
        return fn(*args, **kwargs)
    finally:
        _LOCAL.calls = previous


class RecordingProvider:
    """透明代理：把被调用方法的返回结果转成一条调用记录。"""

    def __init__(self, provider: Any, kind: str) -> None:
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_kind", kind)

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._provider, name)
        if name not in RECORDED_METHODS or not callable(target):
            return target

        provider = self._provider
        kind = self._kind

        # 注意：`estimate_cost` 在 `MockVideoProvider` 里是 async，但在 `BaseProvider`
        # 里是**同步**的（文本/图像/语音都走它）。早先这里一律包成协程，导致
        # `await` 一个普通返回值 → `TypeError: object CostEstimate can't be used in
        # 'await' expression`。按目标真实形态决定包成哪种 wrapper。
        if not inspect.iscoroutinefunction(target):

            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter()
                result = target(*args, **kwargs)
                record(_summarize(provider, kind, name, result, started))
                return result

            return sync_wrapper

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            result = await target(*args, **kwargs)
            record(_summarize(provider, kind, name, result, started))
            return result

        return wrapper


def _summarize(
    provider: Any, kind: str, method: str, result: Any, started: float
) -> dict[str, Any]:
    ok = bool(getattr(result, "ok", True))
    # FR-093 / §11.1：供应商返回的原始摘要与用量在记录时就脱敏，
    # 避免密钥或长正文经由 provider_calls 回显到任务详情接口。
    return {
        "provider": getattr(provider, "id", "") or provider.__class__.__name__,
        "model": getattr(provider, "model", "") or "",
        "kind": kind,
        "method": method,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "status": "SUCCESS" if ok else "FAILED",
        "request_id": getattr(result, "request_id", "") or "",
        "provider_job_id": getattr(result, "provider_job_id", "") or "",
        "usage": redact_payload(
            getattr(result, "usage", None) or {}, truncate_keys=FREE_TEXT_KEYS
        ),
        "cost_cents": int(getattr(result, "cost_cents", 0) or 0),
        "estimated_cost_cents": int(getattr(result, "estimated_cost_cents", 0) or 0),
        "price_version": getattr(result, "price_version", "v1") or "v1",
        "billing_unit": getattr(result, "billing_unit", "call") or "call",
        "error_code": getattr(result, "error_code", None),
        "raw_summary": redact_payload(
            getattr(result, "raw_summary", None) or {}, truncate_keys=FREE_TEXT_KEYS
        ),
    }


def instrument(provider: Any, kind: str) -> Any:
    if isinstance(provider, RecordingProvider):
        return provider
    return RecordingProvider(provider, kind)
