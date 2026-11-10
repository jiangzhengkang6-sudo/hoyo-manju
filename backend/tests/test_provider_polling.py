"""供应商任务轮询：网络抖动不能把「已经提交出去的任务」判死。

真实事故（2026-11-06）：视频任务 16:35 提交到网关，网关 16:36:55 跑完并计费；
本地在 16:36:08 因为一次轮询网络错误就整单失败、全额退回预留。
结果是「本地显示失败、供应商后台显示成功且已扣积分」——钱花了，产物没了，
账本上却一分钱都看不到。

这里盯三件事：
1. 轮询遇到网络错误要接着问，不能一次就放弃；
2. 提交过的任务失败后不能自动重试（否则等于重复下单）；
3. 失败释放的备注里必须带上网关任务 id，让用户能去控制台核对。
"""

from __future__ import annotations

import asyncio
import types

import httpx
import pytest

from app.providers import custom as custom_module
from app.providers.base import ProviderErrorCode
from app.providers.custom import ToapisVideoProvider
from app.services.executor import _release_reason, _submitted_job_ids


class _Response:
    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = str(self._body)
        self.content = b"x"
        self.request = httpx.Request("GET", "https://example.invalid")

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=self.request, response=self  # type: ignore[arg-type]
            )


class _ScriptedClient:
    """按顺序返回「异常 / 响应」，模拟轮询过程中的网络抖动。"""

    def __init__(self, steps: list[object]) -> None:
        self.steps = steps
        self.calls = 0

    async def get(self, url: str, **kwargs: object) -> _Response:
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return step  # type: ignore[return-value]


@pytest.fixture()
def fast_sleep(monkeypatch: pytest.MonkeyPatch):
    """把轮询间隔的等待压成 0，否则测试要真的睡好几秒。"""

    async def _sleep(_seconds: float) -> None:
        # 不能再调 asyncio.sleep：patch 的是同一个模块属性，会无限递归
        return None

    monkeypatch.setattr(custom_module.asyncio, "sleep", _sleep)
    return None


def _provider() -> ToapisVideoProvider:
    return ToapisVideoProvider(model="seedance-2-mini", api_key="k")


def test_polling_survives_one_network_error(fast_sleep) -> None:
    """一次连接失败之后仍然继续轮询，拿到 completed 就算成功。"""
    client = _ScriptedClient(
        [
            httpx.ConnectError("网络不可达"),
            _Response(200, {"status": "running"}),
            _Response(200, {"status": "completed", "result": {"data": []}}),
        ]
    )

    body, failure = asyncio.run(_provider()._wait_task(client, "tsk_vid_demo"))

    assert failure is None, failure
    assert body is not None and body["status"] == "completed"
    assert client.calls == 3, "不能在第一次异常时就放弃"


def test_polling_survives_rate_limit_and_5xx(fast_sleep) -> None:
    """429/503 是网关的暂时性问题，任务本身没失败。"""
    client = _ScriptedClient(
        [
            _Response(429, {}),
            _Response(503, {}),
            _Response(200, {"status": "succeeded"}),
        ]
    )

    body, failure = asyncio.run(_provider()._wait_task(client, "tsk_vid_demo"))

    assert failure is None
    assert body is not None


def test_polling_gives_up_after_repeated_failures(fast_sleep, monkeypatch) -> None:
    """连续失败到阈值才认输，并且把网关任务 id 带出来。"""
    monkeypatch.setattr(custom_module, "_POLL_MAX_TRANSIENT", 2)
    client = _ScriptedClient([httpx.ConnectError("断网")])

    _, failure = asyncio.run(_provider()._wait_task(client, "tsk_vid_lost"))

    assert failure is not None
    assert failure.error_code == ProviderErrorCode.UNAVAILABLE.value
    assert failure.provider_job_id == "tsk_vid_lost"
    assert "tsk_vid_lost" in (failure.error_message or "")


def test_real_task_failure_is_not_treated_as_network_issue(fast_sleep) -> None:
    """网关明确说 failed，就不要再问了。"""
    client = _ScriptedClient([_Response(200, {"status": "failed"})])

    _, failure = asyncio.run(_provider()._wait_task(client, "tsk_vid_failed"))

    assert failure is not None
    assert failure.provider_job_id == "tsk_vid_failed"
    assert client.calls == 1


# --------------------------------------------------------------------------- #
# 失败结算：账要说得清
# --------------------------------------------------------------------------- #


def test_submitted_job_ids_dedupes_and_keeps_order() -> None:
    calls = [
        {"provider_job_id": "tsk_a"},
        {"provider_job_id": ""},
        {"provider_job_id": "tsk_b"},
        {"provider_job_id": "tsk_a"},
    ]
    assert _submitted_job_ids(calls) == ["tsk_a", "tsk_b"]


def test_release_reason_mentions_provider_job_when_submitted() -> None:
    """已提交才提示「可能已计费」——上传阶段就失败的不该吓唬用户。"""
    exc = types.SimpleNamespace(message="首帧上传失败：连不上 toapis.cn")

    plain = _release_reason(exc, [{"provider_job_id": ""}])
    assert "可能已计费" not in plain
    assert plain.startswith("失败释放：")

    submitted = _release_reason(exc, [{"provider_job_id": "tsk_vid_01M6QZ"}])
    assert "tsk_vid_01M6QZ" in submitted
    assert "本地未扣" in submitted
