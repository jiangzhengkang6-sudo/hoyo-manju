"""AudioAgent：TTS、BGM、音效规划（FR-MVP-050/051/052）。"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.story import run_coro
from app.core.errors import ProviderError, StateConflict
from app.db.models import (
    AssetStatus,
    AudioTrack,
    CharacterAsset,
    Episode,
    MediaAsset,
    Project,
    Shot,
    Voice,
)
from app.providers.base import ProviderKind, SpeechRequest
from app.providers.registry import resolve
from app.services import media as media_service


def ensure_default_voices(session: Session, project: Project) -> list[Voice]:
    existing = list(session.scalars(select(Voice).where(Voice.project_id == project.id)))
    if existing:
        return existing
    presets = [
        ("清冷女声", "female", "female-shaonv", "偏低沉，适合克制型女主"),
        ("沉稳男声", "male", "male-qingnian", "中低音，适合男主"),
        ("少年音", "male", "male-qn-jingying", "清亮，适合年轻角色"),
        ("叙述旁白", "neutral", "presenter_male", "平稳，适合旁白"),
    ]
    created = [
        Voice(
            project_id=project.id,
            name=name,
            provider="mock",
            provider_voice_id=pid,
            gender=gender,
            description=desc,
        )
        for name, gender, pid, desc in presets
    ]
    session.add_all(created)
    session.flush()
    return created


# FR-051：语速倍率的合法区间。越界一律夹回，避免供应商侧报错或产出听不清的音频。
VOICE_SPEED_MIN = 0.5
VOICE_SPEED_MAX = 2.0
VOICE_SPEED_DEFAULT = 1.0


def clamp_speed(value: Any) -> float:
    """把语速夹到合法区间；非数值/缺失回落到 1.0。"""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return VOICE_SPEED_DEFAULT
    if speed != speed:  # NaN
        return VOICE_SPEED_DEFAULT
    return max(VOICE_SPEED_MIN, min(VOICE_SPEED_MAX, speed))


def bind_voice(
    session: Session,
    character: CharacterAsset,
    voice_id: str,
    speed: Any = None,
) -> CharacterAsset:
    """绑定音色（FR-050）；`speed` 给了就一并更新（FR-051）。"""
    voice = session.get(Voice, voice_id)
    if voice is None or voice.project_id != character.project_id:
        raise StateConflict("音色不存在或不属于当前项目")
    character.voice_id = voice_id
    if speed is not None:
        character.voice_speed = clamp_speed(speed)
    session.flush()
    return character


def dialogue_requirements(session: Session, *, shots: list[Shot]) -> dict[str, Any]:
    """FR-050：找出「有台词但没绑音色」的角色。

    返回 `missing`（角色名 → 涉及镜号）与 `speaking`（有台词的角色名集合）。
    这是 TTS 的**前置条件**：不满足就必须在提交前拒绝，而不是把台词静默跳过 ——
    静默跳过的结果是成片里台词没声音，用户很难发现。
    """
    episode_ids = {shot.episode_id for shot in shots}
    project_id = ""
    if len(episode_ids) == 1:
        episode = session.get(Episode, next(iter(episode_ids)))
        project_id = episode.project_id if episode else ""

    characters: dict[str, CharacterAsset] = {}
    if project_id:
        characters = {
            c.name: c
            for c in session.scalars(
                select(CharacterAsset).where(CharacterAsset.project_id == project_id)
            )
        }

    speaking: set[str] = set()
    missing: dict[str, list[int]] = {}
    for shot in shots:
        for line in (shot.shot_json or {}).get("dialogue") or []:
            text = (line.get("text") or "").strip()
            name = (line.get("character") or "").strip()
            if not text or not name:
                continue
            speaking.add(name)
            character = characters.get(name)
            if character is None or not character.voice_id:
                missing.setdefault(name, [])
                if shot.shot_no not in missing[name]:
                    missing[name].append(shot.shot_no)

    return {
        "speaking": sorted(speaking),
        "missing": {name: sorted(nos) for name, nos in sorted(missing.items())},
        "has_dialogue": bool(speaking),
    }


def estimate(session: Session, *, shots: list[Shot]) -> dict[str, Any]:
    """TTS 成本预估（FR-010 / §12-6 配额拦截前置条件）。

    口径与 `synthesize_for_shots` 一致：只统计能真正提交的台词
    （有文本、且角色已绑定音色），避免把会被跳过的句子也预留额度。
    """
    provider = resolve(ProviderKind.SPEECH, session)
    caps = provider.capabilities()
    unit_price = int(getattr(caps, "unit_price_cents", 0) or 0)
    req = dialogue_requirements(session, shots=shots)
    missing_names = set(req["missing"])
    lines: list[str] = []
    skipped = 0
    for shot in shots:
        for line in (shot.shot_json or {}).get("dialogue") or []:
            text = (line.get("text") or "").strip()
            name = (line.get("character") or "").strip()
            if not text:
                continue
            # 未绑音色的角色：这一句不可能被合成，不计入预留
            if name in missing_names or not name:
                skipped += 1
                continue
            lines.append(text)
    chars = sum(len(t) for t in lines)
    quantity = math.ceil(chars / 1000) if chars else 0
    total = max(0, int(math.ceil(quantity * unit_price))) if unit_price else 0
    from app.services import cost as cost_service

    snap = cost_service.quota_snapshot(session)
    # FR-050：音色未绑定即为阻断项，提交前就要拒绝（不留任务、不占配额）
    blocking = [
        {
            "code": "MISSING_VOICE",
            "character": name,
            "shot_nos": nos,
            "message": f"角色「{name}」有台词但未绑定音色，请先绑定后再生成配音",
        }
        for name, nos in req["missing"].items()
    ]
    return {
        "provider": provider.id,
        "model": getattr(provider, "model", provider.id),
        "estimate_cents": total,
        "quota_after_cents": snap["remaining_cents"] - total,
        "remaining_cents": snap["remaining_cents"],
        "line_count": len(lines),
        "skipped_count": skipped,
        "char_count": chars,
        "billing_unit": caps.billing_unit,
        "unit_price_cents": unit_price,
        "estimated_latency_seconds": caps.estimated_latency_seconds,
        # FR-050 前置条件：前端据此禁用「生成配音」并给出可执行提示
        "can_generate": not blocking,
        "blocking_problems": blocking,
        "speaking_characters": req["speaking"],
        "has_dialogue": req["has_dialogue"],
    }


def synthesize_for_shots(
    session: Session,
    *,
    episode: Episode,
    shots: list[Shot],
    progress_cb=None,
) -> dict[str, Any]:
    project = session.get(Project, episode.project_id)
    provider = resolve(ProviderKind.SPEECH, session)
    characters = {
        c.name: c
        for c in session.scalars(
            select(CharacterAsset).where(CharacterAsset.project_id == project.id)
        )
    }
    voices = {v.id: v for v in session.scalars(select(Voice).where(Voice.project_id == project.id))}

    created: list[str] = []
    failures: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    # FR-042 / §13.1：实际消耗必须回传，否则 executor 结算时拿不到金额（会按 0 记账）
    cost_cents = 0
    total = max(1, sum(len((s.shot_json or {}).get("dialogue") or []) for s in shots))
    done = 0

    for shot in shots:
        lines = (shot.shot_json or {}).get("dialogue") or []
        if not lines:
            shot.audio_status = "NO_DIALOGUE"
            continue
        cursor = 0
        for line in lines:
            text = (line.get("text") or "").strip()
            name = line.get("character") or ""
            if not text:
                continue
            character = characters.get(name)
            if character is None or not character.voice_id:
                skipped.append(
                    {
                        "shot_no": shot.shot_no,
                        "character": name,
                        "reason": "VOICE_NOT_BOUND",
                        "message": f"角色「{name}」尚未绑定音色，已跳过该句台词",
                    }
                )
                done += 1
                continue
            voice = voices.get(character.voice_id)
            # FR-051：语速来自角色绑定（默认 1.0），并原样留档到音轨与素材
            speed = clamp_speed(getattr(character, "voice_speed", VOICE_SPEED_DEFAULT))
            result = run_coro(
                provider.synthesize(
                    SpeechRequest(
                        text=text,
                        voice_id=(voice.provider_voice_id if voice else ""),
                        speed=speed,
                        emotion=line.get("emotion", ""),
                    )
                )
            )
            if not result.ok:
                failures.append(
                    {
                        "shot_no": shot.shot_no,
                        "code": result.error_code,
                        "message": result.error_message,
                        "retryable": result.retryable,
                    }
                )
                done += 1
                if progress_cb:
                    progress_cb(int(done / total * 100))
                continue
            duration = int(result.data.get("duration_ms", 0))
            cost_cents += int(result.cost_cents or 0)
            for artifact in result.artifacts:
                asset = media_service.save_artifact(
                    session,
                    artifact,
                    kind="audio",
                    role="dialogue",
                    owner_type="shot",
                    owner_id=shot.id,
                    episode_id=episode.id,
                    project_id=project.id,
                    status=AssetStatus.CONFIRMED.value,
                    metadata={
                        "shot_id": shot.id,
                        "shot_no": shot.shot_no,
                        "character": name,
                        "text": text,
                        "voice_id": character.voice_id,
                        "speed": speed,
                        "emotion": line.get("emotion", ""),
                        "provider": provider.id,
                    },
                )
                created.append(asset.id)
                track = AudioTrack(
                    episode_id=episode.id,
                    shot_id=shot.id,
                    character_id=character.id,
                    kind="dialogue",
                    media_id=asset.id,
                    text=text,
                    voice_id=character.voice_id,
                    speed=speed,
                    emotion=line.get("emotion", ""),
                    provider=provider.id,
                    start_ms=cursor,
                    end_ms=cursor + duration,
                )
                session.add(track)
                cursor += duration
            done += 1
            if progress_cb:
                progress_cb(int(done / total * 100))
        shot.audio_status = "READY" if cursor > 0 else shot.audio_status

    session.flush()
    return {
        "media_ids": created,
        "failures": failures,
        "skipped": skipped,
        "count": len(created),
        "cost_cents": cost_cents,
    }


#: §12-4 音轨来源：TTS 合成 / 用户上传顶替 / 有意跳过。
SOURCE_TTS = "tts"
SOURCE_MANUAL = "manual"
SOURCE_SKIPPED = "skipped"


def _dialogue_lines(shot: Shot) -> list[dict[str, Any]]:
    return (shot.shot_json or {}).get("dialogue") or []


def dialogue_lines(session: Session, episode: Episode, shots: list[Shot]) -> list[dict[str, Any]]:
    """§12-4：把「镜头里的台词」和「实际生成的音轨」对齐成一张状态表。

    这是配音页降级入口的依据：`status=missing` 的行才需要提供「上传音频 / 跳过」，
    已完成的行不该再给入口（否则用户会以为要重做）。
    """
    tracks = list(
        session.scalars(
            select(AudioTrack)
            .where(AudioTrack.episode_id == episode.id, AudioTrack.kind == "dialogue")
            .order_by(AudioTrack.start_ms)
        )
    )
    by_shot: dict[str, list[AudioTrack]] = {}
    for track in tracks:
        by_shot.setdefault(track.shot_id or "", []).append(track)

    items: list[dict[str, Any]] = []
    for shot in shots:
        shot_tracks = by_shot.get(shot.id, [])
        for index, line in enumerate(_dialogue_lines(shot)):
            text = (line.get("text") or "").strip()
            if not text:
                continue
            # 按出现顺序与音轨一一对应；多出来的（例如手工新增的）算作额外条目
            track = shot_tracks[index] if index < len(shot_tracks) else None
            source = (track.source if track else "") or (SOURCE_TTS if track else "")
            if track is None:
                status = "missing"
            elif track.source == SOURCE_SKIPPED:
                status = "skipped"
            elif track.stale:
                status = "stale"
            elif not track.media_id:
                status = "missing"
            else:
                status = "ready"
            items.append(
                {
                    "shot_id": shot.id,
                    "shot_no": shot.shot_no,
                    "character": line.get("character") or "",
                    "text": text,
                    "emotion": line.get("emotion") or "",
                    "status": status,
                    "source": source,
                    "media_id": track.media_id if track else None,
                    "track_id": track.id if track else None,
                    "stale": bool(track and track.stale),
                    "stale_reason": track.stale_reason if track else "",
                }
            )
    return items


def _replace_dialogue_track(
    session: Session,
    *,
    episode: Episode,
    shot: Shot,
    index: int,
    **fields: Any,
) -> AudioTrack:
    """把某镜第 `index` 条台词对应的音轨替换掉（没有就新建）。

    重跑同一镜时不能留下两条音轨 —— 否则时间线会重复播放同一句台词。
    """
    tracks = list(
        session.scalars(
            select(AudioTrack)
            .where(AudioTrack.shot_id == shot.id, AudioTrack.kind == "dialogue")
            .order_by(AudioTrack.start_ms)
        )
    )
    if index < len(tracks):
        track = tracks[index]
        for key, value in fields.items():
            setattr(track, key, value)
    else:
        track = AudioTrack(episode_id=episode.id, shot_id=shot.id, kind="dialogue", **fields)
        session.add(track)
    session.flush()
    return track


def attach_manual_audio(
    session: Session,
    *,
    episode: Episode,
    shot: Shot,
    line_index: int,
    media_id: str,
) -> AudioTrack:
    """§12-4：TTS 失败时用用户上传的音频顶替该句台词。"""
    asset = media_service.get_media(session, media_id)
    if asset.kind != "audio":
        raise StateConflict("只能上传音频类素材来顶替配音")
    lines = _dialogue_lines(shot)
    if not (0 <= line_index < len(lines)):
        raise StateConflict("该镜头没有对应序号的台词")
    line = lines[line_index]
    character = session.scalars(
        select(CharacterAsset).where(
            CharacterAsset.project_id == episode.project_id,
            CharacterAsset.name == (line.get("character") or ""),
        )
    ).first()
    duration = int(asset.duration_ms or 0)
    track = _replace_dialogue_track(
        session,
        episode=episode,
        shot=shot,
        index=line_index,
        character_id=character.id if character else None,
        media_id=asset.id,
        text=(line.get("text") or "").strip(),
        voice_id=None,
        # 用户上传的音频语速就是它本身，记 1.0 而不是沿用角色语速（否则会误导核对）
        speed=1.0,
        emotion=line.get("emotion") or "",
        provider="manual",
        source=SOURCE_MANUAL,
        stale=False,
        stale_reason="",
        start_ms=0,
        end_ms=duration,
        rights_declared=True,
    )
    shot.audio_status = "READY"
    return track


def skip_dialogue(
    session: Session,
    *,
    episode: Episode,
    shot: Shot,
    line_index: int,
    reason: str = "",
) -> AudioTrack:
    """§12-4：跳过该句配音，让预览/导出可以继续。

    保留一条 `source=skipped` 的音轨作为**显式记录**：既让状态表知道这行是「有意跳过」
    而不是「还没生成」，也让导出时能给出明确说明。
    """
    lines = _dialogue_lines(shot)
    if not (0 <= line_index < len(lines)):
        raise StateConflict("该镜头没有对应序号的台词")
    line = lines[line_index]
    character = session.scalars(
        select(CharacterAsset).where(
            CharacterAsset.project_id == episode.project_id,
            CharacterAsset.name == (line.get("character") or ""),
        )
    ).first()
    return _replace_dialogue_track(
        session,
        episode=episode,
        shot=shot,
        index=line_index,
        character_id=character.id if character else None,
        media_id=None,
        text=(line.get("text") or "").strip(),
        voice_id=None,
        speed=1.0,
        emotion=line.get("emotion") or "",
        provider="",
        source=SOURCE_SKIPPED,
        stale=False,
        stale_reason=reason,
        start_ms=0,
        end_ms=0,
        rights_declared=False,
    )


def dialogue_progress(session: Session, episode: Episode, shots: list[Shot]) -> dict[str, int]:
    """配音完成度（含降级项），用于前端展示「还差几句」。"""
    items = dialogue_lines(session, episode, shots)
    counts = {"total": len(items), "ready": 0, "missing": 0, "stale": 0, "skipped": 0, "manual": 0}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        if item["source"] == SOURCE_MANUAL:
            counts["manual"] += 1
    return counts


def mark_stale_for_shot(session: Session, shot: Shot, reason: str) -> int:
    tracks = list(
        session.scalars(
            select(AudioTrack).where(
                AudioTrack.shot_id == shot.id, AudioTrack.kind == "dialogue"
            )
        )
    )
    for track in tracks:
        track.stale = True
        track.stale_reason = reason
    session.flush()
    return len(tracks)


def list_tracks(session: Session, episode_id: str, kind: str | None = None) -> list[AudioTrack]:
    stmt = select(AudioTrack).where(AudioTrack.episode_id == episode_id).order_by(AudioTrack.start_ms)
    if kind:
        stmt = stmt.where(AudioTrack.kind == kind)
    return list(session.scalars(stmt))


def add_track(
    session: Session,
    *,
    episode: Episode,
    media_id: str,
    kind: str,
    start_ms: int,
    end_ms: int,
    volume_db: float,
    fade_in_ms: int,
    fade_out_ms: int,
    rights_declared: bool,
) -> AudioTrack:
    asset = media_service.get_media(session, media_id)
    if asset.kind != "audio":
        raise StateConflict("只能挂载音频类素材")
    if not rights_declared and not asset.rights_declared:
        raise StateConflict("上传或选择 BGM/音效时必须声明使用权（FR-MVP-082）")
    if end_ms and end_ms <= start_ms:
        raise StateConflict("结束时间必须大于开始时间")
    track = AudioTrack(
        episode_id=episode.id,
        kind=kind,
        media_id=media_id,
        start_ms=start_ms,
        end_ms=end_ms or (start_ms + (asset.duration_ms or 0)),
        volume_db=volume_db,
        fade_in_ms=fade_in_ms,
        fade_out_ms=fade_out_ms,
        rights_declared=rights_declared or asset.rights_declared,
    )
    session.add(track)
    session.flush()
    return track


def track_to_dict(session: Session, track: AudioTrack) -> dict[str, Any]:
    asset = session.get(MediaAsset, track.media_id) if track.media_id else None
    return {
        "id": track.id,
        "episode_id": track.episode_id,
        "shot_id": track.shot_id,
        "character_id": track.character_id,
        "kind": track.kind,
        "media_id": track.media_id,
        "text": track.text,
        "voice_id": track.voice_id,
        # FR-051：语速 / 情绪 / 供应商与原文、音频一起留档，导出时可直接核对
        "speed": track.speed,
        "emotion": track.emotion,
        "provider": track.provider,
        "start_ms": track.start_ms,
        "end_ms": track.end_ms,
        "volume_db": track.volume_db,
        "fade_in_ms": track.fade_in_ms,
        "fade_out_ms": track.fade_out_ms,
        "stale": track.stale,
        "stale_reason": track.stale_reason,
        "rights_declared": track.rights_declared,
        # §12-4：区分 tts / manual（用户上传顶替）/ skipped（有意跳过）
        "source": track.source,
        "media": media_service.to_dict(asset) if asset else None,
    }


def cleanup_failed(session: Session, track: AudioTrack) -> None:
    session.delete(track)
    session.flush()
