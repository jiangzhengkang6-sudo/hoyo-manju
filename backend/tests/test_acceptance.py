"""MVP 验收用例：故事输入 → 剧本确认 → 角色/画风 → 分镜图 → 视频 → 音频 → 合成 → 预览 → 导出。"""

from __future__ import annotations

import io
import json
import zipfile

from conftest import STORY_TEXT, drain, wait_job
from fastapi.testclient import TestClient

from app.core.config import settings


def _resume(client: TestClient, episode_id: str, gate: str, decision: str = "approve",
            payload: dict | None = None, checkpoint_id: str | None = None,
            target_version: int | None = None) -> dict:
    current = client.get(f"/api/v1/episodes/{episode_id}/workflow").json()
    body = {
        "workflow_run_id": current["id"],
        "gate": gate,
        "decision": decision,
        "payload": payload or {},
    }
    if checkpoint_id is not None:
        body["checkpoint_id"] = checkpoint_id
    else:
        body["checkpoint_id"] = current.get("checkpoint_id")
    if target_version is not None:
        body["target_version"] = target_version
    response = client.post(f"/api/v1/episodes/{episode_id}/workflow/resume", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _run_to_shot_gate(client: TestClient, episode: dict, project: dict) -> dict:
    """执行到分镜图确认门控。"""
    episode_id = episode["id"]
    story = client.post(
        f"/api/v1/episodes/{episode_id}/story",
        json={"input_type": "idea", "source_text": STORY_TEXT},
    )
    assert story.status_code == 200, story.text

    started = client.post(f"/api/v1/episodes/{episode_id}/workflow/start", json={})
    assert started.status_code == 200, started.text
    data = started.json()
    assert data["pending_gate"] == "gate_script_confirmation", data

    script = client.get(f"/api/v1/episodes/{episode_id}/script").json()
    assert script["story_json"]["shots"], script
    assert len(script["story_json"]["shots"]) >= 6

    data = _resume(client, episode_id, "gate_script_confirmation")
    assert data["pending_gate"] == "gate_character_confirmation", data

    characters = client.get(f"/api/v1/projects/{project['id']}/characters").json()["items"]
    assert characters, "未生成角色资产"
    for character in characters:
        assets = client.get(f"/api/v1/characters/{character['id']}/assets").json()["items"]
        usable = [a for a in assets if a["status"] != "REJECTED"]
        assert usable, f"角色 {character['name']} 没有可用参考图"
        confirmed = client.post(
            f"/api/v1/characters/{character['id']}/confirm", json={"media_id": usable[0]["id"]}
        )
        assert confirmed.status_code == 200, confirmed.text

    data = _resume(client, episode_id, "gate_character_confirmation")
    assert data["pending_gate"] == "gate_shot_confirmation", data
    return data


def _confirm_all_shots(client: TestClient, episode_id: str) -> None:
    shots = client.get(f"/api/v1/episodes/{episode_id}/shots").json()["items"]
    assert shots
    for shot in shots:
        usable = [c for c in shot["candidates"] if c["usable"]]
        assert usable, f"分镜 {shot['shot_no']} 没有可用候选图"
        confirmed = client.post(
            f"/api/v1/shots/{shot['id']}/confirm", json={"media_id": usable[0]["id"]}
        )
        assert confirmed.status_code == 200, confirmed.text


def test_full_mvp_loop(client: TestClient, worker, project: dict, episode: dict) -> None:
    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)
    _confirm_all_shots(client, episode_id)

    data = _resume(client, episode_id, "gate_shot_confirmation")
    assert data["pending_gate"] == "gate_video_cost_confirmation", data
    quote = data["state"]["video_quote"]
    assert quote["estimate_cents"] > 0

    data = _resume(client, episode_id, "gate_video_cost_confirmation")
    assert data["pending_gate"] == "gate_video_confirmation", data

    shots = client.get(f"/api/v1/episodes/{episode_id}/shots").json()["items"]
    for shot in shots:
        detail = client.get(f"/api/v1/shots/{shot['id']}/video").json()
        assert detail["video_status"] in {"CANDIDATES_READY", "READY", "SUCCEEDED", "FAILED"}, detail
        history = detail["history"]
        assert history, f"分镜 {shot['shot_no']} 没有视频产物"
        response = client.post(
            f"/api/v1/shots/{shot['id']}/video/confirm",
            json={"media_id": history[-1]["media_id"]},
        )
        assert response.status_code == 200, response.text

    data = _resume(client, episode_id, "gate_video_confirmation")
    assert data["pending_gate"] == "gate_export_confirmation", data
    render = data["state"].get("render") or {}
    assert render.get("ok") is True, render

    data = _resume(
        client, episode_id, "gate_export_confirmation",
        payload={"preset": "720p_9x16", "package": True},
    )
    assert data["status"] == "COMPLETED", data

    episode_state = client.get(f"/api/v1/episodes/{episode_id}").json()
    assert episode_state["stage"] == "EXPORTED", episode_state

    exports = client.get(f"/api/v1/episodes/{episode_id}/exports").json()["items"]
    kinds = {e["kind"] for e in exports}
    assert "preview" in kinds, kinds
    assert "final" in kinds, kinds
    assert "package" in kinds, kinds

    final = next(e for e in exports if e["kind"] == "final")
    assert final["status"] == "SUCCEEDED"
    assert final["media"]["size_bytes"] > 0

    download = client.get(f"/api/v1/exports/{final['id']}/download", follow_redirects=True)
    assert download.status_code == 200, (download.status_code, download.text[:600], final["media"])
    assert len(download.content) > 1000

    package = next(e for e in exports if e["kind"] == "package")
    package_download = client.get(f"/api/v1/exports/{package['id']}/download",
                                  follow_redirects=True)
    assert package_download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(package_download.content)) as archive:
        names = archive.namelist()
        assert "manifest.json" in names
        assert "script.json" in names
        assert "rights.json" in names
        assert any(n.startswith("images/") for n in names)
        assert any(n.startswith("videos/") for n in names)
        assert any(n.startswith("audio/") for n in names)
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["episode_id"] == episode_id
        assert manifest["assets"], "manifest 缺少资产清单"
        assert all(a.get("sha256") for a in manifest["assets"]), "manifest 缺少 sha256"

    subtitles = client.get(f"/api/v1/episodes/{episode_id}/subtitles").json()["items"]
    assert subtitles, "未生成字幕"

    tracks = client.get(f"/api/v1/episodes/{episode_id}/tts").json()["items"]
    assert tracks, "未生成台词音频"

    quota = client.get("/api/v1/quota").json()
    assert quota["used_cents"] > 0
    assert quota["reserved_cents"] == 0, quota

    ledger = client.get(f"/api/v1/cost/ledger?episode_id={episode_id}").json()
    kinds_in_ledger = {item["kind"] for item in ledger["items"]}
    assert {"RESERVE", "SETTLE"} <= kinds_in_ledger, kinds_in_ledger

    approvals = client.get(f"/api/v1/episodes/{episode_id}/approvals").json()["items"]
    gates = {a["stage"] for a in approvals}
    assert "gate_character_confirmation" in gates
    assert "gate_shot_confirmation" in gates


