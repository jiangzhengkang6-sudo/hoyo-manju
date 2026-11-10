"""音色配方指纹：同一角色不该因为「代码/参数悄悄变了」而出现两种声音。

背景：供应商不认 `voice` 参数，音色完全由指令前缀决定。之前的过期判定只看
`voice_id` + `speed` 有没有变，于是出现两种漏判：

- 指令前缀改了（老音频是加前缀之前生成的）→ 参数没变，旧音频不会被标脏；
- 换供应商 / 换模型，音色标识含义变了 → 同样不会被标脏。

表现就是用户在试听时听到同一角色两种音色，而系统坚持说「都是好的」。
现在把「供应商 + 音色标识 + 语速 + 指令前缀」算成指纹存进素材，读的时候比对。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.agents import audio as audio_agent
from app.db.models import AudioTrack, MediaAsset
from app.db.session import session_scope

CHARACTER = "林晚"


def _seed_script_with_dialogue(client: TestClient, episode: dict) -> None:
    shots = [
        {
            "shot_no": i,
            "duration_ms": 3000,
            "scene_no": 1,
            "characters": [CHARACTER],
            "action": f"第 {i} 个动作",
            "camera_movement": "固定",
            "composition": "中景",
            "emotion": "平静",
            "dialogue": [
                {"character": CHARACTER, "text": f"第 {i} 句台词", "emotion": "平静"}
            ],
            "narration": "",
            "sfx": [],
            "image_prompt": f"雨夜街道第 {i} 帧",
            "negative_prompt": "",
        }
        for i in range(1, 3)
    ]
    body = {
        "story_bible": {"logline": "雨夜相遇", "characters": [{"name": CHARACTER}]},
        "beat_sheet": [],
        "shots": shots,
    }
    resp = client.put(f"/api/v1/episodes/{episode['id']}/script", json={"story_json": body})
    assert resp.status_code == 200, resp.text


def _prepare(client: TestClient, project: dict, episode: dict, worker) -> dict:
    """建角色 → 绑音色 → 生成两条台语音轨，返回 TTS 行。"""
    character = client.post(f"/api/v1/projects/{project['id']}/characters", json={"name": CHARACTER}).json()
    _seed_script_with_dialogue(client, episode)
    voices = client.get(f"/api/v1/projects/{project['id']}/voices").json()
    voice_items = voices["items"] if isinstance(voices, dict) else voices
    resp = client.post(
        f"/api/v1/characters/{character['id']}/voice",
        json={"voice_id": voice_items[0]["id"]},
    )
    assert resp.status_code == 200, resp.text

    job = client.post(f"/api/v1/episodes/{episode['id']}/tts/generate", json={}).json()
    from conftest import drain, wait_job

    assert wait_job(client, worker, job["id"])["status"] == "SUCCEEDED"
    drain(worker, timeout=60)
    return character


def _lines(client: TestClient, episode: dict) -> list[dict]:
    """逐句状态表（`/tts` 是音轨列表，不带配方比对，别拿错接口）。"""
    resp = client.get(f"/api/v1/episodes/{episode['id']}/tts/lines")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


# --------------------------------------------------------------------------- #
# 指纹本身
# --------------------------------------------------------------------------- #


def test_fingerprint_changes_with_any_part_of_the_recipe() -> None:
    """四个输入任意一个变了，指纹都要变——这是「认得出旧声音」的前提。"""
    voice = type("V", (), {"provider_voice_id": "Ethan"})()
    base = audio_agent.voice_fingerprint("qwen-tts", voice, 1.0, "用低沉男声朗读：")

    assert base == audio_agent.voice_fingerprint("qwen-tts", voice, 1.0, "用低沉男声朗读：")
    assert base != audio_agent.voice_fingerprint("minimax", voice, 1.0, "用低沉男声朗读：")
    assert base != audio_agent.voice_fingerprint("qwen-tts", voice, 0.9, "用低沉男声朗读：")
    assert base != audio_agent.voice_fingerprint("qwen-tts", voice, 1.0, "")
    other = type("V", (), {"provider_voice_id": "Cherry"})()
    assert base != audio_agent.voice_fingerprint("qwen-tts", other, 1.0, "用低沉男声朗读：")


# --------------------------------------------------------------------------- #
# 生成时留档
# --------------------------------------------------------------------------- #


def test_generated_dialogue_records_voice_fingerprint(
    client: TestClient, project: dict, episode: dict, worker
) -> None:
    """新生成的音频必须带上配方指纹，否则以后永远判不出来。"""
    _prepare(client, project, episode, worker)

    with session_scope() as session:
        tracks = list(
            session.query(AudioTrack).filter(AudioTrack.episode_id == episode["id"])
        )
        assert len(tracks) == 2, tracks
        for track in tracks:
            asset = session.get(MediaAsset, track.media_id)
            meta = asset.metadata_json or {}
            assert meta.get("voice_fingerprint"), meta
            assert len(meta["voice_fingerprint"]) == 16
            assert meta.get("style_prompt") is not None


# --------------------------------------------------------------------------- #
# 读时比对
# --------------------------------------------------------------------------- #


def test_changed_recipe_marks_line_as_drift(
    client: TestClient, project: dict, episode: dict, worker
) -> None:
    """配方变了、音频没重做 → 这一行会被认出来（不是 silently ready）。

    直接改素材上的指纹来模拟「代码升级换了指令前缀」：音轨本身没有 stale
    标记，只有比对才能发现它已经是旧声音。
    """
    _prepare(client, project, episode, worker)

    with session_scope() as session:
        track = session.query(AudioTrack).filter(
            AudioTrack.episode_id == episode["id"], AudioTrack.kind == "dialogue"
        ).first()
        asset = session.get(MediaAsset, track.media_id)
        meta = dict(asset.metadata_json or {})
        meta["voice_fingerprint"] = "deadbeefdeadbeef"  # 假装是另一套配方
        asset.metadata_json = meta
        # 关键：音轨本身不标 stale，只能靠指纹比对发现
        track.stale = False
        track.stale_reason = ""
        session.flush()

    lines = _lines(client, episode)
    drifted = [line for line in lines if line["voice_drift"]]
    assert len(drifted) == 1, lines
    assert drifted[0]["status"] == "stale"
    assert "音色参数已更新" in drifted[0]["stale_reason"]

    untouched = [line for line in lines if not line["voice_drift"]]
    assert untouched and untouched[0]["status"] == "ready"


def test_legacy_audio_without_fingerprint_is_flagged_not_broken(
    client: TestClient, project: dict, episode: dict, worker
) -> None:
    """指纹上线前的历史音频：标成「旧版本」提示，但不直接判过期。

    直接判过期会让升级后所有老项目一片飘红，用户分不清「真坏了」和「没记录」。
    """
    _prepare(client, project, episode, worker)

    with session_scope() as session:
        for track in session.query(AudioTrack).filter(AudioTrack.episode_id == episode["id"]):
            asset = session.get(MediaAsset, track.media_id)
            meta = dict(asset.metadata_json or {})
            meta.pop("voice_fingerprint", None)
            asset.metadata_json = meta
            track.stale = False
            session.flush()

    lines = _lines(client, episode)
    assert all(line["voice_legacy"] for line in lines)
    assert all(not line["voice_drift"] for line in lines)
    assert all(line["status"] == "ready" for line in lines), "没指纹不等于过期"
