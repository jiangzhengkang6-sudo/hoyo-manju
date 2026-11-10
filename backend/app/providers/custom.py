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
import io
import math
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

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

#: 上传接口上限 10MB，留一点余量
_UPLOAD_MAX_BYTES = 9 * 1024 * 1024

#: 轮询任务时允许的连续网络失败次数。任务一旦提交，供应商就开始计费，
#: 一两次抖动就放弃等于「钱照扣、产物不要」；超过这个次数才认定确实拿不到。
_POLL_MAX_TRANSIENT = 6

#: 图像接口 `size` 支持的比例
_IMAGE_RATIOS: dict[str, float] = {
    "1:1": 1.0, "3:2": 1.5, "2:3": 2 / 3, "4:3": 4 / 3, "3:4": 0.75,
    "16:9": 16 / 9, "9:16": 9 / 16, "5:4": 1.25, "4:5": 0.8, "21:9": 21 / 9,
}

#: 参考图上传结果缓存（本地文件 → 公网 URL）。
#: 一批镜头里同一张角色主图会被反复引用（6 镜 × 2 角色 = 12 次），
#: 每次都上传一遍纯属浪费；key 带大小与 mtime，文件变了自然失效。
_REFERENCE_URL_CACHE: dict[str, str] = {}

#: grok-video-1.5 支持的画面比例（比图像少）
_VIDEO_RATIOS: dict[str, float] = {
    "1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "3:2": 1.5, "2:3": 2 / 3,
}

#: seedance-2 系列支持的比例（不含 3:2 / 2:3，含 4:3 / 3:4 / 21:9）
_SEEDANCE_RATIOS: dict[str, float] = {
    "1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16,
    "4:3": 4 / 3, "3:4": 0.75, "21:9": 21 / 9,
}


def _billing_usd(body: dict[str, Any]) -> float:
    """从任务回执里取真实账单（美元）。没有就返回 0，由调用方退回估算。"""
    billing = body.get("billing")
    if not isinstance(billing, dict):
        return 0.0
    try:
        return max(0.0, float(billing.get("cost_usd") or 0))
    except (TypeError, ValueError):
        return 0.0


def _ratio_label(width: int, height: int, options: dict[str, float]) -> str:
    """把像素尺寸换成语义比例（`9:16` 而不是 `1080*1920`）。

    误差超过 3% 就返回空串——宁可让网关用默认值，也不要把 4:3 硬塞成 16:9。
    """
    if not width or not height:
        return ""
    ratio = width / height
    best = min(options, key=lambda key: abs(options[key] - ratio))
    return best if abs(options[best] - ratio) / ratio <= 0.03 else ""


def _mime_for(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }.get(suffix, "image/png")


def _image_size(content: bytes) -> tuple[int, int]:
    """读图片真实像素；读不出返回 (0, 0)。"""
    if not content:
        return 0, 0
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as image:
            return int(image.width), int(image.height)
    except Exception:  # noqa: BLE001
        return 0, 0


