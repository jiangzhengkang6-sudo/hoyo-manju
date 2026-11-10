"""实测：短时长（单镜头）氛围音到底能不能生成。

用法：cd backend && python scripts/check_ambience_short.py [秒数]

只跑一次真实调用（4 秒约 1 分钱），用来回答两个问题：
1. 供应商是否接受「4 秒」这种极短请求，实际返回多长的音频；
2. `audio_fx.adjust_speed(max_ms=...)` 裁剪后是否真的是 4 秒、头部时长是否正确。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.story import run_coro  # noqa: E402
from app.providers.base import SpeechRequest  # noqa: E402
from app.providers.custom import QwenTtsProvider  # noqa: E402
from app.providers.net import apply_provider_proxy  # noqa: E402
from app.services import audio_fx  # noqa: E402

PROMPT = "雨夜巷口，雨点打在铁皮棚上，远处闷雷，低沉的弦乐缓慢铺底，没有人声。"


def main() -> int:
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    print(f"出网策略: {apply_provider_proxy()}")
    provider = QwenTtsProvider()
    print(f"provider={provider.id} model={provider.model} url={provider.url[:60]}...")

    started = time.time()
    result = run_coro(
        provider.synthesize(
            SpeechRequest(
                text=PROMPT, output_format="wav", sample_rate=48000, channels=2
            )
        )
    )
    cost_s = time.time() - started
    if not result.ok:
        print(f"FAILED after {cost_s:.1f}s: {result.error_code} {result.error_message}")
        return 1

    art = result.artifacts[0]
    raw = art.get("content") or b""
    print(
        f"OK after {cost_s:.1f}s  bytes={len(raw)}  mime={art.get('mime_type')}  "
        f"provider_duration_ms={result.data.get('duration_ms')}"
    )

    out_dir = ROOT.parent / ".tmp"
    out_dir.mkdir(exist_ok=True)
    raw_path = out_dir / "ambience_raw.wav"
    raw_path.write_bytes(raw)
    print(f"原始文件: {raw_path}")

    content, duration_ms, mime = audio_fx.adjust_speed(
        raw,
        mime=str(art.get("mime_type") or ""),
        speed=1.0,
        sample_rate=48000,
        channels=2,
        max_ms=target * 1000,
    )
    cut_path = out_dir / f"ambience_{target}s.wav"
    cut_path.write_bytes(content)
    print(f"裁剪后: {cut_path}  duration_ms={duration_ms}  mime={mime}")

    binary = audio_fx.ffmpeg_bin()
    if binary:
        import subprocess

        probe = subprocess.run(
            [binary, "-hide_banner", "-i", str(cut_path), "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        for line in (probe.stderr or "").splitlines():
            if "Duration" in line or "Audio:" in line:
                print("  " + line.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
