"""§12 异常与降级：依赖熔断后「只暂停新任务，已有资源照常可用」。"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ServiceDegraded
from app.services import degraded


@pytest.fixture(autouse=True)
def _clean_degraded():
    """每个用例前后都清空熔断状态，避免相互污染。"""
    degraded.reset_all()
    yield
    degraded.reset_all()


def test_pause_blocks_new_tasks_with_actionable_message() -> None:
    degraded.pause(degraded.KIND_VIDEO, "可灵连续超时")
    with pytest.raises(ServiceDegraded) as excinfo:
        degraded.ensure_available(degraded.KIND_VIDEO, action="视频生成")
    err = excinfo.value
    assert err.status_code == 503
    assert err.code == "SERVICE_DEGRADED"
    assert err.retryable is True
    # 提示里必须写明「什么仍然可用」，否则用户不知道该干嘛
    assert "分镜画面生成与确认" in err.message
    assert "素材包导出" in err.message
    assert err.details["kind"] == "video"
    assert err.details["retry_after_seconds"] == degraded.DEFAULT_TTL_SECONDS


def test_ensure_available_is_noop_when_healthy() -> None:
    degraded.ensure_available(degraded.KIND_VIDEO)  # 不应抛错


def test_note_failure_pauses_only_after_threshold() -> None:
    for _ in range(degraded.FAILURE_THRESHOLD - 1):
        assert degraded.note_failure(degraded.KIND_VIDEO, "超时") is None
    assert not degraded.is_paused(degraded.KIND_VIDEO)
    triggered = degraded.note_failure(degraded.KIND_VIDEO, "超时")
    assert triggered is not None
    assert degraded.is_paused(degraded.KIND_VIDEO)


def test_note_success_self_heals() -> None:
    degraded.pause(degraded.KIND_STORAGE, "S3 502")
    assert degraded.is_paused(degraded.KIND_STORAGE)
    degraded.note_success(degraded.KIND_STORAGE)
    assert not degraded.is_paused(degraded.KIND_STORAGE)
    assert degraded.failure_count(degraded.KIND_STORAGE) == 0


def test_state_expires_after_ttl() -> None:
    degraded.pause(degraded.KIND_IMAGE, "供应商不可用", ttl_seconds=0)
    # TTL=0 表示「已经到点」，下次查询即视为恢复
    assert degraded.state(degraded.KIND_IMAGE) is None
    assert not degraded.is_paused(degraded.KIND_IMAGE)
    degraded.ensure_available(degraded.KIND_IMAGE)  # 不应抛错


def test_snapshot_reports_degraded_flag() -> None:
    assert degraded.snapshot()["degraded"] is False
    degraded.pause(degraded.KIND_VIDEO, "不可用")
    snap = degraded.snapshot()
    assert snap["degraded"] is True
    assert "video" in snap["paused"]
    assert snap["labels"]["video"] == "视频供应商"


def test_video_endpoint_returns_503_when_paused(
    client: TestClient, episode: dict
) -> None:
    """§12-3：视频供应商不可用时，视频入口返回 503 且带可执行提示。"""
    degraded.pause(degraded.KIND_VIDEO, "可灵不可用")
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/video/generate",
        json={"confirm": True, "quote_id": "quote_fake"},
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()["error"]
    assert body["code"] == "SERVICE_DEGRADED"
    assert body["details"]["kind"] == "video"
    assert "素材包导出" in body["message"]


def test_image_endpoint_returns_503_when_paused(
    client: TestClient, episode: dict
) -> None:
    degraded.pause(degraded.KIND_IMAGE, "图像供应商不可用")
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/generate-images",
        json={"shot_nos": []},
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["details"]["kind"] == "image"


def test_tts_endpoint_returns_503_when_paused(
    client: TestClient, episode: dict
) -> None:
    degraded.pause(degraded.KIND_SPEECH, "TTS 失败")
    resp = client.post(f"/api/v1/episodes/{episode['id']}/tts/generate", json={})
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["details"]["kind"] == "speech"


def test_render_endpoint_returns_503_when_storage_paused(
    client: TestClient, episode: dict
) -> None:
    """§12-9：对象存储不可用时暂停新媒体任务。"""
    degraded.pause(degraded.KIND_STORAGE, "对象存储 502")
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/render/preview", json={}
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["details"]["kind"] == "storage"


def test_story_and_script_keep_working_while_video_paused(
    client: TestClient, episode: dict
) -> None:
    """§12-3 的核心：视频暂停**不能**连累剧本与图片链路。"""
    degraded.pause(degraded.KIND_VIDEO, "可灵不可用")
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/story",
        json={"input_type": "idea", "source_text": "雨夜相遇"},
    )
    assert resp.status_code == 200, resp.text
    # 剧本读取同样不受影响
    assert client.get(f"/api/v1/episodes/{episode['id']}/story").status_code == 200


def test_admin_can_view_and_resume_degraded_state(
    client: TestClient, episode: dict
) -> None:
    degraded.pause(degraded.KIND_VIDEO, "可灵不可用")

    snap = client.get("/api/v1/degraded").json()
    assert snap["degraded"] is True
    assert "video" in snap["paused"]

    resp = client.post("/api/v1/degraded/video/resume")
    assert resp.status_code == 200, resp.text
    assert resp.json()["resumed"] is True
    assert client.get("/api/v1/degraded").json()["degraded"] is False


def test_admin_resume_unknown_kind_is_404(client: TestClient) -> None:
    resp = client.post("/api/v1/degraded/nonsense/resume")
    assert resp.status_code == 404


def test_admin_resume_when_not_paused_is_idempotent(client: TestClient) -> None:
    resp = client.post("/api/v1/degraded/video/resume")
    assert resp.status_code == 200
    assert resp.json()["resumed"] is False


def test_degrade_error_codes_exclude_user_input_errors() -> None:
    """用户输入非法/安全拦截不该触发熔断——只有依赖侧问题才算。"""
    from app.services.executor import _DEGRADE_ERROR_CODES

    assert "PROVIDER_UNAVAILABLE" in _DEGRADE_ERROR_CODES
    assert "STORAGE_ERROR" in _DEGRADE_ERROR_CODES
    assert "SAFETY_BLOCKED" not in _DEGRADE_ERROR_CODES
    assert "VALIDATION_ERROR" not in _DEGRADE_ERROR_CODES
    assert "QUOTA_EXCEEDED" not in _DEGRADE_ERROR_CODES


def test_job_type_kind_covers_media_tasks() -> None:
    from app.services.executor import JOB_TYPE_KIND

    assert JOB_TYPE_KIND["VIDEO_GENERATION"] == degraded.KIND_VIDEO
    assert JOB_TYPE_KIND["IMAGE_GENERATION"] == degraded.KIND_IMAGE
    assert JOB_TYPE_KIND["TTS_GENERATION"] == degraded.KIND_SPEECH
    assert JOB_TYPE_KIND["RENDER_EXPORT"] == degraded.KIND_STORAGE
    # 字幕不依赖外部供应商，不该参与熔断
    assert "SUBTITLE_GENERATION" not in JOB_TYPE_KIND


def test_pause_records_since_and_until() -> None:
    entry = degraded.pause(degraded.KIND_TEXT, "文本供应商超时", ttl_seconds=60)
    since = dt.datetime.fromisoformat(entry["since"])
    until = dt.datetime.fromisoformat(entry["until"])
    assert (until - since).total_seconds() == pytest.approx(60, abs=2)
