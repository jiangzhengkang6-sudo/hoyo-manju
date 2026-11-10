"""AudioAgent：TTS、BGM、音效规划（FR-050/051/052）。"""

from __future__ import annotations

import hashlib
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
from app.providers.http_providers import PRICE_TABLE, price_for
from app.providers.registry import resolve
from app.services import audio_fx
from app.services import media as media_service


#: 各 TTS 供应商的默认音色（名称 / 性别 / 供应商侧音色参数 / 描述）。
#:
#: **音色 id 是供应商私有的**：MiniMax 的 `male-qingnian` 在通义那边不存在，
#: 通义收到不认识的值会静默退回默认音色（`TTS_VOICE=Cherry`，女声），
#: 于是「男角色试听出来是女声」。切换供应商必须同时换这套预设。
VOICE_PRESETS: dict[str, list[tuple[str, str, str, str]]] = {
    "qwen": [
        ("清冷女声", "female", "Serena", "偏低沉，适合克制型女主"),
        ("沉稳男声", "male", "Ethan", "中低音，适合男主"),
        ("少年音", "male", "Mochi", "清亮，适合年轻角色"),
        ("叙述旁白", "neutral", "Kai", "平稳，适合旁白"),
    ],
    "minimax": [
        ("清冷女声", "female", "female-shaonv", "偏低沉，适合克制型女主"),
        ("沉稳男声", "male", "male-qingnian", "中低音，适合男主"),
        ("少年音", "male", "male-qn-jingying", "清亮，适合年轻角色"),
        ("叙述旁白", "neutral", "presenter_male", "平稳，适合旁白"),
    ],
}

#: 旧默认音色（MiniMax 风格 id）→ 通义音色参数，供历史数据自动纠正。
LEGACY_VOICE_ALIASES: dict[str, str] = {
    "female-shaonv": "Serena",
    "male-qingnian": "Ethan",
    "male-qn-jingying": "Mochi",
    "presenter_male": "Kai",
    "presenter_female": "Cherry",
}

#: 音色 → 自然语言指令前缀（只对不认 voice 参数的供应商生效，见 voice_style_prompt）
VOICE_STYLE_PROMPTS: dict[str, str] = {
    "Serena": "用清冷克制的中青年女声朗读：",
    "Cherry": "用温柔明亮的女声朗读：",
    "Ethan": "用低沉浑厚的成年男声朗读：",
    "Mochi": "用清亮年轻的少年男声朗读：",
    "Kai": "用平稳克制的旁白男声朗读：",
}

#: 认不出具体音色时按性别兜底
GENDER_STYLE_PROMPTS: dict[str, str] = {
    "male": "用低沉浑厚的成年男声朗读：",
    "female": "用清冷克制的女声朗读：",
    "neutral": "用平稳克制的旁白男声朗读：",
}


def active_speech_provider() -> str:
    from app.core.config import settings

    return (settings.speech_provider or "mock").strip().lower()


def active_voice_presets() -> list[tuple[str, str, str, str]]:
    """当前供应商可用的默认音色（未知供应商退回 MiniMax 那套旧值）。"""
    return VOICE_PRESETS.get(active_speech_provider(), VOICE_PRESETS["minimax"])


def align_voice_to_provider(session: Session, voice: Voice, provider_id: str) -> bool:
    """把音色的供应商音色参数对齐到**当前** TTS 供应商；返回是否发生了改动。

    - 供应商切换后，旧音色 id 在对面不认，会静默变成供应商默认音色；
    - 同时作废 `preview_media_id`：那条试听音频是用错的音色合成的，
      不清掉的话用户一直听到旧声音，怎么点「试听」都是同一个。
    """
    target = (provider_id or "").strip().lower()
    if not target or (voice.provider or "").strip().lower() == target:
        return False
    current_id = (voice.provider_voice_id or "").strip()
    if target == "qwen":
        mapped = LEGACY_VOICE_ALIASES.get(current_id.lower())
        if mapped:
            voice.provider_voice_id = mapped
    voice.provider = target
    voice.preview_media_id = None
    session.flush()
    return True


def voice_style_prompt(voice: Voice | None) -> str:
    """给「不认 voice 参数」的 TTS 供应商用的音色指令前缀。

    通义 `qwen-audio-3.1-tts-next` 实测不认 `voice` 字段（Ethan/Cherry/瞎写的值
    出来都是同一个女声），但认自然语言指令：加「用低沉浑厚的成年男声朗读：」后
    基频从 242Hz 降到 164Hz，时长仅多 0.76s（说明指令本身不会被念出来）。
    """
    if voice is None:
        return ""
    preset = VOICE_STYLE_PROMPTS.get((voice.provider_voice_id or "").strip())
    if preset:
        return preset
    return GENDER_STYLE_PROMPTS.get((voice.gender or "").strip().lower(), "")


