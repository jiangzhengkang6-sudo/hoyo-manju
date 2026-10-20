"""§13 成本结算回归：全流程跑完后，各阶段实际成本必须都记账。

重点防回归：
- 图像/TTS 过去丢掉 `result.cost_cents` → 结算按预留兜底，但实际单价对不上；
- manifest 键名写错会让合成静默按 0 结算（合成永远不花钱）。
"""

from __future__ import annotations

import pytest
from conftest import STORY_TEXT, drain
from fastapi.testclient import TestClient

from app.core.config import settings


def _advance_idempotency_window() -> None:
    """把「当前 5 秒窗口」推进一格，避开同窗口幂等命中。"""
    import time

    from app.services import jobs as jobs_service

    _ = jobs_service  # 保持导入意图明确
    now = time.time()
    time.sleep(max(0.0, 5.0 - (now % 5.0)) + 0.05)


def test_full_loop_settles_every_stage(client: TestClient, worker, project: dict,
                                       episode: dict) -> None:
    from test_acceptance import _confirm_all_shots, _resume, _run_to_shot_gate

    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)
    _confirm_all_shots(client, episode_id)

    data = _resume(client, episode_id, "gate_shot_confirmation")
    assert data["pending_gate"] == "gate_video_cost_confirmation"
    assert data["state"]["video_quote"]["estimate_cents"] > 0

    _resume(client, episode_id, "gate_video_cost_confirmation")
    shots = client.get(f"/api/v1/episodes/{episode_id}/shots").json()["items"]
    for shot in shots:
        detail = client.get(f"/api/v1/shots/{shot['id']}/video").json()
        history = detail["history"]
        assert history, f"分镜 {shot['shot_no']} 没有视频产物"
        client.post(
            f"/api/v1/shots/{shot['id']}/video/confirm",
            json={"media_id": history[-1]["media_id"]},
        )

    data = _resume(client, episode_id, "gate_video_confirmation")
    assert data["pending_gate"] == "gate_export_confirmation"
    assert (data["state"].get("render") or {}).get("ok") is True

    _resume(client, episode_id, "gate_export_confirmation",
            payload={"preset": "720p_9x16", "package": True})
    drain(worker)

    summary = client.get(f"/api/v1/episodes/{episode_id}/cost").json()
    by_stage = {row["stage"]: row["cents"] for row in summary.get("by_stage", [])}
    assert by_stage, summary
    assert summary["total_cents"] > 0, summary

    # 图像与 TTS 必须有实际消耗（此前 cost_cents 被吞掉，只会记预留兜底值）
    image_cents = next(
        (v for k, v in by_stage.items() if "image" in str(k).lower()), None
    )
    assert image_cents is not None, f"缺少图像阶段成本：{list(by_stage)}"
    assert image_cents > 0, by_stage

    # 合成阶段必须留下 >0 的结算（防「manifest 键取错 → 静默按 0 结算」回归）
    render_cents = next(
        (v for k, v in by_stage.items() if "render" in str(k).lower() or "export" in str(k).lower()),
        None,
    )
    assert render_cents is not None, f"缺少合成阶段成本：{list(by_stage)}"
    assert render_cents > 0, by_stage

    # 按任务类型也能看到各阶段（含 TTS 与视频）
    by_kind = {row["kind"]: row["cents"] for row in summary.get("by_kind", [])}
    assert by_kind, summary
    assert any(v > 0 for v in by_kind.values()), by_kind

    # 全流程结束后不应留下任何预留残留
    quota = client.get("/api/v1/quota").json()
    assert quota["reserved_cents"] == 0, quota
    assert quota["used_cents"] > 0, quota


def test_render_cost_matches_timeline_duration(client: TestClient, worker,
                                               project: dict, episode: dict) -> None:
    """合成结算 = ceil(输出时长秒) × 单价，与预估口径一致。"""
    from app.db.models import CostLedger, Job
    from app.db.session import session_scope
    from sqlalchemy import select
    from test_acceptance import _confirm_all_shots, _resume, _run_to_shot_gate

    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)
    _confirm_all_shots(client, episode_id)

    # 幂等键按 5 秒窗口生成：同一窗口内重复点预览会命中幂等、返回既有任务，
    # 拿不到本次的预留。这里推进窗口，确保拿到一个新任务。
    _advance_idempotency_window()

    resp = client.post(f"/api/v1/episodes/{episode_id}/render/preview",
                       json={"watermark": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 路由返回的是扁平结构：{...job, "estimate": {...}}
    job_id = body["id"]
    estimate = body["estimate"]["estimate_cents"]
    assert estimate > 0, body
    assert body["cost_reserved_cents"] == estimate, body

    drain(worker)

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == "SUCCEEDED", job.status
        assert job.cost_settled_cents > 0, "合成成本被按 0 结算"
        # settle() 会把 cost_reserved_cents 清零（预留已释放），所以结算后要按账目核对：
        # ΣRESERVE == 预估，ΣSETTLE == 实际，ΣRELEASE == -ΣRESERVE
        rows = list(
            session.scalars(
                select(CostLedger).where(CostLedger.job_id == job.id)
            )
        )
        by_kind = {"RESERVE": 0, "SETTLE": 0, "RELEASE": 0}
        for row in rows:
            by_kind[row.kind] = by_kind.get(row.kind, 0) + row.amount_cents

        assert by_kind["RESERVE"] == estimate, by_kind
        assert by_kind["SETTLE"] == job.cost_settled_cents, by_kind
        assert by_kind["RELEASE"] == -estimate, by_kind
        assert job.cost_settled_cents == estimate, (
            f"实际结算 {job.cost_settled_cents} 应等于预估 {estimate}"
        )
