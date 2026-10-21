"""空故事输入必须被拦住 —— 否则模型会凭空编一部与用户创意无关的剧本。

线上真实故障：用户在「故事与剧本」页输入创意后立刻点「生成剧本」，
自动保存的防抖还没触发，后端读到的 `source_text` 是空字符串，
DeepSeek 拿到空输入自由发挥，产出一部「便利店店员林晚」的剧本，
用户当场质疑「这是不是和我说的一点关系都没有」。
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_generate_rejects_episode_without_story(client: TestClient, episode: dict) -> None:
    resp = client.post(f"/api/v1/episodes/{episode['id']}/story/generate")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "故事内容为空" in body["error"]["message"]


def test_generate_rejects_whitespace_only_story(client: TestClient, episode: dict) -> None:
    saved = client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "idea", "source_text": "   \n\t  "},
    )
    assert saved.status_code == 200, saved.text

    resp = client.post(f"/api/v1/episodes/{episode['id']}/story/generate")
    assert resp.status_code == 400, resp.text
    assert "故事内容为空" in resp.json()["error"]["message"]


def test_generate_proceeds_once_story_is_saved(client: TestClient, episode: dict) -> None:
    """保存了内容就必须能正常进任务，别把正常路径也拦掉。"""
    saved = client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "idea", "source_text": "雨夜里，林晚在便利店门口等到了一封匿名信。"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["source_text"]

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/story/generate", params={"shot_count": 3}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"]


def test_workflow_start_rejects_empty_story(client: TestClient, episode: dict) -> None:
    resp = client.post(f"/api/v1/episodes/{episode['id']}/workflow/start", json={})
    assert resp.status_code in (400, 409), resp.text
    assert "故事内容为空" in resp.text
