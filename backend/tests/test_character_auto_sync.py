"""剧本生成后自动抓取角色卡（可手改、且手改不被覆盖）。

线上诉求：剧本都生成完了，角色与画风页还是空的 —— 因为角色卡原先只在
工作流跑到 `character_agent` 节点时才从人物表同步。现在改成「剧本一生成
就抓」，并且必须满足：
1. 人物表的 7 个字段全部落到角色卡（不是只有名字）；
2. 用户改过的字段再同步也不会被剧本覆盖（否则「可修改」是假的）；
3. 幂等：重复同步不重复建卡、不无脑涨版本。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import STORY_TEXT, wait_job

CHARACTER_FIELDS = (
    "identity",
    "age_range",
    "appearance",
    "outfit",
    "personality",
    "speech_style",
    "forbidden_changes",
)


def _generate_script(client: TestClient, worker, episode: dict) -> dict:
    saved = client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "idea", "source_text": STORY_TEXT},
    )
    assert saved.status_code == 200, saved.text
    created = client.post(f"/api/v1/episodes/{episode['id']}/story/generate")
    assert created.status_code == 200, created.text
    finished = wait_job(client, worker, created.json()["id"])
    assert finished["status"] == "SUCCEEDED", finished
    return finished


def _characters(client: TestClient, project: dict) -> list[dict]:
    resp = client.get(f"/api/v1/projects/{project['id']}/characters")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _bible_names(client: TestClient, episode: dict) -> list[str]:
    """人物表里的角色名（去重、保序）——Mock 剧本会重复登记同一个名字。"""
    script = client.get(f"/api/v1/episodes/{episode['id']}/script").json()
    names: list[str] = []
    for item in script["story_json"]["story_bible"]["characters"]:
        if item["name"] not in names:
            names.append(item["name"])
    return names


def test_script_generation_creates_complete_character_cards(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    """剧本生成完 → 角色卡已经在，且字段是齐的（不用先跑工作流）。"""
    finished = _generate_script(client, worker, episode)
    synced = finished["result_ref"]["characters_synced"]
    expected = _bible_names(client, episode)
    assert synced["total"] == len(expected) >= 1, synced
    assert synced["created"] == synced["total"]
    assert synced["names"] == expected

    characters = _characters(client, project)
    assert len(characters) == synced["total"]
    for character in characters:
        assert character["source"] == "script"
        assert character["user_edited"] is False
        for field in CHARACTER_FIELDS:
            # 字段必须存在（不能是 KeyError/缺失），空值只能是 `""` 或 `[]`
            assert field in character, (field, character)
            assert character[field] not in (None,), (field, character)
        # 抓取到的设定不能是空的：Mock 剧本给的是完整人物表
        assert character["appearance"]
        assert character["outfit"]


def test_sync_endpoint_is_idempotent(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    _generate_script(client, worker, episode)

    first = client.post(f"/api/v1/episodes/{episode['id']}/characters/sync")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] == 0
    assert body["updated"] == 0
    assert body["total"] >= 1

    second = client.post(f"/api/v1/episodes/{episode['id']}/characters/sync")
    assert second.status_code == 200, second.text
    assert second.json()["updated"] == 0
    # 版本号不能因为「又同步了一次」而增长
    versions = {c["id"]: c["version"] for c in _characters(client, project)}
    again = client.post(f"/api/v1/episodes/{episode['id']}/characters/sync").json()
    assert {c["id"]: c["version"] for c in again["items"]} == versions


def test_manual_edit_survives_resync(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    """用户手改过的角色，重新同步时不能被剧本人物表覆盖。"""
    _generate_script(client, worker, episode)
    character = _characters(client, project)[0]

    resp = client.patch(
        f"/api/v1/characters/{character['id']}",
        json={"appearance": "用户手写：银发，左眼缠着绷带"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_edited"] is True

    resynced = client.post(f"/api/v1/episodes/{episode['id']}/characters/sync")
    assert resynced.status_code == 200, resynced.text
    survived = client.get(f"/api/v1/characters/{character['id']}").json()
    assert survived["appearance"] == "用户手写：银发，左眼缠着绷带"
    assert survived["identity"] == character["identity"]  # 未手改的字段保持原值

    # 显式 force 才允许「用剧本覆盖」
    forced = client.post(
        f"/api/v1/episodes/{episode['id']}/characters/sync", params={"force": True}
    )
    assert forced.status_code == 200, forced.text
    overwritten = client.get(f"/api/v1/characters/{character['id']}").json()
    assert overwritten["appearance"] != "用户手写：银发，左眼缠着绷带"


def test_sync_without_script_is_rejected(
    client: TestClient, project: dict, episode: dict
) -> None:
    resp = client.post(f"/api/v1/episodes/{episode['id']}/characters/sync")
    assert resp.status_code == 404, resp.text


def test_archived_character_is_restored_when_script_needs_it(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    """归档过的角色若又出现在剧本人物表里，同步要把卡恢复，而不是留个 409 死结。"""
    _generate_script(client, worker, episode)
    character = _characters(client, project)[0]

    archived = client.delete(f"/api/v1/characters/{character['id']}")
    assert archived.status_code == 200, archived.text
    assert character["id"] not in {c["id"] for c in _characters(client, project)}

    resynced = client.post(f"/api/v1/episodes/{episode['id']}/characters/sync")
    assert resynced.status_code == 200, resynced.text
    restored = {c["id"]: c for c in _characters(client, project)}
    assert character["id"] in restored
    assert restored[character["id"]]["status"] in {"DRAFT", "CONFIRMED"}


def test_manual_script_save_does_not_create_characters_implicitly(
    client: TestClient, project: dict, episode: dict
) -> None:
    """人工保存剧本不该有「顺手建角色卡」的副作用；要抓就显式调 sync。

    这条约束很重要：`PUT /script` 一旦自动建卡，用户随后手动「新增角色」
    就会撞同名 409（角色已存在），而用户并不知道卡是保存剧本时冒出来的。
    """
    script = {
        "story_bible": {
            "logline": "雨夜追凶",
            "theme": "执念",
            "tone": "冷峻",
            "setting": "旧城区",
            "characters": [
                {"name": "顾青", "identity": "法医", "age_range": "28-32"},
                {"name": "程野", "identity": "刑警"},
            ],
            "scenes": [{"scene_no": 1, "location": "停尸房", "time_of_day": "夜晚"}],
            "conflict": {"goal": "查清真相"},
            "relationships": [],
        },
        "beat_sheet": [{"beat_no": 1, "name": "开场", "summary": "发现尸体", "scene_no": 1, "shots": [1]}],
        "shots": [
            {
                "shot_no": 1,
                "duration_ms": 4000,
                "scene_no": 1,
                "characters": ["顾青"],
                "action": "顾青拉开冷柜",
                "image_prompt": "停尸房，顾青拉开冷柜，冷色调，9:16",
                "camera_movement": "固定",
                "composition": "中景",
            }
        ],
    }
    saved = client.put(f"/api/v1/episodes/{episode['id']}/script", json={"story_json": script})
    assert saved.status_code == 200, saved.text
    assert _characters(client, project) == []

    # 显式抓取：字段给齐（缺的留空，等用户补），不是「只有名字」
    sync = client.post(f"/api/v1/episodes/{episode['id']}/characters/sync")
    assert sync.status_code == 200, sync.text
    assert sync.json()["created"] == 2

    characters = {c["name"]: c for c in _characters(client, project)}
    assert set(characters) == {"顾青", "程野"}
    gu = characters["顾青"]
    assert gu["identity"] == "法医"
    assert gu["age_range"] == "28-32"
    assert gu["appearance"] == ""
    assert gu["forbidden_changes"] == []
    assert gu["source"] == "script"
    assert gu["source_script_version"] == sync.json()["script_version"]


@pytest.mark.parametrize("field", ["name"], ids=["name"])
def test_character_names_follow_bible(
    client: TestClient, worker, project: dict, episode: dict, field: str
) -> None:
    _generate_script(client, worker, episode)
    names = [c[field] for c in _characters(client, project)]
    assert names == _bible_names(client, episode), names
