"""§12-4 TTS 失败降级（上传音频 / 跳过配音）与 §12-8 内容拦截申诉。

PRD §12 原文：
- 「TTS 失败 | 允许用户上传音频或跳过配音后继续预览」
- 「输出内容被拦截 | 禁止确认和导出，提供修改输入或申诉入口」

本文件盯两件事：
1. 降级入口必须**真的能继续预览**，而不是只写个标记；且必须与「还没生成」区分开。
2. 拦截必须**真的挡住导出**（此前 `can_export` 只算不用），并且申诉是人工通道 ——
   只有人工 APPROVED 才放行，否则申诉入口等于摆设。
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from conftest import drain, wait_job

CHARACTER = "林晚"

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


# --------------------------------------------------------------------------- #
# 夹具辅助
# --------------------------------------------------------------------------- #

def _seed_script(client: TestClient, episode: dict, *, shots: int = 3) -> None:
    body = {
        "story_bible": {"logline": "雨夜相遇", "characters": [{"name": CHARACTER}]},
        "beat_sheet": [],
        "shots": [
            {
                "shot_no": i,
                "duration_ms": 3000,
                "scene_no": 1,
                "characters": [CHARACTER],
                "action": f"第 {i} 个动作",
                "camera_movement": "固定",
                "composition": "中景",
                "emotion": "平静",
                "dialogue": [{"character": CHARACTER, "text": f"第 {i} 句台词", "emotion": "平静"}],
                "narration": "",
                "sfx": [],
                "image_prompt": f"雨夜街道第 {i} 帧",
                "negative_prompt": "",
            }
            for i in range(1, shots + 1)
        ],
    }
    resp = client.put(f"/api/v1/episodes/{episode['id']}/script", json={"story_json": body})
    assert resp.status_code == 200, resp.text


def _bind_voice(client: TestClient, episode: dict) -> str:
    project_id = episode["project_id"]
    resp = client.post(f"/api/v1/projects/{project_id}/characters", json={"name": CHARACTER})
    assert resp.status_code == 201, resp.text
    character = resp.json()
    voices = client.get(f"/api/v1/projects/{project_id}/voices").json()
    voices = voices["items"] if isinstance(voices, dict) else voices
    assert voices, "项目应自带预设音色"
    resp = client.post(
        f"/api/v1/characters/{character['id']}/voice", json={"voice_id": voices[0]["id"]}
    )
    assert resp.status_code == 200, resp.text
    return character["id"]


def _upload(client: TestClient, episode: dict, *, kind: str = "audio", name: str = "line.mp3") -> str:
    if kind == "audio":
        payload, mime = b"ID3\x03\x00\x00\x00fake-audio", "audio/mpeg"
    else:
        payload, mime = _PNG, "image/png"
    resp = client.post(
        "/api/v1/media/upload",
        files={"file": (name, io.BytesIO(payload), mime)},
        data={"kind": kind, "rights_declared": "true", "episode_id": episode["id"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _lines(client: TestClient, episode: dict) -> dict:
    resp = client.get(f"/api/v1/episodes/{episode['id']}/tts/lines")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _block_asset(media_id: str) -> None:
    """直接把素材标成 BLOCKED（模拟供应商输出审核拦截）。"""
    from app.db.models import MediaAsset
    from app.db.session import session_scope

    with session_scope() as session:
        asset = session.get(MediaAsset, media_id)
        assert asset is not None
        asset.safety_status = "BLOCKED"
        asset.safety_detail = {"categories": [{"category": "violence", "terms": ["血腥"]}]}


# --------------------------------------------------------------------------- #
# §12-4 逐句状态表
# --------------------------------------------------------------------------- #

def test_lines_report_missing_then_ready(
    client: TestClient, episode: dict, worker
) -> None:
    """缺音频的句子必须被标成 missing —— 这是降级入口出现的唯一依据。"""
    _seed_script(client, episode)
    _bind_voice(client, episode)

    before = _lines(client, episode)
    assert before["counts"]["total"] == 3, before["counts"]
    assert before["counts"]["missing"] == 3, before["counts"]
    assert all(item["status"] == "missing" for item in before["items"])

    resp = client.post(f"/api/v1/episodes/{episode['id']}/tts/generate", json={})
    job = wait_job(client, worker, resp.json()["id"])
    assert job["status"] == "SUCCEEDED", job
    drain(worker, timeout=60)

    after = _lines(client, episode)
    assert after["counts"]["ready"] == 3, after["counts"]
    assert after["counts"]["missing"] == 0, after["counts"]
    assert all(item["source"] == "tts" for item in after["items"])


# --------------------------------------------------------------------------- #
# §12-4 上传音频兜底
# --------------------------------------------------------------------------- #

def test_manual_audio_replaces_missing_line(client: TestClient, episode: dict) -> None:
    """TTS 失败时用户上传的音频要真的顶上去，并标记来源为 manual。"""
    _seed_script(client, episode)
    _bind_voice(client, episode)
    media_id = _upload(client, episode)

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/tts/manual",
        json={"shot_no": 1, "line_index": 0, "media_id": media_id},
    )
    assert resp.status_code == 201, resp.text
    track = resp.json()
    assert track["source"] == "manual", track
    assert track["media_id"] == media_id, track
    assert track["provider"] == "manual", track
    # 用户上传的音频语速就是它本身，不该沿用角色语速
    assert track["speed"] == 1.0, track
    assert track["stale"] is False, track

    lines = _lines(client, episode)
    first = [i for i in lines["items"] if i["shot_no"] == 1][0]
    assert first["status"] == "ready", first
    assert first["source"] == "manual", first
    # 其余两句仍然缺，不该被顺带标记成完成
    assert lines["counts"]["missing"] == 2, lines["counts"]
    assert lines["counts"]["manual"] == 1, lines["counts"]


def test_manual_audio_rejects_non_audio(client: TestClient, episode: dict) -> None:
    _seed_script(client, episode)
    _bind_voice(client, episode)
    image_id = _upload(client, episode, kind="image", name="frame.png")

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/tts/manual",
        json={"shot_no": 1, "line_index": 0, "media_id": image_id},
    )
    assert resp.status_code == 409, resp.text


def test_manual_audio_rejects_unknown_shot(client: TestClient, episode: dict) -> None:
    _seed_script(client, episode)
    _bind_voice(client, episode)
    media_id = _upload(client, episode)

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/tts/manual",
        json={"shot_no": 99, "line_index": 0, "media_id": media_id},
    )
    assert resp.status_code == 404, resp.text


def test_manual_audio_does_not_duplicate_tracks(client: TestClient, episode: dict) -> None:
    """重复上传同一句不能留下两条音轨 —— 否则时间线会重复播放同一句。"""
    _seed_script(client, episode)
    _bind_voice(client, episode)
    first = _upload(client, episode, name="a.mp3")
    second = _upload(client, episode, name="b.mp3")

    for media_id in (first, second):
        resp = client.post(
            f"/api/v1/episodes/{episode['id']}/tts/manual",
            json={"shot_no": 2, "line_index": 0, "media_id": media_id},
        )
        assert resp.status_code == 201, resp.text

    tracks = client.get(f"/api/v1/episodes/{episode['id']}/tts").json()["items"]
    shot2 = [t for t in tracks if t["shot_id"]]
    assert len([t for t in tracks if t["media_id"] == second]) == 1, tracks
    assert not [t for t in tracks if t["media_id"] == first], "旧音频应被替换而不是并存"
    assert len(shot2) == 1, tracks


# --------------------------------------------------------------------------- #
# §12-4 跳过配音
# --------------------------------------------------------------------------- #

def test_skip_dialogue_is_recorded_and_distinct_from_missing(
    client: TestClient, episode: dict
) -> None:
    """跳过必须落成 skipped（有意为之），不能与 missing（还没生成）混为一谈。"""
    _seed_script(client, episode)
    _bind_voice(client, episode)

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/tts/skip",
        json={"shot_nos": [1, 2], "reason": "供应商持续失败，先出片"},
    )
    assert resp.status_code == 200, resp.text
    counts = resp.json()["counts"]
    assert counts["skipped"] == 2, counts
    assert counts["missing"] == 1, counts

    lines = _lines(client, episode)
    statuses = {i["shot_no"]: i["status"] for i in lines["items"]}
    assert statuses == {1: "skipped", 2: "skipped", 3: "missing"}, statuses

    tracks = client.get(f"/api/v1/episodes/{episode['id']}/tts").json()
    assert tracks["skipped_count"] == 2, tracks
    skipped = [t for t in tracks["items"] if t["source"] == "skipped"]
    assert all(t["media_id"] is None for t in skipped), skipped
    assert all(t["stale"] is False for t in skipped), skipped


def test_skip_then_manual_audio_switches_source(client: TestClient, episode: dict) -> None:
    """先跳过又补了音频，状态要跟着回到 ready/manual。"""
    _seed_script(client, episode)
    _bind_voice(client, episode)
    client.post(f"/api/v1/episodes/{episode['id']}/tts/skip", json={"shot_nos": [1]})
    media_id = _upload(client, episode)

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/tts/manual",
        json={"shot_no": 1, "line_index": 0, "media_id": media_id},
    )
    assert resp.status_code == 201, resp.text
    lines = _lines(client, episode)
    first = [i for i in lines["items"] if i["shot_no"] == 1][0]
    assert first["status"] == "ready", first
    assert first["source"] == "manual", first


# --------------------------------------------------------------------------- #
# §12-8 导出闸门
# --------------------------------------------------------------------------- #

def test_export_gate_allows_clean_episode(client: TestClient, episode: dict) -> None:
    resp = client.get(f"/api/v1/episodes/{episode['id']}/export-gate")
    assert resp.status_code == 200, resp.text
    gate = resp.json()
    assert gate["can_export"] is True, gate
    assert gate["blocked"] == [], gate


def test_blocked_asset_blocks_export_and_package(client: TestClient, episode: dict) -> None:
    """§12-8：被拦截时必须真的禁止导出（此前 can_export 只算不用）。"""
    media_id = _upload(client, episode, kind="image", name="blocked.png")
    _block_asset(media_id)

    gate = client.get(f"/api/v1/episodes/{episode['id']}/export-gate").json()
    assert gate["can_export"] is False, gate
    assert media_id in gate["blocked"], gate
    assert media_id in gate["unresolved"], gate

    for path, body in (
        (f"/api/v1/episodes/{episode['id']}/render/export", {"preset": "720p"}),
        (f"/api/v1/episodes/{episode['id']}/render/preview", {"watermark": True}),
    ):
        resp = client.post(path, json=body)
        assert resp.status_code == 422, f"{path}: {resp.text}"
        err = resp.json()["error"]
        assert err["code"] == "SAFETY_BLOCKED", err
        assert media_id in err["details"]["unresolved"], err
        assert err["trace_id"], err

    resp = client.post(f"/api/v1/episodes/{episode['id']}/export/package")
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "SAFETY_BLOCKED"


# --------------------------------------------------------------------------- #
# §12-8 申诉
# --------------------------------------------------------------------------- #

def test_appeal_requires_a_blocked_asset(client: TestClient, episode: dict) -> None:
    clean_id = _upload(client, episode, kind="image", name="ok.png")
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/appeals",
        json={"target_type": "asset", "target_id": clean_id, "reason": "我觉得没问题"},
    )
    assert resp.status_code == 409, resp.text

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/appeals",
        json={"target_type": "asset", "target_id": "ast_nope", "reason": "随便"},
    )
    assert resp.status_code == 404, resp.text

    # 空理由在 schema 层就被拒（人工复核无从判断）
    blocked_id = _upload(client, episode, kind="image", name="blocked.png")
    _block_asset(blocked_id)
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/appeals",
        json={"target_type": "asset", "target_id": blocked_id, "reason": ""},
    )
    assert resp.status_code == 422, resp.text


def test_appeal_is_pending_and_does_not_unblock(
    client: TestClient, episode: dict
) -> None:
    """提交申诉本身不解禁 —— 否则申诉入口就成了绕过审核的后门。"""
    media_id = _upload(client, episode, kind="image", name="blocked.png")
    _block_asset(media_id)

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/appeals",
        json={
            "target_type": "asset",
            "target_id": media_id,
            "reason": "这是剧情需要的冲突戏，不含真实血腥",
            "detail": "第 12 镜",
        },
    )
    assert resp.status_code == 201, resp.text
    appeal = resp.json()
    assert appeal["status"] == "PENDING", appeal
    assert appeal["target_id"] == media_id, appeal

    gate = client.get(f"/api/v1/episodes/{episode['id']}/export-gate").json()
    assert gate["can_export"] is False, gate
    assert media_id in gate["pending"], gate

    listed = client.get(f"/api/v1/episodes/{episode['id']}/appeals").json()
    assert listed["pending"] == 1, listed
    assert listed["items"][0]["id"] == appeal["id"], listed

    # 重复提交同类申诉应被拒（否则工单会被刷屏）
    again = client.post(
        f"/api/v1/episodes/{episode['id']}/appeals",
        json={"target_type": "asset", "target_id": media_id, "reason": "再提一次"},
    )
    assert again.status_code == 409, again.text


def test_approved_appeal_unblocks_export(client: TestClient, episode: dict) -> None:
    """人工批准后闸门放行 —— 这是申诉入口存在的意义。"""
    media_id = _upload(client, episode, kind="image", name="blocked.png")
    _block_asset(media_id)
    appeal_id = client.post(
        f"/api/v1/episodes/{episode['id']}/appeals",
        json={"target_type": "asset", "target_id": media_id, "reason": "人工复核后确认合规"},
    ).json()["id"]

    resp = client.post(
        f"/api/v1/appeals/{appeal_id}/resolve",
        json={"status": "APPROVED", "resolution_note": "复核通过，允许导出"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "APPROVED", resp.json()

    gate = client.get(f"/api/v1/episodes/{episode['id']}/export-gate").json()
    assert gate["can_export"] is True, gate
    assert media_id in gate["approved"], gate
    assert gate["unresolved"] == [], gate

    # 闸门放行后，导出请求不应再被 SAFETY_BLOCKED 拦住
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/render/export", json={"preset": "720p"}
    )
    if resp.status_code == 422:
        assert resp.json()["error"]["code"] != "SAFETY_BLOCKED", resp.text


def test_rejected_appeal_keeps_block(client: TestClient, episode: dict) -> None:
    media_id = _upload(client, episode, kind="image", name="blocked.png")
    _block_asset(media_id)
    appeal_id = client.post(
        f"/api/v1/episodes/{episode['id']}/appeals",
        json={"target_type": "asset", "target_id": media_id, "reason": "请求放行"},
    ).json()["id"]

    resp = client.post(
        f"/api/v1/appeals/{appeal_id}/resolve",
        json={"status": "REJECTED", "resolution_note": "仍不符合规范"},
    )
    assert resp.status_code == 200, resp.text

    gate = client.get(f"/api/v1/episodes/{episode['id']}/export-gate").json()
    assert gate["can_export"] is False, gate
    assert media_id in gate["rejected"], gate

    resp = client.post(f"/api/v1/episodes/{episode['id']}/export/package")
    assert resp.status_code == 422, resp.text


def test_appeal_for_input_block_has_no_asset(client: TestClient, episode: dict) -> None:
    """剧本输入被拦截时没有素材可指，申诉对象用 input。"""
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/appeals",
        json={"target_type": "input", "reason": "这是虚构剧情，不含真实违法内容"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["target_id"] is None, resp.json()
    # 输入类申诉不该影响素材闸门
    gate = client.get(f"/api/v1/episodes/{episode['id']}/export-gate").json()
    assert gate["can_export"] is True, gate
