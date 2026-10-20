"""§12-6 配额全面拦截：图像 / TTS / 合成 都要预留，且不足时在建任务前阻断。

需求 §12：「配额不足 → 阻断新任务，已成功资源可继续预览和导出」；
FR-042：「任务提交时预留配额…同一幂等键不能重复扣费」。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.errors import ErrorCode
from app.providers.base import ProviderCapabilities


def _seed_shots(episode_id: str, count: int = 2, *, dialogue: bool = False) -> None:
    from app.db.models import Shot
    from app.db.session import session_scope

    with session_scope() as session:
        for no in range(1, count + 1):
            shot_json: dict = {"action": f"动作 {no}", "image_prompt": "画面"}
            if dialogue:
                shot_json["dialogue"] = [
                    {"character": "小满", "text": "这是一句用于语音合成的测试台词。", "emotion": "平静"}
                ]
            session.add(
                Shot(
                    episode_id=episode_id,
                    shot_no=no,
                    duration_ms=4000,
                    shot_json=shot_json,
                    stage="CONFIRMED",
                )
            )


class _PricedImageProvider:
    """声明单价的图像供应商替身（单价 10 分/张）。"""

    id = "priced"
    kind = "image"
    model = "priced-v1"
    display_name = "priced"
    unit_price = 10

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=["priced-v1"],
            max_candidates=4,
            billing_unit="image",
            unit_price_cents=self.unit_price,
        )

    def configured(self) -> bool:
        return True

    def describe(self) -> dict:
        return {"id": self.id, "display_name": self.display_name, "kind": "image"}


def test_image_estimate_reports_cost_and_quota(client: TestClient, episode: dict) -> None:
    """FR-010：提交前能拿到预估成本与配额影响。"""
    _seed_shots(episode["id"], count=3)
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/estimate-images",
        json={"candidates_per_shot": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Mock 图像供应商 10 分/张 × 3 镜 × 2 候选 = 60
    assert body["image_count"] == 6
    assert body["estimate_cents"] == 60
    assert body["unit_price_cents"] == 10
    assert body["quota_after_cents"] == body["remaining_cents"] - 60


def test_image_generation_reserves_quota(client: TestClient, episode: dict) -> None:
    """§12-6：图像任务提交时预留配额，成功后按实际消耗结算。"""
    from app.db.models import CostLedger, Job
    from app.db.session import session_scope

    _seed_shots(episode["id"], count=2)
    before = client.get("/api/v1/quota").json()

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/generate-images",
        json={"candidates_per_shot": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["estimate"]["estimate_cents"] == 40  # 2 镜 × 2 候选 × 10 分
    assert body["job"]["cost_reserved_cents"] == 40

    after = client.get("/api/v1/quota").json()
    assert after["reserved_cents"] == before["reserved_cents"] + 40

    with session_scope() as session:
        job = session.get(Job, body["job"]["id"])
        assert job is not None
        assert job.cost_reserved_cents == 40
        kinds = list(
            session.scalars(
                CostLedger.__table__.select().where(CostLedger.job_id == job.id)
            )
        )
        assert len(kinds) == 1, "提交时只应写一条 RESERVE，不能重复扣费"


def test_image_generation_blocked_when_quota_insufficient(
    client: TestClient, episode: dict, monkeypatch
) -> None:
    """§12-6：配额不足 → 402 阻断新任务，且不留下任何任务/预留残留。"""
    from app.core.config import settings

    _seed_shots(episode["id"], count=2)
    before_jobs = client.get("/api/v1/jobs", params={"episode_id": episode["id"]}).json()
    before_total = before_jobs.get("total", len(before_jobs.get("items", [])))
    before_quota = client.get("/api/v1/quota").json()

    original = settings.quota_limit_cents
    # 已用 0，把总额度压到 10 分，低于 40 分的预估
    settings.quota_limit_cents = 10
    try:
        resp = client.post(
            f"/api/v1/episodes/{episode['id']}/shots/generate-images",
            json={"candidates_per_shot": 2},
        )
        assert resp.status_code == 402, resp.text
        body = resp.json()["error"]
        assert body["code"] == ErrorCode.QUOTA_EXCEEDED
        assert body["details"]["need_cents"] == 40
        assert body["retryable"] is False

        after_jobs = client.get("/api/v1/jobs", params={"episode_id": episode["id"]}).json()
        after_total = after_jobs.get("total", len(after_jobs.get("items", [])))
        assert after_total == before_total, "被配额阻断的请求不应创建任务"

        after_quota = client.get("/api/v1/quota").json()
        assert after_quota["reserved_cents"] == before_quota["reserved_cents"], "不应留下预留残留"
    finally:
        settings.quota_limit_cents = original


def _bind_default_voice(client: TestClient, episode: dict, name: str = "小满") -> None:
    """FR-050：有台词的角色必须已绑定音色，否则 TTS 会在提交前被 409 拒绝。

    这些用例测的是**配额**，所以要先满足音色前置条件，否则测不到配额分支。
    """
    project_id = episode["project_id"]
    resp = client.post(f"/api/v1/projects/{project_id}/characters", json={"name": name})
    assert resp.status_code == 201, resp.text
    character = resp.json()
    voices = client.get(f"/api/v1/projects/{project_id}/voices").json()
    voices = voices["items"] if isinstance(voices, dict) else voices
    assert voices, "项目应自带预设音色"
    resp = client.post(
        f"/api/v1/characters/{character['id']}/voice",
        json={"voice_id": voices[0]["id"]},
    )
    assert resp.status_code == 200, resp.text


def test_tts_generation_reserves_quota(client: TestClient, episode: dict) -> None:
    """§12-6：TTS 也要预留配额（此前只有视频做）。"""
    _seed_shots(episode["id"], count=2, dialogue=True)
    _bind_default_voice(client, episode)
    resp = client.post(f"/api/v1/episodes/{episode['id']}/tts/generate", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["estimate"]["line_count"] == 2
    # Mock TTS：10 分/千字，两句共 30 字 → 向上取整 1 个千字单位 → 10 分
    assert body["estimate"]["estimate_cents"] == 10
    assert body["cost_reserved_cents"] == 10


def test_tts_blocked_when_quota_insufficient(client: TestClient, episode: dict) -> None:
    from app.core.config import settings

    _seed_shots(episode["id"], count=2, dialogue=True)
    _bind_default_voice(client, episode)
    original = settings.quota_limit_cents
    settings.quota_limit_cents = 1
    try:
        resp = client.post(f"/api/v1/episodes/{episode['id']}/tts/generate", json={})
        assert resp.status_code == 402, resp.text
        assert resp.json()["error"]["code"] == ErrorCode.QUOTA_EXCEEDED
    finally:
        settings.quota_limit_cents = original


def test_render_blocks_when_quota_insufficient(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    """§12-6：合成（预览/导出）同样受配额约束，不足时在建任务前阻断。"""
    from test_acceptance import _confirm_all_shots, _resume, _run_to_shot_gate

    episode_id = episode["id"]
    # 走到「画面全部确认」——此时时间线才校验得过，才会走到配额这一步
    _run_to_shot_gate(client, episode, project)
    _confirm_all_shots(client, episode_id)

    original = settings.quota_limit_cents
    settings.quota_limit_cents = 0
    try:
        before = client.get("/api/v1/jobs", params={"episode_id": episode_id}).json()
        before_total = before.get("total", len(before.get("items", [])))
        resp = client.post(
            f"/api/v1/episodes/{episode_id}/render/preview", json={"watermark": True}
        )
        assert resp.status_code == 402, resp.text
        assert resp.json()["error"]["code"] == ErrorCode.QUOTA_EXCEEDED
        after = client.get("/api/v1/jobs", params={"episode_id": episode_id}).json()
        after_total = after.get("total", len(after.get("items", [])))
        assert after_total == before_total, "被配额阻断的请求不应创建任务"
    finally:
        settings.quota_limit_cents = original

    # 配额恢复后应能正常建任务（证明拦截没有留下副作用）
    resp = client.post(
        f"/api/v1/episodes/{episode_id}/render/preview", json={"watermark": True}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["estimate"]["estimate_cents"] > 0
    assert body["cost_reserved_cents"] == body["estimate"]["estimate_cents"]


def test_render_estimate_shape(client: TestClient, episode: dict) -> None:
    """FR-010：合成预估包含时长、单价与转场数量。"""
    from app.agents import edit as edit_agent
    from app.db.models import Episode
    from app.db.session import session_scope

    _seed_shots(episode["id"], count=3)
    with session_scope() as session:
        ep = session.get(Episode, episode["id"])
        quote = edit_agent.estimate_render(session, ep, kind="preview")
    assert quote["billing_unit"] == "second"
    assert quote["unit_price_cents"] == settings.render_price_per_second_cents
    # 3 镜 × 4000ms = 12s → 12 分
    assert quote["duration_ms"] == 12000
    assert quote["estimate_cents"] == 12
    assert quote["quota_after_cents"] == quote["remaining_cents"] - 12
    assert quote["transition_count"] == 0


@pytest.mark.parametrize("unit_price", [0, 7])
def test_provider_without_declared_price_reserves_nothing(
    client: TestClient, episode: dict, monkeypatch, unit_price: int
) -> None:
    """未声明单价的供应商不能凭空造出预留；声明了才按单价算。"""
    from app.agents import image as image_agent

    _seed_shots(episode["id"], count=2)
    stub = _PricedImageProvider()
    stub.unit_price = unit_price
    # image_agent 里是 `from ...registry import resolve`，绑定发生在导入期，
    # 因此必须在 image_agent 自己的命名空间里替换才生效。
    monkeypatch.setattr(image_agent, "resolve", lambda *a, **k: stub)

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/generate-images",
        json={"candidates_per_shot": 2},
    )
    assert resp.status_code == 200, resp.text
    expected = 0 if unit_price == 0 else unit_price * 4
    assert resp.json()["estimate"]["estimate_cents"] == expected
    assert resp.json()["job"]["cost_reserved_cents"] == expected


def test_quota_snapshot_accounting_is_self_consistent(client: TestClient) -> None:
    """used = ΣSETTLE；reserved = ΣRESERVE + ΣRELEASE；remaining = limit - used - reserved。"""
    snap = client.get("/api/v1/quota").json()
    assert snap["remaining_cents"] == snap["limit_cents"] - snap["used_cents"] - snap["reserved_cents"]
    assert snap["used_cents"] >= 0
