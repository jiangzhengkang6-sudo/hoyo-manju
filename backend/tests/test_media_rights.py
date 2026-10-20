"""FR-082 权利声明 + §11.1-2 签名 URL 上限。

- FR-082：用户上传的**任何**素材（含图片/文本）都必须声明使用权。
- §11.1-2：签名 URL 默认不超过 30 分钟，且服务端强制上限（调用方不能拉长）。
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient
from PIL import Image

from app.core.config import settings
from app.core.errors import ErrorCode
from app.services import media as media_service


def _png_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    """真实可解码的 PNG —— 损坏的图会被质检打回 REJECTED。"""
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 90, 160)).save(buf, format="PNG")
    return buf.getvalue()


def test_upload_without_rights_declaration_is_rejected(client: TestClient) -> None:
    """FR-082：不声明使用权一律拒绝（此前图片/文本可绕过）。"""
    resp = client.post(
        "/api/v1/media/upload",
        files={"file": ("a.png", _png_bytes(), "image/png")},
        data={"kind": "image"},
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == ErrorCode.STATE_CONFLICT
    assert "使用权" in body["message"]
    assert body["details"]["field"] == "rights_declared"


def test_upload_with_rights_declaration_succeeds(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/media/upload",
        files={"file": ("a.png", _png_bytes(), "image/png")},
        data={"kind": "image", "rights_declared": "true", "source_note": "自制素材"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["rights_declared"] is True
    assert body["rights_note"] == "自制素材"

    rights = client.get(f"/api/v1/media/{body['id']}/rights").json()
    assert rights["rights_declared"] is True
    assert rights["rights_note"] == "自制素材"


def test_text_upload_also_requires_rights(client: TestClient) -> None:
    """FR-082 不区分模态：文本素材同样要声明。"""
    resp = client.post(
        "/api/v1/media/upload",
        files={"file": ("note.txt", "一段用户提供的文本".encode(), "text/plain")},
        data={"kind": "text"},
    )
    assert resp.status_code == 409, resp.text
    assert "使用权" in resp.json()["error"]["message"]


def test_clamp_ttl_caps_but_allows_shorter() -> None:
    ceiling = settings.signed_url_max_ttl_seconds
    # 不传 → 默认值
    assert media_service.clamp_ttl(None) == settings.signed_url_ttl_seconds
    # 更短 → 尊重调用方
    assert media_service.clamp_ttl(60) == 60
    # 更长 → 夹到上限
    assert media_service.clamp_ttl(999_999) == ceiling
    # 非法值 → 回落到默认
    assert media_service.clamp_ttl(0) == settings.signed_url_ttl_seconds
    assert media_service.clamp_ttl(-5) == settings.signed_url_ttl_seconds


def test_signed_url_route_reports_clamped_ttl(client: TestClient) -> None:
    """§11.1-2 端到端：接口回传的是实际生效的 TTL，并标明被夹取。"""
    created = client.post(
        "/api/v1/media/upload",
        files={"file": ("b.png", _png_bytes(), "image/png")},
        data={"kind": "image", "rights_declared": "true"},
    )
    assert created.status_code == 201, created.text
    media_id = created.json()["id"]

    long_req = client.get(
        f"/api/v1/media/{media_id}/url", params={"ttl_seconds": 999_999}
    ).json()
    assert long_req["ttl_seconds"] == settings.signed_url_max_ttl_seconds
    assert long_req["clamped"] is True
    assert long_req["requested_ttl_seconds"] == 999_999
    assert long_req["max_ttl_seconds"] == settings.signed_url_max_ttl_seconds

    short_req = client.get(
        f"/api/v1/media/{media_id}/url", params={"ttl_seconds": 120}
    ).json()
    assert short_req["ttl_seconds"] == 120
    assert short_req["clamped"] is False
