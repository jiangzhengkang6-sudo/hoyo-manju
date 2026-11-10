"""成本台账幂等：重复结算/重复释放必须无害。

线上真实事故：一个生图任务被重复执行（worker 与节点重试各跑一次），
台账里出现「1 次预留 + 2 次释放」→ `reserved` 变成负数（-50），
可用额度被凭空放大。根因是结算/释放信任了内存里的
`job.cost_reserved_cents`，而不是台账本身。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.db.models import CostLedger
from app.db.session import session_scope
from app.services import cost as cost_service
from app.services import jobs as jobs_service


def _job_with_reserve(session, episode_id: str, cents: int = 50):
    job = jobs_service.create_job(
        session,
        type="IMAGE_GENERATION",
        idempotency_key=f"test#cost-idem#{uuid.uuid4().hex[:8]}",
        episode_id=episode_id,
        stage="image_generation",
        input_json={},
        input_summary={},
        max_attempts=1,
        reserve_cents=cents,
    )
    session.flush()
    return job


def _ledger(session, job_id: str) -> list[tuple[str, int]]:
    rows = session.query(CostLedger).filter(CostLedger.job_id == job_id).all()
    return [(row.kind, row.amount_cents) for row in rows]


def test_settle_is_idempotent(client: TestClient, episode: dict) -> None:
    with session_scope() as session:
        job = _job_with_reserve(session, episode["id"], 50)
        cost_service.settle(session, job=job, actual_cents=50)
        # 同一个任务被重跑第二次：对象上还留着 50（旧值），但台账已经结清
        job.cost_reserved_cents = 50
        cost_service.settle(session, job=job, actual_cents=50)
        session.flush()

        ledger = _ledger(session, job.id)
        assert [k for k, _ in ledger].count("SETTLE") == 1
        assert [k for k, _ in ledger].count("RELEASE") == 1
        assert cost_service.outstanding_reserved(session, job.id) == 0


def test_release_after_settle_does_not_double_free(client: TestClient, episode: dict) -> None:
    with session_scope() as session:
        job = _job_with_reserve(session, episode["id"], 50)
        cost_service.settle(session, job=job, actual_cents=50)
        job.cost_reserved_cents = 50          # 模拟陈旧对象
        cost_service.release(session, job=job, reason="失败释放")
        session.flush()

        released = [a for k, a in _ledger(session, job.id) if k == "RELEASE"]
        assert released == [-50]              # 只有结算时那一次
        assert cost_service.outstanding_reserved(session, job.id) == 0


def test_quota_reserved_never_goes_negative(client: TestClient, episode: dict) -> None:
    with session_scope() as session:
        job = _job_with_reserve(session, episode["id"], 50)
        cost_service.settle(session, job=job, actual_cents=50)
        job.cost_reserved_cents = 50
        cost_service.settle(session, job=job, actual_cents=50)
        cost_service.release(session, job=job, reason="异常释放")
        session.flush()

        snap = cost_service.quota_snapshot(session)
        # 允许其它用例残留的预留，但绝不允许负数
        assert snap["reserved_cents"] >= 0


def test_retry_transfers_reservation_instead_of_duplicating(
    client: TestClient, episode: dict
) -> None:
    with session_scope() as session:
        job = jobs_service.create_job(
            session,
            type="IMAGE_GENERATION",
            idempotency_key=f"test#cost-retry#{uuid.uuid4().hex[:8]}",
            episode_id=episode["id"],
            stage="image_generation",
            input_json={},
            input_summary={},
            max_attempts=1,
            reserve_cents=60,
        )
        session.flush()
        jobs_service.set_status(session, job, "EXPIRED")   # 过期任务不走 release
        session.flush()

        new_job = jobs_service.retry_job(session, job, reason="测试重试")
        session.flush()

        # 旧任务结清、新任务承接同一笔金额：总数仍是一份
        assert cost_service.outstanding_reserved(session, job.id) == 0
        assert cost_service.outstanding_reserved(session, new_job.id) == 60
