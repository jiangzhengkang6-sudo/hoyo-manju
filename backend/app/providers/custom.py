"""自定义供应商适配器：DeepSeek 文本 / toapis 图片与视频 / 通义 MaaS TTS。

为什么单独成文件：这三家的协议与既有四个适配器不同，硬套会静默出错。

- **DeepSeek** 是 OpenAI 兼容的同步文本接口（`/chat/completions`），
  与 `OpenAITextProvider` 同形，但基址和密钥独立，所以单独一个 id。
- **toapis 网关**的图片与视频**统一是异步任务**：提交拿到 `task_id`，
  再轮询到 `status=completed`，产物在 `result.data[0].url`。
  这跟 OpenAI 的同步 `images/generations` 完全不同 —— 直接复用
  `OpenAIImageProvider` 会拿到空 `data`（实测：HTTP 200 但 0 张图）。
- **通义 MaaS TTS** 用 `input.text_prompt` 传文本，回 `output.audio.url`（wav），
  不是 OpenAI/MiniMax 的形态。

密钥未配置时 ``configured()`` 返回 False，注册表会自动回退 Mock 供应商。
"""

from __future__ import annotations

import asyncio
import time
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
from app.providers.http_providers import price_for

# --------------------------------------------------------------------------- #
# DeepSeek · 文本（OpenAI 兼容，同步）
# --------------------------------------------------------------------------- #


class DeepSeekTextProvider(BaseProvider):
    id = "deepseek"
    kind = ProviderKind.TEXT
    display_name = "DeepSeek · 文本"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.deepseek_model
        self.api_key = api_key or settings.deepseek_api_key
        self.base_url = (base_url or settings.deepseek_base_url).rstrip("/")

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=[self.model],
            billing_unit="1k_tokens",
            unit_price_cents=price_for(self.id, self.model, "1k_tokens", 1),
        )

    async def generate_json(self, request: TextRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 DEEPSEEK_API_KEY"
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
            # DeepSeek 支持 json_object 模式；要求提示词里出现 "json" 字样
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
        usage = body.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)
        cost = price_for(self.id, model, "1k_tokens", max(tokens, 1) / 1000)
        import json as _json

        try:
            data = _json.loads(content)
        except _json.JSONDecodeError:
            # 供应商没给纯 JSON 时退回原文，让上层决定怎么解析
            data = {"raw": content}
        return ProviderResult(
            ok=True, kind=self.kind, data=data,
            raw_summary={"model": model, "usage": usage},
            usage=usage,
            cost_cents=cost, estimated_cost_cents=cost,
            billing_unit="1k_tokens", request_id=str(body.get("id") or ""),
        )


# --------------------------------------------------------------------------- #
# toapis 异步任务公共逻辑
# --------------------------------------------------------------------------- #


