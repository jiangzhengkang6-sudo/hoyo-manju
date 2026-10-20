"""FFmpeg 渲染器：拼接、混音、字幕烧录、AIGC 标识、预览与正式导出（FR-MVP-061~063）。

合成失败时给出可定位到镜号/音轨的错误，并保证素材包导出不受影响。
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import RenderError
from app.db.models import AssetStatus, Episode, ExportRecord, MediaAsset
from app.agents.edit import (
    RESOLUTIONS,
    build_timeline,
    preset_dimensions,
    resolution_for,
    validate_timeline,
)
from app.providers.mock import ffmpeg_bin
from app.services import media as media_service

ProgressCb = Callable[[int, str], None]


def _font_file() -> str:
    """返回可直接嵌入 filter 描述的字体重路径。

    FFmpeg 的 filter 描述有两级转义：盘符的 `:` 在参数级需要 `\\:`，
    经过描述级后仍要保留一个反斜杠，因此最终必须写成两个反斜杠。
    用单引号包裹路径会被解析器当成新的选项名，因此这里不使用引号。
    """
    for candidate in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).exists():
            return candidate.replace("\\", "/").replace(":", "\\\\:")
    return ""


# drawtext 的 text 取值要经过 filtergraph 与 filter 参数两级解析，
# ASCII 分隔符无法用单引号可靠保护，这里统一替换为等义全角字符；
# 精确文本仍以导出的 SRT/VTT 与字幕表为准。
_DT_SANITIZE = {
    "\\": "＼",
    "'": "’",
    ":": "：",
    ",": "，",
    ";": "；",
    "[": "【",
    "]": "】",
    "%": "％",
}


def _dt_escape(text: str) -> str:
    out = text.replace("\r", " ").replace("\n", " ")
    for src, dst in _DT_SANITIZE.items():
        out = out.replace(src, dst)
    return out


def _dt_enable(start: float, end: float) -> str:
    """between 表达式的逗号属于 filtergraph 级分隔符，需单反斜杠转义。"""
    return f"between(t\\,{start:.2f}\\,{end:.2f})"


def _drawtext(text: str, *, fontsize: int, x: str, y: str, color: str = "white",
              border: int = 2, enable: str | None = None, box: bool = False) -> str:
    font = _font_file()
    parts = [f"text={_dt_escape(text)}"]
    if font:
        parts.append(f"fontfile={font}")
    parts.append(f"fontsize={fontsize}")
    parts.append(f"fontcolor={color}")
    parts.append(f"x={x}")
    parts.append(f"y={y}")
    if border:
        parts.append(f"borderw={border}")
        parts.append("bordercolor=black@0.85")
    if box:
        parts.append("box=1")
        parts.append("boxcolor=black@0.45")
        parts.append("boxborderw=10")
    if enable:
        parts.append(f"enable={enable}")
    return "drawtext=" + ":".join(parts)


def _resolution(kind: str, preset: str, aspect_ratio: str | None = None) -> tuple[int, int]:
    """FR-060/FR-063：输出尺寸跟随项目画幅比例；`preset` 只决定画质档位。"""
    if kind == "preview":
        return resolution_for(aspect_ratio, "preview")
    return preset_dimensions(preset, aspect_ratio)


def _utc_stamp() -> str:
    """FR-063 要求的生成时间：UTC、带 Z 后缀、``YYYY-MM-DDTHH:MM:SSZ``。"""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_episode(
    session: Session,
    episode: Episode,
    *,
    kind: str = "preview",
    preset: str = "720p_9x16",
    watermark: bool = True,
    progress_cb: ProgressCb | None = None,
) -> tuple[MediaAsset, dict[str, Any]]:
    binary = ffmpeg_bin()
    if not binary:
        raise RenderError(
            "FFmpeg 不可用，无法执行自动合成。已完成的分镜素材仍可通过「导出素材包」交付。",
            details={"hint": "请安装 FFmpeg 或设置 FFMPEG_BIN 环境变量"},
        )

    validation = validate_timeline(session, episode)
    if not validation["valid"]:
        raise RenderError(
            "时间线校验未通过，无法开始合成",
            details={"errors": validation["errors"], "warnings": validation["warnings"]},
        )
    timeline = validation["timeline"]
    width, height = _resolution(kind, preset, timeline.get("aspect_ratio"))

    def report(pct: int, msg: str = "") -> None:
        if progress_cb:
            progress_cb(pct, msg)

    report(5, "准备输入素材")

    inputs: list[list[str]] = []
    filters: list[str] = []
    video_labels: list[str] = []
    shot_index: dict[int, int] = {}

    for idx, entry in enumerate(timeline["tracks"]["video"]):
        duration = max(entry["duration_ms"], 100) / 1000.0
        asset = session.get(MediaAsset, entry["media_id"]) if entry["media_id"] else None
        path = media_service.local_path(asset) if asset else None
        if entry["kind"] == "video" and path and path.exists():
            inputs.append(["-i", str(path)])
        elif entry["kind"] == "image" and path and path.exists():
            inputs.append(["-loop", "1", "-t", f"{duration:.3f}", "-i", str(path)])
        else:
            inputs.append([
                "-f", "lavfi", "-t", f"{duration:.3f}",
                "-i", f"color=c=0x0d1018:s={width}x{height}:r=25",
            ])
        # 注意 `fps` 必须放在链尾：`tpad`/`trim`/`setpts` 会把帧率元数据打回
        # 「1/0」，而 xfade 要求输入是恒定帧率，放在中间会被判为非法。
        filters.append(
            f"[{idx}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"tpad=stop_mode=clone:stop_duration=2,trim=duration={duration:.3f},"
            f"setpts=PTS-STARTPTS,fps=25[v{idx}]"
        )
        video_labels.append(f"[v{idx}]")
        shot_index[entry["shot_no"]] = idx

    video_count = len(inputs)
    if video_count == 0:
        raise RenderError("时间线中没有可合成的镜头")

    durations = [
        max(entry["duration_ms"], 100) / 1000.0 for entry in timeline["tracks"]["video"]
    ]

    if video_count == 1:
        filters.append("".join(video_labels) + "concat=n=1:v=1:a=0[vcat]")
    else:
        # FR-061：有转场的相邻镜头用 xfade 重叠，没有转场的用 concat 硬切。
        # 两者都产出「单一视频流」，所以可以在同一条链上逐段拼接、自由混用。
        # xfade 的 offset 是「在已拼接结果上的时间点」：
        #   offset_k = 已拼接时长 - 本次转场时长
        label = video_labels[0]
        elapsed = durations[0]
        for k in range(1, video_count):
            transition = timeline["tracks"]["video"][k].get("transition")
            out_label = "[vcat]" if k == video_count - 1 else f"[vx{k}]"
            if transition and transition.get("xfade"):
                overlap = max(min(int(transition["duration_ms"]), int(durations[k] * 1000)) , 1) / 1000.0
                offset = max(elapsed - overlap, 0.0)
                filters.append(
                    f"{label}{video_labels[k]}xfade=transition={transition['xfade']}:"
                    f"duration={overlap:.3f}:offset={offset:.3f}{out_label}"
                )
                elapsed = elapsed + durations[k] - overlap
            else:
                filters.append(
                    f"{label}{video_labels[k]}concat=n=2:v=1:a=0{out_label}"
                )
                elapsed += durations[k]
            label = out_label

    # --- 音频输入 ---
    audio_inputs: list[list[str]] = []
    audio_filters: list[str] = []
    mix_labels: list[str] = []

    for item in timeline["tracks"]["dialogue"]:
        asset = session.get(MediaAsset, item["media_id"]) if item["media_id"] else None
        path = media_service.local_path(asset) if asset else None
        if not path or not path.exists():
            continue
        audio_inputs.append(["-i", str(path)])
        idx = video_count + len(audio_inputs) - 1
        delay = max(int(item["start_ms"]), 0)
        label = f"[da{idx}]"
        audio_filters.append(
            f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"adelay=delays={delay}:all=1{label}"
        )
        mix_labels.append(label)

    for item in timeline["tracks"]["bgm"]:
        asset = session.get(MediaAsset, item["media_id"]) if item["media_id"] else None
        path = media_service.local_path(asset) if asset else None
        if not path or not path.exists():
            continue
        audio_inputs.append(["-i", str(path)])
        idx = video_count + len(audio_inputs) - 1
        start = max(int(item["start_ms"]), 0)
        end = int(item["end_ms"] or 0)
        length = max((end - start) if end > start else 0, 1) / 1000.0
        label = f"[bg{idx}]"
        chain = [
            "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
            f"atrim=start=0:end={length:.3f}",
            f"volume={max(min(float(item['volume_db']), 24.0), -60.0)}dB",
        ]
        if item["fade_in_ms"]:
            chain.append(f"afade=t=in:st=0:d={item['fade_in_ms'] / 1000.0:.3f}")
        if item["fade_out_ms"]:
            st = max(length - item["fade_out_ms"] / 1000.0, 0)
            chain.append(f"afade=t=out:st={st:.3f}:d={item['fade_out_ms'] / 1000.0:.3f}")
        chain.append(f"adelay=delays={start}:all=1")
        audio_filters.append(f"[{idx}:a]" + ",".join(chain) + label)
        mix_labels.append(label)

    total_seconds = max(timeline["total_duration_ms"], 500) / 1000.0
    filters.extend(audio_filters)

    # 静音底座保证输出时长与视频一致
    inputs.extend(audio_inputs)
    base_idx = video_count + len(audio_inputs)
    inputs.append(["-f", "lavfi", "-t", f"{total_seconds:.3f}", "-i",
                   "anullsrc=r=44100:cl=stereo"])
    filters.append(f"[{base_idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[abase]")
    mix_labels = ["[abase]", *mix_labels]
    if len(mix_labels) == 1:
        filters.append("[abase]anull[aout]")
    else:
        filters.append(
            "".join(mix_labels)
            + f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0[aout]"
        )

    # --- 字幕与标识 ---
    overlay_filters: list[str] = []
    fontsize = max(int(height * 0.030), 18)
    for cue in timeline["tracks"]["subtitles"]:
        text = cue["text"] if not cue["speaker"] else f"{cue['speaker']}：{cue['text']}"
        start = cue["start_ms"] / 1000.0
        end = max(cue["end_ms"], cue["start_ms"] + 300) / 1000.0
        overlay_filters.append(
            _drawtext(
                text,
                fontsize=fontsize,
                x="(w-text_w)/2",
                y=f"h-{int(height * 0.13)}-text_h",
                color="white",
                border=2,
                enable=_dt_enable(start, end),
                box=True,
            )
        )
    # AIGC 标识：正式成片不可关闭
    overlay_filters.append(
        _drawtext(
            settings.aigc_label_text,
            fontsize=max(int(height * 0.020), 13),
            x=f"{int(width * 0.03)}",
            y=f"h-{int(height * 0.035)}",
            color="white@0.85",
            border=1,
            box=True,
        )
    )
    if watermark:
        overlay_filters.append(
            _drawtext(
                settings.preview_watermark_text,
                fontsize=max(int(height * 0.024), 15),
                x=f"w-text_w-{int(width * 0.03)}",
                y=f"{int(height * 0.035)}",
                color="white@0.75",
                border=1,
            )
        )

    video_out = "[vcat]"
    if overlay_filters:
        filters.append(f"[vcat]" + ",".join(overlay_filters) + "[vout]")
        video_out = "[vout]"

    filter_complex = ";".join(filters)

    out_dir = settings.data_dir / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{episode.id}_{kind}_{preset}.mp4"
    # FR-063：生成时间在启动 FFmpeg 前取一次，保证元数据与实际合成时刻一致
    generated_at = _utc_stamp()

    cmd: list[str] = [binary, "-y", "-hide_banner", "-loglevel", "error"]
    for chunk in inputs:
        cmd.extend(chunk)
    cmd.extend(["-filter_complex", filter_complex, "-map", video_out, "-map", "[aout]"])

    # FR-063：MP4 的 mov muxer 默认会丢弃无法映射到标准 atom 的自定义键
    # （实测 project_id / generated_at 会被静默忽略），必须开启 use_metadata_tags
    # 才能把项目 ID 与生成时间真正写进文件；预览版保持原行为不变。
    movflags = "+faststart" if kind == "preview" else "+faststart+use_metadata_tags"

    if kind == "preview":
        cmd.extend([
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-b:v", f"{settings.preview_bitrate_kbps}k",
            "-maxrate", f"{int(settings.preview_bitrate_kbps * 1.5)}k",
            "-bufsize", f"{settings.preview_bitrate_kbps * 2}k",
        ])
    else:
        cmd.extend([
            "-c:v", "libx264", "-preset", "medium", "-crf", "21",
            "-b:v", f"{settings.export_video_bitrate_kbps}k",
            "-profile:v", "high", "-level", "4.1",
        ])
    cmd.extend([
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", movflags,
        "-t", f"{total_seconds:.3f}",
        "-metadata", f"comment={settings.aigc_label_text}",
        "-metadata", f"episode_id={episode.id}",
        "-metadata", f"project_id={episode.project_id}",
    ])
    if kind != "preview":
        # FR-063：正式成片补上生成时间。creation_time 是 FFmpeg/MP4 标准键，
        # generated_at 作为自定义键便于排查；预览版仍只带水印，行为不变。
        cmd.extend([
            "-metadata", f"creation_time={generated_at}",
            "-metadata", f"generated_at={generated_at}",
        ])
    cmd.append(str(out_path))

    report(20, "开始 FFmpeg 合成")
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=1800, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RenderError(
            "FFmpeg 合成超时",
            details={"stage": "ffmpeg", "timeout_seconds": 1800},
        ) from exc
    except OSError as exc:
        raise RenderError(f"FFmpeg 启动失败：{exc}") from exc

    if proc.returncode != 0 or not out_path.exists():
        stderr = (proc.stderr or b"").decode("utf-8", "ignore")
        located = _locate_error(stderr, timeline, shot_index)
        raise RenderError(
            "FFmpeg 合成失败，已定位到具体镜头/音轨",
            details={
                "ffmpeg_tail": stderr[-1500:],
                "located": located,
                "hint": "可修复该镜头素材后重试，或直接导出素材包。",
            },
        )

    report(85, "写入媒体资产")
    content = out_path.read_bytes()
    asset = media_service.save_media(
        session,
        content=content,
        kind="video",
        role="preview" if kind == "preview" else "export",
        filename=f"{episode.title}_{kind}_{preset}.mp4",
        mime_type="video/mp4",
        owner_type="episode",
        owner_id=episode.id,
        episode_id=episode.id,
        project_id=episode.project_id,
        width=width,
        height=height,
        duration_ms=timeline["total_duration_ms"],
        status=AssetStatus.CONFIRMED.value,
        metadata={
            "kind": kind,
            "preset": preset,
            "watermark": watermark,
            "timeline": timeline["tracks"]["video"],
            "aigc": True,
        },
    )
    manifest = {
        "kind": kind,
        "preset": preset,
        "resolution": f"{width}x{height}",
        "duration_ms": timeline["total_duration_ms"],
        "watermark": watermark,
        "warnings": validation["warnings"],
        "shot_count": len(timeline["tracks"]["video"]),
        "dialogue_count": len(timeline["tracks"]["dialogue"]),
        "bgm_count": len(timeline["tracks"]["bgm"]),
        "aigc": settings.aigc_label_text,
    }
    report(100, "合成完成")
    return asset, manifest


def _locate_error(
    stderr: str, timeline: dict[str, Any], shot_index: dict[int, int]
) -> list[dict[str, Any]]:
    """把 FFmpeg 报错尽量定位到镜号或音轨。"""
    located: list[dict[str, Any]] = []
    lowered = stderr.lower()
    for entry in timeline["tracks"]["video"]:
        asset_id = entry.get("media_id") or ""
        if asset_id and asset_id[-6:].lower() in lowered:
            located.append({
                "shot_no": entry["shot_no"],
                "track": "video",
                "media_id": asset_id,
                "message": f"镜 {entry['shot_no']} 的素材处理失败",
            })
    for entry in timeline["tracks"]["dialogue"]:
        asset_id = entry.get("media_id") or ""
        if asset_id and asset_id[-6:].lower() in lowered:
            located.append({
                "shot_no": entry["shot_no"],
                "track": "dialogue",
                "media_id": asset_id,
                "message": f"镜 {entry['shot_no']} 的台词音频处理失败",
            })
    if not located:
        for keyword, track in (
            ("xfade", "video"),
            ("concat", "video"),
            ("amix", "audio"),
            ("adelay", "audio"),
        ):
            if keyword in lowered:
                hint = "（转场拼接失败，请检查相邻镜头素材与转场时长）" if keyword == "xfade" else ""
                located.append({
                    "shot_no": None,
                    "track": track,
                    "message": f"合成阶段「{keyword}」失败{hint}，请检查对应轨道的素材完整性",
                })
                break
    if not located:
        located.append({
            "shot_no": None,
            "track": "unknown",
            "message": "FFmpeg 返回未定位错误，详见 ffmpeg_tail",
        })
    return located


def create_export_record(
    session: Session,
    episode: Episode,
    *,
    kind: str,
    preset: str,
    job_id: str | None = None,
) -> ExportRecord:
    record = ExportRecord(
        episode_id=episode.id,
        kind=kind,
        preset=preset,
        status="RUNNING",
        job_id=job_id,
        watermarked=kind == "preview",
    )
    session.add(record)
    session.flush()
    return record