def voice_fingerprint(
    provider_id: str, voice: Voice | None, speed: float, style_prompt: str
) -> str:
    """一次语音合成的「音色配方」指纹：供应商 + 音色标识 + 语速 + 音色指令。

    只比对 `voice_id` 和 `speed` 是不够的：供应商不认 voice 参数时，音色完全由
    指令前缀决定。改了提示词、换了供应商、或者老音频是「加指令之前」生成的，
    库里参数一个没变，旧音频却已经换了个声音——这正是同一角色试听出两种
    音色的根因。把配方算成指纹存进素材，读的时候一比就知道要不要重做。
    """
    raw = "|".join(
        [
            (provider_id or "").strip(),
            ((voice.provider_voice_id if voice else "") or "").strip(),
            f"{float(speed):.2f}",
            (style_prompt or "").strip(),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def ensure_default_voices(session: Session, project: Project) -> list[Voice]:
    existing = list(session.scalars(select(Voice).where(Voice.project_id == project.id)))
    if existing:
        return existing
    provider_id = active_speech_provider()
    created = [
        Voice(
            project_id=project.id,
            name=name,
            provider=provider_id,
            provider_voice_id=pid,
            gender=gender,
            description=desc,
        )
        for name, gender, pid, desc in active_voice_presets()
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
    episode_id = ""
    if len(episode_ids) == 1:
        episode = session.get(Episode, next(iter(episode_ids)))
        project_id = episode.project_id if episode else ""
        episode_id = episode.id if episode else ""

    characters: dict[str, CharacterAsset] = {}
    if project_id:
        from app.agents import character as character_agent

        characters = character_agent.episode_characters(session, project_id, episode_id or None)

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


def estimate(
    session: Session, *, shots: list[Shot], character_name: str = ""
) -> dict[str, Any]:
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
            # 指定角色时只统计该角色的台词
            if character_name and name != character_name:
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
    only_character_id: str | None = None,
) -> dict[str, Any]:
    project = session.get(Project, episode.project_id)
    provider = resolve(ProviderKind.SPEECH, session)
    from app.agents import character as character_agent

    characters = character_agent.episode_characters(session, project.id, episode.id)
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
        produced = 0
        # 逐句生成；下标即「该镜第几句台词」，必须显式存到音轨上，
        # 否则中间有一句失败时，后面的音频会整体错位（状态表张冠李戴）。
        for index, line in enumerate(lines):
            text = (line.get("text") or "").strip()
            name = (line.get("character") or "").strip()
            if not text:
                continue
            character = characters.get(name)
            # 只给指定角色配音时，别的角色的台词不提交（否则「生成林深的」会把全组都重跑一遍）
            if only_character_id and (character is None or character.id != only_character_id):
                continue
            if character is None or not character.voice_id:
                skipped.append(
                    {
                        "shot_no": shot.shot_no,
                        "line_index": index,
                        "character": name,
                        "reason": "VOICE_NOT_BOUND",
                        "message": f"角色「{name}」尚未绑定音色，已跳过该句台词",
                    }
                )
                done += 1
                continue
            voice = voices.get(character.voice_id)
            if voice is not None:
                # 供应商切换后的旧音色 id 会导致「男声变女声」，用前先对齐
                align_voice_to_provider(session, voice, provider.id)
            # FR-051：语速来自角色绑定（默认 1.0），并原样留档到音轨与素材
            speed = clamp_speed(getattr(character, "voice_speed", VOICE_SPEED_DEFAULT))
            # 供应商不认 voice 时靠这条指令控制音色（不会念出来）
            style_prompt = voice_style_prompt(voice)
            fingerprint = voice_fingerprint(provider.id, voice, speed, style_prompt)
            result = run_coro(
                provider.synthesize(
                    SpeechRequest(
                        text=text,
                        voice_id=(voice.provider_voice_id if voice else ""),
                        speed=speed,
                        emotion=line.get("emotion", ""),
                        style_prompt=style_prompt,
                    )
                )
            )
            if not result.ok:
                failures.append(
                    {
                        "shot_no": shot.shot_no,
                        "line_index": index,
                        "code": result.error_code,
                        "message": result.error_message,
                        "retryable": result.retryable,
                    }
                )
                done += 1
                if progress_cb:
                    progress_cb(int(done / total * 100))
                continue
            base_duration = int(result.data.get("duration_ms", 0))
            cost_cents += int(result.cost_cents or 0)
            for artifact in result.artifacts:
                # 供应商不认 speed 参数：在本地按 atempo 变速（保持音高），
                # 时长同步换算，否则音轨/字幕时间轴会跟音频对不上。
                adjusted, adjusted_ms, adjusted_mime = audio_fx.adjust_speed(
                    artifact.get("content") or b"",
                    mime=str(artifact.get("mime_type") or ""),
                    speed=speed,
                )
                duration = adjusted_ms or base_duration
                patch: dict[str, Any] = {"content": adjusted}
                if adjusted_mime:
                    # 变速后统一转成 WAV：容器/扩展名一起改，别出现「.mp3 里装着 wav」
                    patch["mime_type"] = adjusted_mime
                    name = str(artifact.get("filename") or "tts.wav")
                    patch["filename"] = f"{name.rsplit('.', 1)[0]}.wav"
                if duration:
                    patch["duration_ms"] = duration
                artifact = {**artifact, **patch}
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
                        "line_index": index,
                        "character": name,
                        "text": text,
                        "voice_id": character.voice_id,
                        "speed": speed,
                        "emotion": line.get("emotion", ""),
                        "provider": provider.id,
                        # 音色配方指纹：改了音色/语速/指令/供应商，旧音频就能被认出来
                        "voice_fingerprint": fingerprint,
                        "style_prompt": style_prompt,
                    },
                )
                created.append(asset.id)
                # 重跑同一句必须替换而不是追加一条，否则时间线会重复念台词
                _replace_dialogue_track(
                    session,
                    episode=episode,
                    shot=shot,
                    index=index,
                    character_id=character.id,
                    media_id=asset.id,
                    text=text,
                    voice_id=character.voice_id,
                    speed=speed,
                    emotion=line.get("emotion", ""),
                    provider=provider.id,
                    source=SOURCE_TTS,
                    stale=False,
                    stale_reason="",
                    start_ms=0,
                    end_ms=duration,
                )
                produced += 1
            done += 1
            if progress_cb:
                progress_cb(int(done / total * 100))
        _restack_dialogue(session, shot)
        if produced:
            shot.audio_status = "READY"

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


