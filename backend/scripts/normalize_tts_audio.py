"""修复历史 TTS 音频的「假时长」：供应商返回的 WAV 头部是占位值。

现象：`wave` 读出来 10 小时（`nframes=536870886`），浏览器进度条/总时长跟着错。
做法：过一遍 FFmpeg 重写头部（不变速、只重编码成 44.1k 单声道），
生成新素材并改指向，旧素材标记为已被替代（不覆盖）。

用法（backend 目录）：
    python scripts/normalize_tts_audio.py --dry-run
    python scripts/normalize_tts_audio.py
"""

from __future__ import annotations

import argparse
import io
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.agents import audio as audio_agent  # noqa: E402
from app.db.models import AssetStatus, AudioTrack, Episode, MediaAsset, Shot  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.services import audio_fx  # noqa: E402
from app.services import media as media_service  # noqa: E402


def _claimed_ms(content: bytes) -> int | None:
    try:
        with wave.open(io.BytesIO(content)) as handle:
            return int(handle.getnframes() / handle.getframerate() * 1000)
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    fixed = 0
    with session_scope() as session:
        tracks = list(
            session.scalars(
                select(AudioTrack).where(
                    AudioTrack.kind == "dialogue", AudioTrack.media_id.is_not(None)
                )
            )
        )
        for track in tracks:
            asset = session.get(MediaAsset, track.media_id)
            if asset is None or not asset.storage_key:
                continue
            try:
                content = media_service.read_media(asset)
            except Exception as exc:  # noqa: BLE001
                print(f"! {track.id} 读取失败：{exc}")
                continue
            claimed = _claimed_ms(content)
            declared = int(asset.duration_ms or 0)
            suspicious = claimed is None or (
                declared and abs(claimed - declared) > max(1500, declared * 0.3)
            )
            if not suspicious:
                continue
            output, duration, mime = audio_fx.adjust_speed(
                content, mime=asset.mime_type, speed=1.0
            )
            if mime is None:
                print(f"! {track.id} 重编码失败，跳过")
                continue
            print(
                f"- {track.id} 头部时长 {claimed}ms / 记录 {declared}ms "
                f"→ 实际 {duration}ms"
            )
            if args.dry_run:
                fixed += 1
                continue
            episode = session.get(Episode, track.episode_id)
            new_asset = media_service.save_media(
                session,
                content=output,
                kind="audio",
                role="dialogue",
                filename=f"{(asset.filename or 'tts.wav').rsplit('.', 1)[0]}_norm.wav",
                mime_type=mime,
                owner_type="shot",
                owner_id=track.shot_id or "",
                episode_id=episode.id if episode else None,
                project_id=episode.project_id if episode else None,
                status=AssetStatus.CONFIRMED.value,
                duration_ms=duration,
                metadata={
                    **(asset.metadata_json or {}),
                    "normalized": True,
                    "normalized_from": asset.id,
                },
            )
            media_service.supersede(session, asset)
            track.media_id = new_asset.id
            track.end_ms = int(track.start_ms or 0) + max(int(duration or 0), 1)
            fixed += 1
            if track.shot_id:
                shot = session.get(Shot, track.shot_id)
                if shot is not None:
                    audio_agent._restack_dialogue(session, shot)
        if args.dry_run:
            print(f"待修复 {fixed} 条（dry-run，未写入）")
        else:
            print(f"已修复 {fixed} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
