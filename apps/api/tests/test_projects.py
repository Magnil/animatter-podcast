"""Project creation, listing, and on-disk layout."""

from __future__ import annotations

from fastapi.testclient import TestClient

from animatter.config import Settings


def test_create_project_creates_layout(client: TestClient, settings: Settings) -> None:
    response = client.post("/api/projects", json={"name": "S02E07 对话某某"})
    assert response.status_code == 201, response.text
    project = response.json()
    assert project["name"] == "S02E07 对话某某"
    assert project["id"].startswith("prj_")

    root = settings.projects_dir / project["id"]
    for sub in ("media", "transcripts", "peaks", "edits", "exports", "logs", "derived"):
        assert (root / sub).is_dir(), sub


def test_list_and_get(client: TestClient) -> None:
    created = client.post("/api/projects", json={"name": "第一期"}).json()

    listing = client.get("/api/projects")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == created["id"]

    fetched = client.get(f"/api/projects/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "第一期"


def test_unknown_project_has_hint(client: TestClient) -> None:
    response = client.get("/api/projects/prj_missing")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "project_not_found"
    assert error["hint"]


def test_blank_name_rejected(client: TestClient) -> None:
    response = client.post("/api/projects", json={"name": "   "})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "project_name_empty"


def test_name_is_trimmed(client: TestClient) -> None:
    response = client.post("/api/projects", json={"name": "  带空格  "})
    assert response.status_code == 201
    assert response.json()["name"] == "带空格"