def _shrink_image(content: bytes) -> tuple[bytes, str, str] | None:
    """超过上传上限时压成 JPEG（首帧只做参考，画质损失可接受）。"""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - 没装 Pillow 就放弃压缩
        return None
    try:
        image = Image.open(io.BytesIO(content)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    data = b""
    for quality in (88, 78, 68):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        data = buffer.getvalue()
        if len(data) <= _UPLOAD_MAX_BYTES:
            break
    if len(data) > _UPLOAD_MAX_BYTES:
        return None
    return data, "first_frame.jpg", "image/jpeg"



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

    # ------------------------------------------------------------------ #
    # 本地图片 → 公网 URL
    # 网关明确不接受 base64（也不接受 data URI），本地首帧必须先上传换 URL，
    # 否则图生视频会静默退化成文生视频——首帧一致性直接丢失。
    # ------------------------------------------------------------------ #
    def upload_url(self) -> str:
        if settings.toapis_upload_url:
            return settings.toapis_upload_url
        parsed = urlparse(self.submit_url)
        return f"{parsed.scheme}://{parsed.netloc}/v1/uploads/images"

    async def _upload_image(
        self, client: httpx.AsyncClient, path: Path, label: str = "首帧"
    ) -> tuple[str, ProviderResult | None]:
        try:
            content = path.read_bytes()
        except OSError as exc:
            return "", ProviderResult.failure(
                self.kind, ProviderErrorCode.BAD_REQUEST.value,
                f"{label}文件读取失败：{exc}", retryable=False,
            )
        filename, mime = path.name, _mime_for(path.name)
        if len(content) > _UPLOAD_MAX_BYTES:
            shrunk = _shrink_image(content)
            if shrunk is None:
                return "", ProviderResult.failure(
                    self.kind, ProviderErrorCode.BAD_REQUEST.value,
                    f"{label}超过 {_UPLOAD_MAX_BYTES // (1024 * 1024)}MB 且无法压缩",
                    retryable=False,
                )
            content, filename, mime = shrunk
        try:
            resp = await client.post(
                self.upload_url(),
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (filename, content, mime)},
                data={"purpose": "generation"},
                timeout=120,
            )
        except httpx.HTTPError as exc:
            from app.providers.net import describe_network_error

            return "", ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value,
                f"{label}上传失败：{describe_network_error(exc, self.upload_url())}",
                retryable=True,
            )
        if resp.status_code >= 400:
            code, retryable = self.map_http_error(resp.status_code)
            return "", ProviderResult.failure(
                self.kind, code, f"{self.display_name} {label}上传失败（{resp.status_code}）",
                retryable=retryable, raw={"body": resp.text[:400]},
            )
        body = resp.json() or {}
        url = str(((body.get("data") or {}) if isinstance(body, dict) else {}).get("url") or "")
        if not url:
            return "", ProviderResult.failure(
                self.kind, ProviderErrorCode.UNKNOWN.value,
                f"{self.display_name} {label}上传未返回 URL", raw={"body": str(body)[:300]},
            )
        return url, None

    async def fetch_result(self, task_id: str) -> ProviderResult:
        """按任务编号取回已生成的产物——**不提交新任务、不产生新费用**。

        用途：任务在网关侧成功、本地因为网络/超时没拉回来。补拉时要注意上游
        的硬限制：seedance 的成片在火山 TOS 只保留 24 小时，直链签名也是 24 小时，
        过期后 TOS 直接 403，重查任务也换不回新链接。所以这里把 403 翻译成
        「链接已过期，需要重新生成」，而不是让用户对着 `403 Forbidden` 猜。
        """
        async with httpx.AsyncClient(
            timeout=max(60, settings.provider_timeout_seconds)
        ) as client:
            body, failure = await self._wait_task(client, task_id)
            if failure is not None:
                return failure
            urls = self._result_urls(body or {})
            if not urls:
                return ProviderResult.failure(
                    self.kind, ProviderErrorCode.UNKNOWN.value,
                    f"{self.display_name} 任务没有产物地址",
                    raw={"body": str(body)[:400]}, provider_job_id=task_id,
                )
            artifacts: list[dict[str, Any]] = []
            try:
                for url in urls:
                    content = await self._download(client, url)
                    artifacts.append(self._artifact(content, len(artifacts)))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 401):
                    return ProviderResult.failure(
                        self.kind, ProviderErrorCode.UNAVAILABLE.value,
                        (
                            "产物链接已过期：供应商的成片只在源站保留 24 小时，"
                            "过期后无法再取回，只能重新生成"
                        ),
                        retryable=False, provider_job_id=task_id,
                    )
                return ProviderResult.failure(
                    self.kind, ProviderErrorCode.UNAVAILABLE.value,
                    f"产物下载失败（{exc.response.status_code}）",
                    retryable=True, provider_job_id=task_id,
                )
            except httpx.HTTPError as exc:
                from app.providers.net import describe_network_error

                return ProviderResult.failure(
                    self.kind, ProviderErrorCode.UNAVAILABLE.value,
                    f"产物下载失败：{describe_network_error(exc, '')}",
                    retryable=True, provider_job_id=task_id,
                )
        usd = _billing_usd(body or {})
        cost = int(round(usd * settings.usd_cny_rate * 100)) if usd else 0
        return ProviderResult(
            ok=True,
            kind=self.kind,
            artifacts=artifacts,
            provider_job_id=task_id,
            cost_cents=0,          # 这笔钱在生成时就发生了，补拉不再计费
            estimated_cost_cents=cost,
            raw_summary={"billing_usd": usd, "fetched": True},
        )

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
        """轮询到终态。返回 (最终响应, 失败结果)。

        **网络抖动不等于任务失败**：任务已经提交到网关，供应商那边照样在跑、
        照样计费。之前只要一次 `client.get` 抛异常就整单放弃，于是出现
        「本地 FAILED、供应商后台 completed 且扣了积分」——钱花了，产物丢了。
        现在只要还在 deadline 内就继续问，连续失败到阈值才认输。
        """
        deadline = time.time() + settings.task_timeout_seconds
        interval = max(1, settings.task_poll_interval_seconds)
        url = f"{self.task_base}/{task_id}"
        last_status = ""
        transient = 0
        while time.time() < deadline:
            try:
                poll = await client.get(url, headers=self._headers())
            except httpx.HTTPError:
                transient += 1
                if transient >= _POLL_MAX_TRANSIENT:
                    return None, ProviderResult.failure(
                        self.kind,
                        ProviderErrorCode.UNAVAILABLE.value,
                        (
                            f"{self.display_name} 轮询连续失败 {transient} 次；"
                            f"任务 {task_id} 可能仍在网关执行，请到供应商控制台核对"
                        ),
                        retryable=True,
                        provider_job_id=task_id,
                    )
                await asyncio.sleep(min(interval, 3))
                continue
            # 429 / 5xx 是网关侧的暂时性问题，任务本身没失败，继续等
            if poll.status_code == 429 or poll.status_code >= 500:
                transient += 1
                if transient >= _POLL_MAX_TRANSIENT:
                    return None, ProviderResult.failure(
                        self.kind,
                        ProviderErrorCode.UNAVAILABLE.value,
                        (
                            f"{self.display_name} 轮询反复失败（{poll.status_code}）；"
                            f"任务 {task_id} 可能仍在网关执行，请到供应商控制台核对"
                        ),
                        retryable=True,
                        provider_job_id=task_id,
                        raw={"body": poll.text[:200]},
                    )
                await asyncio.sleep(min(interval, 5))
                continue
            transient = 0
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
                    provider_job_id=task_id,
                )
            await asyncio.sleep(interval)
        return None, ProviderResult.failure(
            self.kind, ProviderErrorCode.TIMEOUT.value,
            (
                f"{self.display_name} 任务轮询超时（{settings.task_timeout_seconds}s，"
                f"最后状态 {last_status}）；任务 {task_id} 可能仍在网关执行"
            ),
            retryable=True,
            provider_job_id=task_id,
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

    async def _download(self, client: httpx.AsyncClient, url: str, *, attempts: int = 3) -> bytes:
        """下载产物。产物已经生成（钱已经花了），下载失败必须重试而不是丢掉。"""
        last_error: httpx.HTTPError | None = None
        for index in range(max(1, attempts)):
            try:
                resp = await client.get(url, timeout=180)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if resp.status_code < 400 and resp.content:
                    return resp.content
                transient = (
                    resp.status_code in (403, 404, 408, 429) or resp.status_code >= 500
                )
                if not transient:
                    resp.raise_for_status()
                last_error = httpx.HTTPStatusError(
                    f"下载失败（{resp.status_code}）", request=resp.request, response=resp
                )
            if index < attempts - 1:
                await asyncio.sleep(1.5 * (index + 1))
        if last_error is not None:
            raise last_error
        raise httpx.HTTPError("产物下载失败：没有拿到任何内容")

    def _artifact(self, content: bytes, index: int) -> dict[str, Any]:
        return {
            "content": content,
            "filename": f"{self.artifact_prefix}_{index + 1}.{self.artifact_ext}",
            "mime_type": self.result_mime,
        }

    async def _run_task(
        self,
        payload: dict[str, Any],
        *,
        want: int = 1,
        prepare: Callable[[httpx.AsyncClient, dict[str, Any]], Awaitable[ProviderResult | None]]
        | None = None,
    ) -> ProviderResult:
        """跑 `want` 次任务并汇总产物。

        `prepare` 在同一个 client 上做提交前的准备工作（如上传首帧换公网 URL），
        返回失败则整体失败——不要静默降级成一个「看起来成功但少了图」的请求。
        """
        artifacts: list[dict[str, Any]] = []
        raw: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(
                timeout=max(60, settings.provider_timeout_seconds)
            ) as client:
                if prepare is not None:
                    failure = await prepare(client, payload)
                    if failure is not None:
                        return failure
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
                    # 网关完成回执里有真实账单（美元）：能拿到就按它结算，
                    # 比按价格表估算准得多（尤其视频按时长档位计价）。
                    usd = _billing_usd(body or {})
                    if usd:
                        raw["billing_usd"] = round(float(raw.get("billing_usd") or 0) + usd, 6)
                    for url in urls:
                        content = await self._download(client, url)
                        artifacts.append(self._artifact(content, len(artifacts)))
        except httpx.TimeoutException as exc:
            from app.providers.net import describe_network_error

            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value,
                describe_network_error(exc, self.submit_url), retryable=True,
            )
        except httpx.HTTPError as exc:
            from app.providers.net import describe_network_error

            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value,
                describe_network_error(exc, self.submit_url), retryable=True,
            )
        if not artifacts:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNKNOWN.value,
                f"{self.display_name} 未返回任何产物",
            )
        cost = self.capabilities().unit_price_cents * len(artifacts)
        usd = float(raw.get("billing_usd") or 0)
        if usd > 0:
            cost = max(1, int(round(usd * settings.usd_cny_rate * 100)))
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
            # nano-banana 支持多图参考（官方口径最多 14 张）；声明出来，
            # 提交前就能拦住「角色太多、参考图超限」而不是等网关报错
            max_reference_images=14,
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
        # `size` 收的是**宽高比**（"9:16"），不是像素；以前不传 → 永远出 1024² 方图，
        # 合成时只能左右补黑边，竖屏漫剧的构图全被浪费。
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
        size = _ratio_label(request.width, request.height, _IMAGE_RATIOS)
        if size:
            payload["size"] = size
        resolution = (settings.gemini_image_resolution or "").strip().upper()
        if resolution in {"1K", "2K", "4K"}:
            payload["metadata"] = {"resolution": resolution}

        # 参考图（角色主图）：远端存储直接给公网 URL；本地文件必须先上传换 URL。
        # 网关明确不收相对签名路径、也不收 base64 —— 少了这一步，角色一致性
        # 就只剩提示词里的文字描述，同一个角色会被画成好几个人。
        remote_refs = [
            uri for uri in request.reference_uris
            if uri and uri.startswith(("http://", "https://"))
        ]
        local_refs = [Path(p) for p in request.reference_paths if p and Path(p).exists()]

        async def _attach_refs(
            client: httpx.AsyncClient, body: dict[str, Any]
        ) -> ProviderResult | None:
            urls = list(remote_refs)
            for path in local_refs:
                url, failure = await self._public_reference(client, path)
                if failure is not None:
                    return failure
                if url:
                    urls.append(url)
            if urls:
                body["reference_images"] = urls
            return None

        result = await self._run_task(
            payload,
            want=max(1, request.count),
            prepare=_attach_refs if (remote_refs or local_refs) else None,
        )
        if result.ok:
            # 产物尺寸以真实文件为准：请求的是 9:16，实际给多少只有解码后才知道
            for artifact in result.artifacts:
                width, height = _image_size(artifact.get("content") or b"")
                artifact.setdefault("width", width or request.width)
                artifact.setdefault("height", height or request.height)
        return result

    async def _public_reference(
        self, client: httpx.AsyncClient, path: Path
    ) -> tuple[str, ProviderResult | None]:
        """本地参考图换公网 URL（带缓存，同一批镜头不重复上传）。"""
        try:
            stat = path.stat()
        except OSError as exc:
            return "", ProviderResult.failure(
                self.kind, ProviderErrorCode.BAD_REQUEST.value,
                f"角色参考图读取失败：{exc}", retryable=False,
            )
        key = f"{path}|{stat.st_size}|{int(stat.st_mtime)}"
        cached = _REFERENCE_URL_CACHE.get(key)
        if cached:
            return cached, None
        url, failure = await self._upload_image(client, path, label="角色参考图")
        if failure is not None:
            return "", failure
        _REFERENCE_URL_CACHE[key] = url
        return url, None


