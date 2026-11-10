"""配音链路的口径回归：一句话对应一条音轨，且必须能对上「第几句」。

历史问题：
- `AudioTrack` 没有 `line_index`，状态表/替换逻辑只能按 start_ms 的下标去猜。
  中间有一句失败或被跳过时，后面的音轨整体错位：第 2 句显示已生成，实际是第 1 句的音频。
- 重跑同一镜时 `synthesize_for_shots` 只追加不替换，时间线会重复念同一句台词。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents import audio as audio_agent
from app.db.models import AudioTrack, Episode, Shot
from app.db.session import session_scope
from app.providers import mock as mock_provider
from app.providers.base import ProviderKind, ProviderResult
from test_quota import _bind_default_voice


def _seed(episode_id: str, lines: list[str]) -> list[str]:
    with session_scope() as session:
        shots = []
        for no, text in enumerate(lines, start=1):
            shot = Shot(
                episode_id=episode_id, shot_no=no, duration_ms=4000,
                shot_json={
                    "action": f"动作{no}",
                    "image_prompt": "画面",
                    "dialogue": [{"character": "小满", "text": text, "emotion": "平静"}],
                },
                stage="CONFIRMED",
            )
            session.add(shot)
            shots.append(shot)
        session.flush()
        return [s.id for s in shots]


def _synthesize(episode_id: str, shot_ids: list[str]) -> dict:
    with session_scope() as session:
        episode = session.get(Episode, episode_id)
        shots = [session.get(Shot, sid) for sid in shot_ids]
        result = audio_agent.synthesize_for_shots(session, episode=episode, shots=shots)
        session.flush()
        return result


def _tracks(shot_id: str) -> list[AudioTrack]:
    with session_scope() as session:
        return list(
            session.query(AudioTrack)
            .filter(AudioTrack.shot_id == shot_id, AudioTrack.kind == "dialogue")
            .order_by(AudioTrack.line_index)
        )


def test_rerun_replaces_track_instead_of_duplicating(
    client: TestClient, project: dict, episode: dict
) -> None:
    shot_ids = _seed(episode["id"], ["这是一句测试台词。"])
    _bind_default_voice(client, episode)

    first = _synthesize(episode["id"], shot_ids)
    assert first["count"] == 1
    second = _synthesize(episode["id"], shot_ids)
    assert second["count"] == 1

    tracks = _tracks(shot_ids[0])
    assert len(tracks) == 1, "重跑同一句不能留下两条音轨（否则成片重复念台词）"
    assert tracks[0].line_index == 0
    assert (tracks[0].start_ms, tracks[0].end_ms) == (0, tracks[0].end_ms)


def test_failed_line_does_not_shift_later_lines(
    client: TestClient, project: dict, episode: dict, monkeypatch
) -> None:
    """同一镜里第 1 句失败时，第 2 句的音轨必须记在 line_index=1 上。"""
    _bind_default_voice(client, episode)
    with session_scope() as session:
        shot = Shot(
            episode_id=episode["id"], shot_no=1, duration_ms=4000,
            shot_json={
                "action": "对话",
                "image_prompt": "画面",
                "dialogue": [
                    {"character": "小满", "text": "失败句：这一句会失败。", "emotion": "平静"},
                    {"character": "小满", "text": "第二句正常。", "emotion": "平静"},
                ],
            },
            stage="CONFIRMED",
        )
        session.add(shot)
        session.flush()
        shot_id = shot.id

    original = mock_provider.MockSpeechProvider.synthesize

    async def flaky(self, request):  # noqa: ANN001
        if "失败句" in request.text:
            return ProviderResult.failure(
                ProviderKind.SPEECH, "PROVIDER_ERROR", "注入失败"
            )
        return await original(self, request)

    monkeypatch.setattr(mock_provider.MockSpeechProvider, "synthesize", flaky)
    result = _synthesize(episode["id"], [shot_id])
    assert result["count"] == 1
    assert result["failures"] and result["failures"][0]["line_index"] == 0

    tracks = _tracks(shot_id)
    assert len(tracks) == 1
    assert tracks[0].line_index == 1, "第二句必须记成 line_index=1，否则状态表会张冠李戴"
    assert tracks[0].start_ms == 0, "只有一句成功时它从头开始即可"

    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        items = audio_agent.dialogue_lines(session, ep, [session.get(Shot, shot_id)])
    by_text = {item["text"]: item["status"] for item in items}
    assert by_text["失败句：这一句会失败。"] == "missing"
    assert by_text["第二句正常。"] == "ready"


def test_skip_only_one_line(client: TestClient, project: dict, episode: dict) -> None:
    _seed(episode["id"], ["第一句。", "第二句。"])
    with session_scope() as session:
        # 给同一镜塞两句台词（逐行跳过的真实场景）
        shot = session.query(Shot).filter(Shot.episode_id == episode["id"]).first()
        shot.shot_json = {
            **shot.shot_json,
            "dialogue": [
                {"character": "小满", "text": "第一句。", "emotion": "平静"},
                {"character": "小满", "text": "第二句。", "emotion": "平静"},
            ],
        }
        session.flush()
        shot_no = shot.shot_no

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/tts/skip",
        json={"shot_nos": [shot_no], "reason": "只跳这一句", "line_index": 1},
    )
    assert resp.status_code == 200, resp.text
    body = client.get(f"/api/v1/episodes/{episode['id']}/tts/lines").json()
    statuses = {(item["text"], item["status"]) for item in body["items"]}
    assert ("第二句。", "skipped") in statuses
    assert ("第一句。", "missing") in statuses, "同一镜的其它台词不该被连带跳过"


def test_estimate_filters_by_character(client: TestClient, project: dict, episode: dict) -> None:
    _seed(episode["id"], ["只有小满的台词。"])
    _bind_default_voice(client, episode, "小满")
    project_id = episode["project_id"]
    other = client.post(f"/api/v1/projects/{project_id}/characters", json={"name": "路人"}).json()

    mine = client.get(
        f"/api/v1/episodes/{episode['id']}/tts/estimate",
        params={"character_id": other["id"]},
    )
    assert mine.status_code == 200
    assert mine.json()["line_count"] == 0, "按角色筛选必须真的筛掉别人的台词"

    characters = client.get(
        f"/api/v1/projects/{project_id}/characters", params={"episode_id": episode["id"]}
    ).json()["items"]
    xiaoman = next(item for item in characters if item["name"] == "小满")
    hit = client.get(
        f"/api/v1/episodes/{episode['id']}/tts/estimate",
        params={"character_id": xiaoman["id"]},
    )
    assert hit.json()["line_count"] == 1


def test_generate_rejected_when_no_dialogue(client: TestClient, project: dict,
                                            episode: dict) -> None:
    with session_scope() as session:
        session.add(Shot(
            episode_id=episode["id"], shot_no=1, duration_ms=4000,
            shot_json={"action": "静默镜头", "image_prompt": "空镜", "dialogue": []},
            stage="CONFIRMED",
        ))
        session.flush()
    resp = client.post(f"/api/v1/episodes/{episode['id']}/tts/generate", json={})
    assert resp.status_code == 409, resp.text


def test_manual_audio_is_stacked_after_previous_line(
    client: TestClient, project: dict, episode: dict
) -> None:
    """给第 2 句上传音频时，它必须排在第 1 句之后，而不是两句叠在 0ms 一起播。"""
    _bind_default_voice(client, episode)
    with session_scope() as session:
        shot = Shot(
            episode_id=episode["id"], shot_no=1, duration_ms=6000,
            shot_json={
                "action": "对话",
                "image_prompt": "画面",
                "dialogue": [
                    {"character": "小满", "text": "第一句台词。", "emotion": "平静"},
                    {"character": "小满", "text": "第二句台词。", "emotion": "平静"},
                ],
            },
            stage="CONFIRMED",
        )
        session.add(shot)
        session.flush()
        shot_id = shot.id

    _synthesize(episode["id"], [shot_id])
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        shot = session.get(Shot, shot_id)
        tracks = audio_agent._shot_dialogue_tracks(session, shot)
        assert len(tracks) == 2
        first_media = tracks[0].media_id
        audio_agent.attach_manual_audio(
            session, episode=ep, shot=shot, line_index=1, media_id=first_media
        )
        session.flush()
        tracks = audio_agent._shot_dialogue_tracks(session, shot)
        assert tracks[0].end_ms <= tracks[1].start_ms, "第二句不能和第一句重叠"


def test_regenerate_subtitles_keeps_manual_edits(
    client: TestClient, project: dict, episode: dict
) -> None:
    """FR-053：人工改过的字幕，重新生成时不能被冲掉或生成重复条目。"""
    from app.agents import edit as edit_agent
    from app.db.models import SubtitleCue

    with session_scope() as session:
        session.add(Shot(
            episode_id=episode["id"], shot_no=1, duration_ms=4000,
            shot_json={
                "action": "独白",
                "image_prompt": "画面",
                "dialogue": [
                    {"character": "小满", "text": "原始第一句。", "emotion": "平静"},
                    {"character": "小满", "text": "原始第二句。", "emotion": "平静"},
                ],
            },
            stage="CONFIRMED",
        ))
        session.flush()

    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        cues = edit_agent.generate_subtitles(session, ep)
        session.flush()
        assert cues, "没有音频时应退回脚本生成字幕"
        target = cues[0]
        target.edited = True
        target.text = "人工修正后的字幕"
        target.start_ms = 1234
        target.end_ms = 2345
        session.flush()
        shot_id = target.shot_id

    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        edit_agent.generate_subtitles(session, ep)
        session.flush()

    with session_scope() as session:
        rows = list(
            session.scalars(
                select(SubtitleCue)
                .where(SubtitleCue.episode_id == episode["id"])
                .order_by(SubtitleCue.start_ms)
            )
        )
        kept = [row for row in rows if row.text == "人工修正后的字幕"]
        assert len(kept) == 1, "人工字幕必须保留且不能重复"
        assert kept[0].start_ms == 1234, "人工调整的时间不能被覆盖"
        # 该镜整体保持原样：人工版本 + 同镜另一句，不会再多出一份自动生成的
        assert not any(row.text == "原始第一句。" for row in rows), "被人工版本取代的那条不该复活"
        assert len([row for row in rows if row.shot_id == shot_id]) == 2
        # 其余镜头照常重建，序号按时间连续
        assert [row.index_no for row in rows] == list(range(1, len(rows) + 1))


def test_voice_speed_actually_changes_audio_duration(
    client: TestClient, project: dict, episode: dict
) -> None:
    """FR-051：语速不能只是个字段——供应商不认 speed，必须在本地真正把音频变速。"""
    from app.db.models import MediaAsset

    _bind_default_voice(client, episode)
    text = "这是一句用来测语速的台词内容。"          # 15 字
    shot_ids = _seed(episode["id"], [text])

    characters = client.get(
        f"/api/v1/projects/{episode['project_id']}/characters",
        params={"episode_id": episode["id"]},
    ).json()["items"]
    xiaoman = next(item for item in characters if item["name"] == "小满")
    voices = client.get(f"/api/v1/projects/{episode['project_id']}/voices").json()
    voices = voices["items"] if isinstance(voices, dict) else voices
    resp = client.post(
        f"/api/v1/characters/{xiaoman['id']}/voice",
        json={"voice_id": voices[0]["id"], "speed": 1.5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["voice_speed"] == 1.5

    _synthesize(episode["id"], shot_ids)
    track = _tracks(shot_ids[0])[0]
    assert track.speed == 1.5

    # Mock 供应商原速时长 ≈ 字数 / 5 秒；1.5 倍速后应约为原来的 2/3
    base_ms = len(text) / 5.0 * 1000
    actual = track.end_ms - track.start_ms
    assert abs(actual - base_ms / 1.5) <= base_ms * 0.12, (
        f"语速没有生效：期望约 {base_ms / 1.5:.0f}ms，实际 {actual}ms"
    )

    with session_scope() as session:
        asset = session.get(MediaAsset, track.media_id)
        assert asset and abs(int(asset.duration_ms) - actual) <= 200


def test_retime_existing_audio_without_regenerating(
    client: TestClient, project: dict, episode: dict
) -> None:
    """已有音频可以直接调速：不调用模型、不计费，且始终基于原始音频计算。"""
    from app.db.models import MediaAsset

    _bind_default_voice(client, episode)
    text = "这是一句用来测已有音频调速的台词。"       # 17 字 → 原速约 3400ms
    shot_ids = _seed(episode["id"], [text])
    _synthesize(episode["id"], shot_ids)
    track = _tracks(shot_ids[0])[0]
    original_media = track.media_id
    base_ms = track.end_ms - track.start_ms

    def retime(speed: float) -> dict:
        return client.post(
            f"/api/v1/episodes/{episode['id']}/tts/{track.id}/speed",
            json={"speed": speed},
        ).json()

    fast = retime(1.5)
    assert fast["speed"] == 1.5
    assert fast["media_id"] != original_media
    assert abs((fast["end_ms"] - fast["start_ms"]) - base_ms / 1.5) <= base_ms * 0.1

    # 第二次调整仍然从原始音频出发（1.5 → 0.8 不是叠加在 1.5 上）
    slow = retime(0.8)
    assert slow["speed"] == 0.8
    assert abs((slow["end_ms"] - slow["start_ms"]) - base_ms / 0.8) <= base_ms * 0.1

    # 调回原速：直接复用原始素材，不再新造文件
    back = retime(1.0)
    assert back["media_id"] == original_media

    with session_scope() as session:
        old = session.get(MediaAsset, original_media)
        assert old is not None
        assert old.metadata_json.get("retimed") is not True


def test_project_unified_speed_seeds_characters(
    client: TestClient, project: dict, episode: dict
) -> None:
    """统一语速：新角色继承它；「应用到全部角色」把现有角色一起改掉并标记配音过期。"""
    pid = episode["project_id"]
    resp = client.patch(f"/api/v1/projects/{pid}", json={"default_voice_speed": 1.2})
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_voice_speed"] == 1.2

    created = client.post(f"/api/v1/projects/{pid}/characters", json={"name": "新角色甲"}).json()
    assert created["voice_speed"] == 1.2, "新角色应继承项目统一语速"

    applied = client.post(f"/api/v1/projects/{pid}/voice-speed", json={"speed": 0.9})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["default_voice_speed"] == 0.9
    assert body["count"] >= 1

    characters = client.get(f"/api/v1/projects/{pid}/characters").json()["items"]
    assert characters and all(abs(float(c["voice_speed"]) - 0.9) < 1e-6 for c in characters)


def test_apply_project_speed_retimes_existing_audio(
    client: TestClient, project: dict, episode: dict
) -> None:
    """统一语速不只改设定：已有音频就地变速（0 费用），且不留下「已过期」。"""
    _bind_default_voice(client, episode)
    shot_ids = _seed(episode["id"], ["这一句用来验证统一语速会直接重调已有音频。"])
    _synthesize(episode["id"], shot_ids)
    track = _tracks(shot_ids[0])[0]
    base_ms = track.end_ms - track.start_ms

    res = client.post(
        f"/api/v1/projects/{episode['project_id']}/voice-speed", json={"speed": 1.4}
    ).json()
    assert res["default_voice_speed"] == 1.4
    assert res["retimed"] >= 1

    updated = _tracks(shot_ids[0])[0]
    assert abs(updated.speed - 1.4) < 1e-6
    assert updated.stale is False
    assert abs((updated.end_ms - updated.start_ms) - base_ms / 1.4) <= base_ms * 0.12