def test_transition_shortens_timeline_and_renders(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    """FR-061：转场让相邻镜头重叠、总时长缩短，且能真正合成出片。"""
    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)
    _confirm_all_shots(client, episode_id)

    shots = client.get(f"/api/v1/episodes/{episode_id}/shots").json()["items"]
    shots.sort(key=lambda s: s["shot_no"])
    assert len(shots) >= 3

    baseline = client.get(f"/api/v1/episodes/{episode_id}/timeline").json()
    base_total = baseline["total_duration_ms"]
    assert baseline["tracks"]["video"][1]["start_ms"] == shots[0]["duration_ms"]

    # 镜 2 用 300ms 淡入淡出，镜 3 用 500ms 左擦除
    for shot, spec in ((shots[1], {"type": "fade", "duration_ms": 300}),
                       (shots[2], {"type": "wipeleft", "duration_ms": 500})):
        patched = client.patch(f"/api/v1/shots/{shot['id']}", json={"transition": spec})
        assert patched.status_code == 200, patched.text

    timeline = client.get(f"/api/v1/episodes/{episode_id}/timeline").json()
    entries = timeline["tracks"]["video"]
    assert entries[1]["start_ms"] == shots[0]["duration_ms"] - 300
    assert entries[1]["transition"]["type"] == "fade"
    assert entries[2]["start_ms"] == entries[1]["end_ms"] - 500
    assert entries[2]["transition"]["xfade"] == "wipeleft"
    # 总时长 = 各镜之和 - 转场重叠之和
    assert timeline["total_duration_ms"] == base_total - 800

    # 转场不影响画面内容，因此不应把分镜标记为过期
    assert not entries[1].get("stale"), entries[1]

    validation = client.get(f"/api/v1/episodes/{episode_id}/timeline/validate").json()
    assert validation["valid"] is True, validation
    codes = {w["code"] for w in validation["warnings"]}
    assert "TRANSITION_UNKNOWN" not in codes

    # 真的能合成：走完整流程到导出
    _resume(client, episode_id, "gate_shot_confirmation")
    _resume(client, episode_id, "gate_video_cost_confirmation")
    for shot in shots:
        detail = client.get(f"/api/v1/shots/{shot['id']}/video").json()
        client.post(
            f"/api/v1/shots/{shot['id']}/video/confirm",
            json={"media_id": detail["history"][-1]["media_id"]},
        )
    data = _resume(client, episode_id, "gate_video_confirmation")
    assert data["pending_gate"] == "gate_export_confirmation", data
    render = data["state"].get("render") or {}
    assert render.get("ok") is True, render

    preview = next(
        e for e in client.get(f"/api/v1/episodes/{episode_id}/exports").json()["items"]
        if e["kind"] == "preview"
    )
    assert preview["status"] == "SUCCEEDED", preview
    duration_ms = preview["media"].get("duration_ms")
    assert duration_ms, preview["media"]
    # 允许一帧（40ms）误差
    assert abs(duration_ms - timeline["total_duration_ms"]) < 120, (duration_ms, timeline["total_duration_ms"])


