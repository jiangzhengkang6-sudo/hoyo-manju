"""FR-050 音色强制绑定 / FR-051 语速留档。

PRD 原文：
- FR-050「每个有台词的角色**必须**绑定一个音色；绑定前可试听，台词修改后关联音频标记过期。」
- FR-051「系统必须支持按镜头或按整集批量生成对白，保留原文、音色、**语速**、情绪、供应商和音频文件。」

本文件盯的是**不满足前置条件时不得静默放过**：以前的实现遇到没绑音色的角色会把那几句台词
悄悄跳过（只在任务结果里留个 `skipped`），成片就缺了这段声音，用户很难发现。
现在必须在**提交前**整体拒绝（409、不留任务、不占配额）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import drain, wait_job

CHARACTER = "林晚"


def _seed_script_with_dialogue(client: TestClient, episode: dict, character: str = CHARACTER) -> None:
    """写一份带台词的分镜脚本（台词挂在指定角色名下）。"""
    shots = [
        {
            "shot_no": i,
            "duration_ms": 3000,
            "scene_no": 1,
            "characters": [character],
            "action": f"第 {i} 个动作",
            "camera_movement": "固定",
            "composition": "中景",
            "emotion": "平静",
            "dialogue": [{"character": character, "text": f"第 {i} 句台词", "emotion": "平静"}],
            "narration": "",
            "sfx": [],
            "image_prompt": f"雨夜街道第 {i} 帧",
            "negative_prompt": "",
        }
        for i in range(1, 4)
    ]
    body = {
        "story_bible": {"logline": "雨夜相遇", "characters": [{"name": character}]},
        "beat_sheet": [],
        "shots": shots,
    }
    resp = client.put(f"/api/v1/episodes/{episode['id']}/script", json={"story_json": body})
    assert resp.status_code == 200, resp.text


def _make_character(client: TestClient, project: dict, name: str = CHARACTER) -> dict:
    resp = client.post(f"/api/v1/projects/{project['id']}/characters", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _voices(client: TestClient, project: dict) -> list[dict]:
    resp = client.get(f"/api/v1/projects/{project['id']}/voices")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["items"] if isinstance(body, dict) else body


def _estimate(client: TestClient, episode: dict) -> dict:
    resp = client.get(f"/api/v1/episodes/{episode['id']}/tts/estimate")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _tts_jobs(client: TestClient, episode: dict) -> list[dict]:
    resp = client.get("/api/v1/jobs", params={"episode_id": episode["id"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"] if isinstance(body, dict) else body
    return [j for j in items if j.get("type") == "TTS_GENERATION"]


# --------------------------------------------------------------------------- #
# FR-050 前置条件：有台词但未绑音色必须被拦
# --------------------------------------------------------------------------- #

def test_estimate_flags_missing_voice(client: TestClient, episode: dict, project: dict) -> None:
    """未绑音色时预估就应给出 `can_generate=false` 与可定位的阻断项。"""
    _make_character(client, project)
    _seed_script_with_dialogue(client, episode)

    quote = _estimate(client, episode)

    assert quote["can_generate"] is False, quote
    assert quote["has_dialogue"] is True
    problems = quote["blocking_problems"]
    assert len(problems) == 1, problems
    assert problems[0]["code"] == "MISSING_VOICE"
    assert problems[0]["character"] == CHARACTER
    # 要能指出是哪几镜，用户才知道去哪儿改
    assert problems[0]["shot_nos"] == [1, 2, 3], problems[0]


def test_tts_generate_refused_without_voice(
    client: TestClient, episode: dict, project: dict
) -> None:
    """FR-050：不满足前置条件时整体拒绝——不留任务、不占配额。"""
    _make_character(client, project)
    _seed_script_with_dialogue(client, episode)
    before = _tts_jobs(client, episode)

    resp = client.post(f"/api/v1/episodes/{episode['id']}/tts/generate", json={})

    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == "MISSING_VOICE", body
    assert body["retryable"] is False
    assert CHARACTER in body["message"]
    assert body["trace_id"]
    assert body["details"]["blocking_problems"][0]["character"] == CHARACTER
    # 关键：没有留下半个任务
    assert _tts_jobs(client, episode) == before


def test_estimate_can_generate_after_binding(
    client: TestClient, episode: dict, project: dict
) -> None:
    """绑好音色后前置条件解除，且配额口径只算真正会合成的句子。"""
    character = _make_character(client, project)
    _seed_script_with_dialogue(client, episode)

    voices = _voices(client, project)
    assert voices, "项目应自带预设音色"
    resp = client.post(
        f"/api/v1/characters/{character['id']}/voice",
        json={"voice_id": voices[0]["id"]},
    )
    assert resp.status_code == 200, resp.text

    quote = _estimate(client, episode)
    assert quote["can_generate"] is True, quote
    assert quote["blocking_problems"] == []
    assert quote["line_count"] == 3, quote
    assert quote["skipped_count"] == 0, quote


def test_binding_then_generating_produces_tracks(
    client: TestClient, episode: dict, project: dict, worker
) -> None:
    """绑定后 TTS 能真正产出音轨（前置条件不是把功能锁死）。"""
    character = _make_character(client, project)
    _seed_script_with_dialogue(client, episode)
    voices = _voices(client, project)
    client.post(
        f"/api/v1/characters/{character['id']}/voice",
        json={"voice_id": voices[0]["id"]},
    )

    resp = client.post(f"/api/v1/episodes/{episode['id']}/tts/generate", json={})
    assert resp.status_code == 200, resp.text
    job = wait_job(client, worker, resp.json()["id"])
    assert job["status"] == "SUCCEEDED", job
    drain(worker, timeout=60)

    tracks = client.get(f"/api/v1/episodes/{episode['id']}/tts").json()["items"]
    assert len(tracks) == 3, tracks
    assert all(t["text"] for t in tracks)


# --------------------------------------------------------------------------- #
# FR-051 语速
# --------------------------------------------------------------------------- #

def test_voice_speed_is_persisted_and_reported(
    client: TestClient, episode: dict, project: dict
) -> None:
    """语速必须能设置并读回（否则「保留语速」无从谈起）。"""
    character = _make_character(client, project)
    voices = _voices(client, project)

    resp = client.post(
        f"/api/v1/characters/{character['id']}/voice",
        json={"voice_id": voices[0]["id"], "speed": 1.5},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["voice_speed"] == 1.5

    got = client.get(f"/api/v1/characters/{character['id']}").json()
    assert got["voice_speed"] == 1.5, got


def test_new_character_defaults_to_normal_speed(
    client: TestClient, episode: dict, project: dict
) -> None:
    """默认原速 1.0，不是 0 也不是 None（0 会被读成静音）。"""
    character = _make_character(client, project)
    got = client.get(f"/api/v1/characters/{character['id']}").json()
    assert got["voice_speed"] == 1.0, got


def test_voice_speed_out_of_range_rejected(
    client: TestClient, episode: dict, project: dict
) -> None:
    """越界语速在 schema 层就被拒（422），不会写进库。"""
    character = _make_character(client, project)
    voices = _voices(client, project)
    voice_id = voices[0]["id"]

    for bad in (0.1, 5.0, 0, -1):
        resp = client.post(
            f"/api/v1/characters/{character['id']}/voice",
            json={"voice_id": voice_id, "speed": bad},
        )
        assert resp.status_code == 422, f"speed={bad} 应被拒：{resp.text}"

    got = client.get(f"/api/v1/characters/{character['id']}").json()
    assert got["voice_speed"] == 1.0, got


def test_tts_tracks_retain_speed_emotion_provider(
    client: TestClient, episode: dict, project: dict, worker
) -> None:
    """FR-051：音轨必须留下语速/情绪/供应商，导出时可直接核对。"""
    character = _make_character(client, project)
    _seed_script_with_dialogue(client, episode)
    voices = _voices(client, project)
    client.post(
        f"/api/v1/characters/{character['id']}/voice",
        json={"voice_id": voices[0]["id"], "speed": 1.25},
    )

    resp = client.post(f"/api/v1/episodes/{episode['id']}/tts/generate", json={})
    job = wait_job(client, worker, resp.json()["id"])
    assert job["status"] == "SUCCEEDED", job
    drain(worker, timeout=60)

    tracks = client.get(f"/api/v1/episodes/{episode['id']}/tts").json()["items"]
    assert tracks, "应产出音轨"
    for track in tracks:
        assert track["speed"] == 1.25, track
        assert track["emotion"] == "平静", track
        assert track["provider"], track
        assert track["voice_id"] == voices[0]["id"], track


def test_changing_speed_marks_existing_audio_stale(
    client: TestClient, episode: dict, project: dict, worker
) -> None:
    """改语速后旧配音必须标记过期（FR-050「关联音频标记过期」的同理要求）。"""
    character = _make_character(client, project)
    _seed_script_with_dialogue(client, episode)
    voices = _voices(client, project)
    client.post(
        f"/api/v1/characters/{character['id']}/voice",
        json={"voice_id": voices[0]["id"]},
    )
    resp = client.post(f"/api/v1/episodes/{episode['id']}/tts/generate", json={})
    job = wait_job(client, worker, resp.json()["id"])
    assert job["status"] == "SUCCEEDED", job
    drain(worker, timeout=60)

    before = client.get(f"/api/v1/episodes/{episode['id']}/tts").json()
    assert before["stale_count"] == 0, before

    resp = client.patch(
        f"/api/v1/characters/{character['id']}", json={"voice_speed": 0.8}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["voice_speed"] == 0.8

    after = client.get(f"/api/v1/episodes/{episode['id']}/tts").json()
    assert after["stale_count"] == len(after["items"]) > 0, after


def test_tts_estimate_accepts_shot_filter(
    client: TestClient, episode: dict, project: dict
) -> None:
    """按镜头批量生成时，预估要只算被选中的镜头（否则配额口径偏大）。"""
    character = _make_character(client, project)
    _seed_script_with_dialogue(client, episode)
    voices = _voices(client, project)
    client.post(
        f"/api/v1/characters/{character['id']}/voice",
        json={"voice_id": voices[0]["id"]},
    )

    resp = client.get(
        f"/api/v1/episodes/{episode['id']}/tts/estimate", params={"shot_nos": "1"}
    )
    assert resp.status_code == 200, resp.text
    quote = resp.json()
    assert quote["shot_count"] == 1, quote
    assert quote["line_count"] == 1, quote
