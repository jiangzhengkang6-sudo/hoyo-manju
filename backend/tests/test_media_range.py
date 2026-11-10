"""媒体内容的 HTTP Range 支持。

浏览器 `<video>` 依赖 206：没有 `Accept-Ranges`，Chrome 拖进度条失败，
Safari 直接黑屏不播；而且「地址少了 /api/v1 前缀」会拿到前端 SPA 的 HTML，
同样是黑屏。这里钉住服务端这一半。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.session import session_scope
from app.services import media as media_service


def _seed(project: dict) -> tuple[str, bytes]:
    content = bytes(range(256)) * 4          # 1024 字节，便于校验切片
    with session_scope() as session:
        asset = media_service.save_media(
            session, content=content, kind="video", role="shot_video",
            filename="clip.mp4", mime_type="video/mp4",
            owner_type="shot", owner_id="sht_x", project_id=project["id"],
        )
        session.flush()
        return asset.id, content


def test_full_get_advertises_range_support(client: TestClient, project: dict) -> None:
    media_id, content = _seed(project)
    resp = client.get(f"/api/v1/media/{media_id}/content")
    assert resp.status_code == 200
    assert resp.headers.get("accept-ranges") == "bytes"
    assert resp.content == content


def test_range_request_returns_206(client: TestClient, project: dict) -> None:
    media_id, content = _seed(project)
    resp = client.get(
        f"/api/v1/media/{media_id}/content", headers={"Range": "bytes=100-199"}
    )
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 100-199/{len(content)}"
    assert resp.headers["content-length"] == "100"
    assert resp.content == content[100:200]


def test_suffix_range(client: TestClient, project: dict) -> None:
    media_id, content = _seed(project)
    resp = client.get(
        f"/api/v1/media/{media_id}/content", headers={"Range": "bytes=-10"}
    )
    assert resp.status_code == 206
    assert resp.content == content[-10:]


def test_invalid_range_returns_416(client: TestClient, project: dict) -> None:
    media_id, content = _seed(project)
    resp = client.get(
        f"/api/v1/media/{media_id}/content", headers={"Range": "bytes=99999-"}
    )
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(content)}"
