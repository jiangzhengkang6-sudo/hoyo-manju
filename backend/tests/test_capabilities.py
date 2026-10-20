"""FR-091 能力探测：提交前按供应商能力声明阻断。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ErrorCode, ProviderCapabilityUnsupported
from app.providers.base import ProviderCapabilities
from app.services import capabilities as caps


def test_aspect_ratio_normalization() -> None:
    assert caps.aspect_ratio_of(1080, 1920) == "9:16"
    assert caps.aspect_ratio_of(1920, 1080) == "16:9"
    assert caps.aspect_ratio_of(1024, 1024) == "1:1"
    assert caps.aspect_ratio_of(0, 0) == ""


def test_undeclared_capabilities_do_not_block() -> None:
    """供应商没声明就不拦——「没填」不等于「不支持」。"""
    caps_service = caps
    empty = ProviderCapabilities(
        models=[],
        image_sizes=[],
        max_image_pixels=0,
        max_reference_images=0,
        max_candidates=0,
        duration_ms_min=0,
        duration_ms_max=0,
        aspect_ratios=[],
        image_formats=[],
    )
    # 不会抛异常
    caps_service.validate_image_request(
        empty, width=4096, height=4096, count=99, reference_count=99
    )
    caps_service.validate_video_request(
        empty, duration_ms=999_999, width=4096, height=4096, has_image=True
    )


def test_image_size_and_reference_limits_block() -> None:
    declared = ProviderCapabilities(
        image_sizes=["1024x1024", "1080x1920"],
        max_reference_images=4,
        max_candidates=4,
        image_formats=["image/png"],
    )
    # 尺寸不在清单里 → 阻断
    with pytest.raises(ProviderCapabilityUnsupported) as excinfo:
        caps.validate_image_request(
            declared, width=768, height=768, count=1, reference_count=0
        )
    assert excinfo.value.code == ErrorCode.PROVIDER_CAPABILITY_UNSUPPORTED
    assert excinfo.value.status_code == 422
    assert excinfo.value.details["problems"][0]["field"] == "size"

    # 参考图超上限 → 阻断
    with pytest.raises(ProviderCapabilityUnsupported) as excinfo:
        caps.validate_image_request(
            declared, width=1080, height=1920, count=1, reference_count=5
        )
    assert excinfo.value.details["problems"][0]["field"] == "reference_count"

    # 候选数超上限 → 阻断
    with pytest.raises(ProviderCapabilityUnsupported) as excinfo:
        caps.validate_image_request(
            declared, width=1080, height=1920, count=8, reference_count=0
        )
    assert excinfo.value.details["problems"][0]["field"] == "count"

    # 格式不在白名单 → 阻断
    with pytest.raises(ProviderCapabilityUnsupported) as excinfo:
        caps.validate_image_request(
            declared,
            width=1080,
            height=1920,
            count=1,
            reference_count=0,
            mime_type="image/webp",
        )
    assert excinfo.value.details["problems"][0]["field"] == "format"

    # 全部满足 → 通过
    caps.validate_image_request(
        declared, width=1080, height=1920, count=2, reference_count=4, mime_type="image/png"
    )


def test_video_duration_and_aspect_block() -> None:
    declared = ProviderCapabilities(
        duration_ms_min=2000,
        duration_ms_max=12000,
        aspect_ratios=["9:16", "16:9"],
        supports_image_to_video=True,
    )
    with pytest.raises(ProviderCapabilityUnsupported) as excinfo:
        caps.validate_video_request(
            declared, duration_ms=15000, width=1080, height=1920, aspect_ratio="9:16"
        )
    assert excinfo.value.details["problems"][0]["field"] == "duration_ms"

    with pytest.raises(ProviderCapabilityUnsupported) as excinfo:
        caps.validate_video_request(
            declared, duration_ms=5000, width=1080, height=1080, aspect_ratio="1:1"
        )
    assert excinfo.value.details["problems"][0]["field"] == "aspect_ratio"

    # 等价写法（1080x1920 ↔ 9:16）不该误伤
    caps.validate_video_request(declared, duration_ms=5000, width=1080, height=1920)

    # 不支持图生视频时，带首帧图应阻断
    text_only = ProviderCapabilities(supports_image_to_video=False)
    with pytest.raises(ProviderCapabilityUnsupported) as excinfo:
        caps.validate_video_request(
            text_only, duration_ms=5000, width=1080, height=1920, has_image=True
        )
    assert excinfo.value.details["problems"][0]["field"] == "image_to_video"


class _RestrictiveImageProvider:
    """声明只支持 1024x1024 的图像供应商替身。"""

    id = "restrictive"
    kind = "image"
    model = "restrictive-v1"
    display_name = "restrictive"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            models=["restrictive-v1"],
            max_candidates=2,
            image_sizes=["1024x1024"],
            max_reference_images=1,
            image_formats=["image/png"],
        )

    def configured(self) -> bool:
        return True

    def describe(self) -> dict:
        return {"id": self.id, "display_name": self.display_name, "kind": "image"}


def _seed_shots(episode_id: str, count: int = 2) -> None:
    from app.db.models import Shot
    from app.db.session import session_scope

    with session_scope() as session:
        for no in range(1, count + 1):
            session.add(
                Shot(
                    episode_id=episode_id,
                    shot_no=no,
                    duration_ms=4000,
                    shot_json={"action": f"动作 {no}", "image_prompt": "画面"},
                    stage="CONFIRMED",
                )
            )


def test_image_route_blocks_before_job_creation(
    client: TestClient, episode: dict, monkeypatch
) -> None:
    """FR-091 端到端：能力不满足时 422 阻断，且**不创建任务**。"""
    from app.providers import registry

    _seed_shots(episode["id"])
    monkeypatch.setattr(registry, "resolve", lambda *a, **k: _RestrictiveImageProvider())

    before = client.get("/api/v1/jobs", params={"episode_id": episode["id"]}).json()
    before_total = before.get("total", len(before.get("items", [])))

    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/generate-images",
        json={"candidates_per_shot": 2},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()["error"]
    assert body["code"] == ErrorCode.PROVIDER_CAPABILITY_UNSUPPORTED
    assert body["retryable"] is False
    assert body["details"]["problems"][0]["field"] == "size"
    assert "1024x1024" in body["message"]

    after = client.get("/api/v1/jobs", params={"episode_id": episode["id"]}).json()
    after_total = after.get("total", len(after.get("items", [])))
    assert after_total == before_total, "被阻断的请求不应创建任务"


def test_image_route_allows_compliant_request(
    client: TestClient, episode: dict, monkeypatch
) -> None:
    from app.providers import registry

    _seed_shots(episode["id"])

    class _OkProvider(_RestrictiveImageProvider):
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities(
                models=["ok-v1"],
                max_candidates=4,
                image_sizes=["1080x1920"],
                max_reference_images=4,
            )

    monkeypatch.setattr(registry, "resolve", lambda *a, **k: _OkProvider())
    resp = client.post(
        f"/api/v1/episodes/{episode['id']}/shots/generate-images",
        json={"candidates_per_shot": 2},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["type"] == "IMAGE_GENERATION"


def test_upload_quota_rejects_bad_format_and_size(client: TestClient) -> None:
    """§11.3：格式白名单 + 单文件大小上限。"""
    from app.core.config import settings

    # 不在白名单里的格式
    resp = client.post(
        "/api/v1/media/upload",
        files={"file": ("evil.exe", b"MZ\x00\x00", "application/x-msdownload")},
        data={"kind": "image"},
    )
    assert resp.status_code == 409, resp.text
    assert "不支持的上传格式" in resp.json()["error"]["message"]

    # 允许的格式但超过单文件上限（把上限临时调小以构造场景）
    original = settings.upload_max_bytes
    settings.upload_max_bytes = 8
    try:
        resp = client.post(
            "/api/v1/media/upload",
            files={"file": ("big.png", b"x" * 64, "image/png")},
            data={"kind": "image"},
        )
        assert resp.status_code == 409, resp.text
        assert "超过单文件上限" in resp.json()["error"]["message"]
    finally:
        settings.upload_max_bytes = original


def test_upload_quota_rejects_workspace_overflow(client: TestClient) -> None:
    """§11.3：工作区总容量配额。"""
    from app.core.config import settings

    original = settings.upload_workspace_quota_bytes
    settings.upload_workspace_quota_bytes = 1
    try:
        resp = client.post(
            "/api/v1/media/upload",
            files={"file": ("a.png", b"x" * 64, "image/png")},
            data={"kind": "image"},
        )
        assert resp.status_code == 402, resp.text
        body = resp.json()["error"]
        assert body["code"] == ErrorCode.QUOTA_EXCEEDED
        assert body["details"]["quota_bytes"] == 1
    finally:
        settings.upload_workspace_quota_bytes = original