# --------------------------------------------------------------------------- #
# toapis · 视频（Grok video）
# --------------------------------------------------------------------------- #


class ToapisVideoProvider(_ToapisTaskProvider):
    """toapis 视频网关。同一网关下不同模型的**字段协议不一样**：

    - `grok-video-1.5`：`video_generation_mode` + `image`，时长 1~15 秒；
    - `seedance-2*`：`image_with_roles=[{url, role:"first_frame"}]`，时长 4~15 秒。

    写死一套参数就会出现「提交成功但没有图/没有首帧」。所以按模型名分派。
    """

    id = "grok"  # 历史 id，数据库里已有记录，不随模型改名
    kind = ProviderKind.VIDEO
    display_name = "视频（toapis 网关）"
    result_mime = "video/mp4"
    artifact_ext = "mp4"
    artifact_prefix = "video"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.grok_video_model
        self.api_key = api_key or settings.grok_video_api_key
        self.submit_url = (base_url or settings.grok_video_url).rstrip("/")
        self.task_base = self.submit_url

    @property
    def family(self) -> str:
        return "seedance" if "seedance" in (self.model or "").lower() else "grok"

    def _seconds(self, duration_ms: int) -> int:
        """各模型的时长下限不同：seedance 最短 4 秒，比剧本镜头长时要靠合成裁剪。"""
        low, high = (4, 15) if self.family == "seedance" else (1, 15)
        return max(low, min(high, math.ceil(duration_ms / 1000)))

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        low = 4000 if self.family == "seedance" else 2000
        return ProviderCapabilities(
            models=[self.model], duration_ms_min=low, duration_ms_max=15000,
            billing_unit="second", estimated_latency_seconds=300,
            unit_price_cents=price_for(self.id, self.model, "second", 1),
        )

    async def estimate_cost(self, request: VideoRequest) -> CostEstimate:
        seconds = self._seconds(request.duration_ms)
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
        seconds = self._seconds(request.duration_ms)
        aspect = _ratio_label(request.width, request.height, _SEEDANCE_RATIOS if self.family == "seedance" else _VIDEO_RATIOS) or (
            "9:16" if request.height >= request.width else "16:9"
        )
        payload: dict[str, Any] = {
            "model": model,
            "prompt": request.prompt,
            "duration": seconds,
            "resolution": settings.grok_video_resolution,
            "aspect_ratio": aspect,
        }
        if self.family == "seedance":
            # 成片音轨我们自己配（TTS），模型侧出声会和配音打架
            payload["generate_audio"] = False
        else:
            payload["video_generation_mode"] = "text_to_video"

        async def _attach_first_frame(
            client: httpx.AsyncClient, body: dict[str, Any]
        ) -> ProviderResult | None:
            """把首帧换成网关能取到的公网 URL。"""
            url = ""
            path = request.image_path
            if path and Path(path).exists():
                url, failure = await self._upload_image(client, Path(path))
                if failure is not None:
                    return failure
            elif request.image_uri.startswith(("http://", "https://")):
                url = request.image_uri
            if not url:
                # 没有任何可用首帧：只能文生视频（画面一致性会丢，但至少不空转）
                return None
            if self.family == "seedance":
                body["image_with_roles"] = [{"url": url, "role": "first_frame"}]
            else:
                body["image"] = url
                body["video_generation_mode"] = "first_frame_image_to_video"
            return None

        result = await self._run_task(payload, want=1, prepare=_attach_first_frame)
        if result.ok:
            # 拿到网关真实账单时按账单结算；拿不到才退回「按秒 × 单价」
            if not (result.raw_summary or {}).get("billing_usd"):
                cost = price_for(self.id, model, "second", seconds)
                result.cost_cents = cost
                result.estimated_cost_cents = cost
            for artifact in result.artifacts:
                artifact["duration_ms"] = seconds * 1000
                artifact.setdefault("width", request.width)
                artifact.setdefault("height", request.height)
        return result


