"""FR-010 长文本输入必须显示字符数与预计生成成本。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.agents import story as story_agent


def test_estimate_reports_char_counts(client: TestClient, episode: dict) -> None:
    text = "雨夜，林小满在便利店门口遇见了陆行舟。" * 3
    body = client.post(
        f"/api/v1/episodes/{episode['id']}/story/estimate",
        json={"input_type": "text", "source_text": text},
    ).json()

    assert body["source_chars"] == len(text)
    assert body["prompt_chars"] > 0
    assert body["system_chars"] > 0
    # 计费口径 = 提示词 + 系统提示（供应商按 1k tokens 计）
    assert body["billing_chars"] == body["prompt_chars"] + body["system_chars"]
    assert body["episode_id"] == episode["id"]


def test_estimate_cost_matches_billing_unit(client: TestClient, episode: dict) -> None:
    """Mock 文本供应商：1 分/千字符。预估必须与这个口径一致。"""
    body = client.post(
        f"/api/v1/episodes/{episode['id']}/story/estimate",
        json={"input_type": "idea", "source_text": "雨夜相遇"},
    ).json()
    assert body["declared"] is True
    assert body["unit_price_cents"] == 1
    assert body["billing_unit"] == "1k_tokens"
    expected = -(-body["billing_chars"] // 1000)  # 向上取整
    assert body["estimate_cents"] == expected


def test_long_text_is_flagged(client: TestClient, episode: dict) -> None:
    short = client.post(
        f"/api/v1/episodes/{episode['id']}/story/estimate",
        json={"source_text": "短故事"},
    ).json()
    assert short["long_text"] is False
    assert short["long_text_threshold"] == story_agent.LONG_TEXT_THRESHOLD

    long_text = "字" * (story_agent.LONG_TEXT_THRESHOLD + 1)
    long_body = client.post(
        f"/api/v1/episodes/{episode['id']}/story/estimate",
        json={"source_text": long_text},
    ).json()
    assert long_body["long_text"] is True


def test_longer_text_costs_at_least_as_much(client: TestClient, episode: dict) -> None:
    a = client.post(
        f"/api/v1/episodes/{episode['id']}/story/estimate",
        json={"source_text": "短"},
    ).json()
    b = client.post(
        f"/api/v1/episodes/{episode['id']}/story/estimate",
        json={"source_text": "长" * 1800},
    ).json()
    assert b["estimate_cents"] >= a["estimate_cents"]
    assert b["source_chars"] > a["source_chars"]


def test_estimate_uses_same_prompt_as_generation(
    client: TestClient, episode: dict
) -> None:
    """预估与真实调用必须共用提示词，否则预估对不上实扣。"""
    prompt = story_agent.build_script_prompt(source_text="雨夜相遇", shot_count=6)
    body = client.post(
        f"/api/v1/episodes/{episode['id']}/story/estimate",
        json={"source_text": "雨夜相遇", "shot_count": 6},
    ).json()
    assert body["prompt_chars"] == len(prompt)


def test_estimate_respects_shot_count(client: TestClient, episode: dict) -> None:
    few = client.post(
        f"/api/v1/episodes/{episode['id']}/story/estimate",
        json={"source_text": "雨夜", "shot_count": 4},
    ).json()
    many = client.post(
        f"/api/v1/episodes/{episode['id']}/story/estimate",
        json={"source_text": "雨夜", "shot_count": 12},
    ).json()
    assert few["shot_count"] == 4
    assert many["shot_count"] == 12


def test_estimate_rejects_over_length_input(client: TestClient, episode: dict) -> None:
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/story/estimate",
        json={"source_text": "字" * 20001},
    )
    assert resp.status_code == 422


def test_estimate_on_unknown_episode_is_404(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/episodes/epi_does_not_exist/story/estimate",
        json={"source_text": "x"},
    )
    assert resp.status_code == 404