def _track_duration(session: Session, track: AudioTrack | None) -> int:
    """音轨真实时长（毫秒）；没有素材时退回 start/end 差值。"""
    if track is None:
        return 0
    if track.media_id:
        asset = session.get(MediaAsset, track.media_id)
        if asset and asset.duration_ms:
            return int(asset.duration_ms)
    return max(int(track.end_ms or 0) - int(track.start_ms or 0), 0)


def dialogue_lines(session: Session, episode: Episode, shots: list[Shot]) -> list[dict[str, Any]]:
    """§12-4：把「镜头里的台词」和「实际生成的音轨」对齐成一张状态表。

    这是配音页降级入口的依据：`status=missing` 的行才需要提供「上传音频 / 跳过」，
    已完成的行不该再给入口（否则用户会以为要重做）。

    除显式 `stale` 标记外，这里还会做一次**音色配方比对**：把当前应有的
    指纹（供应商 + 音色 + 语速 + 指令）和素材里存的比一比，不一致就说明
    这条音频是旧配方出来的——不会再出现「参数没动、声音却变了」。
    """
    from app.agents import character as character_agent

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

    # 算「当前应该用哪套音色」：角色 → 音色 → 语速 → 指令前缀
    characters = character_agent.episode_characters(
        session, episode.project_id, episode.id
    )
    voices = {
        voice.id: voice
        for voice in session.scalars(
            select(Voice).where(Voice.project_id == episode.project_id)
        )
    }
    try:
        provider_id = resolve(ProviderKind.SPEECH, session).id
    except Exception:  # pragma: no cover - 供应商不可用时退化成配置值
        provider_id = active_speech_provider()
    # 一次取齐本集台词素材，避免逐行 session.get 造成 N+1
    media_ids = [track.media_id for track in tracks if track.media_id]
    assets = (
        {
            asset.id: asset
            for asset in session.scalars(
                select(MediaAsset).where(MediaAsset.id.in_(media_ids))
            )
        }
        if media_ids
        else {}
    )

    items: list[dict[str, Any]] = []
    for shot in shots:
        shot_tracks = by_shot.get(shot.id, [])
        for index, line in enumerate(_dialogue_lines(shot)):
            text = (line.get("text") or "").strip()
            if not text:
                continue
            # 先按 line_index 精确对位；只有历史数据（没有序号）才退回按位置
            track = next((t for t in shot_tracks if t.line_index == index), None)
            if track is None:
                legacy = [t for t in shot_tracks if t.line_index is None]
                track = legacy[index] if index < len(legacy) else None
            source = (track.source if track else "") or (SOURCE_TTS if track else "")

            name = (line.get("character") or "").strip()
            character = characters.get(name)
            voice = voices.get(character.voice_id) if character and character.voice_id else None
            expect_speed = (
                clamp_speed(getattr(character, "voice_speed", VOICE_SPEED_DEFAULT))
                if character is not None
                else VOICE_SPEED_DEFAULT
            )
            expect_fp = voice_fingerprint(
                provider_id, voice, expect_speed, voice_style_prompt(voice)
            )
            asset = assets.get(track.media_id) if track and track.media_id else None
            saved_fp = (
                str((asset.metadata_json or {}).get("voice_fingerprint") or "")
                if asset is not None
                else ""
            )
            # 有指纹且对不上 = 配方变了；没指纹 = 指纹上线前的历史音频（不判过期，只标旧）
            voice_drift = bool(saved_fp and saved_fp != expect_fp)
            voice_legacy = bool(asset is not None and not saved_fp)

            if track is None:
                status = "missing"
            elif track.source == SOURCE_SKIPPED:
                status = "skipped"
            elif track.stale:
                status = "stale"
            elif not track.media_id:
                status = "missing"
            elif voice_drift:
                status = "stale"
            else:
                status = "ready"
            stale_reason = track.stale_reason if track else ""
            if voice_drift and not stale_reason:
                stale_reason = "音色参数已更新（音色或语速改过），这条音频还是旧配方生成的"
            items.append(
                {
                    "shot_id": shot.id,
                    "shot_no": shot.shot_no,
                    "line_index": index,
                    "character": line.get("character") or "",
                    "text": text,
                    "emotion": line.get("emotion") or "",
                    "status": status,
                    "source": source,
                    "voice_id": track.voice_id if track else None,
                    "speed": track.speed if track else None,
                    "media_id": track.media_id if track else None,
                    "duration_ms": _track_duration(session, track),
                    "track_id": track.id if track else None,
                    "stale": status == "stale",
                    "stale_reason": stale_reason,
                    # 前端据此提示「旧音色/参数已变，建议重生成」
                    "voice_drift": voice_drift,
                    "voice_legacy": voice_legacy,
                    "expect_speed": expect_speed,
                }
            )
    return items