# --------------------------------------------------------------------------- #
# 通义 MaaS · 图像生成（同步返回图片 URL）
# --------------------------------------------------------------------------- #


#: 宽高比 → 通义生图的推荐尺寸（模型对尺寸有档位限制，直接传 1080*1920 可能被拒）
_QWEN_IMAGE_SIZES: dict[str, str] = {
    "9:16": "928*1664",
    "16:9": "1664*928",
    "1:1": "1328*1328",
    "3:4": "1140*1472",
    "4:3": "1472*1140",
}


def _qwen_image_size(width: int, height: int) -> str:
    """按请求宽高比挑一个模型支持的尺寸；比例不在表里就返回空串（不传 size）。"""
    if not width or not height:
        return ""
    ratio = width / height
    best = min(
        _QWEN_IMAGE_SIZES.items(),
        key=lambda kv: abs(float(kv[0].split(":")[0]) / float(kv[0].split(":")[1]) - ratio),
    )
    target = float(best[0].split(":")[0]) / float(best[0].split(":")[1])
    # 误差超过 3% 就不硬套，交给模型默认尺寸
    return best[1] if abs(target - ratio) / ratio <= 0.03 else ""


def _qwen_image_urls(body: dict[str, Any]) -> list[str]:
    """从响应里抠图片地址：优先 multimodal choices，再兜底 results。"""
    urls: list[str] = []
    output = body.get("output") or {}
    for choice in output.get("choices") or []:
        content = ((choice or {}).get("message") or {}).get("content") or []
        for item in content:
            if isinstance(item, dict) and item.get("image"):
                urls.append(str(item["image"]))
    for item in output.get("results") or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
    return urls


