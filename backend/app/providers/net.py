"""供应商出网：统一代理策略与网络错误文案。

为什么单独一个模块：httpx 默认 `trust_env=True`，会读进程的 `HTTP(S)_PROXY`。
在 IDE / 沙箱 / 某些终端里这些变量常被注入成 `127.0.0.1:xxxx` 的本地代理，
对境外域名往往直接回 `502 Bad Gateway`，于是任务报「网络错误：502」——
看起来像供应商挂了，实际是代理挡住了，排查方向会被带偏。
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import httpx

from app.core.config import settings

_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def _proxy_alive(proxy_url: str) -> bool:
    """代理端口是否在监听。梯子没开时这里返回 False。"""
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    host, port = parsed.hostname, parsed.port or 80
    if not host:
        return False
    sock = socket.socket()
    sock.settimeout(0.8)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def apply_provider_proxy() -> str:
    """按 `PROVIDER_PROXY_URL` 调整进程代理环境变量，返回当前策略。

    - 空：跟随环境变量（默认，行为不变）
    - `direct` / `none` / `off`：清掉代理环境变量，强制直连
    - 具体地址：所有出网走这个代理；**代理没在监听时自动退回直连**，
      否则梯子一关，连国内可达的 DeepSeek / 通义也会一起失败。
    """
    raw = (settings.provider_proxy_url or "").strip()
    if not raw:
        return "env"
    if raw.lower() in {"direct", "none", "off", "false", "0"}:
        for key in _PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        return "direct"
    if not _proxy_alive(raw):
        for key in _PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        return f"direct（代理 {raw} 未启动，已暂时跳过）"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[key] = raw
    return raw


def describe_network_error(exc: Exception, url: str = "") -> str:
    """把 httpx 的网络异常翻译成能指导排查的话（带目标域名与代理状态）。"""
    host = urlparse(url).hostname or url or "供应商"
    if isinstance(exc, httpx.ProxyError):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "未知代理"
        return (
            f"代理 {proxy} 无法访问 {host}：{exc}。"
            "可在 .env 设 PROVIDER_PROXY_URL=direct 直连，或填一个可用代理"
        )
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
        return (
            f"连不上 {host}（网络不可达或被拦截）：{exc}。"
            "请检查网络/代理；若该域名在本地无法访问，换一个可达的供应商即可"
        )
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return f"{host} 响应超时：{exc}。网关可能在排队，稍后重试"
    return f"{host} 请求失败：{exc}"
