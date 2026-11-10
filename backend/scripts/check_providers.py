"""供应商连通性体检：只做 DNS / TCP / HTTP 探测，**不生成任何内容，零费用**。

用法（在 backend 目录）：
    python scripts/check_providers.py

排查「生图/生视频一直失败」时先跑这个：
- TCP 失败 / 连不上 → 网络到不了网关（换国内域名 toapis.cn，或开代理）
- 经代理 502 而直连正常 → 代理挡住了（.env 设 PROVIDER_PROXY_URL=direct）
- HTTP 401 → 网关可达但密钥无效；HTTP 4xx 其它 → 参数问题
- 有密钥时额外查 `/v1/models`，能确认「域名可达 + 密钥可用」两件事

注意：之前的版本用裸 socket 探 TCP，**完全不看代理**，
所以配了 PROVIDER_PROXY_URL 也照样报「不可达」，把人误导成供应商挂了。
现在先应用应用的代理策略，再用 httpx 走同样的出网路径。
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.providers.net import apply_provider_proxy  # noqa: E402


def _probe(url: str, api_key: str = "") -> str:
    """按应用真实出网路径（含代理环境变量）探一次 HTTP。"""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
        # 401/403 也算「通」：网关在线，只是不接受这次凭证
        return f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {str(exc)[:70]}"


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""


def main() -> None:
    policy = apply_provider_proxy()
    rows = [
        ("text", settings.text_provider, settings.deepseek_base_url,
         settings.deepseek_api_key, ""),
        ("image", settings.image_provider, settings.gemini_image_url,
         settings.gemini_image_api_key, f"{_origin(settings.gemini_image_url)}/v1/models"),
        ("video", settings.video_provider, settings.grok_video_url,
         settings.grok_video_api_key, f"{_origin(settings.grok_video_url)}/v1/models"),
        ("speech", settings.speech_provider, settings.tts_url,
         settings.tts_api_key, ""),
    ]
    print(f"代理策略：{policy}    PROVIDER_PROXY_URL={settings.provider_proxy_url or '(空=跟随环境变量)'}")
    print("-" * 100)
    print(f"{'模态':6} {'供应商':8} {'密钥':4} {'目标':44} 结果")
    print("-" * 100)
    for kind, provider, url, api_key, auth_url in rows:
        host = urlparse(url).hostname or "-"
        if kind == "speech":
            # TTS 是 POST 端点，GET 必然 4xx；连得上就说明网关在线。
            # 这里别用 /v1/models 去判密钥——该域名没有这个路径，会误报「密钥无效」。
            detail = _probe(url, api_key)
            if detail.startswith("HTTP 4"):
                detail += "（端点在线；真实合成走 POST，需实际调用才能验密钥）"
        else:
            detail = _probe(url, "")
        if api_key and auth_url:
            # 有 key 时再打一次带凭证的模型列表：能同时验证「可达 + 密钥有效」
            detail += f" · 密钥 {_probe(auth_url, api_key)}"
        elif not api_key:
            detail += " · 无密钥（会回退 Mock）"
        print(f"{kind:6} {provider:8} {'有' if api_key else '无':4} {host:44} {detail}")
    print("-" * 100)
    if _origin(settings.gemini_image_url):
        upload = settings.toapis_upload_url or f"{_origin(settings.gemini_image_url)}/v1/uploads/images"
        print(f"首帧/参考图上传接口：{upload}")
    print("提示：401 = 域名可达但密钥无效；连不上 = 网络不可达（换 toapis.cn 或开代理）；经代理 502 = 代理问题。")


if __name__ == "__main__":
    main()
