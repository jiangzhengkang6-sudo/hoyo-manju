"""PRD §3.1 / §5.1-1 / §5.1-6：工作区画像与项目级资产库。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_workspace_profile(client: TestClient) -> None:
    resp = client.get("/api/v1/workspace")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["id"]
    assert body["workspace_id"] == body["user"]["workspace_id"]
    # 配额快照与 /quota 口径一致
    assert "quota" in body
    assert set(body["counts"]) >= {
        "projects",
        "active_projects",
        "archived_projects",
        "episodes",
        "jobs",
        "media",
        "characters",
        "styles",
    }
    assert body["defaults"]["aspect_ratio"] == "9:16"


def test_workspace_counts_track_projects(client: TestClient, project: dict) -> None:
    body = client.get("/api/v1/workspace").json()
    assert body["counts"]["projects"] >= 1
    assert body["counts"]["active_projects"] >= 1


def test_workspace_rename(client: TestClient) -> None:
    resp = client.patch("/api/v1/workspace", json={"name": "王工的创作空间"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "王工的创作空间"
    assert client.get("/api/v1/workspace").json()["user"]["name"] == "王工的创作空间"


def test_workspace_rejects_overlong_name(client: TestClient) -> None:
    resp = client.patch("/api/v1/workspace", json={"name": "长" * 200})
    assert resp.status_code == 409, resp.text


def test_workspace_rename_ignores_blank(client: TestClient) -> None:
    before = client.get("/api/v1/workspace").json()["user"]["name"]
    resp = client.patch("/api/v1/workspace", json={"name": "   "})
    assert resp.status_code == 200
    assert resp.json()["name"] == before


def test_workspace_assets_aggregates_across_projects(
    client: TestClient, project: dict
) -> None:
    """§5.1-6：资产库要能跨项目看到角色与画风。"""
    other = client.post(
        "/api/v1/projects", json={"name": "第二个项目", "genre": "都市"}
    ).json()

    client.post(
        f"/api/v1/projects/{project['id']}/characters",
        json={"name": "林小满", "identity": "20 岁女性"},
    )
    client.post(
        f"/api/v1/projects/{other['id']}/characters",
        json={"name": "陆行舟", "identity": "25 岁男性"},
    )
    client.post(
        f"/api/v1/projects/{project['id']}/styles",
        json={"name": "日系赛璐璐", "description": "柔和"},
    )

    body = client.get("/api/v1/workspace/assets").json()
    names = {c["name"] for c in body["characters"]}
    assert {"林小满", "陆行舟"} <= names
    assert any(s["name"] == "日系赛璐璐" for s in body["styles"])
    # 每个角色都要带上来源项目名，否则跨项目视图无法定位
    assert all(c["project_name"] for c in body["characters"])
    assert len(body["projects"]) >= 2


def test_workspace_assets_filters(client: TestClient, project: dict) -> None:
    client.post(
        f"/api/v1/projects/{project['id']}/characters",
        json={"name": "唯一角色", "identity": "30 岁男性"},
    )
    client.post(
        f"/api/v1/projects/{project['id']}/styles",
        json={"name": "唯一画风", "description": "冷色"},
    )

    only_styles = client.get("/api/v1/workspace/assets?kind=style").json()
    assert only_styles["characters"] == []
    assert any(s["name"] == "唯一画风" for s in only_styles["styles"])

    only_chars = client.get("/api/v1/workspace/assets?kind=character").json()
    assert only_chars["styles"] == []

    by_name = client.get("/api/v1/workspace/assets?q=唯一角色").json()
    assert [c["name"] for c in by_name["characters"]] == ["唯一角色"]

    by_project = client.get(
        f"/api/v1/workspace/assets?project_id={project['id']}"
    ).json()
    assert all(c["project_id"] == project["id"] for c in by_project["characters"])