def _shot_dialogue_tracks(session: Session, shot: Shot) -> list[AudioTrack]:
    """该镜的台语音轨，按「台词序号」排序（历史数据没有序号时排在后面，再按时间）。"""
    return list(
        session.scalars(
            select(AudioTrack)
            .where(AudioTrack.shot_id == shot.id, AudioTrack.kind == "dialogue")
            .order_by(
                AudioTrack.line_index.is_(None),
                AudioTrack.line_index,
                AudioTrack.start_ms,
            )
        )
    )


def _replace_dialogue_track(
    session: Session,
    *,
    episode: Episode,
    shot: Shot,
    index: int,
    **fields: Any,
) -> AudioTrack:
    """把某镜第 `index` 条台词对应的音轨替换掉（没有就新建）。

    优先按 `line_index` 精确对位；历史数据没有序号时才退回「按位置」，
    并顺手把序号补上（下次就不会再错位）。
    重跑同一镜不能留下两条音轨 —— 否则时间线会重复播放同一句台词。
    """
    tracks = _shot_dialogue_tracks(session, shot)
    target = next((t for t in tracks if t.line_index == index), None)
    if target is None:
        legacy = [t for t in tracks if t.line_index is None]
        if index < len(legacy):
            target = legacy[index]
    fields.setdefault("line_index", index)
    if target is not None:
        for key, value in fields.items():
            setattr(target, key, value)
    else:
        track = AudioTrack(episode_id=episode.id, shot_id=shot.id, kind="dialogue", **fields)
        session.add(track)
        target = track
    session.flush()
    return target


def _restack_dialogue(session: Session, shot: Shot) -> None:
    """把该镜的对白音轨按台词顺序首尾相接（从 0 开始）。

    替换/重跑之后如果不重排，旧音轨会保留原来的 start_ms，和新音轨重叠，
    成片里就变成两句话一起说。这里只改时间，不动时长。
    """
    cursor = 0
    for track in _shot_dialogue_tracks(session, shot):
        span = max(int(track.end_ms or 0) - int(track.start_ms or 0), 0)
        if span <= 0:
            asset = session.get(MediaAsset, track.media_id) if track.media_id else None
            # 跳过配音的音轨没有真实时长：按文本长度估一个占位（字幕不至于挤成 0 秒）
            span = int(
                (asset.duration_ms if asset and asset.duration_ms else 0)
                or max(600, len(track.text or "") * 180)
            )
        track.start_ms = cursor
        track.end_ms = cursor + span
        cursor += span
    session.flush()


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
    from app.agents import character as character_agent

    character = character_agent.episode_characters(
        session, episode.project_id, episode.id
    ).get((line.get("character") or "").strip())
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
    _restack_dialogue(session, shot)
    shot.audio_status = "READY"
    return track