def test_transition_validation_blocks_unknown_and_too_long(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    """FR-060：非法转场要给出可定位的错误，而不是等到合成才炸。"""
    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)
    _confirm_all_shots(client, episode_id)
    shots = sorted(
        client.get(f"/api/v1/episodes/{episode_id}/shots").json()["items"],
        key=lambda s: s["shot_no"],
    )

    client.patch(f"/api/v1/shots/{shots[1]['id']}", json={"transition": {"type": "bogus", "duration_ms": 300}})
    validation = client.get(f"/api/v1/episodes/{episode_id}/timeline/validate").json()
    assert validation["valid"] is False
    problem = next(e for e in validation["errors"] if e["code"] == "TRANSITION_UNKNOWN")
    assert problem["shot_no"] == shots[1]["shot_no"]

    # 超过「相邻两镜较短者的一半」
    too_long = shots[1]["duration_ms"]
    client.patch(
        f"/api/v1/shots/{shots[1]['id']}",
        json={"transition": {"type": "fade", "duration_ms": too_long}},
    )
    validation = client.get(f"/api/v1/episodes/{episode_id}/timeline/validate").json()
    assert validation["valid"] is False
    assert any(e["code"] == "TRANSITION_TOO_LONG" for e in validation["errors"])

    # 清除转场后恢复合法
    client.patch(f"/api/v1/shots/{shots[1]['id']}", json={"transition": None})
    validation = client.get(f"/api/v1/episodes/{episode_id}/timeline/validate").json()
    assert validation["valid"] is True, validation


