"""供应商网络错误必须说人话：区分「代理挡住」和「网络到不了」。

线上事故：生图一直失败，任务里只有一句「网络错误：502 Bad Gateway」，
看起来像供应商挂了；实际是进程继承了 IDE 注入的本地代理（127.0.0.1:56800），
而图片网关 toapis.com 本身在本地就不可达（TCP 443 超时）。
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.providers import net


def test_proxy_error_mentions_proxy_and_host(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:56800")
    msg = net.describe_network_error(
        httpx.ProxyError("502 Bad Gateway"), "https://toapis.com/v1/images/generations"
    )
    assert "toapis.com" in msg
    assert "代理" in msg
    assert "PROVIDER_PROXY_URL" in msg


def test_connect_timeout_says_unreachable() -> None:
    exc = httpx.ConnectTimeout("timed out")
    msg = net.describe_network_error(exc, "https://toapis.com/v1/images/generations")
    assert "连不上 toapis.com" in msg
    assert "网络" in msg


def test_read_timeout_suggests_retry() -> None:
    msg = net.describe_network_error(httpx.ReadTimeout("read timeout"), "https://maas.example.com")
    assert "超时" in msg and "重试" in msg


@pytest.fixture()
def clean_proxy_env(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    yield


def test_proxy_direct_mode_clears_env(monkeypatch, clean_proxy_env) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:56800")
    monkeypatch.setattr(net.settings, "provider_proxy_url", "direct", raising=False)
    assert net.apply_provider_proxy() == "direct"
    assert os.environ.get("HTTPS_PROXY") is None


def test_proxy_explicit_url_sets_env(monkeypatch, clean_proxy_env) -> None:
    monkeypatch.setattr(
        net.settings, "provider_proxy_url", "http://127.0.0.1:7890", raising=False
    )
    monkeypatch.setattr(net, "_proxy_alive", lambda url: True)
    assert net.apply_provider_proxy() == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_proxy_empty_keeps_env(monkeypatch, clean_proxy_env) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:56800")
    monkeypatch.setattr(net.settings, "provider_proxy_url", "", raising=False)
    assert net.apply_provider_proxy() == "env"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:56800"


def test_dead_proxy_falls_back_to_direct(monkeypatch, clean_proxy_env) -> None:
    """梯子没开时代理端口不通：必须退回直连，否则国内可达的供应商也会全挂。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:56800")
    monkeypatch.setattr(
        net.settings, "provider_proxy_url", "http://127.0.0.1:7897", raising=False
    )
    monkeypatch.setattr(net, "_proxy_alive", lambda url: False)
    mode = net.apply_provider_proxy()
    assert mode.startswith("direct")
    assert "未启动" in mode
    assert os.environ.get("HTTPS_PROXY") is None


def test_alive_proxy_is_used(monkeypatch, clean_proxy_env) -> None:
    monkeypatch.setattr(
        net.settings, "provider_proxy_url", "http://127.0.0.1:7897", raising=False
    )
    monkeypatch.setattr(net, "_proxy_alive", lambda url: True)
    assert net.apply_provider_proxy() == "http://127.0.0.1:7897"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"
