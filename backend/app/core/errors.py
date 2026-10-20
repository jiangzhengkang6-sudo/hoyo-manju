"""统一错误码与异常类型。"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

#: 当前请求的追踪号（API.md §0.1 要求错误体带 `trace_id`）。
#: 用 ContextVar 而非 Request.state，是为了让 service / 执行器层抛异常时
#: 无需把 request 一路透传下去也能带上同一个 id。
_trace_id: ContextVar[str | None] = ContextVar("hoyomanju_trace_id", default=None)


def new_trace_id() -> str:
    """生成一个短追踪号（12 位十六进制，日志里可读）。"""
    return uuid.uuid4().hex[:12]


def set_trace_id(value: str | None) -> None:
    """由中间件在请求入口调用。"""
    _trace_id.set(value)


def current_trace_id() -> str | None:
    return _trace_id.get()


def ensure_trace_id() -> str:
    """取当前追踪号；没有就现生成一个（后台任务 / 测试直调场景）。"""
    existing = _trace_id.get()
    if existing:
        return existing
    generated = new_trace_id()
    _trace_id.set(generated)
    return generated


class ErrorCode:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    STATE_CONFLICT = "STATE_CONFLICT"
    IDEMPOTENCY_REPLAY = "IDEMPOTENCY_REPLAY"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    STORAGE_ERROR = "STORAGE_ERROR"
    RENDER_ERROR = "RENDER_ERROR"
    CHECKPOINT_STALE = "CHECKPOINT_STALE"
    PROVIDER_CAPABILITY_UNSUPPORTED = "PROVIDER_CAPABILITY_UNSUPPORTED"
    SERVICE_DEGRADED = "SERVICE_DEGRADED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: 错误码 -> 是否建议重试
RETRYABLE = {
    ErrorCode.PROVIDER_ERROR: True,
    ErrorCode.PROVIDER_TIMEOUT: True,
    ErrorCode.STORAGE_ERROR: True,
    ErrorCode.RENDER_ERROR: True,
    ErrorCode.SERVICE_DEGRADED: True,
    ErrorCode.INTERNAL_ERROR: True,
}


class AppError(Exception):
    """业务异常基类，会被异常处理器转换为统一错误体。"""

    status_code = 400
    code = ErrorCode.VALIDATION_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if details is not None:
            self.details = details
        if status_code is not None:
            self.status_code = status_code
        self.retryable = RETRYABLE.get(self.code, False) if retryable is None else retryable

    @property
    def details(self) -> dict[str, Any]:
        return getattr(self, "_details", {})

    @details.setter
    def details(self, value: dict[str, Any]) -> None:
        self._details = value or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "retryable": self.retryable,
                "trace_id": ensure_trace_id(),
            }
        }


class NotFound(AppError):
    status_code = 404
    code = ErrorCode.NOT_FOUND


class Conflict(AppError):
    status_code = 409
    code = ErrorCode.CONFLICT


class StateConflict(AppError):
    status_code = 409
    code = ErrorCode.STATE_CONFLICT


class IdempotencyReplay(AppError):
    status_code = 409
    code = ErrorCode.IDEMPOTENCY_REPLAY


class SchemaValidationFailed(AppError):
    status_code = 422
    code = ErrorCode.SCHEMA_VALIDATION_FAILED


class SafetyBlocked(AppError):
    status_code = 422
    code = ErrorCode.SAFETY_BLOCKED


class QuotaExceeded(AppError):
    status_code = 402
    code = ErrorCode.QUOTA_EXCEEDED


class ProviderError(AppError):
    status_code = 502
    code = ErrorCode.PROVIDER_ERROR


class ProviderTimeout(AppError):
    status_code = 504
    code = ErrorCode.PROVIDER_TIMEOUT


class StorageError(AppError):
    status_code = 503
    code = ErrorCode.STORAGE_ERROR


class RenderError(AppError):
    status_code = 500
    code = ErrorCode.RENDER_ERROR


class CheckpointStale(AppError):
    status_code = 409
    code = ErrorCode.CHECKPOINT_STALE


class ProviderCapabilityUnsupported(AppError):
    """FR-091：请求参数不满足供应商能力声明，提交前阻断。"""

    status_code = 422
    code = ErrorCode.PROVIDER_CAPABILITY_UNSUPPORTED


class ServiceDegraded(AppError):
    """§12：某类外部依赖（视频供应商 / 对象存储）暂时不可用，暂停新建该类任务。

    关键点：**只阻断新任务**，已成功资源仍可预览与导出（PRD §12「配额不足」
    与「对象存储暂时不可用」两条都强调这一点）。
    """

    status_code = 503
    code = ErrorCode.SERVICE_DEGRADED


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    payload = {
        "error": {
            "code": ErrorCode.INTERNAL_ERROR,
            "message": f"服务器内部错误：{exc}",
            "details": {"type": type(exc).__name__},
            "retryable": True,
            "trace_id": ensure_trace_id(),
        }
    }
    return JSONResponse(status_code=500, content=payload)
