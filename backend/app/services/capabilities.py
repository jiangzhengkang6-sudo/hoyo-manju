"""供应商能力探测（FR-091）。

「提交任务前根据供应商能力声明校验尺寸、参考图数量、时长和格式，不满足时提前阻断。」

设计原则：
- **提交前阻断，而不是生成中静默夹取**：静默夹取会让用户以为按自己的参数生成了，
  实际产物却是另一套参数；这里统一返回 `PROVIDER_CAPABILITY_UNSUPPORTED`
  并把「哪一项 / 声明值 / 请求值」写进 `details`，让用户能改。
- **能力声明缺失即视为不限制**：供应商没声明 `max_reference_images` 就不拦，
  避免把「没填」当成「不支持」。
- 校验在 **API 提交层**（`generation.py` / `media` 上传）调用；agent 内部仍保留
  夹取逻辑作为最后一道兜底（例如工作流自动推进时没有交互对象）。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ErrorCode, ProviderCapabilityUnsupported

#: 需要阻断时使用的错误码（不在 RETRYABLE 中，即不可重试）
CAPABILITY_ERROR_CODE = ErrorCode.PROVIDER_CAPABILITY_UNSUPPORTED


def _declared(value: Any) -> Any:
    """把「未声明」统一成 None，便于判断供应商有没有声明这一项。

    注意顺序：先判 bool。`False == 0`，如果先按「0 即未声明」处理，
    `supports_image_to_video=False` 这种**显式否定**的声明会被误当成未声明，
    从而漏掉本该阻断的情况。
    """
    if isinstance(value, bool):
        return value
    if value is None or value == "" or value == 0 or value == [] or value == {}:
        return None
    return value


def supported_aspect_ratios(caps: Any) -> list[str] | None:
    """供应商声明的画幅比（归一成 `W:H`）。"""
    raw = _declared(getattr(caps, "aspect_ratios", None))
    if not raw:
        return None
    out: list[str] = []
    for item in raw:
        text = str(item).strip().replace("：", ":")
        out.append(text)
    return out or None


def aspect_ratio_of(width: int, height: int) -> str:
    """把宽高换算成最简 `W:H`（用于与供应商声明比对）。"""
    import math

    if width <= 0 or height <= 0:
        return ""
    divisor = math.gcd(int(width), int(height))
    return f"{int(width) // divisor}:{int(height) // divisor}"


def _ratio_pair(text: str) -> tuple[float, float] | None:
    clean = (text or "").strip().replace("：", ":")
    if ":" not in clean:
        return None
    left, _, right = clean.partition(":")
    try:
        return float(left), float(right)
    except ValueError:
        return None


def _same_ratio(left: str, right: str) -> bool:
    """按交叉相乘判断两个画幅比是否等价（9:16 == 1080:1920）。"""
    a = _ratio_pair(left)
    b = _ratio_pair(right)
    if a is None or b is None:
        return left.strip() == right.strip()
    if a[1] == 0 or b[1] == 0:
        return left.strip() == right.strip()
    return abs(a[0] * b[1] - b[0] * a[1]) < 1e-6


def check_aspect_ratio(caps: Any, *, aspect_ratio: str, width: int = 0, height: int = 0) -> str | None:
    """校验画幅比；返回错误提示（None 表示通过）。

    只在调用方没给 `aspect_ratio` 时才用宽高反推，并且**只把供应商声明的
    清单当作允许集合**——不能把请求自身的宽高比塞进允许集合，否则任何显式
    传入的 `aspect_ratio` 都会因为「与自己的宽高一致」而蒙混过关。
    """
    declared = supported_aspect_ratios(caps)
    if declared is None:
        return None
    wanted = (aspect_ratio or "").strip() or aspect_ratio_of(width, height)
    if not wanted:
        return None
    if not any(_same_ratio(wanted, item) for item in declared):
        return f"该供应商不支持 {wanted} 画幅，支持：{'、'.join(declared)}"
    return None


def check_duration(caps: Any, *, duration_ms: int) -> str | None:
    lo = _declared(getattr(caps, "duration_ms_min", None))
    hi = _declared(getattr(caps, "duration_ms_max", None))
    if lo is None and hi is None:
        return None
    if lo is not None and duration_ms < int(lo):
        return f"时长 {duration_ms}ms 低于供应商下限 {int(lo)}ms"
    if hi is not None and duration_ms > int(hi):
        return f"时长 {duration_ms}ms 超过供应商上限 {int(hi)}ms"
    return None


def check_reference_images(caps: Any, *, count: int) -> str | None:
    """参考图数量校验（`max_reference_images` 未声明时不限制）。"""
    hi = _declared(getattr(caps, "max_reference_images", None))
    if hi is not None and count > int(hi):
        return f"参考图 {count} 张超过供应商上限 {int(hi)} 张"
    lo = _declared(getattr(caps, "min_reference_images", None))
    if lo is not None and count < int(lo):
        return f"参考图 {count} 张低于供应商下限 {int(lo)} 张"
    return None


def check_candidates(caps: Any, *, count: int) -> str | None:
    hi = _declared(getattr(caps, "max_candidates", None))
    if hi is not None and count > int(hi):
        return f"候选数 {count} 超过供应商上限 {int(hi)}"
    return None


def check_image_size(caps: Any, *, width: int, height: int) -> str | None:
    """尺寸校验：先看是否声明了固定尺寸清单，再看是否声明了像素上限。"""
    sizes = _declared(getattr(caps, "image_sizes", None))
    if sizes:
        pairs = {str(item).strip().replace("：", ":") for item in sizes}
        wanted = f"{width}x{height}"
        alt = f"{width}×{height}"
        if wanted not in pairs and alt not in pairs:
            return f"该供应商不支持 {wanted} 尺寸，支持：{'、'.join(sorted(pairs))}"
    max_pixels = _declared(getattr(caps, "max_image_pixels", None))
    if max_pixels is not None and int(width) * int(height) > int(max_pixels):
        return f"分辨率 {width}x{height} 超过供应商上限 {int(max_pixels)} 像素"
    return check_aspect_ratio(caps, aspect_ratio="", width=width, height=height)


def check_image_formats(caps: Any, *, mime_type: str) -> str | None:
    formats = _declared(getattr(caps, "image_formats", None))
    if not formats:
        return None
    allowed = {str(item).strip().lower() for item in formats}
    wanted = (mime_type or "").split(";")[0].strip().lower()
    if wanted and wanted not in allowed:
        return f"该供应商不支持 {wanted} 格式，支持：{'、'.join(sorted(allowed))}"
    return None


# --------------------------------------------------------------------------- #
# 组合校验
# --------------------------------------------------------------------------- #

def validate_image_request(
    caps: Any,
    *,
    width: int,
    height: int,
    count: int,
    reference_count: int,
    mime_type: str = "",
) -> None:
    """图像请求校验；不通过时抛 `CapabilityUnsupported`。"""
    problems: list[dict[str, Any]] = []

    message = check_image_size(caps, width=width, height=height)
    if message:
        problems.append({"field": "size", "requested": f"{width}x{height}", "message": message})

    message = check_candidates(caps, count=count)
    if message:
        problems.append({"field": "count", "requested": count, "message": message})

    message = check_reference_images(caps, count=reference_count)
    if message:
        problems.append(
            {"field": "reference_count", "requested": reference_count, "message": message}
        )

    if mime_type:
        message = check_image_formats(caps, mime_type=mime_type)
        if message:
            problems.append({"field": "format", "requested": mime_type, "message": message})

    _raise_if_any(problems, action="图像生成")


def validate_video_request(
    caps: Any,
    *,
    duration_ms: int,
    width: int,
    height: int,
    aspect_ratio: str = "",
    has_image: bool = True,
) -> None:
    """视频请求校验；不通过时抛 `CapabilityUnsupported`。"""
    problems: list[dict[str, Any]] = []

    message = check_duration(caps, duration_ms=duration_ms)
    if message:
        problems.append({"field": "duration_ms", "requested": duration_ms, "message": message})

    message = check_aspect_ratio(caps, aspect_ratio=aspect_ratio, width=width, height=height)
    if message:
        problems.append(
            {
                "field": "aspect_ratio",
                "requested": aspect_ratio or aspect_ratio_of(width, height),
                "message": message,
            }
        )

    if has_image and getattr(caps, "supports_image_to_video", True) is False:
        problems.append(
            {
                "field": "image_to_video",
                "requested": True,
                "message": "该供应商不支持图生视频，请改用支持的首帧图模式或其他供应商",
            }
        )

    _raise_if_any(problems, action="视频生成")


def _raise_if_any(problems: list[dict[str, Any]], *, action: str) -> None:
    if not problems:
        return
    detail = "；".join(item["message"] for item in problems)
    raise ProviderCapabilityUnsupported(
        f"{action}参数不满足当前供应商的能力声明：{detail}",
        details={"problems": problems},
    )
