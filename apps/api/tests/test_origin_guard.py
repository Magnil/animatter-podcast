"""Cross-origin write protection for the loopback API."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_local_origin_allowed(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        json={"name": "本地页面"},
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 201


def test_foreign_origin_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/projects",
        json={"name": "恶意页面"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_rejected"


def test_foreign_origin_allowed_for_reads(client: TestClient) -> None:
    response = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
