"""FR-014 局部重生成：按剧情节拍 / 按分镜，且不得改动未选中的分镜。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Job, StoryDocument
from app.db.session import session_scope


def _script_with_beats() -> dict:
    """两幕、两节拍，每节拍两镜 —— 便于验证「只动选中节拍」。"""
    return {
        "story_bible": {"logline": "雨夜相遇", "characters": []},
        "beat_sheet": [
            {"beat_no": 1, "name": "相遇", "summary": "两人在雨中相遇", "scene_no": 1, "shots": [1, 2]},
            {"beat_no": 2, "name": "冲突", "summary": "争执升级", "scene_no": 1, "shots": [3, 4]},
        ],
        "shots": [
            {
                "shot_no": no,
                "duration_ms": 4000,
                "scene_no": 1,
                "characters": [],
                "action": f"动作 {no}",
                "camera_movement": "固定",
                "composition": "中景",
                "emotion": "平静",
                "dialogue": [],
                "narration": "",
                "sfx": [],
                "image_prompt": f"场景 {no}",
                "negative_prompt": "",
            }
            for no in (1, 2, 3, 4)
        ],
    }


def _save_script(client: TestClient, episode_id: str) -> None:
    resp = client.put(
        f"/api/v1/episodes/{episode_id}/script",
        json={"story_json": _script_with_beats()},
    )
    assert resp.status_code == 200, resp.text


def test_beat_regenerate_maps_to_shot_numbers(client: TestClient, episode: dict) -> None:
    _save_script(client, episode["id"])
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/beats/regenerate",
        json={"beat_nos": [1], "instruction": "更温柔一些"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 节拍 1 下辖镜 1、2 —— 必须只带这两个
    assert body["shot_nos"] == [1, 2]
    assert body["type"] == "SCRIPT_REGENERATE"
    assert body["status"] in ("QUEUED", "PENDING", "RUNNING")

    with session_scope() as session:
        job = session.get(Job, body["id"])
        assert job.input_json["beat_nos"] == [1]
        assert job.input_json["shot_nos"] == [1, 2]
        assert job.input_json["instruction"] == "更温柔一些"


def test_beat_regenerate_multiple_beats_merges_shots(
    client: TestClient, episode: dict
) -> None:
    _save_script(client, episode["id"])
    body = client.post(
        f"/api/v1/episodes/{episode['id']}/beats/regenerate",
        json={"beat_nos": [2, 1]},
    ).json()
    assert body["shot_nos"] == [1, 2, 3, 4]


def test_beat_regenerate_unknown_beat_is_404(client: TestClient, episode: dict) -> None:
    _save_script(client, episode["id"])
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/beats/regenerate",
        json={"beat_nos": [99]},
    )
    assert resp.status_code == 404, resp.text
    assert "剧情节拍不存在" in resp.json()["error"]["message"]


def test_beat_regenerate_without_beat_sheet_is_409(
    client: TestClient, episode: dict
) -> None:
    """剧本没有节拍表时应给出可读错误，并指向按分镜重生成。"""
    script = _script_with_beats()
    script["beat_sheet"] = []
    resp = client.put(
        f"/api/v1/episodes/{episode['id']}/script", json={"story_json": script}
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/beats/regenerate",
        json={"beat_nos": [1]},
    )
    assert resp.status_code == 409, resp.text
    assert "剧情节拍表" in resp.json()["error"]["message"]


def test_beat_regenerate_requires_at_least_one_beat(
    client: TestClient, episode: dict
) -> None:
    _save_script(client, episode["id"])
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/beats/regenerate",
        json={"beat_nos": []},
    )
    assert resp.status_code == 422


def test_beat_regenerate_does_not_touch_other_beats(
    client: TestClient, episode: dict
) -> None:
    """FR-014：未选中的节拍对应分镜不得被改动。"""
    _save_script(client, episode["id"])
    client.post(
        f"/api/v1/episodes/{episode['id']}/beats/regenerate",
        json={"beat_nos": [1]},
    )
    with session_scope() as session:
        doc = session.scalars(
            select(StoryDocument)
            .where(StoryDocument.episode_id == episode["id"])
            .order_by(StoryDocument.version.desc())
        ).first()
    shots = {s["shot_no"]: s for s in doc.story_json["shots"]}
    # 镜 3、4 属于节拍 2，未被选中，动作必须保持原样
    assert shots[3]["action"] == "动作 3"
    assert shots[4]["action"] == "动作 4"


def test_shot_regenerate_still_works(client: TestClient, episode: dict) -> None:
    """按分镜重生成的老入口不能因为新增节拍入口而回归。"""
    _save_script(client, episode["id"])
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/regenerate",
        json={"shot_nos": [3]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["type"] == "SCRIPT_REGENERATE"
