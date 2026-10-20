"""真实供应商适配器（火山方舟 / OpenAI / 可灵 / MiniMax）。

所有适配器统一：错误码映射、超时、重试建议、能力声明、成本单位、原始响应摘要。
未配置密钥时 ``configured()`` 返回 False，注册表会自动回退到 Mock 供应商。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.providers.base import (
    BaseProvider,
    CostEstimate,
    ImageRequest,
    ProviderCapabilities,
    ProviderErrorCode,
    ProviderKind,
    ProviderResult,
    SpeechRequest,
    TextRequest,
    VideoRequest,
)

# --------------------------------------------------------------------------- #
# 价格表（分；billing_unit 对应单位）
# --------------------------------------------------------------------------- #

PRICE_TABLE: dict[str, dict[str, int]] = {
    "ark:doubao-seed-1-6-250615": {"1k_tokens": 1},
    "ark:doubao-seedream-3-0-t2i-250415": {"image": 25},
    "ark:doubao-seedance-1-0-pro-250528": {"second": 100},
    "openai:gpt-4o-mini": {"1k_tokens": 1},
    "openai:gpt-image-1": {"image": 40},
    "kling:kling-v1": {"second": 90},
    "minimax:speech-01": {"1k_chars": 3},
    # 自定义接入（价格仅用于配额预估，非账单口径）
    "deepseek:deepseek-chat": {"1k_tokens": 1},
    "gemini:gemini-nano-banana-2.1": {"image": 25},
    "grok:grok-video-1.5": {"second": 100},
    "qwen:qwen-audio-3.1-tts-next": {"1k_chars": 3},
}


def price_for(provider: str, model: str, unit: str, quantity: float) -> int:
    table = PRICE_TABLE.get(f"{provider}:{model}") or {}
    unit_price = table.get(unit, 1)
    return max(1, int(math.ceil(quantity * unit_price))) if quantity > 0 else 0


# --------------------------------------------------------------------------- #
# 火山方舟
# --------------------------------------------------------------------------- #

class ArkTextProvider(BaseProvider):
    id = "ark"
    kind = ProviderKind.TEXT
    display_name = "火山方舟 · 文本"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.ark_text_model
        self.api_key = api_key or settings.ark_api_key
        self.base_url = (base_url or settings.ark_base_url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        # 单价取自 PRICE_TABLE，保证预估预留与实结口径一致（§12-6）
        return ProviderCapabilities(
            models=[self.model], billing_unit="1k_tokens", estimated_latency_seconds=30,
            unit_price_cents=price_for(self.id, self.model, "1k_tokens", 1),
        )

    async def generate_json(self, request: TextRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 ARK_API_KEY"
            )
        model = request.model or self.model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system or "你是专业漫剧编剧。"},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.json_schema:
            payload["response_format"] = {"type": "json_object"}
        started = time.time()
        try:
            async with httpx.AsyncClient(timeout=settings.provider_timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except httpx.TimeoutException:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "文本供应商响应超时", retryable=True
            )
        except httpx.HTTPError as exc:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"网络错误：{exc}", retryable=True
            )
        if resp.status_code >= 400:
            code, retryable = self.map_http_error(resp.status_code)
            return ProviderResult.failure(
                self.kind, code, f"文本供应商返回 {resp.status_code}",
                retryable=retryable, raw={"body": resp.text[:400]},
            )
        body = resp.json()
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.BAD_REQUEST.value,
                "模型未返回合法 JSON", raw={"content": content[:400]},
            )
        usage = body.get("usage") or {}
        tokens = usage.get("total_tokens", 0)
        cost = price_for(self.id, model, "1k_tokens", tokens / 1000)
        return ProviderResult(
            ok=True, kind=self.kind, data=data,
            raw_summary={"id": body.get("id"), "latency_ms": int((time.time() - started) * 1000)},
            usage=usage, cost_cents=cost, estimated_cost_cents=cost,
            billing_unit="1k_tokens", request_id=body.get("id", ""),
        )


class ArkImageProvider(BaseProvider):
    id = "ark"
    kind = ProviderKind.IMAGE
    display_name = "火山方舟 · 图像（Seedream）"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.ark_image_model
        self.api_key = api_key or settings.ark_api_key
        self.base_url = (base_url or settings.ark_base_url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        # FR-091：Seedream 的尺寸 / 参考图数量声明
        return ProviderCapabilities(
            models=[self.model],
            max_candidates=4,
            billing_unit="image",
            estimated_latency_seconds=20,
            image_sizes=["1024x1024", "1080x1920", "1920x1080"],
            max_image_pixels=1920 * 1920,
            max_reference_images=4,
            aspect_ratios=["1:1", "9:16", "16:9"],
            unit_price_cents=price_for(self.id, self.model, "image", 1),
        )

    async def submit(self, request: ImageRequest) -> Any:
        from app.providers.base import SubmittedJob

        return SubmittedJob(provider_job_id=f"ark-{abs(hash(request.prompt)) % 10**8}")

    async def get_result(self, provider_job_id: str) -> ProviderResult:
        return ProviderResult(ok=True, kind=self.kind, provider_job_id=provider_job_id)

    async def generate(self, request: ImageRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 ARK_API_KEY"
            )
        model = request.model or self.model
        size = f"{request.width}x{request.height}"
        prompt = request.prompt
        if request.negative_prompt:
            prompt = f"{prompt}\n\n避免出现：{request.negative_prompt}"
        artifacts: list[dict[str, Any]] = []
        request_id = ""
        try:
            async with httpx.AsyncClient(timeout=settings.provider_timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/images/generations",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": model,
                        "prompt": prompt,
                        "size": size,
                        "response_format": "b64_json",
                        "n": max(1, request.count),
                    },
                )
        except httpx.TimeoutException:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "图像供应商超时", retryable=True
            )
        except httpx.HTTPError as exc:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"网络错误：{exc}", retryable=True
            )
        if resp.status_code >= 400:
            code, retryable = self.map_http_error(resp.status_code)
            return ProviderResult.failure(
                self.kind, code, f"图像供应商返回 {resp.status_code}",
                retryable=retryable, raw={"body": resp.text[:400]},
            )
        body = resp.json()
        request_id = body.get("id", "")
        for idx, item in enumerate(body.get("data") or []):
            if item.get("b64_json"):
                content = base64.b64decode(item["b64_json"])
            elif item.get("url"):
                async with httpx.AsyncClient(timeout=60) as client:
                    dl = await client.get(item["url"])
                content = dl.content
            else:
                continue
            artifacts.append(
                {
                    "content": content,
                    "filename": f"ark_{request_id or 'img'}_{idx + 1}.png",
                    "mime_type": "image/png",
                    "width": request.width,
                    "height": request.height,
                }
            )
        if not artifacts:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNKNOWN.value, "图像供应商未返回图片"
            )
        cost = price_for(self.id, model, "image", len(artifacts))
        return ProviderResult(
            ok=True, kind=self.kind, data={"count": len(artifacts)}, artifacts=artifacts,
            raw_summary={"id": request_id, "model": model},
            usage={"images": len(artifacts)}, cost_cents=cost, estimated_cost_cents=cost,
            billing_unit="image", request_id=request_id,
        )


class ArkVideoProvider(BaseProvider):
    id = "ark"
    kind = ProviderKind.VIDEO
    display_name = "火山方舟 · 视频（Seedance）"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.ark_video_model
        self.api_key = api_key or settings.ark_api_key
        self.base_url = (base_url or settings.ark_base_url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        # FR-091：Seedance 只支持 16:9 与 9:16，且必须带首帧图
        return ProviderCapabilities(
            models=[self.model],
            duration_ms_min=2000,
            duration_ms_max=12000,
            billing_unit="second",
            estimated_latency_seconds=180,
            aspect_ratios=["9:16", "16:9"],
            supports_image_to_video=True,
            supports_text_to_image=False,
            video_formats=["video/mp4"],
            unit_price_cents=price_for(self.id, self.model, "second", 1),
        )

    async def estimate_cost(self, request: VideoRequest) -> CostEstimate:
        seconds = max(1, math.ceil(request.duration_ms / 1000))
        model = request.model or self.model
        return CostEstimate(
            estimate_cents=price_for(self.id, model, "second", seconds),
            billing_unit="second", provider=self.id, model=model,
            estimated_wait_seconds=180,
            breakdown=[{"item": "时长（秒）", "quantity": seconds,
                        "unit_price_cents": PRICE_TABLE.get(f"{self.id}:{model}", {}).get("second", 100)}],
        )

    async def generate(self, request: VideoRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 ARK_API_KEY"
            )
        model = request.model or self.model
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        if request.image_uri:
            content.append({"type": "image_url", "image_url": {"url": request.image_uri}})
        try:
            async with httpx.AsyncClient(timeout=settings.provider_timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/contents/generations/tasks",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": model,
                        "content": content,
                        "duration": max(2, math.ceil(request.duration_ms / 1000)),
                        "ratio": "9:16" if request.height >= request.width else "16:9",
                    },
                )
                if resp.status_code >= 400:
                    code, retryable = self.map_http_error(resp.status_code)
                    return ProviderResult.failure(
                        self.kind, code, f"视频供应商返回 {resp.status_code}",
                        retryable=retryable, raw={"body": resp.text[:400]},
                    )
                task_id = resp.json().get("id", "")
                deadline = time.time() + min(settings.provider_timeout_seconds * 4, 900)
                while time.time() < deadline:
                    await asyncio.sleep(5)
                    poll = await client.get(
                        f"{self.base_url}/contents/generations/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                    body = poll.json()
                    status = body.get("status")
                    if status in {"succeeded", "success"}:
                        url = (body.get("content") or {}).get("video_url")
                        if not url:
                            return ProviderResult.failure(
                                self.kind, ProviderErrorCode.UNKNOWN.value, "任务成功但无视频地址"
                            )
                        dl = await client.get(url)
                        seconds = max(1, math.ceil(request.duration_ms / 1000))
                        cost = price_for(self.id, model, "second", seconds)
                        return ProviderResult(
                            ok=True, kind=self.kind, provider_job_id=task_id,
                            artifacts=[{
                                "content": dl.content,
                                "filename": f"ark_{task_id}.mp4",
                                "mime_type": "video/mp4",
                                "width": request.width, "height": request.height,
                                "duration_ms": request.duration_ms,
                            }],
                            raw_summary={"task_id": task_id, "status": status},
                            usage={"seconds": seconds}, cost_cents=cost,
                            estimated_cost_cents=cost, billing_unit="second", request_id=task_id,
                        )
                    if status in {"failed", "canceled"}:
                        return ProviderResult.failure(
                            self.kind, ProviderErrorCode.UNAVAILABLE.value,
                            f"视频任务失败：{body.get('error')}", raw={"task_id": task_id},
                        )
        except httpx.TimeoutException:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "视频供应商超时", retryable=True
            )
        except httpx.HTTPError as exc:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"网络错误：{exc}", retryable=True
            )
        return ProviderResult.failure(
            self.kind, ProviderErrorCode.TIMEOUT.value, "视频任务轮询超时", retryable=True
        )


# --------------------------------------------------------------------------- #
# OpenAI 兼容
# --------------------------------------------------------------------------- #

class OpenAITextProvider(BaseProvider):
    id = "openai"
    kind = ProviderKind.TEXT
    display_name = "OpenAI 兼容 · 文本"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.openai_text_model
        self.api_key = api_key or settings.openai_api_key
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=[self.model], billing_unit="1k_tokens",
            unit_price_cents=price_for(self.id, self.model, "1k_tokens", 1),
        )

    async def generate_json(self, request: TextRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 OPENAI_API_KEY"
            )
        model = request.model or self.model
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system or "你是专业漫剧编剧。"},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature,
        }
        if request.json_schema:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=settings.provider_timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except httpx.TimeoutException:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "文本供应商超时", retryable=True
            )
        except httpx.HTTPError as exc:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"网络错误：{exc}", retryable=True
            )
        if resp.status_code >= 400:
            code, retryable = self.map_http_error(resp.status_code)
            return ProviderResult.failure(
                self.kind, code, f"文本供应商返回 {resp.status_code}", retryable=retryable,
                raw={"body": resp.text[:400]},
            )
        body = resp.json()
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.BAD_REQUEST.value, "模型未返回合法 JSON",
                raw={"content": content[:400]},
            )
        usage = body.get("usage") or {}
        cost = price_for(self.id, model, "1k_tokens", usage.get("total_tokens", 0) / 1000)
        return ProviderResult(
            ok=True, kind=self.kind, data=data, raw_summary={"id": body.get("id")},
            usage=usage, cost_cents=cost, estimated_cost_cents=cost,
            billing_unit="1k_tokens", request_id=body.get("id", ""),
        )


class OpenAIImageProvider(BaseProvider):
    id = "openai"
    kind = ProviderKind.IMAGE
    display_name = "OpenAI 兼容 · 图像"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.openai_image_model
        self.api_key = api_key or settings.openai_api_key
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=[self.model], max_candidates=4, billing_unit="image",
            unit_price_cents=price_for(self.id, self.model, "image", 1),
        )

    async def submit(self, request: ImageRequest) -> Any:
        from app.providers.base import SubmittedJob

        return SubmittedJob(provider_job_id=f"oai-{abs(hash(request.prompt)) % 10**8}")

    async def get_result(self, provider_job_id: str) -> ProviderResult:
        return ProviderResult(ok=True, kind=self.kind, provider_job_id=provider_job_id)

    async def generate(self, request: ImageRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 OPENAI_API_KEY"
            )
        model = request.model or self.model
        try:
            async with httpx.AsyncClient(timeout=settings.provider_timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/images/generations",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": model, "prompt": request.prompt, "size": "1024x1536",
                          "n": max(1, request.count)},
                )
        except httpx.TimeoutException:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "图像供应商超时", retryable=True
            )
        except httpx.HTTPError as exc:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"网络错误：{exc}", retryable=True
            )
        if resp.status_code >= 400:
            code, retryable = self.map_http_error(resp.status_code)
            return ProviderResult.failure(
                self.kind, code, f"图像供应商返回 {resp.status_code}", retryable=retryable,
                raw={"body": resp.text[:400]},
            )
        body = resp.json()
        artifacts = []
        for idx, item in enumerate(body.get("data") or []):
            if item.get("b64_json"):
                content = base64.b64decode(item["b64_json"])
            elif item.get("url"):
                async with httpx.AsyncClient(timeout=60) as client:
                    dl = await client.get(item["url"])
                content = dl.content
            else:
                continue
            artifacts.append({
                "content": content, "filename": f"oai_{idx + 1}.png", "mime_type": "image/png",
                "width": request.width, "height": request.height,
            })
        if not artifacts:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNKNOWN.value, "图像供应商未返回图片"
            )
        cost = price_for(self.id, model, "image", len(artifacts))
        return ProviderResult(
            ok=True, kind=self.kind, data={"count": len(artifacts)}, artifacts=artifacts,
            raw_summary={"model": model}, usage={"images": len(artifacts)},
            cost_cents=cost, estimated_cost_cents=cost, billing_unit="image",
        )


# --------------------------------------------------------------------------- #
# 可灵（图生视频）
# --------------------------------------------------------------------------- #

class KlingVideoProvider(BaseProvider):
    id = "kling"
    kind = ProviderKind.VIDEO
    display_name = "可灵 · 图生视频"

    def __init__(self, model: str = "kling-v1", api_key: str = "", base_url: str = "") -> None:
        self.model = model or "kling-v1"
        self.api_key = api_key or settings.kling_api_key
        self.base_url = (base_url or settings.kling_base_url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=[self.model], duration_ms_min=5000, duration_ms_max=10000,
            billing_unit="second", estimated_latency_seconds=240,
            unit_price_cents=price_for(self.id, self.model, "second", 1),
        )

    async def estimate_cost(self, request: VideoRequest) -> CostEstimate:
        seconds = max(5, math.ceil(request.duration_ms / 1000))
        return CostEstimate(
            estimate_cents=price_for(self.id, self.model, "second", seconds),
            billing_unit="second", provider=self.id, model=self.model, estimated_wait_seconds=240,
            breakdown=[{"item": "时长（秒）", "quantity": seconds, "unit_price_cents": 90}],
        )

    async def generate(self, request: VideoRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 KLING_API_KEY"
            )
        if not request.image_uri:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.BAD_REQUEST.value, "可灵图生视频需要首帧图片"
            )
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model_name": self.model,
            "image": request.image_uri,
            "prompt": request.prompt,
            "duration": str(max(5, math.ceil(request.duration_ms / 1000))),
            "aspect_ratio": "9:16" if request.height >= request.width else "16:9",
        }
        try:
            async with httpx.AsyncClient(timeout=settings.provider_timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/videos/image2video", headers=headers, json=payload
                )
                if resp.status_code >= 400:
                    code, retryable = self.map_http_error(resp.status_code)
                    return ProviderResult.failure(
                        self.kind, code, f"可灵返回 {resp.status_code}", retryable=retryable,
                        raw={"body": resp.text[:400]},
                    )
                task_id = (resp.json().get("data") or {}).get("task_id", "")
                deadline = time.time() + 900
                while time.time() < deadline:
                    await asyncio.sleep(6)
                    poll = await client.get(
                        f"{self.base_url}/v1/videos/image2video/{task_id}", headers=headers
                    )
                    data = poll.json().get("data") or {}
                    status = data.get("task_status")
                    if status == "succeed":
                        url = ((data.get("task_result") or {}).get("videos") or [{}])[0].get("url")
                        if not url:
                            return ProviderResult.failure(
                                self.kind, ProviderErrorCode.UNKNOWN.value, "任务成功但无视频地址"
                            )
                        dl = await client.get(url)
                        seconds = max(5, math.ceil(request.duration_ms / 1000))
                        cost = price_for(self.id, self.model, "second", seconds)
                        return ProviderResult(
                            ok=True, kind=self.kind, provider_job_id=task_id,
                            artifacts=[{
                                "content": dl.content, "filename": f"kling_{task_id}.mp4",
                                "mime_type": "video/mp4", "width": request.width,
                                "height": request.height, "duration_ms": request.duration_ms,
                            }],
                            raw_summary={"task_id": task_id, "status": status},
                            usage={"seconds": seconds}, cost_cents=cost,
                            estimated_cost_cents=cost, billing_unit="second", request_id=task_id,
                        )
                    if status == "failed":
                        return ProviderResult.failure(
                            self.kind, ProviderErrorCode.UNAVAILABLE.value,
                            f"可灵任务失败：{data.get('task_status_msg')}", raw={"task_id": task_id},
                        )
        except httpx.TimeoutException:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "可灵请求超时", retryable=True
            )
        except httpx.HTTPError as exc:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"网络错误：{exc}", retryable=True
            )
        return ProviderResult.failure(
            self.kind, ProviderErrorCode.TIMEOUT.value, "可灵任务轮询超时", retryable=True
        )


# --------------------------------------------------------------------------- #
# MiniMax 语音
# --------------------------------------------------------------------------- #

class MinimaxSpeechProvider(BaseProvider):
    id = "minimax"
    kind = ProviderKind.SPEECH
    display_name = "MiniMax · 语音合成"

    def __init__(self, model: str = "speech-01", api_key: str = "", base_url: str = "") -> None:
        self.model = model or "speech-01"
        self.api_key = api_key or settings.minimax_api_key
        self.base_url = (base_url or settings.minimax_base_url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=[self.model], billing_unit="1k_chars",
            unit_price_cents=price_for(self.id, self.model, "1k_chars", 1),
        )

    async def synthesize(self, request: SpeechRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 MINIMAX_API_KEY"
            )
        url = f"{self.base_url}/t2a_v2"
        if settings.minimax_group_id:
            url += f"?GroupId={settings.minimax_group_id}"
        payload = {
            "model": self.model,
            "text": request.text,
            "stream": False,
            "voice_setting": {
                "voice_id": request.voice_id or "female-shaonv",
                "speed": request.speed,
                "emotion": request.emotion or "neutral",
            },
            "audio_setting": {"format": "mp3", "sample_rate": 32000},
        }
        try:
            async with httpx.AsyncClient(timeout=settings.provider_timeout_seconds) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.TimeoutException:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "TTS 供应商超时", retryable=True
            )
        except httpx.HTTPError as exc:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"网络错误：{exc}", retryable=True
            )
        if resp.status_code >= 400:
            code, retryable = self.map_http_error(resp.status_code)
            return ProviderResult.failure(
                self.kind, code, f"TTS 供应商返回 {resp.status_code}", retryable=retryable,
                raw={"body": resp.text[:400]},
            )
        body = resp.json()
        hex_audio = (body.get("data") or {}).get("audio")
        if not hex_audio:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNKNOWN.value,
                f"TTS 未返回音频：{body.get('base_resp')}", raw={"body": str(body)[:300]},
            )
        content = bytes.fromhex(hex_audio)
        extra = body.get("extra_info") or {}
        duration_ms = int(extra.get("audio_length", 0)) or max(800, len(request.text) * 200)
        cost = price_for(self.id, self.model, "1k_chars", len(request.text) / 1000)
        return ProviderResult(
            ok=True, kind=self.kind, data={"duration_ms": duration_ms},
            artifacts=[{
                "content": content, "filename": f"tts_{abs(hash(request.text)) % 10**8}.mp3",
                "mime_type": "audio/mpeg", "duration_ms": duration_ms,
            }],
            raw_summary={"model": self.model}, usage={"chars": len(request.text)},
            cost_cents=cost, estimated_cost_cents=cost, billing_unit="1k_chars",
            request_id=body.get("trace_id", ""),
        )


# --------------------------------------------------------------------------- #
# 通用 HTTP TTS（OpenAI 兼容 /audio/speech）
# --------------------------------------------------------------------------- #

class OpenAICompatSpeechProvider(BaseProvider):
    id = "openai"
    kind = ProviderKind.SPEECH
    display_name = "OpenAI 兼容 · 语音"

    def __init__(self, model: str = "tts-1", api_key: str = "", base_url: str = "") -> None:
        self.model = model or "tts-1"
        self.api_key = api_key or settings.openai_api_key
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=[self.model], billing_unit="1k_chars",
            unit_price_cents=price_for(self.id, self.model, "1k_chars", 1),
        )

    async def synthesize(self, request: SpeechRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 OPENAI_API_KEY"
            )
        try:
            async with httpx.AsyncClient(timeout=settings.provider_timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/audio/speech",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": request.text,
                          "voice": request.voice_id or "alloy", "speed": request.speed,
                          "response_format": "mp3"},
                )
        except httpx.TimeoutException:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "TTS 供应商超时", retryable=True
            )
        except httpx.HTTPError as exc:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"网络错误：{exc}", retryable=True
            )
        if resp.status_code >= 400:
            code, retryable = self.map_http_error(resp.status_code)
            return ProviderResult.failure(
                self.kind, code, f"TTS 供应商返回 {resp.status_code}", retryable=retryable,
                raw={"body": resp.text[:400]},
            )
        duration_ms = max(800, len(request.text) * 200)
        cost = price_for(self.id, self.model, "1k_chars", len(request.text) / 1000)
        return ProviderResult(
            ok=True, kind=self.kind, data={"duration_ms": duration_ms},
            artifacts=[{
                "content": resp.content, "filename": f"tts_{abs(hash(request.text)) % 10**8}.mp3",
                "mime_type": "audio/mpeg", "duration_ms": duration_ms,
            }],
            raw_summary={"model": self.model}, usage={"chars": len(request.text)},
            cost_cents=cost, estimated_cost_cents=cost, billing_unit="1k_chars",
        )


def probe_storage(path: str) -> bool:  # pragma: no cover - 工具函数
    return Path(path).exists()