class QwenImageProvider(BaseProvider):
    """通义 MaaS · 生图（qwen-image 系列，同步接口）。

    为什么单独实现：toapis（Gemini）是**异步任务**协议，而通义网关的
    `multimodal-generation` 是**同步**的——一次请求直接返回图片 URL。
    两条链路的提交、轮询、产物解析都不同，硬套会拿不到图。

    另一个实际收益：它与 TTS 是同一个域名（国内可达），在没有国际出口的
    机器上依然能出图。
    """

    id = "qwen"
    kind = ProviderKind.IMAGE
    display_name = "通义 MaaS · 图像生成"

    def __init__(self, model: str = "", api_key: str = "", base_url: str = "") -> None:
        self.model = model or settings.qwen_image_model
        # 缺省复用 TTS 的 key：同一个网关、同一份凭证，少一处配置
        self.api_key = api_key or settings.qwen_image_api_key or settings.tts_api_key
        self.url = (base_url or settings.qwen_image_url).rstrip()

    def configured(self) -> bool:
        return bool(self.api_key)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=[self.model], max_candidates=4, billing_unit="image",
            estimated_latency_seconds=60,
            unit_price_cents=price_for(self.id, self.model, "image", 1),
        )

    async def generate(self, request: ImageRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.AUTH.value, "未配置 QWEN_IMAGE_API_KEY 或 TTS_API_KEY"
            )
        model = request.model or self.model
        prompt = request.prompt
        if request.negative_prompt:
            prompt = f"{prompt}\n避免出现：{request.negative_prompt}"
        count = max(1, request.count)
        artifacts: list[dict[str, Any]] = []
        raw: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(
                timeout=max(120, settings.provider_timeout_seconds)
            ) as client:
                for index in range(count):
                    size = _qwen_image_size(request.width, request.height)
                    parameters: dict[str, Any] = {"watermark": False}
                    if size:
                        parameters["size"] = size
                    payload = {
                        "model": model,
                        "input": {
                            "messages": [
                                {"role": "user", "content": [{"text": prompt}]}
                            ]
                        },
                        "parameters": parameters,
                    }
                    resp = await client.post(
                        self.url,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    if resp.status_code == 400 and size and "size" in resp.text.lower():
                        # 档位不被接受：退掉 size 再试一次（400 不计费）
                        parameters.pop("size", None)
                        payload["parameters"] = parameters
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
                        if "quota" in resp.text.lower() or "余额不足" in resp.text:
                            return ProviderResult.failure(
                                self.kind, ProviderErrorCode.UNAVAILABLE.value,
                                "通义账户额度不足，请充值后重试（与密钥无关）",
                                retryable=False, raw={"body": resp.text[:300]},
                            )
                        return ProviderResult.failure(
                            self.kind, code,
                            f"{self.display_name} 失败（{resp.status_code}）",
                            retryable=retryable, raw={"body": resp.text[:300]},
                        )
                    body = resp.json() or {}
                    urls = _qwen_image_urls(body)
                    if not urls:
                        return ProviderResult.failure(
                            self.kind, ProviderErrorCode.UNKNOWN.value,
                            f"{self.display_name} 未返回图片地址", raw={"body": str(body)[:300]},
                        )
                    raw[f"call_{index}"] = {"request_id": body.get("request_id")}
                    for url in urls:
                        content = await client.get(url, timeout=180)
                        content.raise_for_status()
                        # 真实尺寸以图片为准：模型不一定严格按 size 出图
                        width, height = request.width, request.height
                        try:
                            from PIL import Image as _Image

                            with _Image.open(io.BytesIO(content.content)) as image:
                                width, height = image.size
                        except Exception:  # noqa: BLE001 - 尺寸读不出就退回请求值
                            pass
                        artifacts.append(
                            {
                                "content": content.content,
                                "filename": f"qwen_{index + 1}.png",
                                "mime_type": "image/png",
                                "width": width,
                                "height": height,
                            }
                        )
        except httpx.TimeoutException as exc:
            from app.providers.net import describe_network_error

            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value,
                describe_network_error(exc, self.url), retryable=True,
            )
        except httpx.HTTPError as exc:
            from app.providers.net import describe_network_error

            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value,
                describe_network_error(exc, self.url), retryable=True,
            )
        if not artifacts:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNKNOWN.value, f"{self.display_name} 未返回任何产物"
            )
        cost = self.capabilities().unit_price_cents * len(artifacts)
        return ProviderResult(
            ok=True, kind=self.kind, artifacts=artifacts,
            raw_summary=raw, cost_cents=cost, estimated_cost_cents=cost,
        )