class _ToapisTaskProvider(BaseProvider):
    """toapis 网关的「提交 → 轮询 → 下载」公共实现。

    子类只需给出 `submit_url` / `task_base` / `result_mime` / `artifact_ext`。
    """

    submit_url: str = ""
    task_base: str = ""
    result_mime: str = "application/octet-stream"
    artifact_ext: str = "bin"
    artifact_prefix: str = "out"
    missing_key_hint: str = "未配置密钥"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _submit_task(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> tuple[str, ProviderResult | None]:
        resp = await client.post(self.submit_url, headers=self._headers(), json=payload)
        if resp.status_code >= 400:
            body_text = resp.text[:400]
            # 网关把「余额不足」也塞进 403，若按 HTTP 码映射会误报成密钥错误，
            # 用户会去查 key 而不是去充值。这里单独识别。
            lowered = body_text.lower()
            if "quota_not_enough" in lowered or "余额不足" in body_text:
                return "", ProviderResult.failure(
                    self.kind, ProviderErrorCode.UNAVAILABLE.value,
                    f"{self.display_name} 账户额度不足，请到网关后台充值后重试"
                    "（与密钥无关）",
                    retryable=False, raw={"body": body_text},
                )
            code, retryable = self.map_http_error(resp.status_code)
            return "", ProviderResult.failure(
                self.kind, code, f"{self.display_name} 提交失败（{resp.status_code}）",
                retryable=retryable, raw={"body": body_text},
            )
        task_id = str((resp.json() or {}).get("id") or "")
        if not task_id:
            return "", ProviderResult.failure(
                self.kind, ProviderErrorCode.UNKNOWN.value,
                f"{self.display_name} 未返回任务 id", raw={"body": resp.text[:300]},
            )
        return task_id, None

    async def _wait_task(
        self, client: httpx.AsyncClient, task_id: str
    ) -> tuple[dict[str, Any] | None, ProviderResult | None]:
        """轮询到终态。返回 (最终响应, 失败结果)。"""
        deadline = time.time() + settings.task_timeout_seconds
        interval = max(1, settings.task_poll_interval_seconds)
        url = f"{self.task_base}/{task_id}"
        last_status = ""
        while time.time() < deadline:
            poll = await client.get(url, headers=self._headers())
            if poll.status_code >= 400:
                code, retryable = self.map_http_error(poll.status_code)
                return None, ProviderResult.failure(
                    self.kind, code, f"{self.display_name} 轮询失败（{poll.status_code}）",
                    retryable=retryable, raw={"body": poll.text[:400]},
                )
            body = poll.json() or {}
            status = str(body.get("status") or "").lower()
            last_status = status
            if status in {"completed", "succeeded", "success"}:
                return body, None
            if status in {"failed", "error", "cancelled", "canceled"}:
                return None, ProviderResult.failure(
                    self.kind, ProviderErrorCode.UNKNOWN.value,
                    f"{self.display_name} 任务失败：{status}",
                    raw={"body": str(body)[:400]},
                )
            await asyncio.sleep(interval)
        return None, ProviderResult.failure(
            self.kind, ProviderErrorCode.TIMEOUT.value,
            f"{self.display_name} 任务轮询超时（{settings.task_timeout_seconds}s，最后状态 {last_status}）",
            retryable=True,
        )

    @staticmethod
    def _result_urls(body: dict[str, Any]) -> list[str]:
        """toapis 的产物在 result.data[].url。"""
        result = body.get("result") or {}
        urls = []
        for item in result.get("data") or []:
            url = item.get("url") if isinstance(item, dict) else None
            if url:
                urls.append(str(url))
        return urls

    async def _download(self, client: httpx.AsyncClient, url: str) -> bytes:
        resp = await client.get(url, timeout=180)
        resp.raise_for_status()
        return resp.content

    def _artifact(self, content: bytes, index: int) -> dict[str, Any]:
        return {
            "content": content,
            "filename": f"{self.artifact_prefix}_{index + 1}.{self.artifact_ext}",
            "mime_type": self.result_mime,
        }

    async def _run_task(
        self, payload: dict[str, Any], *, want: int = 1
    ) -> ProviderResult:
        """跑 `want` 次任务并汇总产物。"""
        artifacts: list[dict[str, Any]] = []
        raw: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(
                timeout=max(60, settings.provider_timeout_seconds)
            ) as client:
                for index in range(max(1, want)):
                    task_id, failure = await self._submit_task(client, payload)
                    if failure is not None:
                        return failure
                    body, failure = await self._wait_task(client, task_id)
                    if failure is not None:
                        return failure
                    urls = self._result_urls(body or {})
                    if not urls:
                        return ProviderResult.failure(
                            self.kind, ProviderErrorCode.UNKNOWN.value,
                            f"{self.display_name} 任务完成但没有产物 URL",
                            raw={"body": str(body)[:400]},
                        )
                    raw[f"task_{index}"] = {
                        "id": task_id, "status": (body or {}).get("status"),
                    }
                    for url in urls:
                        content = await self._download(client, url)
                        artifacts.append(self._artifact(content, len(artifacts)))
        except httpx.TimeoutException:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value,
                f"{self.display_name} 请求超时", retryable=True,
            )
        except httpx.HTTPError as exc:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value,
                f"网络错误：{exc}", retryable=True,
            )
        if not artifacts:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNKNOWN.value,
                f"{self.display_name} 未返回任何产物",
            )
        cost = self.capabilities().unit_price_cents * len(artifacts)
        return ProviderResult(
            ok=True, kind=self.kind, artifacts=artifacts,
            raw_summary=raw, cost_cents=cost, estimated_cost_cents=cost,
        )


