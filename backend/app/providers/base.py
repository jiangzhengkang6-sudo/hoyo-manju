"""供应商适配器协议与统一结果类型（需求文档 §8.3）。"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ProviderKind(str, enum.Enum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    SPEECH = "speech"


class ProviderErrorCode(str, enum.Enum):
    TIMEOUT = "PROVIDER_TIMEOUT"
    RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    AUTH = "PROVIDER_AUTH"
    BAD_REQUEST = "PROVIDER_BAD_REQUEST"
    CONTENT_BLOCKED = "PROVIDER_CONTENT_BLOCKED"
    UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    UNKNOWN = "PROVIDER_ERROR"


@dataclass(slots=True)
class ProviderCapabilities:
    models: list[str] = field(default_factory=list)
    supports_image_to_video: bool = True
    supports_text_to_image: bool = True
    max_candidates: int = 4
    duration_ms_min: int = 1000
    duration_ms_max: int = 15000
    aspect_ratios: list[str] = field(default_factory=lambda: ["9:16", "16:9", "1:1"])
    billing_unit: str = "call"
    estimated_latency_seconds: int = 10
    # --- FR-091 能力探测：以下字段留空/为 0 表示「供应商未声明」，即不做限制 ---
    #: 支持的固定尺寸清单，如 ["1024x1024", "1080x1920"]
    image_sizes: list[str] = field(default_factory=list)
    #: 总像素上限，如 1920*1920
    max_image_pixels: int = 0
    #: 支持的图片 MIME 类型
    image_formats: list[str] = field(
        default_factory=lambda: ["image/png", "image/jpeg", "image/webp"]
    )
    #: 参考图数量上下限（0 表示未声明）
    max_reference_images: int = 0
    min_reference_images: int = 0
    #: 支持的视频 MIME 类型
    video_formats: list[str] = field(default_factory=lambda: ["video/mp4"])
    #: 计费单位的单价（分）；0 表示未声明（此时不预留配额）
    unit_price_cents: int = 0


@dataclass(slots=True)
class TextRequest:
    prompt: str
    system: str = ""
    json_schema: dict[str, Any] | None = None
    temperature: float = 0.7
    max_tokens: int = 4096
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ImageRequest:
    prompt: str
    negative_prompt: str = ""
    width: int = 1080
    height: int = 1920
    count: int = 2
    reference_media_ids: list[str] = field(default_factory=list)
    reference_uris: list[str] = field(default_factory=list)
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VideoRequest:
    prompt: str
    image_uri: str = ""
    image_media_id: str = ""
    duration_ms: int = 4000
    motion_strength: float = 0.4
    camera_movement: str = "固定"
    width: int = 1080
    height: int = 1920
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SpeechRequest:
    text: str
    voice_id: str = ""
    speed: float = 1.0
    emotion: str = ""
    model: str = ""


@dataclass(slots=True)
class CostEstimate:
    estimate_cents: int
    billing_unit: str = "call"
    price_version: str = "v1"
    provider: str = ""
    model: str = ""
    estimated_wait_seconds: int = 10
    breakdown: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ProviderResult:
    ok: bool
    kind: ProviderKind
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    raw_summary: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    cost_cents: int = 0
    estimated_cost_cents: int = 0
    price_version: str = "v1"
    billing_unit: str = "call"
    request_id: str = ""
    provider_job_id: str = ""
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False

    @classmethod
    def failure(
        cls,
        kind: ProviderKind,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        raw: dict[str, Any] | None = None,
    ) -> "ProviderResult":
        return cls(
            ok=False,
            kind=kind,
            error_code=code,
            error_message=message,
            retryable=retryable,
            raw_summary=raw or {},
        )


@dataclass(slots=True)
class SubmittedJob:
    provider_job_id: str
    status: str = "RUNNING"
    raw_summary: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TextProvider(Protocol):
    id: str

    async def generate_json(self, request: TextRequest) -> ProviderResult: ...


@runtime_checkable
class ImageProvider(Protocol):
    id: str

    async def submit(self, request: ImageRequest) -> SubmittedJob: ...

    async def get_result(self, provider_job_id: str) -> ProviderResult: ...

    async def generate(self, request: ImageRequest) -> ProviderResult: ...


@runtime_checkable
class VideoProvider(Protocol):
    id: str

    async def estimate_cost(self, request: VideoRequest) -> CostEstimate: ...

    async def generate(self, request: VideoRequest) -> ProviderResult: ...


@runtime_checkable
class SpeechProvider(Protocol):
    id: str

    async def synthesize(self, request: SpeechRequest) -> ProviderResult: ...


#: 按「计费单位」换算数量的简单口径：文本/TTS 按千字符，图片按张数，视频按秒。
def _estimate_quantity(kind: ProviderKind, request: Any) -> float:
    if kind is ProviderKind.IMAGE:
        return float(getattr(request, "count", 1) or 1)
    if kind is ProviderKind.VIDEO:
        duration_ms = float(getattr(request, "duration_ms", 0) or 0)
        return math.ceil(duration_ms / 1000) if duration_ms > 0 else 0.0
    if kind is ProviderKind.SPEECH:
        text = str(getattr(request, "text", "") or "")
        return math.ceil(len(text) / 1000) if text else 0.0
    prompt = str(getattr(request, "prompt", "") or "")
    system = str(getattr(request, "system", "") or "")
    return math.ceil(max(len(prompt) + len(system), 1) / 1000)


class BaseProvider:
    """适配器公共能力：错误码映射、成本单位、原始响应摘要。"""

    id = "base"
    kind = ProviderKind.TEXT
    display_name = "base"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def estimate_cost(self, request: Any) -> CostEstimate:  # noqa: ANN401 - 按模态传不同 Request
        """按能力声明里的单价预估成本（FR-010 / §12-6）。

        供应商没声明 `unit_price_cents` 时返回 0，调用方就不会预留配额——
        宁可少预留也不要用一个拍脑袋的价格把用户额度锁死。
        """
        caps = self.capabilities()
        unit_price = int(getattr(caps, "unit_price_cents", 0) or 0)
        if unit_price <= 0:
            return CostEstimate(
                estimate_cents=0,
                billing_unit=caps.billing_unit,
                provider=self.id,
                model=getattr(self, "model", self.id),
                breakdown=[],
            )
        quantity = _estimate_quantity(self.kind, request)
        cents = int(math.ceil(quantity * unit_price)) if quantity > 0 else 0
        return CostEstimate(
            estimate_cents=max(0, cents),
            billing_unit=caps.billing_unit,
            provider=self.id,
            model=getattr(self, "model", self.id),
            estimated_wait_seconds=caps.estimated_latency_seconds,
            breakdown=[
                {
                    "item": caps.billing_unit,
                    "quantity": quantity,
                    "unit_price_cents": unit_price,
                }
            ],
        )

    def describe(self) -> dict[str, Any]:
        caps = self.capabilities()
        from dataclasses import asdict

        return {
            "id": self.id,
            "display_name": self.display_name,
            "kind": self.kind.value,
            "models": caps.models,
            # FR-091：能力声明整体透出（/config/providers 与 /settings/providers），
            # 前端据此在提交前提示不可选项，而不是等生成失败。
            "capabilities": asdict(caps),
        }

    @staticmethod
    def map_http_error(status: int) -> tuple[str, bool]:
        if status in (401, 403):
            return ProviderErrorCode.AUTH.value, False
        if status == 429:
            return ProviderErrorCode.RATE_LIMITED.value, True
        if status in (400, 422):
            return ProviderErrorCode.BAD_REQUEST.value, False
        if status >= 500:
            return ProviderErrorCode.UNAVAILABLE.value, True
        return ProviderErrorCode.UNKNOWN.value, False
