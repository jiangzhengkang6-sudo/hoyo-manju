"""toapis 网关适配器：图像按比例出图、视频真图生视频、按秒计费。

这几条都是**协议细节**，写错了不会有异常，只会静默降级：
- 图像不传 `size` → 永远 1024² 方图，竖屏构图被裁；
- 视频把本地签名路径（`/api/v1/storage/signed/...`）当首帧 URL → 网关取不到，
  退化成文生视频，画面前后不一致；
- 视频按「产物个数」计费 → 2 秒和 8 秒一个价，配额预估与实扣永远对不上。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image

from app.providers import custom
from app.providers.base import ImageRequest, VideoRequest


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None, content: bytes = b"") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.content = content
        self.text = str(payload or "")[:300]

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(self.status_code)


class _FakeClient:
    """按 URL 分流：上传 / 提交 / 轮询 / 下载。"""

    def __init__(
        self, *, upload_status: int = 200, task_status: str = "completed",
        billing_usd: float | None = None,
    ) -> None:
        self.upload_status = upload_status
        self.task_status = task_status
        self.billing_usd = billing_usd
        self.uploads: list[dict] = []
        self.submits: list[dict] = []

    def __call__(self, *args, **kwargs) -> "_FakeClient":  # 让 httpx.AsyncClient(...) 直接返回自身
        return self

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url, headers=None, json=None, files=None, data=None, timeout=None):  # noqa: ARG002
        if "/uploads/" in url:
            name, content, mime = (files or {}).get("file", ("x", b"", ""))
            self.uploads.append({"url": url, "name": name, "size": len(content), "mime": mime})
            return _Resp(
                self.upload_status,
                {"success": True, "data": {"id": "up_1", "url": "https://files.toapis.cn/f/first.png"}},
            )
        self.submits.append({"url": url, "payload": json, "headers": headers})
        return _Resp(200, {"id": "task_1", "status": "queued"})

    async def get(self, url, headers=None, timeout=None):  # noqa: ARG002
        if url.startswith("https://cdn."):
            return _Resp(200, {}, b"\x00\x00\x00\x18ftypmp42fake")
        body: dict = {
            "status": self.task_status,
            "result": {"data": [{"url": "https://cdn.example.com/out.mp4"}]},
        }
        if self.billing_usd is not None:
            body["billing"] = {"status": "settled", "cost_usd": str(self.billing_usd)}
        return _Resp(200, body)


def _png(path: Path, size: tuple[int, int] = (90, 160)) -> Path:
    Image.new("RGB", size, (20, 30, 40)).save(path)
    return path


def test_image_payload_uses_ratio_and_resolution(monkeypatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(custom.httpx, "AsyncClient", client)
    monkeypatch.setattr(custom.settings, "gemini_image_resolution", "2K")
    provider = custom.ToapisImageProvider(
        model="gemini-nano-banana-2.1", api_key="k",
        base_url="https://toapis.cn/v1/images/generations",
    )
    result = asyncio.run(
        provider.generate(ImageRequest(prompt="雨夜天台", count=1, width=1080, height=1920))
    )
    assert result.ok, result.error_message
    payload = client.submits[0]["payload"]
    assert payload["size"] == "9:16"      # 宽高比，不是像素
    assert payload["n"] == 1
    assert payload["metadata"] == {"resolution": "2K"}


def test_image_artifact_reports_real_pixel_size(monkeypatch, tmp_path) -> None:
    path = _png(tmp_path / "img.png", (120, 200))
    content = path.read_bytes()

    class _C(_FakeClient):
        async def get(self, url, headers=None, timeout=None):  # noqa: ARG002
            if url.startswith("https://cdn."):
                return _Resp(200, {}, content)
            return _Resp(200, {"status": "completed", "result": {"data": [{"url": "https://cdn.x/a.png"}]}})

    monkeypatch.setattr(custom.httpx, "AsyncClient", _C())
    provider = custom.ToapisImageProvider(api_key="k", base_url="https://toapis.cn/v1/images/generations")
    result = asyncio.run(provider.generate(ImageRequest(prompt="x", count=1)))
    assert (result.artifacts[0]["width"], result.artifacts[0]["height"]) == (120, 200)


def test_video_uploads_first_frame_and_uses_first_frame_mode(monkeypatch, tmp_path) -> None:
    frame = _png(tmp_path / "frame.png", (90, 160))
    client = _FakeClient()
    monkeypatch.setattr(custom.httpx, "AsyncClient", client)
    provider = custom.ToapisVideoProvider(
        model="grok-video-1.5", api_key="k",
        base_url="https://toapis.cn/v1/videos/generations",
    )
    result = asyncio.run(
        provider.generate(
            VideoRequest(
                prompt="推镜", image_uri="", image_path=str(frame),
                duration_ms=2000, width=1080, height=1920,
            )
        )
    )
    assert result.ok, result.error_message
    # 本地首帧必须先上传换公网 URL（网关明确不收 base64 / 本地路径）
    assert client.uploads and client.uploads[0]["url"] == "https://toapis.cn/v1/uploads/images"
    payload = client.submits[0]["payload"]
    assert payload["video_generation_mode"] == "first_frame_image_to_video"
    assert payload["image"] == "https://files.toapis.cn/f/first.png"
    assert payload["duration"] == 2
    assert payload["resolution"] == "720p"
    assert payload["aspect_ratio"] == "9:16"


def test_video_cost_is_charged_by_seconds(monkeypatch, tmp_path) -> None:
    frame = _png(tmp_path / "frame.png")
    monkeypatch.setattr(custom.httpx, "AsyncClient", _FakeClient())
    provider = custom.ToapisVideoProvider(
        model="grok-video-1.5", api_key="k", base_url="https://toapis.cn/v1/videos/generations"
    )
    result = asyncio.run(
        provider.generate(
            VideoRequest(prompt="x", image_path=str(frame), duration_ms=4000,
                         width=1080, height=1920)
        )
    )
    assert result.ok
    unit = custom.price_for("grok", "grok-video-1.5", "second", 1)
    assert result.cost_cents == unit * 4          # 4 秒
    assert result.artifacts[0]["duration_ms"] == 4000


def test_billing_receipt_wins_over_price_table(monkeypatch, tmp_path) -> None:
    """网关回执有真实账单（美元）时按账单结算：预估价与实扣价的差异才有据可查。"""
    frame = _png(tmp_path / "frame.png")
    monkeypatch.setattr(custom.httpx, "AsyncClient", _FakeClient(billing_usd=0.13338))
    monkeypatch.setattr(custom.settings, "usd_cny_rate", 7.2)
    provider = custom.ToapisVideoProvider(
        api_key="k", base_url="https://toapis.cn/v1/videos/generations"
    )
    result = asyncio.run(
        provider.generate(VideoRequest(prompt="x", image_path=str(frame), duration_ms=4000))
    )
    assert result.ok
    assert result.cost_cents == round(0.13338 * 7.2 * 100)   # 96 分


def test_seedance_uses_image_with_roles(monkeypatch, tmp_path) -> None:
    frame = _png(tmp_path / "frame.png")
    client = _FakeClient()
    monkeypatch.setattr(custom.httpx, "AsyncClient", client)
    provider = custom.ToapisVideoProvider(
        model="seedance-2-mini", api_key="k", base_url="https://toapis.cn/v1/videos/generations"
    )
    result = asyncio.run(
        provider.generate(
            VideoRequest(prompt="推镜", image_path=str(frame), duration_ms=2000,
                         width=1080, height=1920)
        )
    )
    assert result.ok, result.error_message
    payload = client.submits[0]["payload"]
    assert payload["duration"] == 4                      # 该模型最短 4 秒
    assert payload["image_with_roles"] == [
        {"url": "https://files.toapis.cn/f/first.png", "role": "first_frame"}
    ]
    assert payload["generate_audio"] is False            # 配音自己配，别让模型出声
    assert "video_generation_mode" not in payload        # 那是 grok 的字段
    assert provider.capabilities().duration_ms_min == 4000


def test_video_without_frame_falls_back_to_text_mode(monkeypatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(custom.httpx, "AsyncClient", client)
    provider = custom.ToapisVideoProvider(
        model="grok-video-1.5", api_key="k", base_url="https://toapis.cn/v1/videos/generations"
    )
    result = asyncio.run(
        provider.generate(VideoRequest(prompt="x", image_uri="", image_path="", duration_ms=2000))
    )
    assert result.ok
    assert client.uploads == []
    assert client.submits[0]["payload"]["video_generation_mode"] == "text_to_video"
    assert "image" not in client.submits[0]["payload"]


def test_upload_failure_aborts_instead_of_silent_text_fallback(monkeypatch, tmp_path) -> None:
    frame = _png(tmp_path / "frame.png")
    client = _FakeClient(upload_status=500)
    monkeypatch.setattr(custom.httpx, "AsyncClient", client)
    provider = custom.ToapisVideoProvider(api_key="k", base_url="https://toapis.cn/v1/videos/generations")
    result = asyncio.run(
        provider.generate(VideoRequest(prompt="x", image_path=str(frame), duration_ms=2000))
    )
    assert not result.ok
    assert "首帧上传失败" in (result.error_message or "")
    assert client.submits == []       # 没有偷偷改成文生视频去花钱


def test_upload_url_follows_submit_origin(monkeypatch) -> None:
    monkeypatch.setattr(custom.settings, "toapis_upload_url", "")
    provider = custom.ToapisVideoProvider(api_key="k", base_url="https://toapis.cn/v1/videos/generations")
    assert provider.upload_url() == "https://toapis.cn/v1/uploads/images"
    other = custom.ToapisVideoProvider(api_key="k", base_url="https://toapis.com/v1/videos/generations")
    assert other.upload_url() == "https://toapis.com/v1/uploads/images"


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [(1080, 1920, "9:16"), (1920, 1080, "16:9"), (1080, 1080, "1:1"), (1080, 1440, "3:4")],
)
def test_image_ratio_mapping(width: int, height: int, expected: str) -> None:
    assert custom._ratio_label(width, height, custom._IMAGE_RATIOS) == expected


def test_video_ratio_mapping_drops_unsupported() -> None:
    # 4:3 不在 grok 支持清单里：宁可返回空（走默认），也不要硬塞
    assert custom._ratio_label(1440, 1080, custom._VIDEO_RATIOS) == ""