# --------------------------------------------------------------------------- #
# toapis · 图片（Gemini nano-banana）
# --------------------------------------------------------------------------- #


class ToapisImageProvider(_ToapisTaskProvider):
    id = "gemini"
    kind = ProviderKind.IMAGE
    display_name = "Gemini 图片（toapis）"
    result_mime = "image/png"
    artifact_ext = "png"
    artifact_prefix = "gemini"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.gemini_image_model
        self.api_key = api_key or settings.gemini_image_api_key
        self.submit_url = (base_url or settings.gemini_image_url).rstrip("/")
        # 轮询地址 = 提交地址 + /{task_id}（实测可用）
        self.task_base = self.submit_url

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=[self.model], max_candidates=4, billing_unit="image",
            estimated_latency_seconds=240,
            unit_price_cents=price_for(self.id, self.model, "image", 1),
        )

    async def submit(self, request: ImageRequest):  # noqa: ANN201 - 协议对齐
        from app.providers.base import SubmittedJob

        return SubmittedJob(provider_job_id=f"gemini-{abs(hash(request.prompt)) % 10**8}")

    async def get_result(self, provider_job_id: str) -> ProviderResult:
        return ProviderResult(ok=True, kind=self.kind, provider_job_id=provider_job_id)

    async def generate(self, request: ImageRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 GEMINI_IMAGE_API_KEY"
            )
        model = request.model or self.model
        prompt = request.prompt
        if request.negative_prompt:
            prompt = f"{prompt}\n\n避免出现：{request.negative_prompt}"
        # toapis 只吃 model + prompt，多传 size/n 会 400（实测）
        payload = {"model": model, "prompt": prompt}
        result = await self._run_task(payload, want=max(1, request.count))
        if result.ok:
            # 把真实尺寸补进产物，便于下游校验与合成
            for artifact in result.artifacts:
                artifact.setdefault("width", request.width)
                artifact.setdefault("height", request.height)
        return result


# --------------------------------------------------------------------------- #
# toapis · 视频（Grok video）
# --------------------------------------------------------------------------- #


class ToapisVideoProvider(_ToapisTaskProvider):
    id = "grok"
    kind = ProviderKind.VIDEO
    display_name = "Grok 视频（toapis）"
    result_mime = "video/mp4"
    artifact_ext = "mp4"
    artifact_prefix = "grok"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.grok_video_model
        self.api_key = api_key or settings.grok_video_api_key
        self.submit_url = (base_url or settings.grok_video_url).rstrip("/")
        self.task_base = self.submit_url

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=[self.model], duration_ms_min=2000, duration_ms_max=15000,
            billing_unit="second", estimated_latency_seconds=300,
            unit_price_cents=price_for(self.id, self.model, "second", 1),
        )

    async def estimate_cost(self, request: VideoRequest) -> CostEstimate:
        import math as _math

        seconds = max(1, _math.ceil(request.duration_ms / 1000))
        unit = price_for(self.id, self.model, "second", 1)
        return CostEstimate(
            estimate_cents=price_for(self.id, self.model, "second", seconds),
            billing_unit="second", provider=self.id, model=self.model,
            estimated_wait_seconds=300,
            breakdown=[
                {"item": "时长（秒）", "quantity": seconds, "unit_price_cents": unit}
            ],
        )

    async def generate(self, request: VideoRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 GROK_VIDEO_API_KEY"
            )
        model = request.model or self.model
        # 实测：只给 model+prompt 会 400「输入参数无效」，
        # 必须带 aspect_ratio（或 resolution）才会创建任务。
        aspect = "9:16" if request.height >= request.width else "16:9"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": request.prompt,
            "aspect_ratio": aspect,
        }
        if request.image_uri:
            # 图生视频：先带上首帧；若网关不认这个字段再退回纯文生
            payload["image"] = request.image_uri
        result = await self._run_task(payload, want=1)
        if not result.ok and request.image_uri and "image" in payload:
            payload.pop("image", None)
            result = await self._run_task(payload, want=1)
        if result.ok:
            for artifact in result.artifacts:
                artifact.setdefault("duration_ms", request.duration_ms)
                artifact.setdefault("width", request.width)
                artifact.setdefault("height", request.height)
        return result