def test_worker_restart_no_double_billing(client: TestClient, worker, project: dict,
                                          episode: dict, monkeypatch) -> None:
    """任务重放：同一 idempotency_key 不会重复创建任务，重复执行不重复计费。"""
    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)

    shots = client.get(f"/api/v1/episodes/{episode_id}/shots").json()["items"]
    first = shots[0]
    body = {"shot_nos": [first["shot_no"]], "candidates_per_shot": 2}

    media_before = len(
        client.get(f"/api/v1/episodes/{episode_id}/media?kind=image").json()["items"]
    )

    # 把幂等窗口放到足够大，确保两次「连点」一定落在同一个时间桶里（生产默认 5 秒）。
    monkeypatch.setattr(settings, "idempotency_window_seconds", 3600)

    # 模拟用户双击：两次请求背靠背发出，必须命中同一个任务
    r1 = client.post(f"/api/v1/episodes/{episode_id}/shots/generate-images", json=body)
    assert r1.status_code == 200, r1.text
    job_a = r1.json()["job"]
    r2 = client.post(f"/api/v1/episodes/{episode_id}/shots/generate-images", json=body)
    assert r2.status_code == 200, r2.text
    job_b = r2.json()["job"]
    assert job_b["id"] == job_a["id"], "幂等键未生效，重复创建了任务"

    assert wait_job(client, worker, job_a["id"])["status"] == "SUCCEEDED"
    drain(worker, timeout=60)

    # 只产出一份候选素材（= 只真正生成了一次）
    media_after = len(
        client.get(f"/api/v1/episodes/{episode_id}/media?kind=image").json()["items"]
    )
    assert media_after - media_before == body["candidates_per_shot"], "重复请求产生了重复素材"

    # 该集只应存在一个本次窗口的 IMAGE_GENERATION 任务
    same_window = [
        j
        for j in client.get(
            f"/api/v1/jobs?episode_id={episode_id}&type=IMAGE_GENERATION"
        ).json()["items"]
        if j["id"] == job_a["id"]
    ]
    assert len(same_window) == 1

    # 失败重试：新任务带 parent_job_id，且使用 #retry 后缀的幂等键
    job_detail = client.get(f"/api/v1/jobs/{job_a['id']}").json()
    assert job_detail["status"] == "SUCCEEDED"
    assert job_detail["provider_calls"], "未记录供应商调用"

    retried = client.post(f"/api/v1/jobs/{job_a['id']}/retry")
    assert retried.status_code == 200, retried.text
    retry_job_id = retried.json()["job"]["id"]
    assert retried.json()["job"]["parent_job_id"] == job_a["id"]
    assert wait_job(client, worker, retry_job_id)["status"] in {"SUCCEEDED", "FAILED"}


def test_idempotency_window_buckets() -> None:
    """幂等键的时间桶：同桶复用（防连点），跨桶新建（允许重新生成）。"""
    from app.services import jobs as jobs_service

    same_a = jobs_service.build_windowed_key("epi", "IMAGE_GENERATION", "1", window_seconds=3600)
    same_b = jobs_service.build_windowed_key("epi", "IMAGE_GENERATION", "1", window_seconds=3600)
    assert same_a == same_b

    other = jobs_service.build_windowed_key("epi", "IMAGE_GENERATION", "1", window_seconds=1)
    assert other != same_a, "不同窗口应产生不同幂等键，否则无法重新生成"

    distinct = jobs_service.build_windowed_key("epi", "IMAGE_GENERATION", "2", window_seconds=3600)
    assert distinct != same_a, "不同分镜应产生不同幂等键"


