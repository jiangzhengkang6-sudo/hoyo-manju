"""生成任务的「随时停止」：取消后不再往下一镜/下一个资产花钱。

语义：
- 排队中取消 → 立即 CANCELLED，预留全额释放；
- 执行中取消 → 当前这一张/一条会跑完（供应商已经在算钱），之后不再提交；
- 已生成的产物保留，结算按**实际完成的部分**记账。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.agents import image as image_agent
from app.agents import video as video_agent
from app.db.models import Job, Shot
from app.db.session import session_scope
from app.services import cost as cost_service
from app.services import executor as executor_service
from app.services import jobs as jobs_service


def _two_shots(episode_id: str) -> list[str]:
    with session_scope() as session:
        shots = [
            Shot(
                episode_id=episode_id, shot_no=no, duration_ms=4000,
                shot_json={"action": f"动作{no}", "image_prompt": f"雨夜街道 {no}"},
                stage="CANDIDATES_READY",
            )
            for no in (1, 2)
        ]
        session.add_all(shots)
        session.flush()
        return [s.id for s in shots]


def test_image_batch_stops_before_next_shot(client: TestClient, project: dict,
                                            episode: dict) -> None:
    _two_shots(episode["id"])
    checks = {"n": 0}

    def probe() -> bool:
        checks["n"] += 1
        return checks["n"] > 1        # 第一镜检查时放行，第二镜之前收到取消

    with session_scope() as session:
        from app.db.models import Episode

        ep = session.get(Episode, episode["id"])
        shots = list(session.query(Shot).filter(Shot.episode_id == ep.id).order_by(Shot.shot_no))
        result = image_agent.generate_for_shots(
            session, episode=ep, shots=shots, candidates_per_shot=1, should_cancel=probe
        )
        session.flush()
        assert result["cancelled"] is True
        assert result["count"] == 1                 # 只生成了第一镜
        assert len(result["media_ids"]) == 1
        assert int(result["cost_cents"]) > 0        # 已生成的那张照常计费

        shot2 = session.get(Shot, shots[1].id)
        assert shot2.image_status != "CANDIDATES_READY"


def test_video_batch_does_not_submit_after_cancel(client: TestClient, project: dict,
                                                  episode: dict) -> None:
    _two_shots(episode["id"])
    with session_scope() as session:
        from app.db.models import Episode

        ep = session.get(Episode, episode["id"])
        shots = list(session.query(Shot).filter(Shot.episode_id == ep.id))
        result = video_agent.generate_for_shots(
            session, episode=ep, shots=shots, should_cancel=lambda: True
        )
        assert result["cancelled"] is True
        assert result["count"] == 0
        assert result["media_ids"] == []


def test_cancelled_job_ends_as_cancelled_and_releases_quota(
    client: TestClient, project: dict, episode: dict
) -> None:
    _two_shots(episode["id"])
    with session_scope() as session:
        job = jobs_service.create_job(
            session,
            type="IMAGE_GENERATION",
            idempotency_key=f"test#cancel#{uuid.uuid4().hex[:8]}",
            episode_id=episode["id"],
            stage="image_generation",
            input_json={"shot_nos": [1, 2], "candidates_per_shot": 1},
            input_summary={"shots": 2},
            max_attempts=1,
            reserve_cents=100,
        )
        job.cancel_requested = True          # 用户已经点了停止
        session.flush()
        job_id = job.id

    with session_scope() as session:
        job = session.get(Job, job_id)
        executor_service.run_job(session, job, allow_auto_retry=False)
        session.flush()
        assert job.status == "CANCELLED"
        assert int(job.cost_settled_cents or 0) == 0
        assert cost_service.outstanding_reserved(session, job_id) == 0


def test_cancel_is_idempotent_while_cancelling(client: TestClient, project: dict,
                                               episode: dict) -> None:
    _two_shots(episode["id"])
    with session_scope() as session:
        job = jobs_service.create_job(
            session,
            type="IMAGE_GENERATION",
            idempotency_key=f"test#cancel2#{uuid.uuid4().hex[:8]}",
            episode_id=episode["id"],
            stage="image_generation",
            input_json={"shot_nos": [1]},
            input_summary={"shots": 1},
            max_attempts=1,
            reserve_cents=50,
        )
        jobs_service.set_status(session, job, "RUNNING")
        first = jobs_service.cancel_job(session, job)
        second = jobs_service.cancel_job(session, job)     # 连点两次不该 409
        assert first["status"] == second["status"] == "CANCELLING"
        assert "跑完" in first["note"]
