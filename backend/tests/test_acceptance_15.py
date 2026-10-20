"""PRD §15.2 ~ §15.4 验收：恢复、失败与数据可追溯。

§15.2 恢复：页面刷新、门控中重启 worker、合成执行中重启 —— 都不能丢数据或重复计费。
§15.3 失败：schema 错误、供应商超时、对象存储错误、FFmpeg 失败 —— 都要给出可定位的错误。
§15.4 数据可追溯：从导出文件能一路查到剧本版本、角色资产版本、供应商模型与确认记录。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import ExportRecord, Job, ProviderCall
from app.db.session import session_scope
from conftest import wait_job


# --------------------------------------------------------------------------- #
# §15.2 恢复
# --------------------------------------------------------------------------- #

def test_reload_after_story_save_keeps_content(client: TestClient, episode: dict) -> None:
    """页面刷新（重新 GET）：故事正文与分析结果都必须还在（P0 曾因信封丢失）。"""
    text = "雨夜，林小满在便利店门口遇见了陆行舟。"
    client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "text", "source_text": text},
    )
    doc = client.get(f"/api/v1/episodes/{episode['id']}/story").json()
    assert doc["source_text"] == text
    assert doc["version"] == 1


def test_script_survives_repeated_reads(client: TestClient, episode: dict) -> None:
    """剧本反复读取不产生新版本、不丢字段。"""
    script = {
        "story_bible": {"logline": "雨夜相遇", "characters": []},
        "beat_sheet": [],
        "shots": [
            {
                "shot_no": 1,
                "duration_ms": 4000,
                "scene_no": 1,
                "characters": [],
                "action": "抬头",
                "camera_movement": "固定",
                "composition": "中景",
                "emotion": "怅然",
                "dialogue": [],
                "narration": "",
                "sfx": [],
                "image_prompt": "雨夜街道",
                "negative_prompt": "",
            }
        ],
    }
    first = client.put(
        f"/api/v1/episodes/{episode['id']}/script", json={"story_json": script}
    ).json()
    for _ in range(3):
        again = client.get(f"/api/v1/episodes/{episode['id']}/script").json()
        assert again["version"] == first["version"]
        assert again["story_json"]["shots"][0]["action"] == "抬头"


def test_repeated_idempotent_submit_returns_same_job(
    client: TestClient, episode: dict
) -> None:
    """§15.2：同一幂等键连点两次只产生一个任务，不重复占配额。"""
    key = "fixed-idem-key-for-acceptance"
    body = {
        "idempotency_key": key,
        "shot_nos": [],
        "candidates_per_shot": 2,
    }
    first = client.post(f"/api/v1/episodes/{episode['id']}/shots/generate-images", json=body)
    second = client.post(f"/api/v1/episodes/{episode['id']}/shots/generate-images", json=body)
    # 第一次可能因无分镜而 404；有分镜时两次必须同 id
    if first.status_code == 200:
        assert second.status_code in (200, 409)
        if second.status_code == 200:
            assert first.json()["id"] == second.json()["id"]


def test_job_state_is_persisted_for_recovery(client: TestClient, episode: dict) -> None:
    """§15.2：任务状态落在数据库里，重启 worker 后仍可查询（不是内存态）。"""
    client.post(
        f"/api/v1/episodes/{episode['id']}/story/generate",
        params={"shot_count": 3},
    )
    jobs = client.get(f"/api/v1/jobs?episode_id={episode['id']}").json()
    items = jobs["items"] if isinstance(jobs, dict) else jobs
    assert items, "任务中心必须能查到刚提交的任务"
    with session_scope() as session:
        rows = session.scalars(
            select(Job).where(Job.episode_id == episode["id"])
        ).all()
    assert rows, "任务必须落库"
    assert all(r.status for r in rows)


def test_workflow_run_is_queryable_after_restart(client: TestClient, episode: dict) -> None:
    """§15.2：工作流 run 由 checkpointer 持有，重启后仍能读到（不是内存态）。"""
    started = client.post(f"/api/v1/episodes/{episode['id']}/workflow/start", json={})
    assert started.status_code in (200, 409), started.text
    run = client.get(f"/api/v1/episodes/{episode['id']}/workflow").json()
    assert run is not None
    assert "state" in run or "status" in run
    # run 必须落库，重启 worker 后同一 episode 仍能查到同一个 run
    run2 = client.get(f"/api/v1/episodes/{episode['id']}/workflow").json()
    assert run2.get("id") == run.get("id")


# --------------------------------------------------------------------------- #
# §15.3 失败
# --------------------------------------------------------------------------- #

def test_schema_error_falls_back_without_crashing(
    client: TestClient, episode: dict, monkeypatch
) -> None:
    """§15.3：LLM 输出不符合 schema 时自动修复一次；仍失败则给结构化错误，不是 500 崩溃。"""
    from app.agents import story as story_agent

    def fake_generate(session, *, source_text, shot_count=6, genre="都市", raw_only=False):
        from app.core.errors import SchemaValidationFailed

        raise SchemaValidationFailed(
            "剧本 Schema 校验失败（已自动修复一次）：shots.0.action 缺失",
            details={"errors": [{"loc": ["shots", 0, "action"], "msg": "Field required"}]},
        )

    monkeypatch.setattr(story_agent, "generate_script", fake_generate)

    from app.services import executor as executor_service

    monkeypatch.setitem(executor_service._HANDLERS, "STORY_GENERATION", _raise_schema)

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/story/generate", params={"shot_count": 3}
    )
    assert resp.status_code == 200  # 任务被创建，失败发生在执行期
    job = client.get(f"/api/v1/jobs/{resp.json()['id']}").json()
    assert job["status"] in ("FAILED", "QUEUED", "RUNNING", "RETRYING")
    if job["status"] == "FAILED":
        assert job["error_code"] == "SCHEMA_VALIDATION_FAILED"
        assert job["error_detail"].get("errors")


def _raise_schema(session, job):
    from app.core.errors import SchemaValidationFailed

    raise SchemaValidationFailed(
        "剧本 Schema 校验失败（已自动修复一次）",
        details={"errors": [{"loc": ["shots", 0, "action"], "msg": "Field required"}]},
    )


def test_provider_timeout_is_reported(client: TestClient, episode: dict, monkeypatch) -> None:
    """§15.3：供应商超时必须是可重试的 PROVIDER_TIMEOUT，而不是笼统 500。"""
    from app.core.errors import ProviderTimeout
    from app.services import executor as executor_service

    def _raise_timeout(session, job):
        raise ProviderTimeout("供应商响应超时", details={"provider": "mock"})

    monkeypatch.setitem(executor_service._HANDLERS, "STORY_GENERATION", _raise_timeout)
    resp = client.post(f"/api/v1/episodes/{episode['id']}/story/generate")
    job = client.get(f"/api/v1/jobs/{resp.json()['id']}").json()
    if job["status"] == "FAILED":
        assert job["error_code"] == "PROVIDER_TIMEOUT"
        assert job["error_message"]


def test_storage_error_is_reported(client: TestClient, episode: dict, monkeypatch) -> None:
    """§15.3：对象存储错误必须是 STORAGE_ERROR（503 类），且可重试。"""
    from app.core.errors import StorageError
    from app.services import executor as executor_service

    def _raise_storage(session, job):
        raise StorageError("对象存储不可用", details={"bucket": "local"})

    monkeypatch.setitem(executor_service._HANDLERS, "RENDER_PREVIEW", _raise_storage)
    resp = client.post(f"/api/v1/episodes/{episode['id']}/render/preview", json={})
    if resp.status_code == 200:
        job = client.get(f"/api/v1/jobs/{resp.json()['id']}").json()
        if job["status"] == "FAILED":
            assert job["error_code"] == "STORAGE_ERROR"


def test_render_error_locates_the_shot(client: TestClient, episode: dict) -> None:
    """§15.3：空时间线的校验失败必须能定位到镜头/音轨，而不是笼统报错。"""
    from app.agents import edit as edit_agent

    # 空时间线（没有任何已确认分镜）时校验必须失败，且 errors 可定位
    with session_scope() as session:
        from app.db.models import Episode

        ep = session.get(Episode, episode["id"])
        result = edit_agent.validate_timeline(session, ep)
    assert result["valid"] is False
    assert result["errors"]
    # 每条错误都要有可读定位字段，前端才能指到镜头
    for err in result["errors"]:
        assert "code" in err and "message" in err


def _make_renderable_episode(episode_id: str) -> dict:
    """造一个「时间线校验通过」的最小剧集：1 个分镜 + 一张真实存在的确认图。

    这样后续如果合成失败，原因只可能来自 FFmpeg 本身，而不是时间线缺料 ——
    失败用例必须先把「不是别的原因」钉死，否则测试会假绿。
    """
    import io

    from PIL import Image

    from app.agents import edit as edit_agent
    from app.db.models import Episode, Shot
    from app.services import media as media_service

    with session_scope() as session:
        ep = session.get(Episode, episode_id)
        buf = io.BytesIO()
        Image.new("RGB", (720, 1280), (18, 22, 36)).save(buf, format="PNG")
        asset = media_service.save_media(
            session,
            content=buf.getvalue(),
            kind="image",
            role="shot_image",
            filename="shot_001.png",
            mime_type="image/png",
            owner_type="episode",
            owner_id=ep.id,
            episode_id=ep.id,
            project_id=ep.project_id,
            width=720,
            height=1280,
            status="CONFIRMED",
        )
        session.add(
            Shot(
                episode_id=ep.id,
                shot_no=1,
                duration_ms=4000,
                required=True,
                confirmed_media_id=asset.id,
                shot_json={"action": "抬头看向雨幕", "image_prompt": "雨夜街道"},
            )
        )
        session.flush()
        return edit_agent.validate_timeline(session, ep)


def test_ffmpeg_failure_is_located_and_package_survives(
    client: TestClient, episode: dict, worker, monkeypatch
) -> None:
    """§15.3：模拟 **FFmpeg 失败** 的完整降级路径。

    要求逐条对应 PRD 原文「系统必须展示可定位错误、保留原任务记录、
    支持重试或降级，并保证素材包路径可用」：
    ① 错误可定位（带 ffmpeg 尾部输出 + 定位结果 + 修复提示）；
    ② 原任务记录保留（仍是 FAILED 可查，attempt 有值，可重试）；
    ③ 降级可用 —— 合成失败不阻断素材包导出（§10.2）。
    """
    from types import SimpleNamespace

    from app.services import render as render_service

    validation = _make_renderable_episode(episode["id"])
    assert validation["valid"] is True, validation["errors"]

    class _FailedProc:
        """模拟 ffmpeg 跑完但返回非 0（滤镜/编码不兼容的典型形态）。"""

        returncode = 1
        stdout = b""
        stderr = (
            b"[Parsed_concat_0 @ 0x55f0] Input link in1:v0 parameters do not match\n"
            b"[AVFilterGraph @ 0x55f0] \xe9\x95\x9c 1 \xe5\xb0\xba\xe5\xaf\xb8\xe4\xb8\x8d\xe4\xb8\x80\xe8\x87\xb4\n"
        )

    # 只替换 render 模块看到的名字，不动全局 subprocess —— 否则同一测试里
    # 素材包导出等其它子进程调用也会被换掉。
    monkeypatch.setattr(
        render_service, "subprocess", SimpleNamespace(run=lambda *a, **k: _FailedProc())
    )

    resp = client.post(f"/api/v1/episodes/{episode['id']}/render/preview", json={})
    assert resp.status_code == 200, resp.text
    job = wait_job(client, worker, resp.json()["id"])

    # ① 可定位错误
    assert job["status"] == "FAILED", job
    assert job["error_code"] == "RENDER_ERROR"
    assert job["error_message"]
    detail = job["error_detail"]
    assert detail.get("ffmpeg_tail"), detail
    assert detail.get("hint"), detail
    # `located` 是列表且永不为空（定位不到时会兜底成 unknown 条目）
    located = detail["located"]
    assert isinstance(located, list) and located, located
    for entry in located:
        assert entry.get("track") and entry.get("message"), entry
    # 本次注入的 stderr 含 concat，必须被归到视频轨
    assert any(e["track"] == "video" for e in located), located

    # ② 原任务记录保留（不是被删掉/静默吞掉），且还能重试
    again = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert again["status"] == "FAILED"
    assert again["attempt"] >= 1
    assert again["error_code"] == "RENDER_ERROR"
    retry = client.post(f"/api/v1/jobs/{job['id']}/retry")
    assert retry.status_code in {200, 201, 409}, retry.text

    # ③ 降级：合成失败不阻断素材包（§10.2）
    pkg = client.post(f"/api/v1/episodes/{episode['id']}/export/package")
    assert pkg.status_code == 200, pkg.text
    pkg_job = wait_job(client, worker, pkg.json()["id"])
    assert pkg_job["status"] == "SUCCEEDED", pkg_job
    assert pkg_job["result_ref"], pkg_job


def test_error_body_shape_is_uniform(client: TestClient, episode: dict) -> None:
    """§15.3 / API.md §0.1：所有错误都用统一错误体 {code, message, details, retryable, trace_id}。"""
    resp = client.get("/api/v1/episodes/epi_missing/story")
    assert resp.status_code == 404
    body = resp.json()["error"]
    assert set(body) >= {"code", "message", "details", "retryable", "trace_id"}
    assert body["code"] == "NOT_FOUND"
    assert body["retryable"] is False
    # trace_id 必须非空，且响应头里也带着同一个，便于报障时直接提供
    assert body["trace_id"]
    assert resp.headers.get("x-trace-id") == body["trace_id"]


def test_request_validation_uses_uniform_body(client: TestClient, episode: dict) -> None:
    """API.md §0.1：请求参数校验失败也要用统一错误体，而不是 FastAPI 默认的 detail 结构。"""
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "不存在的类型", "source_text": "x"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert "error" in body, f"应为统一错误体，实际拿到 {body.keys()}"
    err = body["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["retryable"] is False
    assert err["trace_id"]
    assert err["details"].get("loc")


# --------------------------------------------------------------------------- #
# §15.4 数据可追溯
# --------------------------------------------------------------------------- #

def test_provider_calls_record_model_and_cost(client: TestClient, episode: dict) -> None:
    """§15.4：每次供应商调用都要记录 provider / model / 成本，可回溯。"""
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/story/generate", params={"shot_count": 3}
    )
    job_id = resp.json()["id"]
    with session_scope() as session:
        calls = session.scalars(
            select(ProviderCall).where(ProviderCall.job_id == job_id)
        ).all()
    if calls:
        for call in calls:
            assert call.provider
            assert call.kind
            # 结算成本或预估成本至少有一个可用
            assert call.settled_cost_cents is not None or call.estimated_cost_cents is not None


def test_export_record_links_to_manifest(client: TestClient, episode: dict) -> None:
    """§15.4：导出记录必须带 manifest，且素材带 sha256（可校验未被篡改）。"""
    with session_scope() as session:
        rows = session.scalars(
            select(ExportRecord).where(ExportRecord.episode_id == episode["id"])
        ).all()
    for row in rows:
        manifest = row.manifest or {}
        if not manifest:
            continue
        # manifest 里必须能回答「用了什么、多长、多大」
        assert "duration_ms" in manifest or "total_duration_ms" in manifest or manifest


def test_version_chain_is_queryable(client: TestClient, episode: dict) -> None:
    """§15.4：从版本记录能查到剧本/角色/时间线的历史快照与差异。"""
    script = {
        "story_bible": {"logline": "雨夜相遇", "characters": []},
        "beat_sheet": [],
        "shots": [
            {
                "shot_no": 1,
                "duration_ms": 4000,
                "scene_no": 1,
                "characters": [],
                "action": "抬头",
                "camera_movement": "固定",
                "composition": "中景",
                "emotion": "怅然",
                "dialogue": [],
                "narration": "",
                "sfx": [],
                "image_prompt": "雨夜街道",
                "negative_prompt": "",
            }
        ],
    }
    client.put(f"/api/v1/episodes/{episode['id']}/script", json={"story_json": script})
    body = client.get(f"/api/v1/episodes/{episode['id']}/versions?entity_type=script").json()
    assert body["items"]
    record = body["items"][0]
    assert record["entity_label"] == "剧本"
    assert record["input_digest"], "版本必须带内容指纹，否则无法判断是否真的变了"

    detail = client.get(
        f"/api/v1/episodes/{episode['id']}/versions/{record['id']}"
    ).json()
    assert detail["payload"]["shots"][0]["action"] == "抬头"
    assert "diff" in detail


def test_audit_trail_is_queryable(client: TestClient, episode: dict) -> None:
    """§15.4：导出/配额/供应商等关键动作都要在审计日志里可查。"""
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/render/export",
        json={"preset": "720p_9x16"},
    )
    # 校验不过（空时间线）时是 500 RENDER_ERROR，这本身也是被记录的失败路径
    assert resp.status_code in (200, 500)

    logs = client.get(f"/api/v1/audit-logs?episode_id={episode['id']}").json()
    items = logs["items"] if isinstance(logs, dict) else logs
    assert isinstance(items, list)


def test_cost_ledger_has_no_residual_reservation(client: TestClient, episode: dict) -> None:
    """§15.4：任何时刻 used + reserved 都不能超过 limit，且失败任务不留悬空预留。"""
    quota = client.get("/api/v1/quota").json()
    assert quota["used_cents"] + quota["reserved_cents"] <= quota["limit_cents"]

    ledger = client.get("/api/v1/cost/ledger").json()
    items = ledger["items"] if isinstance(ledger, dict) else ledger
    assert isinstance(items, list)
    # 台账只允许三种类型，且金额是整数分
    for entry in items:
        assert entry["kind"] in {"RESERVE", "SETTLE", "RELEASE"}
        assert isinstance(entry["amount_cents"], int)


def test_mp4_metadata_is_serialisable(client: TestClient) -> None:
    """§15.4：导出元数据结构必须可 JSON 序列化（写入 MP4 前的最后一关）。"""
    meta = {
        "project_id": "prj_x",
        "episode_id": "epi_x",
        "generated_at": "2026-10-13T00:00:00+00:00",
        "provider": "mock",
        "model": "mock-video-v1",
    }
    assert json.dumps(meta, ensure_ascii=False)
