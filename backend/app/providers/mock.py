"""Mock 供应商：无需外部 API 即可产出真实可用的图片/视频/音频，用于本地闭环与测试。

- 文本：按 ``TextRequest.metadata["task"]`` 生成结构合法的剧本/提示词 JSON。
- 图像：用 Pillow 绘制分镜画面（含角色色块、构图框、镜号、提示词摘要）。
- 视频：用 FFmpeg 对静帧做推拉运镜，输出 H.264 MP4。
- 语音：用 FFmpeg 按文本长度合成音频波形（可当作占位配音）。

它同时是「供应商超时 / 失败」故障注入的载体：metadata 里带 ``fail`` 即按需失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import subprocess
from pathlib import Path
from typing import Any

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

# --------------------------------------------------------------------------- #
# FFmpeg
# --------------------------------------------------------------------------- #

_ffmpeg_cache: str | None = None


def ffmpeg_bin() -> str | None:
    global _ffmpeg_cache
    if _ffmpeg_cache:
        return _ffmpeg_cache or None
    if settings.ffmpeg_bin and Path(settings.ffmpeg_bin).exists():
        _ffmpeg_cache = settings.ffmpeg_bin
        return _ffmpeg_cache
    try:
        import imageio_ffmpeg

        _ffmpeg_cache = imageio_ffmpeg.get_ffmpeg_exe()
        return _ffmpeg_cache
    except Exception:
        pass
    from shutil import which

    found = which("ffmpeg")
    _ffmpeg_cache = found or ""
    return found


def _run_ffmpeg(args: list[str], timeout: int = 180) -> tuple[bool, str]:
    binary = ffmpeg_bin()
    if not binary:
        return False, "FFmpeg 不可用"
    cmd = [binary, "-y", "-hide_banner", "-loglevel", "error", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, "FFmpeg 执行超时"
    except OSError as exc:
        return False, f"FFmpeg 启动失败：{exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or b"").decode("utf-8", "ignore")[-800:]
    return True, ""


# --------------------------------------------------------------------------- #
# 颜色 / 文本工具
# --------------------------------------------------------------------------- #

_PALETTE = [
    (232, 93, 117), (86, 148, 255), (255, 176, 32), (76, 201, 176),
    (168, 118, 255), (255, 121, 87), (94, 205, 255), (198, 214, 96),
]


def color_for(name: str) -> tuple[int, int, int]:
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return _PALETTE[digest[0] % len(_PALETTE)]


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _pick_font(size: int):
    from PIL import ImageFont

    for candidate in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    for para in (text or "").split("\n"):
        line = ""
        for ch in para:
            line += ch
            if len(line) >= width:
                out.append(line)
                line = ""
        out.append(line)
    return out[:6]


# --------------------------------------------------------------------------- #
# 剧本生成（Mock 文本）
# --------------------------------------------------------------------------- #

_CAMERA_CYCLE = ["固定", "推", "拉", "摇", "移", "跟"]
_COMPOSITION_CYCLE = ["全景", "中景", "近景", "特写", "中景", "过肩"]
_TIME_CYCLE = ["清晨", "白天", "黄昏", "夜晚"]


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[。！？!?\n；;]+", text or "") if p.strip()]
    return parts or ["一个没有留下名字的人，走进了一座没有出口的城。"]


def _extract_names(text: str) -> list[str]:
    names: list[str] = []
    for m in re.finditer(r"([\u4e00-\u9fa5]{2,3})(?:说|问|喊|道|答|笑|哭)", text or ""):
        if m.group(1) not in names:
            names.append(m.group(1))
    for token in re.findall(r"[\u4e00-\u9fa5]{2,3}", text or ""):
        if token in {"一个", "没有", "什么", "自己", "他们", "我们", "时候", "地方", "因为", "所以"}:
            continue
    if not names:
        names = ["林晚", "沈砚"]
    return names[:4]


def build_mock_script(source_text: str, *, shot_count: int, genre: str = "都市") -> dict[str, Any]:
    """根据故事文本确定性地产出一份合法剧本 JSON。"""
    sentences = _split_sentences(source_text)
    names = _extract_names(source_text)
    main_name, second_name = names[0], (names[1] if len(names) > 1 else "沈砚")

    characters = [
        {
            "name": main_name,
            "identity": f"{genre}故事主角",
            "age_range": "22-28",
            "appearance": "身形清瘦，眼神沉静，黑发微乱",
            "outfit": "深色风衣，内搭浅色衬衫",
            "personality": "克制、执着，遇事先观察再行动",
            "speech_style": "语速偏慢，句子短，习惯用反问",
            "forbidden_changes": ["发型", "瞳色", "风衣款式"],
            "is_main": True,
        },
        {
            "name": second_name,
            "identity": "关键对手/引路人",
            "age_range": "28-35",
            "appearance": "高挑，轮廓锋利，左眉有一道浅疤",
            "outfit": "剪裁利落的黑色西装",
            "personality": "精明、掌控欲强，话里带钩",
            "speech_style": "语速快，喜欢用陈述句下结论",
            "forbidden_changes": ["左眉疤痕", "西装颜色"],
            "is_main": True,
        },
    ]

    scenes = [
        {"scene_no": 1, "location": "旧城区巷口", "time_of_day": "黄昏", "summary": sentences[0][:80]},
        {"scene_no": 2, "location": "临街咖啡馆", "time_of_day": "夜晚", "summary": (sentences[1] if len(sentences) > 1 else "两人第一次正面交锋。")[:80]},
        {"scene_no": 3, "location": "天台", "time_of_day": "深夜", "summary": (sentences[2] if len(sentences) > 2 else "真相被摊开在夜色里。")[:80]},
    ]

    shots: list[dict[str, Any]] = []
    for i in range(shot_count):
        scene_no = 1 + (i * len(scenes) // max(shot_count, 1))
        scene_no = min(scene_no, len(scenes))
        speaker = main_name if i % 2 == 0 else second_name
        sentence = sentences[i % len(sentences)]
        dialogue_text = sentence[:40]
        shots.append(
            {
                "shot_no": i + 1,
                "duration_ms": 4000,
                "scene_no": scene_no,
                "characters": [main_name] if i % 3 == 2 else [main_name, second_name],
                "action": f"{speaker}在{scenes[scene_no - 1]['location']}中{sentence[:24]}",
                "camera_movement": _CAMERA_CYCLE[i % len(_CAMERA_CYCLE)],
                "composition": _COMPOSITION_CYCLE[i % len(_COMPOSITION_CYCLE)],
                "emotion": ["压抑", "试探", "紧绷", "松动", "决绝"][i % 5],
                "dialogue": [{"character": speaker, "text": dialogue_text, "emotion": "克制"}],
                "narration": sentence[:60] if i % 3 == 0 else "",
                "sfx": ["环境底噪", "脚步回响"] if i % 2 == 0 else ["雨声"],
                "image_prompt": (
                    f"{genre}题材，{scenes[scene_no - 1]['location']}，{scenes[scene_no - 1]['time_of_day']}，"
                    f"{_COMPOSITION_CYCLE[i % len(_COMPOSITION_CYCLE)]}构图，"
                    f"{'、'.join([main_name] if i % 3 == 2 else [main_name, second_name])}，"
                    f"{sentence[:40]}，电影感打光，竖屏 9:16"
                ),
                "negative_prompt": "低清，畸形手指，多余肢体，文字水印，过度锐化",
                "required": True,
            }
        )

    beats = [
        {"beat_no": 1, "name": "开场建立", "summary": sentences[0][:60], "scene_no": 1, "shots": [1, 2]},
        {"beat_no": 2, "name": "冲突升级", "summary": (sentences[1] if len(sentences) > 1 else "冲突出现")[:60], "scene_no": 2, "shots": [3, 4]},
        {"beat_no": 3, "name": "转折与收束", "summary": (sentences[-1])[:60], "scene_no": 3, "shots": [5, 6]},
    ]

    return {
        "story_bible": {
            "logline": (source_text or "一个关于选择与代价的故事")[:120],
            "theme": f"{genre}语境下的选择与代价",
            "tone": "冷峻、克制、带悬念",
            "setting": scenes[0]["location"],
            "characters": characters,
            "scenes": scenes,
            "conflict": {
                "goal": f"{main_name}想弄清事情的真相",
                "obstacle": f"{second_name}不断设局阻挠",
                "stakes": "一旦失败将失去唯一的退路",
            },
            "relationships": [
                {"from": main_name, "to": second_name, "relation": "亦敌亦友的旧识"},
            ],
        },
        "beat_sheet": beats,
        "shots": shots,
    }


# --------------------------------------------------------------------------- #
# 图像绘制
# --------------------------------------------------------------------------- #

def render_storyboard_image(
    *,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    shot_no: int | None,
    characters: list[str],
    composition: str = "中景",
    camera_movement: str = "固定",
    label: str = "",
    seed: int = 0,
    watermark: str | None = None,
) -> bytes:
    from PIL import Image, ImageDraw

    digest = hashlib.sha256(f"{prompt}|{seed}".encode("utf-8")).digest()
    base_hue = digest[0] / 255.0
    top = _mix((22, 26, 38), (60, 70, 110), base_hue)
    bottom = _mix((12, 14, 22), (40, 30, 60), 1 - base_hue)

    img = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(img, "RGBA")

    # 渐变背景
    for y in range(height):
        t = y / max(height - 1, 1)
        draw.line([(0, y), (width, y)], fill=_mix(top, bottom, t))

    # 光源光晕
    glow = Image.new("RGB", (width, height), (0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    cx = int(width * (0.3 + 0.4 * (digest[1] / 255.0)))
    cy = int(height * (0.2 + 0.2 * (digest[2] / 255.0)))
    radius = int(min(width, height) * 0.55)
    for r in range(radius, 0, -max(radius // 60, 1)):
        alpha = int(70 * (1 - r / radius) ** 2)
        gdraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 226, 180))
    glow = glow.point(lambda v: v)
    img = Image.blend(img, Image.composite(glow, img, glow.convert("L").point(lambda v: min(v, 90))), 0.55)
    draw = ImageDraw.Draw(img, "RGBA")

    # 地平线 / 场景分层
    horizon = int(height * 0.62)
    draw.rectangle([0, horizon, width, height], fill=(10, 12, 20, 170))
    for i in range(6):
        bx = int(width * (i / 6.0)) + int(digest[3 + i % 8]) % max(width // 12, 1)
        bw = int(width * 0.12)
        bh = int(height * (0.06 + 0.05 * ((digest[i % 32] / 255.0))))
        draw.rectangle([bx, horizon - bh, bx + bw, horizon], fill=(16, 20, 32, 210))

    # 角色剪影（用角色色相区分，保证同角色跨镜颜色一致）
    if characters:
        slot = width / (len(characters) + 1)
        for idx, name in enumerate(characters):
            c = color_for(name)
            cx = int(slot * (idx + 1))
            body_h = int(height * 0.30)
            body_w = int(width * 0.16)
            base_y = horizon + int(height * 0.06)
            draw.ellipse(
                [cx - body_w // 3, base_y - body_h - body_w // 2,
                 cx + body_w // 3, base_y - body_h + body_w // 6],
                fill=(*c, 235),
            )
            draw.rounded_rectangle(
                [cx - body_w // 2, base_y - body_h, cx + body_w // 2, base_y],
                radius=body_w // 4, fill=(*_mix(c, (10, 10, 16), 0.45), 240),
            )
            draw.text((cx - body_w // 2, base_y + 8), name, font=_pick_font(max(width // 44, 14)),
                      fill=(235, 238, 245, 235))

    # 构图辅助框
    draw.rectangle([width * 0.06, height * 0.06, width * 0.94, height * 0.94],
                   outline=(255, 255, 255, 40), width=2)
    thirds = [(1 / 3, "v"), (2 / 3, "v"), (1 / 3, "h"), (2 / 3, "h")]
    for ratio, orient in thirds:
        if orient == "v":
            draw.line([(width * ratio, height * 0.06), (width * ratio, height * 0.94)],
                      fill=(255, 255, 255, 26), width=1)
        else:
            draw.line([(width * 0.06, height * ratio), (width * 0.94, height * ratio)],
                      fill=(255, 255, 255, 26), width=1)

    # 顶部信息条
    font_title = _pick_font(max(width // 26, 20))
    font_small = _pick_font(max(width // 46, 13))
    bar_h = int(height * 0.085)
    draw.rectangle([0, 0, width, bar_h], fill=(8, 10, 16, 205))
    title = f"镜 {shot_no:02d}" if shot_no else (label or "角色设定")
    draw.text((width * 0.035, bar_h * 0.28), title, font=font_title, fill=(255, 214, 102))
    meta = f"{composition} · {camera_movement}" if composition else ""
    draw.text((width * 0.035 + font_title.size * 4.2, bar_h * 0.42), meta, font=font_small,
              fill=(180, 190, 205))

    # 底部提示词
    bottom_h = int(height * 0.20)
    draw.rectangle([0, height - bottom_h, width, height], fill=(8, 10, 16, 215))
    y = height - bottom_h + int(bottom_h * 0.10)
    for line in _wrap(prompt, 22)[:4]:
        draw.text((width * 0.035, y), line, font=font_small, fill=(224, 230, 240))
        y += int(font_small.size * 1.5)

    if watermark:
        draw.text((width * 0.035, height - bottom_h - int(font_small.size * 2.2)), watermark,
                  font=font_small, fill=(255, 255, 255, 150))

    # AIGC 标识（不可关闭）
    draw.rectangle([width * 0.035, height * 0.90, width * 0.42, height * 0.945],
                   fill=(0, 0, 0, 120))
    draw.text((width * 0.05, height * 0.906), settings.aigc_label_text, font=font_small,
              fill=(255, 255, 255, 210))

    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Provider 实现
# --------------------------------------------------------------------------- #

class MockTextProvider(BaseProvider):
    id = "mock"
    kind = ProviderKind.TEXT
    display_name = "Mock 文本（本地确定性）"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(models=["mock-text-v1"], billing_unit="1k_tokens",
                                    estimated_latency_seconds=1, unit_price_cents=1)

    async def generate_json(self, request: TextRequest) -> ProviderResult:
        meta = request.metadata or {}
        task = meta.get("task", "generic")
        await asyncio.sleep(0)
        if meta.get("simulate") == "timeout":
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "供应商响应超时（故障注入）",
                retryable=True,
            )
        if meta.get("simulate") == "schema_error":
            return ProviderResult(
                ok=True, kind=self.kind, data={"unexpected": True},
                raw_summary={"mock": "schema_error"}, usage={"total_tokens": 120},
                cost_cents=1, billing_unit="1k_tokens", request_id="mock-req",
            )

        if task == "script":
            data = build_mock_script(
                meta.get("source_text", ""),
                shot_count=int(meta.get("shot_count", 6)),
                genre=meta.get("genre", "都市"),
            )
        elif task == "shot_rewrite":
            base_prompt = meta.get("image_prompt", "")
            # 模拟「重写后确实变了」：追加一层电影感描述（幂等，不重复追加）
            enhanced = base_prompt
            if base_prompt and "电影感光影" not in base_prompt:
                enhanced = f"{base_prompt}，电影感光影，浅景深"
            data = {
                "action": meta.get("action", ""),
                "image_prompt": enhanced,
                "negative_prompt": meta.get("negative_prompt", ""),
                "camera_movement": meta.get("camera_movement", "固定"),
                "composition": meta.get("composition", "中景"),
            }
        elif task == "shot_suggest":
            # 单镜「按大纲生成」草稿：给出结构完整、可直接编辑的一条
            base_prompt = meta.get("image_prompt", "")
            data = {
                "action": f"{meta.get('action') or '人物停在原地'}，光影收紧，情绪更沉",
                "dialogue": [],
                "narration": "",
                "composition": meta.get("composition") or "中景",
                "camera_movement": meta.get("camera_movement") or "固定",
                "duration_ms": 4000,
                "characters": [],
                "image_prompt": (
                    f"{base_prompt}，电影感光影，浅景深" if base_prompt else "竖屏 9:16，电影感光影"
                ),
                "negative_prompt": "低清，畸形手指",
            }
        elif task == "story_analysis":
            data = {
                "theme": meta.get("theme", "选择与代价"),
                "characters_relation": meta.get("relations", []),
                "scenes": meta.get("scenes", []),
                "beats": meta.get("beats", []),
                "conflict": meta.get("conflict", {}),
            }
        else:
            data = {"text": f"[mock] {request.prompt[:200]}"}

        return ProviderResult(
            ok=True,
            kind=self.kind,
            data=data,
            raw_summary={"mock": True, "task": task},
            usage={"prompt_tokens": len(request.prompt) // 2, "completion_tokens": 512,
                   "total_tokens": len(request.prompt) // 2 + 512},
            cost_cents=1,
            estimated_cost_cents=1,
            billing_unit="1k_tokens",
            request_id=f"mock-{task}-{abs(hash(request.prompt)) % 10**8}",
        )


class MockImageProvider(BaseProvider):
    id = "mock"
    kind = ProviderKind.IMAGE
    display_name = "Mock 图像（Pillow 本地绘制）"

    def capabilities(self) -> ProviderCapabilities:
        # unit_price_cents 与 generate() 里的 10 分/张保持一致，预估与实结才对得上
        return ProviderCapabilities(models=["mock-image-v1"], max_candidates=4,
                                    billing_unit="image", estimated_latency_seconds=2,
                                    unit_price_cents=10)

    async def submit(self, request: ImageRequest) -> Any:
        from app.providers.base import SubmittedJob

        return SubmittedJob(provider_job_id=f"mockimg-{abs(hash(request.prompt)) % 10**8}")

    async def get_result(self, provider_job_id: str) -> ProviderResult:
        return ProviderResult(ok=True, kind=self.kind, provider_job_id=provider_job_id)

    async def generate(self, request: ImageRequest) -> ProviderResult:
        if request.metadata.get("simulate") == "timeout":
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "图像供应商超时（故障注入）",
                retryable=True,
            )
        await asyncio.sleep(0)
        artifacts = []
        for i in range(max(1, request.count)):
            content = render_storyboard_image(
                prompt=request.prompt,
                negative_prompt=request.negative_prompt,
                width=request.width,
                height=request.height,
                shot_no=request.metadata.get("shot_no"),
                characters=list(request.metadata.get("characters", [])),
                composition=request.metadata.get("composition", "中景"),
                camera_movement=request.metadata.get("camera_movement", "固定"),
                label=request.metadata.get("label", ""),
                seed=i + 1,
                watermark=request.metadata.get("watermark"),
            )
            artifacts.append(
                {
                    "content": content,
                    "filename": f"{request.metadata.get('label') or 'image'}_{i + 1}.png",
                    "mime_type": "image/png",
                    "width": request.width,
                    "height": request.height,
                }
            )
        return ProviderResult(
            ok=True,
            kind=self.kind,
            data={"count": len(artifacts)},
            artifacts=artifacts,
            raw_summary={"mock": True, "count": len(artifacts)},
            usage={"images": len(artifacts)},
            cost_cents=10 * len(artifacts),
            estimated_cost_cents=10 * len(artifacts),
            billing_unit="image",
            request_id=f"mockimg-{abs(hash(request.prompt)) % 10**8}",
        )


class MockVideoProvider(BaseProvider):
    id = "mock"
    kind = ProviderKind.VIDEO
    display_name = "Mock 视频（FFmpeg 运镜）"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=["mock-video-v1"],
            supports_image_to_video=True,
            duration_ms_min=1000,
            duration_ms_max=15000,
            billing_unit="second",
            estimated_latency_seconds=20,
            unit_price_cents=self._price_per_second_cents(),
        )

    def _price_per_second_cents(self) -> int:
        return 30

    async def estimate_cost(self, request: VideoRequest) -> CostEstimate:
        seconds = max(1, math.ceil(request.duration_ms / 1000))
        unit = self._price_per_second_cents()
        return CostEstimate(
            estimate_cents=seconds * unit,
            billing_unit="second",
            price_version="v1",
            provider=self.id,
            model=request.model or "mock-video-v1",
            estimated_wait_seconds=15,
            breakdown=[{"item": "时长（秒）", "quantity": seconds, "unit_price_cents": unit}],
        )

    async def generate(self, request: VideoRequest) -> ProviderResult:
        if request.metadata.get("simulate") == "timeout":
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "视频供应商超时（故障注入）",
                retryable=True,
            )
        await asyncio.sleep(0)
        source = request.metadata.get("first_frame_path") or request.image_uri
        if not source or not Path(source).exists():
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.BAD_REQUEST.value, "缺少可用的首帧图片"
            )

        seconds = max(1.0, request.duration_ms / 1000.0)
        out = Path(source).with_suffix("")
        out = out.parent / f"{out.name}_video.mp4"
        direction = "in" if request.camera_movement in {"推", "跟"} else "out"
        if request.camera_movement in {"摇", "移"}:
            expr = "x='iw/2-(iw/zoom/2)+sin(on/25)*20':y='ih/2-(ih/zoom/2)'"
        else:
            expr = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        zoom = "min(zoom+0.0015,1.25)" if direction == "in" else "if(lte(zoom,1.0),1.25,max(1.001,zoom-0.0015))"
        vf = (
            f"scale={request.width * 2}:{request.height * 2},"
            f"zoompan=z='{zoom}':d={int(seconds * 25)}:s={request.width}x{request.height}:fps=25:{expr},"
            f"format=yuv420p"
        )
        ok, err = _run_ffmpeg(
            [
                "-loop", "1", "-i", str(source),
                "-t", f"{seconds:.3f}",
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(out),
            ]
        )
        if not ok:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"视频合成失败：{err}",
                retryable=True, raw={"ffmpeg": err},
            )
        content = out.read_bytes()
        return ProviderResult(
            ok=True,
            kind=self.kind,
            data={"duration_ms": request.duration_ms},
            artifacts=[
                {
                    "content": content,
                    "filename": f"{out.stem}.mp4",
                    "mime_type": "video/mp4",
                    "width": request.width,
                    "height": request.height,
                    "duration_ms": request.duration_ms,
                }
            ],
            raw_summary={"mock": True, "duration_ms": request.duration_ms},
            usage={"seconds": seconds},
            cost_cents=int(math.ceil(seconds)) * self._price_per_second_cents(),
            estimated_cost_cents=int(math.ceil(seconds)) * self._price_per_second_cents(),
            billing_unit="second",
            request_id=f"mockvid-{abs(hash(request.prompt + str(request.duration_ms))) % 10**8}",
        )


class MockSpeechProvider(BaseProvider):
    id = "mock"
    kind = ProviderKind.SPEECH
    display_name = "Mock 语音（FFmpeg 波形）"

    def capabilities(self) -> ProviderCapabilities:
        # 与 synthesize() 里的「每 100 字 1 分」同口径
        return ProviderCapabilities(models=["mock-tts-v1"], billing_unit="1k_chars",
                                    estimated_latency_seconds=2, unit_price_cents=10)

    @staticmethod
    def duration_for(text: str, speed: float = 1.0) -> int:
        chars = max(len(text), 1)
        ms = int(chars / 5.0 * 1000 / max(speed, 0.1))  # 约 5 字/秒
        return max(800, min(ms, 60_000))

    async def synthesize(self, request: SpeechRequest) -> ProviderResult:
        if request.model == "__simulate_timeout__":
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.TIMEOUT.value, "TTS 供应商超时（故障注入）",
                retryable=True,
            )
        await asyncio.sleep(0)
        # 语速统一由 `audio_fx.adjust_speed`（atempo）处理；这里按原速生成，
        # 否则会「供应商先变一次 + 本地再变一次」，时长被算两遍。
        duration_ms = self.duration_for(request.text)
        seconds = duration_ms / 1000.0
        base = 150 + (abs(hash(request.voice_id or "default")) % 120)
        out = Path(settings.data_dir) / "tmp"
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"tts_{abs(hash(request.text)) % 10**10}.mp3"
        # 用正弦叠加 + 幅度包络模拟人声节奏，产出真实可播放音频
        expr = (
            f"sin(2*PI*{base}*t)*0.25 + sin(2*PI*{base * 2}*t)*0.12"
            f"*(0.6+0.4*sin(2*PI*2.5*t))"
        )
        ok, err = _run_ffmpeg(
            [
                "-f", "lavfi",
                "-i", f"aevalsrc='{expr}*min(1,t*8)*min(1,(1-t/{seconds:.3f})*8)':s=44100:d={seconds:.3f}",
                "-c:a", "libmp3lame", "-b:a", "96k", str(target),
            ]
        )
        if not ok:
            return ProviderResult.failure(
                self.kind, ProviderErrorCode.UNAVAILABLE.value, f"TTS 合成失败：{err}",
                retryable=True, raw={"ffmpeg": err},
            )
        content = target.read_bytes()
        return ProviderResult(
            ok=True,
            kind=self.kind,
            data={"duration_ms": duration_ms},
            artifacts=[
                {
                    "content": content,
                    "filename": target.name,
                    "mime_type": "audio/mpeg",
                    "duration_ms": duration_ms,
                }
            ],
            raw_summary={"mock": True, "duration_ms": duration_ms},
            usage={"chars": len(request.text)},
            cost_cents=max(1, len(request.text) // 100),
            estimated_cost_cents=max(1, len(request.text) // 100),
            billing_unit="1k_chars",
            request_id=f"mocktts-{abs(hash(request.text)) % 10**8}",
        )
