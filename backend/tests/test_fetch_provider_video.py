"""按供应商任务号补拉片段（不产生新费用）。

背景：任务在网关侧成功并计费，本地因网络/超时判了失败。供应商的成片只在
源站保留 24 小时，过期就真没了——所以补拉入口必须把「过期」翻译成人话，
并且补拉成功时把之前被全额释放的那笔成本如实补记回来。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agents import video as video_agent
from app.db.models import CostLedger, Job, MediaAsset, ProviderCall, Shot
from app.db.session import session_scope
from app.providers.base import ProviderErrorCode, ProviderKind, ProviderResult
from app.services import media as media_service

_FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128


def _seed_shot(episode: dict, shot_no: int = 2) -> str:
    with session_scope() as session:
        shot = Shot(
            episode_id=episode["id"],
            shot_no=shot_no,
            duration_ms=4000,
            stage="CONFIRMED",
            shot_json={"action": "跪在王座前", "image_prompt": "王座厅"},
        )
        session.add(shot)
        session.flush()
        return shot.id


class _FakeProvider:
    """替身：只实现补拉需要的两个口子。"""

    id = "grok"
    model = "seedance-2-mini"

    def __init__(self, result: ProviderResult) -> None:
        self._result = result

    async def fetch_result(self, task_id: str) -> ProviderResult:  # noqa: ARG002
        return self._result


def _ok_result(task_id: str, usd: float = 0.164826) -> ProviderResult:
    return ProviderResult(
        ok=True,
        kind=ProviderKind.VIDEO,
        artifacts=[{"content": _FAKE_MP4, "filename": "clip.mp4", "mime_type": "video/mp4"}],
        provider_job_id=task_id,
        cost_cents=0,
        estimated_cost_cents=int(round(usd * 7.2 * 100)),
        raw_summary={"billing_usd": usd, "fetched": True},
    )


@pytest.fixture()
def no_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(media_service, "probe_duration_ms", lambda asset: None)
    return None


@pytest.fixture()
def fast_sleep(monkeypatch: pytest.MonkeyPatch):
    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.providers.custom.asyncio.sleep", _sleep)
    return None


def test_fetch_provider_attaches_clip(
    client: TestClient, project: dict, episode: dict, monkeypatch, no_probe
) -> None:
    shot_id = _seed_shot(episode)
    task_id = "tsk_vid_01M6QZYTNS9MRV06DH8CW14XHS"
    monkeypatch.setattr(
        video_agent, "resolve", lambda kind, session: _FakeProvider(_ok_result(task_id))
    )

    resp = client.post(
        f"/api/v1/shots/{shot_id}/video/fetch-provider", json={"task_id": task_id}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["video_status"] == "CONFIRMED"
    assert body["task_id"] == task_id
    with session_scope() as session:
        shot = session.get(Shot, shot_id)
        assert shot.video_status == "CONFIRMED"
        asset = session.get(MediaAsset, shot.video_media_id)
        assert asset.kind == "video"
        assert asset.owner_id == shot_id
        assert (asset.metadata_json or {}).get("provider_task_id") == task_id


def test_fetch_provider_reports_expired_link(
    client: TestClient, project: dict, episode: dict, monkeypatch, no_probe
) -> None:
    """过期不是「网络错误」，必须说清楚只能重新生成。"""
    shot_id = _seed_shot(episode)

    def expired(task_id: str) -> ProviderResult:
        return ProviderResult.failure(
            ProviderKind.VIDEO,
            ProviderErrorCode.UNAVAILABLE.value,
            "产物链接已过期：供应商的成片只在源站保留 24 小时，过期后无法再取回，只能重新生成",
            retryable=False,
            provider_job_id=task_id,
        )

    class _P:
        id = "grok"

        async def fetch_result(self, task_id: str) -> ProviderResult:
            return expired(task_id)

    monkeypatch.setattr(video_agent, "resolve", lambda kind, session: _P())

    resp = client.post(
        f"/api/v1/shots/{shot_id}/video/fetch-provider",
        json={"task_id": "tsk_vid_expired0000000000000"},
    )

    assert resp.status_code >= 400
    message = resp.json()["error"]["message"]
    assert "24 小时" in message or "过期" in message
    with session_scope() as session:
        assert session.get(Shot, shot_id).video_status == "PENDING"


def test_fetch_provider_backfills_settlement(
    client: TestClient, project: dict, episode: dict, monkeypatch, no_probe
) -> None:
    """补拉成功要把「供应商已计费」补记进账本（原任务失败时全额释放过）。"""
    shot_id = _seed_shot(episode)
    task_id = "tsk_vid_01M6QZYTNS9MRV06DH8CW14XHS"

    with session_scope() as session:
        job = Job(
            project_id=project["id"],
            episode_id=episode["id"],
            type="VIDEO_GENERATION",
            status="FAILED",
            stage="video_generation",
            idempotency_key=f"test-{task_id}",
            provider="grok",
            model="seedance-2-mini",
        )
        session.add(job)
        session.flush()
        session.add(
            ProviderCall(
                job_id=job.id,
                provider="grok",
                model="seedance-2-mini",
                kind="video",
                provider_job_id=task_id,
                status="FAILED",
            )
        )
        job_id = job.id
    monkeypatch.setattr(
        video_agent, "resolve", lambda kind, session: _FakeProvider(_ok_result(task_id))
    )

    resp = client.post(
        f"/api/v1/shots/{shot_id}/video/fetch-provider", json={"task_id": task_id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["billed_cents"] > 0

    with session_scope() as session:
        rows = list(
            session.query(CostLedger).filter(
                CostLedger.job_id == job_id, CostLedger.kind == "SETTLE"
            )
        )
        assert rows, "补记漏了，账本还是显示这笔没花钱"
        assert rows[0].amount_cents == resp.json()["billed_cents"]
        assert task_id in rows[0].note


def test_fetch_provider_does_not_double_settle(
    client: TestClient, project: dict, episode: dict, monkeypatch, no_probe
) -> None:
    """已经结算过的任务再拉一次，不能再扣一笔。"""
    shot_id = _seed_shot(episode)
    task_id = "tsk_vid_01M6QZYTNS9MRV06DH8CW14XHS"

    with session_scope() as session:
        job = Job(
            project_id=project["id"],
            episode_id=episode["id"],
            type="VIDEO_GENERATION",
            status="SUCCEEDED",
            stage="video_generation",
            idempotency_key=f"test2-{task_id}",
            provider="grok",
            model="seedance-2-mini",
            cost_settled_cents=119,
        )
        session.add(job)
        session.flush()
        session.add(
            ProviderCall(
                job_id=job.id,
                provider="grok",
                model="seedance-2-mini",
                kind="video",
                provider_job_id=task_id,
                status="SUCCESS",
            )
        )
        job_id = job.id
    monkeypatch.setattr(
        video_agent, "resolve", lambda kind, session: _FakeProvider(_ok_result(task_id))
    )

    client.post(f"/api/v1/shots/{shot_id}/video/fetch-provider", json={"task_id": task_id})

    with session_scope() as session:
        rows = list(
            session.query(CostLedger).filter(
                CostLedger.job_id == job_id, CostLedger.kind == "SETTLE"
            )
        )
        assert rows == [], "已经结算过的任务不该再记一笔"
