"""FR-052 / FR-053：BGM 上传与字幕人工校正。

FR-052：BGM 必须能上传、试听、挂载、删除。
FR-053：字幕必须能人工校正；**时间轴不合法要拦在写入前**，
否则会产出播放器里不显示（或倒放）的字幕，且 FFmpeg 的 `subtitles` 滤镜会跳过该条，
导致成片与导出的 SRT/VTT 对不上。
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from conftest import drain, wait_job

#: 1×1 PNG，避免依赖 Pillow 也能构造一个合法上传
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


def _upload_audio(client: TestClient, episode: dict) -> str:
    resp = client.post(
        "/api/v1/media/upload",
        files={"file": ("bgm.mp3", io.BytesIO(b"ID3\x03\x00\x00\x00fake-audio"), "audio/mpeg")},
        data={"kind": "audio", "rights_declared": "true", "episode_id": episode["id"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed_script_with_dialogue(client: TestClient, episode: dict) -> None:
    """写一份带台词的分镜脚本。

    `generate_subtitles` 是从**时间线的 dialogue 轨**派生的，而时间线只收已确认分镜；
    所以要先有剧本，这个用例才有字幕可校正。
    """
    shots = [
        {
            "shot_no": i,
            "duration_ms": 3000,
            "scene_no": 1,
            "characters": ["林晚"],
            "action": f"第 {i} 个动作",
            "camera_movement": "固定",
            "composition": "中景",
            "emotion": "平静",
            "dialogue": [{"character": "林晚", "text": f"第 {i} 句台词", "emotion": "平静"}],
            "narration": "",
            "sfx": [],
            "image_prompt": f"雨夜街道第 {i} 镜",
            "negative_prompt": "",
        }
        for i in range(1, 4)
    ]
    body = {
        "story_bible": {"logline": "雨夜相遇", "characters": [{"name": "林晚"}]},
        "beat_sheet": [],
        "shots": shots,
    }
    resp = client.put(f"/api/v1/episodes/{episode['id']}/script", json={"story_json": body})
    assert resp.status_code == 200, resp.text


def _cues(client: TestClient, episode: dict) -> list[dict]:
    body = client.get(f"/api/v1/episodes/{episode['id']}/subtitles").json()
    return body["items"] if isinstance(body, dict) else body


def _make_cue(client: TestClient, episode: dict, worker) -> dict:
    """确保存在至少一条字幕，返回第一条。

    字幕生成是**异步任务**（返回 job），必须把 worker 跑起来才会有条目。
    """
    existing = _cues(client, episode)
    if existing:
        return existing[0]
    _seed_script_with_dialogue(client, episode)
    resp = client.post(f"/api/v1/episodes/{episode['id']}/subtitles/generate")
    assert resp.status_code == 200, resp.text
    job = wait_job(client, worker, resp.json()["id"])
    assert job["status"] == "SUCCEEDED", job
    drain(worker, timeout=60)
    cues = _cues(client, episode)
    assert cues, f"字幕任务成功后仍无条目：{job}"
    return cues[0]


# --------------------------------------------------------------------------- #
# FR-052 BGM
# --------------------------------------------------------------------------- #

def test_audio_upload_is_accepted(client: TestClient, episode: dict) -> None:
    """FR-052：本页上传音频必须可用（前端「上传并选用」依赖它）。"""
    media_id = _upload_audio(client, episode)
    got = client.get(f"/api/v1/media/{media_id}").json()
    assert got["id"] == media_id
    assert got["kind"] == "audio"


def test_bgm_can_be_attached_and_previewed(client: TestClient, episode: dict) -> None:
    """FR-052：挂载音轨 + 试听 + 删除，闭环要通。"""
    media_id = _upload_audio(client, episode)

    created = client.post(
        f"/api/v1/episodes/{episode['id']}/bgm",
        json={
            "media_id": media_id,
            "kind": "bgm",
            "start_ms": 0,
            "end_ms": 8000,
            "volume_db": -6,
            "fade_in_ms": 500,
            "fade_out_ms": 800,
            "rights_declared": True,
        },
    )
    assert created.status_code == 201, created.text
    track = created.json()

    listed = client.get(f"/api/v1/episodes/{episode['id']}/bgm").json()
    items = listed["items"] if isinstance(listed, dict) else listed
    assert any(t["id"] == track["id"] for t in items)

    # 试听走 /media/{id}/content，必须能取到字节
    content = client.get(f"/api/v1/media/{media_id}/content")
    assert content.status_code == 200
    assert content.content

    deleted = client.delete(f"/api/v1/episodes/{episode['id']}/bgm/{track['id']}")
    assert deleted.status_code == 200
    after = client.get(f"/api/v1/episodes/{episode['id']}/bgm").json()
    after_items = after["items"] if isinstance(after, dict) else after
    assert not any(t["id"] == track["id"] for t in after_items)


# --------------------------------------------------------------------------- #
# FR-053 字幕校正
# --------------------------------------------------------------------------- #

def test_subtitle_text_can_be_corrected(client: TestClient, episode: dict, worker) -> None:
    """FR-053：改文本要生效并标记 edited。"""
    cue = _make_cue(client, episode, worker)
    resp = client.patch(
        f"/api/v1/episodes/{episode['id']}/subtitles/{cue['id']}",
        json={"text": "人工校正后的字幕"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "人工校正后的字幕"
    assert body["edited"] is True


def test_subtitle_time_can_be_corrected(client: TestClient, episode: dict, worker) -> None:
    """FR-053：改时间轴要生效，且只改一端时另一端保持原值。"""
    cue = _make_cue(client, episode, worker)
    resp = client.patch(
        f"/api/v1/episodes/{episode['id']}/subtitles/{cue['id']}",
        json={"start_ms": 500, "end_ms": 2500},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_ms"] == 500
    assert body["end_ms"] == 2500


def test_subtitle_rejects_inverted_time_range(client: TestClient, episode: dict, worker) -> None:
    """回归：`start_ms >= end_ms` 必须在写入前拒绝（422），不能静默存下倒置时间轴。"""
    cue = _make_cue(client, episode, worker)
    original = _cues(client, episode)
    before = next(c for c in original if c["id"] == cue["id"])

    resp = client.patch(
        f"/api/v1/episodes/{episode['id']}/subtitles/{cue['id']}",
        json={"start_ms": 9000, "end_ms": 1000},
    )
    assert resp.status_code == 422, f"倒置时间轴应被拒绝，实际 {resp.status_code}"
    err = resp.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["retryable"] is False
    assert err["details"]["start_ms"] == 9000
    assert err["details"]["end_ms"] == 1000

    # 被拒绝的请求不能改动库里的数据
    after = next(c for c in _cues(client, episode) if c["id"] == cue["id"])
    assert after["start_ms"] == before["start_ms"]
    assert after["end_ms"] == before["end_ms"]


def test_subtitle_rejects_equal_time_range(client: TestClient, episode: dict, worker) -> None:
    """零长度字幕同样无意义，必须拒绝。"""
    cue = _make_cue(client, episode, worker)
    resp = client.patch(
        f"/api/v1/episodes/{episode['id']}/subtitles/{cue['id']}",
        json={"start_ms": 3000, "end_ms": 3000},
    )
    assert resp.status_code == 422


def test_subtitle_rejects_negative_time(client: TestClient, episode: dict, worker) -> None:
    """负时间会破坏时间线校验，必须拒绝。"""
    cue = _make_cue(client, episode, worker)
    resp = client.patch(
        f"/api/v1/episodes/{episode['id']}/subtitles/{cue['id']}",
        json={"start_ms": -100},
    )
    assert resp.status_code == 422


def test_subtitle_rejects_non_integer_time(client: TestClient, episode: dict, worker) -> None:
    """时间必须是整数毫秒；传字符串数字可接受，传乱码要 422 而不是 500。"""
    cue = _make_cue(client, episode, worker)
    ok = client.patch(
        f"/api/v1/episodes/{episode['id']}/subtitles/{cue['id']}",
        json={"start_ms": "800", "end_ms": "1800"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["start_ms"] == 800

    bad = client.patch(
        f"/api/v1/episodes/{episode['id']}/subtitles/{cue['id']}",
        json={"start_ms": "不是数字"},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_subtitle_patch_404_for_unknown_cue(client: TestClient, episode: dict) -> None:
    resp = client.patch(
        f"/api/v1/episodes/{episode['id']}/subtitles/sub_does_not_exist",
        json={"text": "x"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_corrected_subtitle_appears_in_export(client: TestClient, episode: dict, worker) -> None:
    """FR-053：校正后的文本必须体现在导出的 SRT 里（否则改了等于没改）。"""
    cue = _make_cue(client, episode, worker)
    marker = "导出可见的校正文本"
    client.patch(
        f"/api/v1/episodes/{episode['id']}/subtitles/{cue['id']}",
        json={"text": marker},
    )
    resp = client.get(
        f"/api/v1/episodes/{episode['id']}/subtitles/export", params={"format": "srt"}
    )
    assert resp.status_code == 200
    assert marker in resp.text
