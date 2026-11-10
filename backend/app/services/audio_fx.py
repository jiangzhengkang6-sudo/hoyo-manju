"""音频后处理：按目标语速变速（保持音高）。

为什么需要它：当前语音供应商（通义 `qwen-audio-3.1-tts-next`）**不认 speed 参数**，
`SpeechRequest.speed` 传过去会被忽略——角色语速调到 0.8 或 1.3，出来的音频完全一样。
所以语速统一在本地用 FFmpeg 的 `atempo` 滤镜实现：只改节奏、不改音高，
对任何供应商都成立。
"""

from __future__ import annotations

import io
import subprocess
import tempfile
import wave
from pathlib import Path

from app.core.config import settings

_FFMPEG_CACHE: str | None = None


def ffmpeg_bin() -> str | None:
    """找 FFmpeg：配置的路径 → imageio-ffmpeg → PATH。"""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE or None
    if settings.ffmpeg_bin and Path(settings.ffmpeg_bin).exists():
        _FFMPEG_CACHE = settings.ffmpeg_bin
        return _FFMPEG_CACHE
    try:
        import imageio_ffmpeg

        _FFMPEG_CACHE = imageio_ffmpeg.get_ffmpeg_exe()
        return _FFMPEG_CACHE
    except Exception:  # noqa: BLE001
        pass
    from shutil import which

    found = which("ffmpeg")
    _FFMPEG_CACHE = found or ""
    return found


def atempo_chain(speed: float) -> list[str]:
    """把倍率拆成若干个 0.5~2.0 的 atempo（单级滤镜只支持这个区间）。"""
    remaining = max(float(speed), 0.01)
    filters: list[str] = []
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.4f}")
    return filters


def _wav_duration_ms(content: bytes) -> int | None:
    try:
        with wave.open(io.BytesIO(content), "rb") as handle:
            return int(handle.getnframes() / handle.getframerate() * 1000)
    except Exception:  # noqa: BLE001
        return None


def adjust_speed(
    content: bytes,
    *,
    mime: str = "",
    speed: float = 1.0,
    sample_rate: int = 44100,
    channels: int = 1,
    max_ms: int | None = None,
) -> tuple[bytes, int | None, str | None]:
    """按 `speed` 变速并统一转成**头部正确**的 WAV；返回 `(字节, 时长ms, MIME)`。

    为什么即使 1.0 倍也要重编码：供应商返回的 WAV 头部是占位值
    （`nframes=536870886` → 解析出 10 小时时长），浏览器读到的总时长是错的、
    拖动进度条也不准。过一遍 FFmpeg 重写头部（并统一 44.1k 单声道）才干净。

    失败时原样返回 `(content, None, None)`——语速不对可以重跑，
    但绝不能因为后处理把整段配音丢掉。
    """
    if not content:
        return content, None, None
    binary = ffmpeg_bin()
    if not binary:
        return content, None, None

    in_suffix = ".wav" if "wav" in (mime or "").lower() else ".mp3"
    with tempfile.TemporaryDirectory(prefix="tts-fx-") as tmp:
        src = Path(tmp) / f"in{in_suffix}"
        dst = Path(tmp) / "out.wav"
        src.write_bytes(content)
        cmd = [binary, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
        if abs(float(speed) - 1.0) >= 0.01:
            cmd += ["-filter:a", ",".join(atempo_chain(float(speed)))]
        if max_ms and max_ms > 0:
            # 单镜头垫乐必须贴合镜头长度：多出来的部分裁掉，避免压到下一镜
            cmd += ["-t", f"{max_ms / 1000.0:.3f}"]
        cmd += ["-ar", str(sample_rate), "-ac", str(channels), "-c:a", "pcm_s16le", str(dst)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return content, None, None
        if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            return content, None, None
        output = dst.read_bytes()

    return output, _wav_duration_ms(output), "audio/wav"
