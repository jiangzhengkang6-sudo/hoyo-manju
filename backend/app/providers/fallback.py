"""主备供应商切换（FR-090）。

设计要点：

- **包装而非散落**：业务代码只拿 `registry.resolve()` 的返回值，切换逻辑集中在这里，
  调用方（executor / workflow nodes / agents）不需要知道备用供应商的存在。
- **只对「换一家就能好」的错误切换**：超时、限流、不可用、5xx 才切；参数错误、
  鉴权失败、内容被拦截换谁都一样，切过去只会多花一次钱。
- **两侧各自记账**：主供应商与备用供应商各自被 `provider_log.instrument()` 包过，
  所以失败的那次尝试也会留一条 `provider_calls` 记录，便于排查与计费对账。
"""

from __future__ import annotations

from typing import Any

from app.providers.base import ProviderKind

#: 会触发切换的错误码
SWITCHABLE_ERROR_CODES = frozenset({
    "PROVIDER_TIMEOUT",
    "PROVIDER_RATE_LIMITED",
    "PROVIDER_UNAVAILABLE",
    "PROVIDER_ERROR",
})

#: 参与切换的方法（`estimate_cost` 不切：报价必须与实际执行的那家一致）
SWITCHABLE_METHODS = frozenset({
    "generate_json",
    "generate",
    "synthesize",
    "submit",
    "get_result",
})


#: 备用供应商不支持该方法时的哨兵值
_NO_FALLBACK = object()


def should_switch(result: Any) -> bool:
    """判断一次供应商调用结果是否值得切到备用供应商。"""
    if result is None:
        return False
    # SubmittedJob 等没有 ok 字段的结果不算失败
    if not hasattr(result, "ok"):
        return False
    if getattr(result, "ok", True):
        return False
    code = getattr(result, "error_code", None) or ""
    if code in SWITCHABLE_ERROR_CODES:
        return True
    return bool(getattr(result, "retryable", False))


class FallbackProvider:
    """主供应商的透明代理：可重试失败时改用备用供应商重试一次。"""

    def __init__(self, primary: Any, fallback: Any, kind: ProviderKind,
                 *, on_switch: Any = None) -> None:
        object.__setattr__(self, "_primary", primary)
        object.__setattr__(self, "_fallback", fallback)
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_on_switch", on_switch)
        #: 最近一次实际服务本次调用的供应商 id（供上层记录/展示）
        object.__setattr__(self, "last_provider_id", getattr(primary, "id", ""))

    # --- 元信息一律沿用主供应商 ---
    @property
    def id(self) -> str:
        return getattr(self._primary, "id", "")

    @property
    def kind(self) -> ProviderKind:
        return self._kind

    @property
    def model(self) -> str:
        return getattr(self._primary, "model", "")

    @property
    def display_name(self) -> str:
        return getattr(self._primary, "display_name", "")

    @property
    def fallback_id(self) -> str:
        return getattr(self._fallback, "id", "")

    def configured(self) -> bool:
        check = getattr(self._primary, "configured", None)
        return bool(check()) if callable(check) else True

    def capabilities(self) -> Any:
        return self._primary.capabilities()

    def describe(self) -> dict[str, Any]:
        info = dict(self._primary.describe())
        info["fallback_id"] = self.fallback_id
        return info

    def __getattr__(self, name: str) -> Any:
        primary = object.__getattribute__(self, "_primary")
        fallback = object.__getattribute__(self, "_fallback")
        target = getattr(primary, name)
        if name not in SWITCHABLE_METHODS or not callable(target):
            return target

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                result = await target(*args, **kwargs)
            except Exception as exc:  # 主供应商直接抛异常也视为一次失败尝试
                switched = await self._try_fallback(name, fallback, *args, **kwargs)
                if switched is _NO_FALLBACK:
                    raise exc
                return switched
            if should_switch(result):
                switched = await self._try_fallback(name, fallback, *args, **kwargs)
                if switched is _NO_FALLBACK:
                    return result
                return switched
            object.__setattr__(self, "last_provider_id", getattr(primary, "id", ""))
            return result

        return wrapper

    async def _try_fallback(self, name: str, fallback: Any, *args: Any, **kwargs: Any) -> Any:
        switched = getattr(fallback, name, None)
        if not callable(switched):
            # 备用供应商不支持该方法：把决定权交回调用方
            return _NO_FALLBACK
        result = await switched(*args, **kwargs)
        object.__setattr__(self, "last_provider_id", getattr(fallback, "id", ""))
        callback = object.__getattribute__(self, "_on_switch")
        if callable(callback):
            callback(getattr(fallback, "id", ""))
        if hasattr(result, "raw_summary") and isinstance(result.raw_summary, dict):
            result.raw_summary = {**result.raw_summary, "fallback_from": getattr(
                object.__getattribute__(self, "_primary"), "id", "")}
        return result


def wrap_with_fallback(primary: Any, fallback: Any, kind: ProviderKind,
                       *, on_switch: Any = None) -> Any:
    """没有备用供应商时原样返回主供应商，避免多包一层。"""
    if fallback is None:
        return primary
    return FallbackProvider(primary, fallback, kind, on_switch=on_switch)