# --------------------------------------------------------------------------- #
# 通义 MaaS · TTS（同步返回音频 URL）
# --------------------------------------------------------------------------- #


class QwenTtsProvider(BaseProvider):
    id = "qwen"
    kind = ProviderKind.SPEECH
    display_name = "通义 MaaS · 语音合成"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.tts_api_model
        self.api_key = api_key or settings.tts_api_key
        self.url = (base_url or settings.tts_url or "").strip()

    def configured(self) -> bool:
        return bool(self.api_key and self.url)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=[self.model], billing_unit="1k_chars",
            unit_price_cents=price_for(self.id, self.model, "1k_chars", 1),
        )

    async def synthesize(self, request: SpeechRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 TTS_API_KEY"
            )
        if not self.url:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.BAD_REQUEST.value, "未配置 TTS_URL"
            )
        model = request.model or self.model
        # 实测：文本必须放在 input.text_prompt，放别处一律 400「text_prompt must not be empty」
        payload = {
            "model": model,
            "input": {
                "text_prompt": request.text,
                "voice": request.voice_id or settings.tts_voice,
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=max(60, settings.provider_timeout_seconds)
            ) as client:
                resp = await client.post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if resp.status_code >= 400:
                    code, retryable = self.map_http_error(resp.status_code)
                    return ProviderResult.failure(
                        self.kind, code,
                        f"TTS 供应商返回 {resp.status_code}", retryable=retryable,
                        raw={"body": resp.text[:400]},
                    )
                body = resp.json() or {}
                audio = ((body.get("output") or {}).get("audio") or {})
                audio_url = str(audio.get("url") or "")
                if not audio_url:
                    return ProviderResult.failure(
                        self.kind, ProviderErrorCode.UNKNOWN.value,
                        "TTS 未返回音频 URL", raw={"body": str(body)[:400]},
                    )
                audio_resp = await client.get(audio_url, timeout=180)
                audio_resp.raise_for_status()
                content = audio_resp.content
        except httpx.TimeoutException:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "TTS 供应商超时", retryable=True
            )
        except httpx.HTTPError as exc:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"网络错误：{exc}", retryable=True
            )
        duration_ms = int(float(audio.get("duration") or 0) * 1000) or max(
            800, len(request.text) * 200
        )
        # 网关返回 wav；按 URL 后缀判断真实容器，避免把 wav 标成 mp3
        suffix = "wav" if ".wav" in audio_url.lower() else "mp3"
        mime = "audio/wav" if suffix == "wav" else "audio/mpeg"
        cost = price_for(self.id, model, "1k_chars", max(len(request.text), 1) / 1000)
        return ProviderResult(
            ok=True, kind=self.kind,
            data={"duration_ms": duration_ms},
            artifacts=[{
                "content": content,
                "filename": f"tts_{abs(hash(request.text)) % 10**8}.{suffix}",
                "mime_type": mime,
                "duration_ms": duration_ms,
            }],
            raw_summary={"model": model, "request_id": body.get("request_id")},
            usage={"chars": len(request.text)},
            cost_cents=cost, estimated_cost_cents=cost, billing_unit="1k_chars",
            request_id=str(body.get("request_id") or ""),
        )