def retime_track(
    session: Session,
    *,
    episode: Episode,
    track: AudioTrack,
    speed: float,
) -> AudioTrack:
    """把**已经生成好**的音频按新倍率重调语速（本地 FFmpeg，不调用模型、不计费）。

    始终从「原始音频」出发：atempo 反复叠加会累积失真，所以第一次调速时把
    原始素材与它当时的语速记进 metadata，之后每次都拿它当基准换算净倍率。
    """
    if track.kind != "dialogue" or not track.media_id:
        raise StateConflict("这条音轨没有可调整的音频")
    target = clamp_speed(speed)
    asset = media_service.get_media(session, track.media_id)
    meta = dict(asset.metadata_json or {})
    base_id = str(meta.get("speed_base_media_id") or asset.id)
    base = session.get(MediaAsset, base_id) or asset
    base_meta = dict(base.metadata_json or {})
    base_speed = float(meta.get("speed_base_speed") or base_meta.get("speed") or 1.0)
    ratio = target / base_speed if base_speed else target

    if abs(ratio - 1.0) < 0.01:
        # 回到原始语速：直接复用原始素材，不额外造一份文件。
        # 它可能在前一次调速时被标成 SUPERSEDED，这里要恢复成可用状态。
        base.status = AssetStatus.CONFIRMED.value
        new_asset = base
        duration = int(base.duration_ms or (track.end_ms - track.start_ms))
    else:
        content = media_service.read_media(base)
        new_bytes, new_ms, new_mime = audio_fx.adjust_speed(
            content, mime=base.mime_type, speed=ratio
        )
        if new_mime is None:
            raise StateConflict("音频变速失败，请稍后重试或重新生成这句")
        stem = (base.filename or "tts.wav").rsplit(".", 1)[0]
        new_asset = media_service.save_media(
            session,
            content=new_bytes,
            kind="audio",
            role="dialogue",
            filename=f"{stem}_{target:.2f}x.wav",
            mime_type=new_mime,
            owner_type="shot",
            owner_id=track.shot_id or "",
            episode_id=episode.id,
            project_id=episode.project_id,
            status=AssetStatus.CONFIRMED.value,
            duration_ms=new_ms,
            metadata={
                **base_meta,
                "retimed": True,
                "speed": target,
                # 记住「基准素材 + 它的语速」，下次还从它算，避免二次变速失真
                "speed_base_media_id": base.id,
                "speed_base_speed": base_speed,
                "retimed_from": asset.id,
            },
        )
        duration = int(new_ms or 0)

    if new_asset.id != asset.id:
        media_service.supersede(session, asset)
    track.media_id = new_asset.id
    track.speed = target
    track.end_ms = int(track.start_ms or 0) + max(duration, 1)
    track.stale = False
    track.stale_reason = ""
    if track.shot_id:
        shot = session.get(Shot, track.shot_id)
        if shot is not None:
            _restack_dialogue(session, shot)
    session.flush()
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
    from app.agents import character as character_agent

    character = character_agent.episode_characters(
        session, episode.project_id, episode.id
    ).get((line.get("character") or "").strip())
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
    _restack_dialogue(session, shot)
    return track


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


# --------------------------------------------------------------------------- #
# 氛围音 / BGM（分层方案：台词逐句合成，垫乐与音效单独一层）
#
# qwen-audio-3.1-tts-next 本身是「音频创作」模型（Audiogen），把场景写进
# text_prompt 就能出纯环境音 / 纯音乐（实测 10 秒一单、48kHz 立体声、无人声）。
# 这里刻意**不**把它混进台词，否则每句台词都会从头起一段音乐，拼接后全是断点。
# --------------------------------------------------------------------------- #

AMBIENCE_MIN_SECONDS = 3           # 允许很短：单镜头只需要覆盖镜头时长
AMBIENCE_MAX_SECONDS = 120         # 单次上限（播客场景另有 240 秒）
AMBIENCE_DEFAULT_SECONDS = 30
AMBIENCE_DEFAULT_VOLUME_DB = -14.0  # 垫在台词下面，不抢人声


def _fit_fades(duration_ms: int, fade_in_ms: int, fade_out_ms: int) -> tuple[int, int]:
    """按音频长度收敛淡入淡出。

    默认 800/1500ms 是给整集垫乐（几十秒）用的；4 秒的单镜头如果照搬，
    淡出就吃掉 37%，听感变成「刚起来就散了」。这里按时长比例封顶。
    """
    duration = max(int(duration_ms or 0), 0)
    fade_in = max(0, int(fade_in_ms or 0))
    fade_out = max(0, int(fade_out_ms or 0))
    if duration <= 0:
        return fade_in, fade_out
    return min(fade_in, int(duration * 0.15)), min(fade_out, int(duration * 0.25))


