"""角色卡按剧集过滤：同一项目下的其他故事角色不该出现在本集角色页。

线上现象：EP02（异世界）的角色与画风页里混进了 EP03（刑侦）的陈默/林小满/赵九，
用户以为是「和原文无关的素材」。原因是角色卡是项目级资产库，列表接口
只按 project_id 查，没有按剧集收敛。
"""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import StoryDocument
from app.db.session import session_scope

SCRIPT_TEMPLATE = {
    "story_bible": {
        "logline": "测试",
        "theme": "测试",
        "tone": "测试",
        "setting": "测试",
        "characters": [{"name": "__NAME__", "identity": "主角"}],
        "scenes": [{"scene_no": 1, "location": "旧城区", "time_of_day": "夜晚"}],
        "conflict": {"goal": "查清真相"},
        "relationships": [],
    },
    "beat_sheet": [{"beat_no": 1, "name": "开场", "summary": "开场", "scene_no": 1, "shots": [1]}],
    "shots": [
        {
            "shot_no": 1,
            "duration_ms": 4000,
            "scene_no": 1,
            "characters": ["__NAME__"],
            "action": "走动",
            "image_prompt": "雨夜街道，9:16",
            "camera_movement": "固定",
            "composition": "中景",
        }
    ],
}


def _save_script(client: TestClient, episode_id: str, name: str) -> None:
    import copy

    script = copy.deepcopy(SCRIPT_TEMPLATE)
    script["story_bible"]["characters"][0]["name"] = name
    script["shots"][0]["characters"] = [name]
    resp = client.put(f"/api/v1/episodes/{episode_id}/script", json={"story_json": script})
    assert resp.status_code == 200, resp.text


def _new_episode(client: TestClient, project_id: str, title: str) -> dict:
    resp = client.post(
        f"/api/v1/projects/{project_id}/episodes",
        json={"title": title, "target_duration_ms": 60000},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _names(payload: dict) -> list[str]:
    return [c["name"] for c in payload["items"]]


def test_characters_are_scoped_to_episode(
    client: TestClient, project: dict, episode: dict
) -> None:
    other = _new_episode(client, project["id"], "第二集")
    _save_script(client, episode["id"], "陈默")
    _save_script(client, other["id"], "莉莉丝")
    assert client.post(f"/api/v1/episodes/{episode['id']}/characters/sync").status_code == 200
    assert client.post(f"/api/v1/episodes/{other['id']}/characters/sync").status_code == 200

    first = client.get(
        f"/api/v1/projects/{project['id']}/characters", params={"episode_id": episode["id"]}
    ).json()
    assert _names(first) == ["陈默"], first
    assert first["hidden"] == 1

    second = client.get(
        f"/api/v1/projects/{project['id']}/characters", params={"episode_id": other["id"]}
    ).json()
    assert _names(second) == ["莉莉丝"], second

    # 全项目视图（资产库页面）不受影响
    every = client.get(f"/api/v1/projects/{project['id']}/characters").json()
    assert set(_names(every)) == {"陈默", "莉莉丝"}
    assert every["hidden"] == 0


def test_include_all_shows_every_character(
    client: TestClient, project: dict, episode: dict
) -> None:
    other = _new_episode(client, project["id"], "第二集")
    _save_script(client, episode["id"], "陈默")
    _save_script(client, other["id"], "莉莉丝")
    client.post(f"/api/v1/episodes/{episode['id']}/characters/sync")
    client.post(f"/api/v1/episodes/{other['id']}/characters/sync")

    resp = client.get(
        f"/api/v1/projects/{project['id']}/characters",
        params={"episode_id": episode["id"], "include_all": "true"},
    ).json()
    assert set(_names(resp)) == {"陈默", "莉莉丝"}


def test_same_name_in_two_episodes_stays_separate(
    client: TestClient, project: dict, episode: dict
) -> None:
    """同名不同人：两集各有一张卡，设定互不覆盖（线上真实事故）。

    EP02「重生之我在异世界当魔王」与 EP03「连环杀人案」的主角都叫陈默，
    角色卡原先以 (project_id, name) 唯一，后同步的会把先同步的设定覆盖掉，
    角色页于是出现「与原文无关的角色」。
    """
    other = _new_episode(client, project["id"], "第二集")
    _save_script(client, episode["id"], "陈默")
    _save_script(client, other["id"], "陈默")

    with session_scope() as session:
        docs = {doc.episode_id: doc for doc in session.scalars(select(StoryDocument))}
        # 直接改两集人物表里的身份，便于区分是谁的卡（JSON 列必须整体赋值才会落库）
        for eid, identity in ((episode["id"], "刑警队长"), (other["id"], "异世界魔王")):
            doc = docs[eid]
            script = copy.deepcopy(doc.story_json)
            script["story_bible"]["characters"][0]["identity"] = identity
            doc.story_json = script
        session.flush()

    assert client.post(f"/api/v1/episodes/{episode['id']}/characters/sync").status_code == 200
    assert client.post(f"/api/v1/episodes/{other['id']}/characters/sync").status_code == 200

    first = client.get(
        f"/api/v1/projects/{project['id']}/characters", params={"episode_id": episode["id"]}
    ).json()
    second = client.get(
        f"/api/v1/projects/{project['id']}/characters", params={"episode_id": other["id"]}
    ).json()
    assert [c["identity"] for c in first["items"]] == ["刑警队长"], first
    assert [c["identity"] for c in second["items"]] == ["异世界魔王"], second
    # 两张不同的卡
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_manual_character_is_visible_in_every_episode(
    client: TestClient, project: dict, episode: dict
) -> None:
    """手工新增的角色没有「属于哪一集」，不该被过滤掉（否则新建后刷新就消失）。"""
    other = _new_episode(client, project["id"], "第二集")
    _save_script(client, episode["id"], "陈默")
    client.post(f"/api/v1/episodes/{episode['id']}/characters/sync")
    created = client.post(
        f"/api/v1/projects/{project['id']}/characters",
        json={"name": "临时角色", "identity": "路人"},
    )
    assert created.status_code == 201, created.text

    for episode_id in (episode["id"], other["id"]):
        payload = client.get(
            f"/api/v1/projects/{project['id']}/characters", params={"episode_id": episode_id}
        ).json()
        assert "临时角色" in _names(payload), payload