def test_checkpoint_stale_rejected(client: TestClient, worker, project: dict,
                                   episode: dict) -> None:
    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)

    response = client.post(
        f"/api/v1/episodes/{episode_id}/workflow/resume",
        json={
            "workflow_run_id": client.get(f"/api/v1/episodes/{episode_id}/workflow").json()["id"],
            "gate": "gate_shot_confirmation",
            "decision": "approve",
            "checkpoint_id": "ckpt-does-not-exist",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CHECKPOINT_STALE"


def test_failure_retry_and_recovery(client: TestClient, worker, project: dict,
                                    episode: dict) -> None:
    """供应商超时 → 任务可失败、可重试、状态可恢复。"""
    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)

    shots = client.get(f"/api/v1/episodes/{episode_id}/shots").json()["items"]
    shot = shots[0]
    media = [c for c in shot["candidates"] if c["usable"]][0]
    assert client.post(f"/api/v1/shots/{shot['id']}/confirm",
                       json={"media_id": media["id"]}).status_code == 200

    # 直接构造一个必然失败的任务（模拟供应商超时）
    from app.db.session import session_scope
    from app.services import executor as executor_service
    from app.services import jobs as jobs_service

    with session_scope() as session:
        job = jobs_service.create_job(
            session,
            type="VIDEO_GENERATION",
            idempotency_key="test#timeout#1",
            episode_id=episode_id,
            stage="video_generation",
            input_json={"shot_nos": [shot["shot_no"]], "simulate": "timeout"},
            input_summary={"shot_no": shot["shot_no"]},
            max_attempts=1,
        )
        job_id = job.id
        session.flush()

    # 未结束的任务不允许重试（状态冲突），避免重复计费
    conflict = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert conflict.status_code == 409, conflict.text

    drain(worker, timeout=60)

    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["status"] in {"FAILED", "SUCCEEDED"}, detail
    if detail["status"] == "FAILED":
        assert detail["error_code"]
        retried = client.post(f"/api/v1/jobs/{job_id}/retry").json()["job"]
        assert retried["status"] in {"QUEUED", "RUNNING", "RETRYING", "CREATED"}, retried
        assert retried["idempotency_key"].startswith("test#timeout#1#retry")

    # 剧集不会因为一个任务失败而无法查询/恢复
    overview = client.get(f"/api/v1/episodes/{episode_id}/overview")
    assert overview.status_code == 200
    workflow = client.get(f"/api/v1/episodes/{episode_id}/workflow")
    assert workflow.status_code == 200
    assert workflow.json()["pending_gate"] == "gate_shot_confirmation"


def test_cancel_job(client: TestClient, worker, project: dict, episode: dict) -> None:
    from app.db.session import session_scope
    from app.services import jobs as jobs_service

    with session_scope() as session:
        job = jobs_service.create_job(
            session,
            type="IMAGE_GENERATION",
            idempotency_key="test#cancel#1",
            episode_id=episode["id"],
            stage="image_generation",
            input_json={"shot_nos": [1]},
            input_summary={},
        )
        job_id = job.id
        session.flush()

    cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["job"]["status"] == "CANCELLED"
    assert cancelled.json()["released_cents"] == 0


def test_auto_retry_schedules_retrying(client: TestClient, worker, project: dict,
                                       episode: dict) -> None:
    """FR-072 / §10.2：可重试错误且还有剩余次数时 → RETRYING（退避后重入队），不直接判死。"""
    from app.db.session import session_scope
    from app.services import jobs as jobs_service

    with session_scope() as session:
        job = jobs_service.create_job(
            session,
            type="VIDEO_GENERATION",
            idempotency_key="test#autoretry#1",
            episode_id=episode["id"],
            stage="video_generation",
            input_json={"shot_nos": [1], "simulate": "timeout"},
            input_summary={"shot_no": 1},
            max_attempts=2,
            auto_start=False,
        )
        job_id = job.id
        # 可重试错误 + 还有剩余次数 → 安排自动重试
        assert jobs_service.schedule_auto_retry(session, job, retryable=True) is True
        assert job.status == "RETRYING", job.status
        assert job.attempt == 2, job.attempt
        assert job.next_attempt_at is not None
        session.flush()

    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["status"] == "RETRYING", detail
    assert detail["attempt"] == 2 and detail["max_attempts"] == 2
    assert detail["next_attempt_at"], detail

    # 次数用尽 / 不可重试 → 不再安排
    with session_scope() as session:
        job = jobs_service.get_job(session, job_id)
        assert jobs_service.schedule_auto_retry(session, job, retryable=True) is False
        job.attempt = 1
        assert jobs_service.schedule_auto_retry(session, job, retryable=False) is False


def test_auto_retry_then_fail_exhausts_attempts(client: TestClient, worker, project: dict,
                                                episode: dict) -> None:
    """一直失败的任务最终仍会 FAILED，但 attempt 已递增（证明自动重试真的跑过）。"""
    from app.core.errors import ProviderTimeout
    from app.db.session import session_scope
    from app.services import executor as executor_service
    from app.services import jobs as jobs_service

    # 注册一个必然以「可重试错误」失败的任务类型，避免依赖供应商/素材状态
    @executor_service.handler("TEST_FLAKY")
    def _flaky(_session, _job):  # pragma: no cover - 仅测试用
        raise ProviderTimeout("模拟供应商超时")

    try:
        with session_scope() as session:
            job = jobs_service.create_job(
                session,
                type="TEST_FLAKY",
                idempotency_key="test#autoretry#2",
                episode_id=episode["id"],
                stage="test_flaky",
                input_json={},
                input_summary={},
                max_attempts=2,
            )
            job_id = job.id

        final = wait_job(client, worker, job_id, timeout=90)
        assert final["status"] == "FAILED", final
        assert final["attempt"] == 2, final
        assert final["error_code"] == "PROVIDER_TIMEOUT", final
    finally:
        executor_service._HANDLERS.pop("TEST_FLAKY", None)


def test_expire_stale_jobs_marks_expired(client: TestClient, episode: dict) -> None:
    """FR-070：长期停留在非终态的任务标记为 EXPIRED。"""
    import datetime as dt

    from app.db.models import Job
    from app.db.session import session_scope
    from app.services import jobs as jobs_service

    with session_scope() as session:
        job = jobs_service.create_job(
            session,
            type="IMAGE_GENERATION",
            idempotency_key="test#expire#1",
            episode_id=episode["id"],
            stage="image_generation",
            input_json={"shot_nos": [1]},
            input_summary={},
            auto_start=False,
        )
        job_id = job.id
        # 把创建时间倒推到很久以前，模拟长期无人接管
        job.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
        session.flush()
        expired = jobs_service.expire_stale_jobs(session, ttl_seconds=3600)
        assert expired >= 1, expired

    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert detail["status"] == "EXPIRED", detail
    assert detail["error_code"] == "JOB_EXPIRED"


def _shot_versions(client: TestClient, episode_id: str) -> dict[int, int]:
    items = client.get(f"/api/v1/episodes/{episode_id}/shots").json()["items"]
    return {s["shot_no"]: s["current_version"] for s in items}


def test_regenerate_scope_and_downstream(client: TestClient, worker, project: dict,
                                         episode: dict) -> None:
    """FR-014：默认只改选中分镜（未选中不产生新版本）；勾选「重新规划后续镜头」才连带下游。"""
    episode_id = episode["id"]
    client.post(
        f"/api/v1/episodes/{episode_id}/story",
        json={"input_type": "idea", "source_text": STORY_TEXT},
    )
    started = client.post(f"/api/v1/episodes/{episode_id}/story/generate").json()
    wait_job(client, worker, started["id"])

    before = _shot_versions(client, episode_id)
    assert len(before) >= 6, before

    # 只重生成第 1 镜
    job = client.post(
        f"/api/v1/episodes/{episode_id}/shots/regenerate", json={"shot_nos": [1]}
    ).json()
    final = wait_job(client, worker, job["id"])
    assert final["status"] == "SUCCEEDED", final
    assert final["result_ref"]["changed_shot_nos"] == [1], final["result_ref"]

    after = _shot_versions(client, episode_id)
    assert after[1] == before[1] + 1, (before, after)
    for no in before:
        if no != 1:
            assert after[no] == before[no], f"未选中的第 {no} 镜不该产生新版本"

    # 勾选「重新规划后续镜头」：从第 3 镜起连带重排
    job = client.post(
        f"/api/v1/episodes/{episode_id}/shots/regenerate",
        json={"shot_nos": [3], "replan_downstream": True},
    ).json()
    final = wait_job(client, worker, job["id"])
    assert final["status"] == "SUCCEEDED", final
    changed = final["result_ref"]["changed_shot_nos"]
    assert 3 in changed and len(changed) > 1, changed

    after2 = _shot_versions(client, episode_id)
    assert after2[3] == after[3] + 1
    assert after2[1] == after[1], "第 3 镜之前的分镜不该被改动"
    for no in changed:
        assert after2[no] == after[no] + 1, f"下游第 {no} 镜应被重排"

    # 剧本版本也随之上移（每次真正有改动才落新版本）
    versions = client.get(f"/api/v1/episodes/{episode_id}/story/versions").json()["items"]
    assert versions[0]["version"] >= 3, versions[0]


def test_script_validation_blocks_bad_input(client: TestClient, episode: dict) -> None:
    bad = {
        "shots": [
            {"shot_no": 2, "duration_ms": 4000, "characters": ["不存在的人"], "dialogue": []},
            {"shot_no": 1, "duration_ms": 4000, "characters": [], "dialogue": []},
        ]
    }
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/script/validate", json={"story_json": bad}
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["errors"]


def test_safety_check(client: TestClient) -> None:
    response = client.post("/api/v1/safety/check", json={"text": "血腥暴力与色情内容", "scope": "input"})
    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] is False
    assert body["categories"]


def test_events_sse_history(client: TestClient, worker, project: dict, episode: dict) -> None:
    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)
    history = client.get(f"/api/v1/events/history?episode_id={episode_id}").json()["items"]
    assert history
    types = {item["type"] for item in history}
    assert "workflow.gate_waiting" in types
    assert history == sorted(history, key=lambda i: i["id"])
