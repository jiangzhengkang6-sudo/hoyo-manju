"""TTS 产物下载的短退避重试。

线上现象：点「试听」偶发返回 409「当前状态不允许该操作」，错误里其实是
`Client error '403 Forbidden'` —— 网关刚返回的 audio.url 立刻 GET 会被 OSS 拒，
隔一两秒再取就正常。此前直接 raise_for_status()，一次抖动就让试听失败。
"""

from __future__ import annotations

import asyncio

from app.providers.custom import _download_audio, _oss_reason


class _Resp:
    def __init__(self, status: int, body: bytes = b"", text: str = "") -> None:
        self.status_code = status
        self.content = body
        self.text = text or body.decode("utf-8", "ignore")


class _Client:
    """按顺序返回预设响应的假 AsyncClient。"""

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self.calls = 0

    async def get(self, url: str, timeout: float | None = None) -> _Resp:  # noqa: ARG002
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]


def _run(coro):
    return asyncio.run(coro)


def test_retries_until_object_is_ready() -> None:
    client = _Client([_Resp(403), _Resp(403), _Resp(200, b"RIFF....WAVE")])
    resp, err = _run(_download_audio(client, "http://oss/x.wav", attempts=4, base_delay=0))
    assert err is None
    assert resp is not None and resp.status_code == 200
    assert client.calls == 3, client.calls


def test_failure_carries_oss_error_code() -> None:
    body = "<Error><Code>AccessDenied</Code><Message>Invalid signature</Message></Error>"
    client = _Client([_Resp(403, text=body)] * 4)
    resp, err = _run(_download_audio(client, "http://oss/x.wav", attempts=2, base_delay=0))
    assert resp is not None and resp.status_code == 403
    assert err and "403" in err and "AccessDenied" in err, err


def test_non_transient_error_is_not_retried() -> None:
    client = _Client([_Resp(400, text="bad request")] * 3)
    resp, err = _run(_download_audio(client, "http://oss/x.wav", attempts=4, base_delay=0))
    assert resp is not None and resp.status_code == 400
    assert client.calls == 1, client.calls
    assert err and "400" in err


def test_oss_reason_prefers_xml_fields() -> None:
    resp = _Resp(403, text="<Error><Code>NoSuchKey</Code><Message>not found</Message></Error>")
    assert _oss_reason(resp) == "NoSuchKey not found"