def clamp_ambience_seconds(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = AMBIENCE_DEFAULT_SECONDS
    return max(AMBIENCE_MIN_SECONDS, min(AMBIENCE_MAX_SECONDS, seconds))


def _ambience_unit_price(provider_id: str, model: str) -> float:
    table = PRICE_TABLE.get(f"{provider_id}:{model}") or {}
    return float(table.get("second", 0) or 0)


def ambience_span(
    session: Session,
    *,
    episode: Episode,
    shot_from: int,
    shot_to: int | None = None,
) -> dict[str, Any]:
    """取一组镜头在时间线上的合并窗口。

    一段垫乐可以跨多个镜头共用（同一个场景里换镜头不该重新起一段音乐），
    所以窗口取「首镜起点 → 末镜结束」，中间被移除的镜头自然跳过。
    """
    from app.agents import edit as edit_agent

    first = int(shot_from)
    last = int(shot_to if shot_to is not None else shot_from)
    if last < first:
        first, last = last, first

    timeline = edit_agent.build_timeline(session, episode)
    picked = [
        entry
        for entry in timeline["tracks"]["video"]
        if first <= int(entry.get("shot_no") or 0) <= last
    ]
    if not picked:
        raise StateConflict(f"第 {first}~{last} 镜不在时间线上：请先确认画面与视频")
    picked.sort(key=lambda entry: int(entry.get("start_ms") or 0))
    start_ms = min(int(entry.get("start_ms") or 0) for entry in picked)
    end_ms = max(
        int(entry.get("start_ms") or 0) + int(entry.get("duration_ms") or 0)
        for entry in picked
    )
    shot_nos = [int(entry.get("shot_no") or 0) for entry in picked]
    return {
        "shot_from": min(shot_nos),
        "shot_to": max(shot_nos),
        "shot_nos": shot_nos,
        "shot_ids": [str(entry.get("shot_id") or "") for entry in picked],
        # 音轨挂在首镜上：同一位置只该有一条垫乐，重新生成就替换它
        "shot_id": str(picked[0].get("shot_id") or ""),
        "start_ms": start_ms,
        "duration_ms": max(end_ms - start_ms, 500),
    }


def ambience_window(
    session: Session, *, episode: Episode, shot_no: int
) -> tuple[str, int, int]:
    """取某个镜头在时间线上的 `(shot_id, 起点ms, 时长ms)`（单镜兼容入口）。"""
    span = ambience_span(session, episode=episode, shot_from=shot_no)
    return str(span["shot_id"]), int(span["start_ms"]), int(span["duration_ms"])


def _normalize_range(
    shot_no: int | None, shot_from: int | None, shot_to: int | None
) -> tuple[int, int] | None:
    """把 `shot_no` 与 `shot_from/shot_to` 归一到 `(起始镜, 结束镜)`。"""
    first = shot_from if shot_from is not None else shot_no
    if first is None:
        return None
    last = shot_to if shot_to is not None else first
    first, last = int(first), int(last)
    return (last, first) if last < first else (first, last)


#: 画面提示词里这些词描述的是「镜头怎么拍」，不是场景内容，拼氛围描述时要剔掉
_SCENE_NOISE = (
    "9:16", "竖屏", "横屏", "画风", "构图", "近景", "中景", "远景",
    "特写", "全景", "过肩", "仰拍", "俯拍", "跟拍", "厚涂", "动画",
)


def _scene_phrase(image_prompt: str) -> str:
    """从画面提示词里抽出「在哪、什么时候」这半句。

    `image_prompt` 的结构是「竖屏9:16，日系厚涂动画风，黄昏时分的黑曜石王座厅，
    全景构图，……」，前两段是画风/构图样板，真正有用的是第 3 段起。
    """
    parts = [part.strip() for part in (image_prompt or "").split("，") if part.strip()]
    keep = [part for part in parts if not any(word in part for word in _SCENE_NOISE)]
    # 只取第一段：后面通常开始写人物长相与动作，对「垫什么声音」没用
    return keep[0] if keep else ""


def _instrument_for(mood: str) -> str:
    """按情绪挑一个配器方向（只是给用户一个能改的起点，不追求精确）。"""
    if any(word in mood for word in ("热血", "激昂", "愤怒", "紧张", "压迫", "肃杀")):
        return "战鼓与低音弦乐逐步推进"
    if any(word in mood for word in ("悲伤", "疲惫", "绝望", "孤独", "麻木", "压抑")):
        return "低沉的弦乐缓慢铺底"
    if any(word in mood for word in ("温柔", "平静", "温暖", "轻松", "治愈")):
        return "钢琴与弦乐轻缓铺底"
    return "弦乐缓慢铺底"


def suggest_ambience(
    session: Session,
    *,
    episode: Episode,
    shot_from: int,
    shot_to: int | None = None,
) -> dict[str, Any]:
    """按所选镜头的内容拼一句氛围描述草稿（**不调用模型，零成本**）。

    用户在不同镜头要不同的氛围音，但每次手写描述很麻烦；分镜里其实已经有
    场景（`image_prompt`）、音效（`sfx`）和情绪（`emotion`），直接拼一句就能用。
    """
    span_range = _normalize_range(None, shot_from, shot_to)
    if span_range is None:  # 理论到不了：shot_from 必填
        raise StateConflict("请先选择要覆盖的镜头")
    first, last = span_range

    rows = list(
        session.scalars(
            select(Shot)
            .where(
                Shot.episode_id == episode.id,
                Shot.shot_no >= first,
                Shot.shot_no <= last,
            )
            .order_by(Shot.shot_no)
        )
    )
    if not rows:
        raise StateConflict(f"第 {first}~{last} 镜还没有分镜内容")

    sfx: list[str] = []
    moods: list[str] = []
    for shot in rows:
        data = shot.shot_json or {}
        for item in data.get("sfx") or []:
            text = str(item).strip()
            if text and text not in sfx:
                sfx.append(text)
        for item in str(data.get("emotion") or "").replace("、", ",").split(","):
            text = item.strip()
            if text and text not in moods:
                moods.append(text)

    scene = _scene_phrase(str((rows[0].shot_json or {}).get("image_prompt") or ""))
    mood_text = "、".join(moods[:3])
    instrument = _instrument_for(mood_text)

    segments = [part for part in (scene, "、".join(sfx[:4])) if part]
    segments.append(
        f"{instrument}，气氛{mood_text}" if mood_text else instrument
    )
    prompt = "；".join(segments) + "；没有人声。"

    # 时长优先取时间线（含转场），镜头还没确认时退回分镜时长累加
    try:
        span = ambience_span(
            session, episode=episode, shot_from=first, shot_to=last
        )
        duration_ms = int(span["duration_ms"])
        start_ms = int(span["start_ms"])
        shot_nos = span["shot_nos"]
    except StateConflict:
        duration_ms = sum(int(shot.duration_ms or 0) for shot in rows)
        start_ms = 0
        shot_nos = [int(shot.shot_no) for shot in rows]

    return {
        "prompt": prompt,
        "shot_from": first,
        "shot_to": last,
        "shot_nos": shot_nos,
        "start_ms": start_ms,
        "duration_ms": duration_ms,
        "seconds": max(1, math.ceil(duration_ms / 1000)),
        "sources": {
            "scene": scene,
            "sfx": sfx[:4],
            "emotion": moods[:3],
            "shots": len(rows),
        },
    }



def estimate_ambience(
    session: Session,
    *,
    seconds: Any = None,
    episode: Episode | None = None,
    shot_no: int | None = None,
    shot_from: int | None = None,
    shot_to: int | None = None,
) -> dict[str, Any]:
    """氛围音成本预估：按**生成音频时长**计费（不是按字数）。

    指定镜头（`shot_no` 或 `shot_from/shot_to`）时以该段在时间线上的长度为准，
    忽略手填时长——一段垫乐跨几个镜头共用也只需要一个总长度。
    """
    from app.services import cost as cost_service

    provider = resolve(ProviderKind.SPEECH, session)
    model = str(getattr(provider, "model", "") or "")
    window: dict[str, Any] | None = None
    span_range = _normalize_range(shot_no, shot_from, shot_to)
    if span_range is not None:
        if episode is None:
            raise StateConflict("按镜头生成时必须提供剧集")
        span = ambience_span(
            session, episode=episode, shot_from=span_range[0], shot_to=span_range[1]
        )
        seconds = math.ceil(int(span["duration_ms"]) / 1000)
        window = {**span, "shot_no": span["shot_from"]}
    seconds = clamp_ambience_seconds(seconds)
    cents = price_for(provider.id, model, "second", seconds)
    snap = cost_service.quota_snapshot(session)
    return {
        "provider": provider.id,
        "model": model,
        "seconds": seconds,
        "min_seconds": AMBIENCE_MIN_SECONDS,
        "max_seconds": AMBIENCE_MAX_SECONDS,
        "billing_unit": "second",
        "unit_price_cents": round(_ambience_unit_price(provider.id, model), 3),
        "estimate_cents": cents,
        "quota_after_cents": snap["remaining_cents"] - cents,
        "can_generate": snap["remaining_cents"] >= cents,
        "window": window,
    }


def generate_ambience(
    session: Session,
    *,
    episode: Episode,
    prompt: str,
    seconds: Any = None,
    shot_no: int | None = None,
    shot_from: int | None = None,
    shot_to: int | None = None,
    volume_db: float | None = None,
    fade_in_ms: int = 800,
    fade_out_ms: int = 1500,
    progress_cb=None,
) -> dict[str, Any]:
    """生成一轨氛围音 / BGM，并挂到时间线（整条替换，不叠轨）。

    只调用一次供应商（长提示词、无引号台词），返回的音频当「垫乐」用：
    - 与台词音轨分层，混音时按 volume_db + 淡入淡出压在人声下；
    - 指定 `shot_from/shot_to` 时只覆盖这段镜头（跨镜头共用一段垫乐）；
    - 同一位置的垫乐只保留一条，重新生成就替换旧的，否则两条会同时播放。
    """
    text = (prompt or "").strip()
    if not text:
        raise StateConflict("请先描述想要的氛围或音乐（例如：雨夜巷口，低沉弦乐铺底）")
    window: dict[str, Any] | None = None
    span_range = _normalize_range(shot_no, shot_from, shot_to)
    if span_range is not None:
        span = ambience_span(
            session, episode=episode, shot_from=span_range[0], shot_to=span_range[1]
        )
        seconds = math.ceil(int(span["duration_ms"]) / 1000)
        window = {**span, "shot_no": span["shot_from"]}
    seconds = clamp_ambience_seconds(seconds)
    provider = resolve(ProviderKind.SPEECH, session)
    model = str(getattr(provider, "model", "") or "")

    if progress_cb:
        progress_cb(5)
    result = run_coro(
        provider.synthesize(
            SpeechRequest(
                text=text,
                output_format="wav",
                sample_rate=48000,
                channels=2,
            )
        )
    )
    if not result.ok:
        raise ProviderError(
            result.error_message or "氛围音生成失败",
            retryable=result.retryable,
        )
    if not result.artifacts:
        raise ProviderError("供应商未返回音频")

    if progress_cb:
        progress_cb(65)
    artifact = result.artifacts[0]
    # 供应商返回的 wav 头是占位值，必须过一遍 ffmpeg 重写头部（同时保住立体声）
    # 供应商按最短 N 秒出片，单镜头要裁到镜头长度，否则会压到下一镜
    target_ms = int(window["duration_ms"]) if window else None
    content, duration_ms, mime = audio_fx.adjust_speed(
        artifact.get("content") or b"",
        mime=str(artifact.get("mime_type") or ""),
        speed=1.0,
        sample_rate=48000,
        channels=2,
        max_ms=target_ms,
    )
    duration = int(duration_ms or result.data.get("duration_ms") or seconds * 1000)
    # 计费按供应商实际生成时长（裁剪不退税）
    generated_ms = int(result.data.get("duration_ms") or duration)
    if not content:
        raise ProviderError("氛围音生成结果为空")
    fade_in, fade_out = _fit_fades(duration, fade_in_ms, fade_out_ms)

    asset = media_service.save_artifact(
        session,
        {
            "content": content,
            "mime_type": mime or "audio/wav",
            "duration_ms": duration,
        },
        kind="audio",
        role="bgm",
        owner_type="episode",
        owner_id=episode.id,
        episode_id=episode.id,
        project_id=episode.project_id,
        status=AssetStatus.CONFIRMED.value,
        rights_declared=True,
        rights_note="AI 生成（模型：qwen-audio-3.1-tts-next）",
        metadata={
            "prompt": text,
            "requested_seconds": seconds,
            "provider": provider.id,
            "model": model,
            "generated": True,
            "kind": "ambience" if window else "bgm",
            "shot_no": window["shot_no"] if window else None,
            # 跨镜头共用一段垫乐时，前端要能显示「第 1–3 镜」
            "shot_from": window["shot_from"] if window else None,
            "shot_to": window["shot_to"] if window else None,
            "shot_nos": window["shot_nos"] if window else None,
        },
    )

    volume = AMBIENCE_DEFAULT_VOLUME_DB if volume_db is None else float(volume_db)
    kind = "ambience" if window else "bgm"
    start_ms = int(window["start_ms"]) if window else 0
    stmt = select(AudioTrack).where(
        AudioTrack.episode_id == episode.id, AudioTrack.kind == kind
    )
    if window:
        stmt = stmt.where(AudioTrack.shot_id == window["shot_id"])
    else:
        stmt = stmt.where(AudioTrack.shot_id.is_(None))
    track = session.scalar(stmt.order_by(AudioTrack.created_at))
    if track is None:
        track = AudioTrack(
            episode_id=episode.id,
            shot_id=window["shot_id"] if window else None,
            kind=kind,
            media_id=asset.id,
            start_ms=start_ms,
            end_ms=start_ms + duration,
            volume_db=volume,
            fade_in_ms=fade_in,
            fade_out_ms=fade_out,
            text=text[:400],
            provider=provider.id,
            rights_declared=True,
            source=SOURCE_TTS,
        )
        session.add(track)
    else:
        # 换素材而不是新建一条：两条 bgm 轨会同时播
        if track.media_id and track.media_id != asset.id:
            old = session.get(MediaAsset, track.media_id)
            if old is not None:
                media_service.supersede(session, old)
        track.media_id = asset.id
        track.shot_id = window["shot_id"] if window else None
        track.start_ms = start_ms
        track.end_ms = start_ms + duration
        track.volume_db = volume
        track.fade_in_ms = fade_in
        track.fade_out_ms = fade_out
        track.text = text[:400]
        track.provider = provider.id
        track.rights_declared = True
        track.source = SOURCE_TTS
        track.stale = False
        track.stale_reason = ""
    session.flush()

    cost_cents = price_for(provider.id, model, "second", generated_ms / 1000)
    if progress_cb:
        progress_cb(100)
    return {
        "track": track_to_dict(session, track),
        "media_id": asset.id,
        "duration_ms": duration,
        "generated_ms": generated_ms,
        "window": window,
        "cost_cents": cost_cents,
        "prompt": text,
        "provider": provider.id,
        "model": model,
    }


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
        raise StateConflict("上传或选择 BGM/音效时必须声明使用权（FR-082）")
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
