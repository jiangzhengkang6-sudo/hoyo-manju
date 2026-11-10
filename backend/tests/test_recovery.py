"""待核队列：失败但已提交的任务，24 小时内自动补拉。

供应商任务一旦提交就开始计费；本地因网络判失败时钱已经花了，而成片只在
源站保留 24 小时。这一层就是在这 24 小时里自动试几次，把能捞的捞回来。
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.db.models import CostLedger, Job, MediaAsset, ProviderCall, Shot
from app.db.session import session_scope
from app.providers.base import ProviderErrorCode, ProviderKind, ProviderResult
from app.services import media as media_service
from app.services import recovery

_FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
TASK_ID = "tsk_vid_01M6QZYTNS9MRV06DH8CW14XHS"


class _FakeProvider:
    id = "grok"
    model = "seedance-2-mini"

    def __init__(self, result: ProviderResult) -> None:
        self._result = result
        self.calls = 0

    async def fetch_result(self, task_id: str) -> ProviderResult:  # noqa: ARG002
        self.calls += 1
        return self._result


def _ok(usd: float = 0.164826) -> ProviderResult:
    return ProviderResult(
        ok=True,
        kind=ProviderKind.VIDEO,
        artifacts=[{"content": _FAKE_MP4, "filename": "clip.mp4", "mime_type": "video/mp4"}],
        provider_job_id=TASK_ID,
        raw_summary={"billing_usd": usd},
    )


def _failed(expired: bool = False) -> ProviderResult:
    return ProviderResult.failure(
        ProviderKind.VIDEO,
        ProviderErrorCode.UNAVAILABLE.value,
        "产物链接已过期：供应商的成片只在源站保留 24 小时" if expired else "网络不可达",
        retryable=not expired,
        provider_job_id=TASK_ID,
    )


def _seed(
    project: dict,
    episode: dict,
    *,
    finished_hours_ago: float = 1,
    with_task_id: bool = True,
    settled: int = 0,
) -> tuple[str, str]:
    with session_scope() as session:
        shot = Shot(
            episode_id=episode["id"],
            shot_no=2,
            duration_ms=4000,
            stage="CONFIRMED",
            shot_json={"action": "跪在王座前"},
        )
        session.add(shot)
        session.flush()
        job = Job(
            project_id=project["id"],
            episode_id=episode["id"],
            type="VIDEO_GENERATION",
            status="FAILED",
            stage="video_generation",
            idempotency_key=f"recover-{TASK_ID}-{finished_hours_ago}",
            provider="grok",
            model="seedance-2-mini",
            input_json={"shot_nos": [2], "params": {}},
            finished_at=dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(hours=finished_hours_ago),
            cost_settled_cents=settled,
        )
        session.add(job)
        session.flush()
        session.add(
            ProviderCall(
                job_id=job.id,
                provider="grok",
                model="seedance-2-mini",
                kind="video",
                provider_job_id=TASK_ID if with_task_id else "",
                status="FAILED",
            )
        )
        return job.id, shot.id


@pytest.fixture()
def no_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(media_service, "probe_duration_ms", lambda asset: None)
    return None


def test_recovers_clip_and_backfills_ledger(
    client: TestClient, project: dict, episode: dict, monkeypatch, no_probe
) -> None:
    job_id, shot_id = _seed(project, episode)
    provider = _FakeProvider(_ok())
    monkeypatch.setattr(recovery, "resolve", lambda kind, session: provider)

    with session_scope() as session:
        results = recovery.sweep(session, limit=5)

    assert results and results[0]["ok"] is True, results
    with session_scope() as session:
        shot = session.get(Shot, shot_id)
        assert shot.video_status == "CONFIRMED"
        asset = session.get(MediaAsset, shot.video_media_id)
        assert asset.metadata_json["provider_task_id"] == TASK_ID
        job = session.get(Job, job_id)
        assert (job.result_ref or {}).get("recovered") is True
        assert job.error_detail["recover"]["recovered_at"]
        settles = list(
            session.query(CostLedger).filter(
                CostLedger.job_id == job_id, CostLedger.kind == "SETTLE"
            )
        )
        assert settles and settles[0].amount_cents > 0


def test_skips_job_without_provider_task_id(
    client: TestClient, project: dict, episode: dict, monkeypatch
) -> None:
    _seed(project, episode, with_task_id=False)
    provider = _FakeProvider(_ok())
    monkeypatch.setattr(recovery, "resolve", lambda kind, session: provider)

    with session_scope() as session:
        assert recovery.sweep(session, limit=5) == []
    assert provider.calls == 0, "没有编号就不该去问网关"


def test_skips_expired_window(
    client: TestClient, project: dict, episode: dict, monkeypatch
) -> None:
    """超过 24 小时就别再试了：成片在源站已经没了，问了也是白问。"""
    _seed(project, episode, finished_hours_ago=30)
    provider = _FakeProvider(_ok())
    monkeypatch.setattr(recovery, "resolve", lambda kind, session: provider)

    with session_scope() as session:
        assert recovery.sweep(session, limit=5) == []


def test_does_not_settle_twice(
    client: TestClient, project: dict, episode: dict, monkeypatch, no_probe
) -> None:
    """已经结算过的任务补拉回来，不能再记一笔。"""
    job_id, _ = _seed(project, episode, settled=119)
    monkeypatch.setattr(recovery, "resolve", lambda kind, session: _FakeProvider(_ok()))

    with session_scope() as session:
        recovery.sweep(session, limit=5)

    with session_scope() as session:
        settles = list(
            session.query(CostLedger).filter(
                CostLedger.job_id == job_id, CostLedger.kind == "SETTLE"
            )
        )
        assert settles == []


def test_expired_link_stops_retrying_after_attempts(
    client: TestClient, project: dict, episode: dict, monkeypatch
) -> None:
    """网关明确说过期，就把次数记满，别再反复问。"""
    job_id, _ = _seed(project, episode)
    monkeypatch.setattr(
        recovery, "resolve", lambda kind, session: _FakeProvider(_failed(expired=True))
    )

    with session_scope() as session:
        job = session.get(Job, job_id)
        # 直接跑到上限
        for _ in range(recovery.MAX_ATTEMPTS):
            job.error_detail = {
                **(job.error_detail or {}),
                "recover": {"attempts": recovery.MAX_ATTEMPTS, "last_at": "2000-01-01T00:00:00+00:00"},
            }
        session.flush()

    with session_scope() as session:
        assert recovery.candidates(session, limit=5) == []


def test_retry_interval_prevents_hot_loop(
    client: TestClient, project: dict, episode: dict, monkeypatch
) -> None:
    """刚试过 5 分钟内不再试，避免每分钟都去打网关。"""
    job_id, _ = _seed(project, episode)
    with session_scope() as session:
        job = session.get(Job, job_id)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        job.error_detail = {"recover": {"attempts": 1, "last_at": now}}
        session.flush()

    with session_scope() as session:
        assert recovery.candidates(session, limit=5) == []
