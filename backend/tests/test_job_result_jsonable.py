"""任务结果必须能写进 JSON 列：否则任务会「产物已出、状态永远运行中」。

线上现象：BGM 生成完成后，音轨和素材都已经落库、任务却一直显示 RUNNING 100%，
重试按钮也点不动。根因是 `generate_ambience` 的结果里嵌了
`media_service.to_dict()`，其中 `created_at` 是 datetime；写 `Job.result_ref`
（JSON 列）时 commit 抛 `TypeError`，而那次 commit 同时带着「任务已完成」的状态，
事务一回滚，状态就停在 RUNNING。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi.testclient import TestClient

from app.agents import audio as audio_agent
from app.db.models import AudioTrack, Episode, Job
from app.db.session import session_scope
from app.services import executor as executor_service
from app.services import jobs as jobs_service
from app.services import media as media_service


def test_jsonable_turns_datetime_into_utc_iso() -> None:
    naive = dt.datetime(2026, 10, 26, 9, 45, 50)
    payload = jobs_service.jsonable({"at": naive, "nested": [{"again": naive}], "n": 1})

    assert payload["at"] == "2026-10-26T09:45:50+00:00", "naive 时间按 UTC 处理"
    assert payload["nested"][0]["again"] == payload["at"]
    json.dumps(payload)  # 不抛 TypeError 才算过


def test_media_dict_emits_iso_string(client: TestClient, project: dict) -> None:
    with session_scope() as session:
        asset = media_service.save_media(
            session,
            content=b"RIFF0000WAVEfmt ",
            kind="audio",
            role="bgm",
            filename="probe.wav",
            owner_type="project",
            owner_id=project["id"],
            project_id=project["id"],
            mime_type="audio/wav",
        )
        session.flush()
        data = media_service.to_dict(asset)

    assert isinstance(data["created_at"], str), "JSON 列里不能留 datetime"
    assert data["created_at"].endswith("+00:00")
    json.dumps(data)


def test_bgm_job_reaches_succeeded_instead_of_stuck_running(
    client: TestClient, project: dict, episode: dict
) -> None:
    """端到端：mock 供应商跑完一单 BGM，任务必须落到 SUCCEEDED。"""
    with session_scope() as session:
        job = jobs_service.create_job(
            session,
            type="BGM_GENERATION",
            idempotency_key=f"test#bgm-jsonable#{uuid.uuid4().hex[:8]}",
            episode_id=episode["id"],
            stage="audio_generation",
            input_json={
                "prompt": "雨夜巷口，雨点打在铁皮棚上，低沉的弦乐缓慢铺底",
                "seconds": 6,
                "volume_db": -14,
            },
            input_summary={"seconds": 6},
            max_attempts=1,
            reserve_cents=6,
        )
        session.flush()
        job_id = job.id

    with session_scope() as session:
        job = session.get(Job, job_id)
        executor_service.run_job(session, job, allow_auto_retry=False)
        session.flush()

        assert job.status == "SUCCEEDED", job.error_message or job.error_detail
        assert job.progress == 100
        assert job.finished_at is not None
        assert isinstance(job.result_ref, dict), "结果必须真的写进去了"
        assert job.result_ref["track"]["media"]["created_at"], "media 时间已转字符串"
        assert int(job.cost_settled_cents or 0) > 0

    with session_scope() as session:
        tracks = list(
            session.query(AudioTrack).filter(AudioTrack.episode_id == episode["id"]).all()
        )
        assert len(tracks) == 1
        episode_row = session.get(Episode, episode["id"])
        assert episode_row is not None


def test_short_ambience_fades_are_capped_by_duration() -> None:
    """4 秒的单镜头垫乐不能套用整集的 800/1500ms 淡入淡出。"""
    fade_in, fade_out = audio_agent._fit_fades(4000, 800, 1500)
    assert fade_in == 600
    assert fade_out == 1000

    # 长音频保持原样
    assert audio_agent._fit_fades(30000, 800, 1500) == (800, 1500)
