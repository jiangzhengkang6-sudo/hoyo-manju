"""§11.1-3 审计补全：导出/合成、删除/归档、配额调整、供应商变更、媒体下载。

本文件只验证「审计流水真的被写下来了」以及「detail 脱敏生效」，
不重复验证各业务接口本身的行为（那些在各自的测试文件里）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.models import AuditLog
from app.db.session import session_scope
from app.services import audit as audit_service


def _audit_rows(
    *,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    project_id: str | None = None,
) -> list[AuditLog]:
    """直接读库取审计行，绕开 HTTP 层，避免受分页/排序影响。"""
    from sqlalchemy import select

    with session_scope() as session:
        stmt = select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if target_type:
            stmt = stmt.where(AuditLog.target_type == target_type)
        if target_id:
            stmt = stmt.where(AuditLog.target_id == target_id)
        if project_id:
            stmt = stmt.where(AuditLog.project_id == project_id)
        rows = list(session.scalars(stmt))
        for row in rows:
            # 会话关闭后 ORM 属性会失效，先转成普通 dict 快照
            session.expunge(row)
        return rows


def _snapshot(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "project_id": row.project_id,
        "episode_id": row.episode_id,
        "detail": dict(row.detail or {}),
    }


# --------------------------------------------------------------------------- #
# 1. 导出 / 合成
# --------------------------------------------------------------------------- #

def test_render_preview_writes_render_audit(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    """合成预览必须写 RENDER 审计行，且带 kind 与成本预估。"""
    from conftest import STORY_TEXT
    from test_acceptance import _confirm_all_shots, _resume, _run_to_shot_gate

    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)
    _confirm_all_shots(client, episode_id)

    response = client.post(
        f"/api/v1/episodes/{episode_id}/render/preview", json={"watermark": True}
    )
    assert response.status_code == 200, response.text
    job = response.json()

    rows = [
        _snapshot(r)
        for r in _audit_rows(action=audit_service.ACTION_RENDER)
        if r.episode_id == episode_id
    ]
    assert rows, "合成预览没有写入 RENDER 审计行"
    entry = rows[-1]
    assert entry["action"] == "RENDER"
    assert entry["target_type"] == audit_service.TARGET_EXPORT
    assert entry["target_id"] == job["id"], "target_id 应为任务 id"
    assert entry["episode_id"] == episode_id
    assert entry["project_id"] == project["id"]
    assert entry["detail"]["kind"] == "preview"
    assert entry["detail"]["estimate_cents"] == job["estimate"]["estimate_cents"]


def test_render_export_writes_export_audit(
    client: TestClient, worker, project: dict, episode: dict
) -> None:
    """正式导出必须写 EXPORT 审计行，detail 含预设（格式）与成本预估。"""
    from test_acceptance import _confirm_all_shots, _resume, _run_to_shot_gate

    episode_id = episode["id"]
    _run_to_shot_gate(client, episode, project)
    _confirm_all_shots(client, episode_id)

    response = client.post(
        f"/api/v1/episodes/{episode_id}/render/export",
        json={"preset": "720p_9x16", "watermark": False},
    )
    assert response.status_code == 200, response.text
    job = response.json()

    rows = [
        _snapshot(r)
        for r in _audit_rows(action=audit_service.ACTION_EXPORT)
        if r.episode_id == episode_id and r.target_id == job["id"]
    ]
    assert rows, "正式导出没有写入 EXPORT 审计行"
    entry = rows[-1]
    assert entry["target_type"] == audit_service.TARGET_EXPORT
    assert entry["detail"]["kind"] == "final"
    assert entry["detail"]["preset"] == "720p_9x16"
    assert entry["detail"]["estimate_cents"] == job["estimate"]["estimate_cents"]


def test_export_package_writes_export_audit(client: TestClient, episode: dict) -> None:
    """ZIP 素材包下载同样计一次导出。"""
    response = client.post(f"/api/v1/episodes/{episode['id']}/export/package")
    assert response.status_code == 200, response.text
    job = response.json()

    rows = [
        _snapshot(r)
        for r in _audit_rows(action=audit_service.ACTION_EXPORT)
        if r.target_id == job["id"]
    ]
    assert rows, "素材包没有写入 EXPORT 审计行"
    entry = rows[-1]
    assert entry["episode_id"] == episode["id"]
    assert entry["detail"]["kind"] == "package"
    assert entry["detail"]["format"] == "zip"


# --------------------------------------------------------------------------- #
# 2. 删除 / 归档
# --------------------------------------------------------------------------- #

def test_archive_character_writes_delete_audit(
    client: TestClient, project: dict, episode: dict
) -> None:
    """删除角色必须写 DELETE 审计行。"""
    created = client.post(
        f"/api/v1/projects/{project['id']}/characters",
        json={"name": "林晚", "identity": "女主", "is_main": True},
    )
    assert created.status_code == 201, created.text
    character = created.json()

    deleted = client.delete(f"/api/v1/characters/{character['id']}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "ARCHIVED"

    rows = [
        _snapshot(r)
        for r in _audit_rows(action=audit_service.ACTION_DELETE)
        if r.target_id == character["id"]
    ]
    assert rows, "删除角色没有写入 DELETE 审计行"
    entry = rows[-1]
    assert entry["target_type"] == audit_service.TARGET_CHARACTER
    assert entry["project_id"] == project["id"]
    assert entry["detail"]["name"] == "林晚"


def test_archive_episode_writes_archive_audit(
    client: TestClient, project: dict, episode: dict
) -> None:
    """归档剧集必须写 ARCHIVE 审计行。"""
    response = client.post(f"/api/v1/episodes/{episode['id']}/archive")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ARCHIVED"

    rows = [
        _snapshot(r)
        for r in _audit_rows(action=audit_service.ACTION_ARCHIVE)
        if r.target_id == episode["id"]
    ]
    assert rows, "归档剧集没有写入 ARCHIVE 审计行"
    entry = rows[-1]
    assert entry["target_type"] == audit_service.TARGET_EPISODE
    assert entry["episode_id"] == episode["id"]
    assert entry["project_id"] == project["id"]


def test_delete_bgm_writes_delete_audit(client: TestClient, episode: dict) -> None:
    """删除音轨必须写 DELETE 审计行。"""
    from app.db.models import AudioTrack

    with session_scope() as session:
        track = AudioTrack(episode_id=episode["id"], kind="bgm", start_ms=0, end_ms=1000)
        session.add(track)
        session.flush()
        track_id = track.id

    response = client.delete(f"/api/v1/episodes/{episode['id']}/bgm/{track_id}")
    assert response.status_code == 200, response.text

    rows = [
        _snapshot(r)
        for r in _audit_rows(action=audit_service.ACTION_DELETE)
        if r.target_id == track_id
    ]
    assert rows, "删除音轨没有写入 DELETE 审计行"
    assert rows[-1]["episode_id"] == episode["id"]
    assert rows[-1]["detail"]["track_kind"] == "bgm"


# --------------------------------------------------------------------------- #
# 3. 配额调整
# --------------------------------------------------------------------------- #

def test_adjust_quota_writes_quota_audit(client: TestClient) -> None:
    """配额调整必须写 QUOTA_ADJUST，detail 记录调整前后取值。"""
    before = int(settings.quota_limit_cents)
    target = before + 5000
    try:
        response = client.put("/api/v1/quota", json={"limit_cents": target, "note": "测试调整"})
        assert response.status_code == 200, response.text
        assert response.json()["limit_cents"] == target

        rows = _audit_rows(action=audit_service.ACTION_QUOTA_ADJUST)
        assert rows, "配额调整没有写入 QUOTA_ADJUST 审计行"
        entry = _snapshot(rows[-1])
        assert entry["target_type"] == audit_service.TARGET_QUOTA
        assert entry["detail"]["before_cents"] == before
        assert entry["detail"]["after_cents"] == target
    finally:
        settings.quota_limit_cents = before


# --------------------------------------------------------------------------- #
# 4. 供应商变更
# --------------------------------------------------------------------------- #

def test_update_providers_writes_provider_change_audit(client: TestClient) -> None:
    """供应商变更写 PROVIDER_CHANGE，detail 指明 kind 与变更字段，且不含明文密钥。"""
    secret = "sk-live-abcdef1234567890"
    response = client.put(
        "/api/v1/settings/providers",
        json={
            "text": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
                "api_key": secret,
            }
        },
    )
    assert response.status_code == 200, response.text

    rows = _audit_rows(action=audit_service.ACTION_PROVIDER_CHANGE)
    assert rows, "供应商变更没有写入 PROVIDER_CHANGE 审计行"
    entry = _snapshot(rows[-1])
    assert entry["target_type"] == audit_service.TARGET_PROVIDER
    assert entry["detail"]["kind"] == "text"
    assert entry["detail"]["api_key_updated"] is True
    assert "api_key" in entry["detail"]["changed_fields"]
    # 明文密钥绝不能出现在审计 detail 里
    assert secret not in str(entry["detail"]), entry["detail"]


# --------------------------------------------------------------------------- #
# 5. 媒体下载
# --------------------------------------------------------------------------- #

def test_media_download_writes_download_audit(client: TestClient, episode: dict) -> None:
    """媒体内容回传与签名 URL 都要写 DOWNLOAD 审计行。"""
    from app.services import media as media_service

    with session_scope() as session:
        asset = media_service.save_media(
            session,
            content=b"\x89PNG\r\n\x1a\n-fake-audit-bytes",
            kind="image",
            role="shot_image",
            filename="audit.png",
            mime_type="image/png",
            owner_type="episode",
            owner_id=episode["id"],
            episode_id=episode["id"],
            status="CONFIRMED",
        )
        media_id = asset.id

    signed = client.get(f"/api/v1/media/{media_id}/url")
    assert signed.status_code == 200, signed.text
    content = client.get(f"/api/v1/media/{media_id}/content")
    assert content.status_code == 200, content.text

    rows = [
        _snapshot(r)
        for r in _audit_rows(action=audit_service.ACTION_DOWNLOAD)
        if r.target_id == media_id
    ]
    assert len(rows) >= 2, f"签名 URL 与内容下载都应留痕，实际 {len(rows)} 条"
    vias = {r["detail"].get("via") for r in rows}
    assert vias == {"signed_url", "content"}, vias
    for entry in rows:
        assert entry["target_type"] == audit_service.TARGET_MEDIA
        assert entry["episode_id"] == episode["id"]


# --------------------------------------------------------------------------- #
# 6. detail 脱敏
# --------------------------------------------------------------------------- #

def test_audit_detail_redacts_secret(client: TestClient, project: dict) -> None:
    """密钥样式的值必须脱敏后再入库，不能原样出现。"""
    secret = "sk-verysecretvalue1234567890"
    with session_scope() as session:
        audit_service.record(
            session,
            action=audit_service.ACTION_EXPORT,
            target_type=audit_service.TARGET_EXPORT,
            target_id="exp_manual",
            project_id=project["id"],
            detail={"note": f"token={secret}", "api_key": secret},
        )
        session.flush()

    rows = [r for r in _audit_rows(target_id="exp_manual")]
    assert rows, "手写审计行未落库"
    stored = rows[-1].detail or {}
    assert secret not in str(stored), f"明文密钥泄漏到审计 detail：{stored}"
    # 字段名与结构不变，只有值被脱敏
    assert set(stored) == {"note", "api_key"}


# --------------------------------------------------------------------------- #
# 7. 查询接口
# --------------------------------------------------------------------------- #

def test_audit_logs_endpoint_filters(client: TestClient, project: dict, episode: dict) -> None:
    """GET /audit-logs 支持按 action / target_type / episode 过滤。"""
    client.post(f"/api/v1/episodes/{episode['id']}/archive")

    # 别名路由与既有 /audit 返回同一份流水
    alias = client.get("/api/v1/audit-logs", params={"limit": 200})
    assert alias.status_code == 200, alias.text
    legacy = client.get("/api/v1/audit", params={"limit": 200})
    assert legacy.status_code == 200, legacy.text
    assert alias.json() == legacy.json()

    response = client.get(
        "/api/v1/audit-logs",
        params={"episode_id": episode["id"], "action": "ARCHIVE"},
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items, "按 episode + action 过滤没有返回归档审计行"
    for item in items:
        assert item["action"] == "ARCHIVE"
        assert item["episode_id"] == episode["id"]

    by_type = client.get(
        "/api/v1/audit-logs",
        params={"target_type": audit_service.TARGET_EPISODE, "project_id": project["id"]},
    )
    assert by_type.status_code == 200, by_type.text
    assert by_type.json()["items"], "按 target_type + project 过滤应至少返回归档行"
