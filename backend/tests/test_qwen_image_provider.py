"""通义同步生图适配器：请求形状与产物解析。

与 toapis（异步任务）不同，通义网关的 `multimodal-generation` 一次请求直接返回
图片 URL，不需要 /tasks 轮询。这两个协议混用会拿不到图，所以单独测。
"""

from __future__ import annotations

import asyncio

from app.providers import custom
from app.providers.base import ImageRequest


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None, content: bytes = b"") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.content = content
        self.text = str(payload)[:300]

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(self.status_code)


class _Client:
    last_payload: dict | None = None

    def __init__(self, *args, **kwargs) -> None:  # noqa: ARG002
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url, headers=None, json=None):  # noqa: ARG002
        _Client.last_payload = json
        return _Resp(
            200,
            {
                "output": {
                    "choices": [
                        {"message": {"content": [{"image": "http://oss/x.png"}]}}
                    ]
                },
                "request_id": "req_1",
            },
        )

    async def get(self, url, timeout=None):  # noqa: ARG002
        return _Resp(200, {}, b"\x89PNG\r\n\x1a\nfake")


def test_generate_parses_multimodal_response(monkeypatch) -> None:
    monkeypatch.setattr(custom.httpx, "AsyncClient", _Client)
    provider = custom.QwenImageProvider(
        model="qwen-image-2.1-turbo", api_key="k", base_url="http://gateway/img"
    )
    result = asyncio.run(
        provider.generate(ImageRequest(prompt="雨夜街道，霓虹", count=1, width=720, height=1280))
    )
    assert result.ok, result.error_message
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact["content"].startswith(b"\x89PNG")
    assert (artifact["width"], artifact["height"]) == (720, 1280)

    payload = _Client.last_payload or {}
    # 文本必须放在 messages[0].content[0].text；尺寸按宽高比映射到模型支持的档位
    text = payload["input"]["messages"][0]["content"][0]["text"]
    assert text.startswith("雨夜街道")
    assert (payload.get("parameters") or {}).get("size") == "928*1664"
    assert payload["input"]["messages"][0]["role"] == "user"


def test_negative_prompt_is_appended(monkeypatch) -> None:
    monkeypatch.setattr(custom.httpx, "AsyncClient", _Client)
    provider = custom.QwenImageProvider(api_key="k", base_url="http://gateway/img")
    asyncio.run(
        provider.generate(
            ImageRequest(prompt="主角", negative_prompt="低清, 多手指", count=1)
        )
    )
    text = (_Client.last_payload or {})["input"]["messages"][0]["content"][0]["text"]
    assert "避免出现：低清, 多手指" in text


def test_missing_key_fails_fast() -> None:
    provider = custom.QwenImageProvider(api_key="", base_url="http://gateway/img")
    provider.api_key = ""
    result = asyncio.run(provider.generate(ImageRequest(prompt="x")))
    assert not result.ok
    assert result.error_code == "PROVIDER_AUTH"