# --------------------------------------------------------------------------- #
# 通义 MaaS · TTS（同步返回音频 URL）
# --------------------------------------------------------------------------- #


def _oss_reason(resp: httpx.Response) -> str:
    """从 OSS 的 XML 错误体里抠出 Code/Message，便于定位签名/权限/对象不存在。"""
    body = (resp.text or "")[:400]
    code = re.search(r"<Code>(.*?)</Code>", body)
    message = re.search(r"<Message>(.*?)</Message>", body)
    if code or message:
        return " ".join(
            p for p in (code.group(1) if code else "", message.group(1) if message else "") if p
        )
    return body.replace("\n", " ")[:120]


async def _download_audio(
    client: httpx.AsyncClient, url: str, *, attempts: int = 4, base_delay: float = 1.2
) -> tuple[httpx.Response | None, str | None]:
    """下载 TTS 产物，对「对象还没就绪」造成的 403/404 做短退避重试。

    实测：网关返回 `audio.url` 后**立刻** GET，OSS 偶发 403（签名本身是对的，
    对象还不可见/被短暂限流），隔一两秒再取就正常。此前直接
    `raise_for_status()`，于是「试听」时好时坏，错误又被路由映射成
    409「当前状态不允许该操作」，极难排查。
    """
    last: httpx.Response | None = None
    for i in range(attempts):
        try:
            resp = await client.get(url, timeout=180)
        except httpx.HTTPError as exc:
            if i == attempts - 1:
                return None, f"网络错误：{exc}"
            await asyncio.sleep(base_delay * (i + 1))
            continue
        if resp.status_code < 400 and resp.content:
            return resp, None
        last = resp
        transient = resp.status_code in (403, 404, 408, 429) or resp.status_code >= 500
        if transient and i < attempts - 1:
            await asyncio.sleep(base_delay * (i + 1))
            continue
        break
    if last is None:
        return None, "音频下载失败"
    return last, f"HTTP {last.status_code} {_oss_reason(last)}".strip()


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
        # 该模型**不认 voice 参数**（实测 Ethan/Cherry/不存在的值出来都是同一个女声），
        # 只认「指令前缀」：加「用低沉浑厚的成年男声朗读：」后基频 242Hz→164Hz，
        # 时长只多 0.76s（指令不会被念出来）。所以音色靠 style_prompt 实现。
        text = request.text
        style = (request.style_prompt or "").strip()
        if style:
            text = f"{style}{text}"
        # 实测：文本必须放在 input.text_prompt，放别处一律 400「text_prompt must not be empty」
        payload_input: dict = {
            "text_prompt": text,
            "voice": request.voice_id or settings.tts_voice,
        }
        # 只传用户显式要求的输出参数：不传时沿用供应商默认（台词一直是默认）
        if request.output_format:
            payload_input["format"] = request.output_format
        if request.sample_rate:
            payload_input["sample_rate"] = request.sample_rate
        if request.channels:
            payload_input["channels"] = request.channels
        payload = {"model": model, "input": payload_input}
        try:
            async with httpx.AsyncClient(
                timeout=max(60, settings.provider_timeout_seconds)
            ) as client:
                # DNS 抖动会瞬时抛 `getaddrinfo failed (Errno 11001)`，隔一两秒就恢复；
                # 任务级重试间隔是 5 秒起步，两次尝试常常落在同一个抖动窗口里（实测
                # 11 秒内两次全挂，用户只看到「失败」）。所以连接类错误在这里就地重试。
                resp = None
                for attempt in range(3):
                    try:
                        resp = await client.post(
                            self.url,
                            headers={
                                "Authorization": f"Bearer {self.api_key}",
                                "Content-Type": "application/json",
                            },
                            json=payload,
                        )
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if attempt >= 2:
                            raise
                        await asyncio.sleep(0.8 * (attempt + 1))
                if resp is None:
                    return ProviderResult.failure(
                        self.kind, ProviderErrorCode.UNAVAILABLE.value,
                        "TTS 请求没有发出（连接始终失败）", retryable=True,
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
                audio_resp, audio_err = await _download_audio(client, audio_url)
                if audio_err is not None or audio_resp is None:
                    # 403/404 常见于「网关刚返回 URL、OSS 对象还没就绪」，重试几次就能下来；
                    # 这里把 OSS 的错误码原样带出来，否则界面只会显示「网络错误」看不出所以然。
                    return ProviderResult.failure(
                        self.kind, ProviderErrorCode.UNAVAILABLE.value,
                        f"音频下载失败：{audio_err}", retryable=True,
                        raw={"url": audio_url[:200], "body": getattr(audio_resp, "text", "")[:300]},
                    )
                content = audio_resp.content
        except httpx.TimeoutException as exc:
            from app.providers.net import describe_network_error

            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value,
                describe_network_error(exc, self.url), retryable=True,
            )
        except httpx.HTTPError as exc:
            from app.providers.net import describe_network_error

            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value,
                describe_network_error(exc, self.url), retryable=True,
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
