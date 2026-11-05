"""Health endpoint behaviour: healthy with a real toolchain, degraded without it."""

from __future__ import annotations

from fastapi.testclient import TestClient

from animatter.config import Settings
from animatter.main import create_app


def test_healthz_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    checks = body["checks"]
    assert checks["database"]["ok"] is True
    assert checks["ffmpeg"]["ok"] is True, checks["ffmpeg"]
    assert checks["ffprobe"]["ok"] is True, checks["ffprobe"]
    assert checks["disk"]["ok"] is True


def test_healthz_reports_asr_credentials_without_failing(tmp_path, monkeypatch) -> None:
    # Isolate from the developer's real .env: this asserts the *unconfigured*
    # path, which must degrade one feature without failing the process.
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    settings = Settings(
        data_dir=tmp_path / "d",
        log_level="ERROR",
        dashscope_api_key=None,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        body = test_client.get("/healthz").json()
    assert body["checks"]["asr_credentials"]["ok"] is False
    assert body["status"] == "ok"  # missing key degrades a feature, not the process


def test_healthz_reports_configured_credential(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "d", log_level="ERROR", dashscope_api_key="sk-test")
    app = create_app(settings)
    with TestClient(app) as test_client:
        body = test_client.get("/healthz").json()
    assert body["checks"]["asr_credentials"]["ok"] is True


def test_healthz_degraded_without_ffmpeg(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "d",
        log_level="ERROR",
        ffmpeg_path="animatter-definitely-missing-ffmpeg",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        response = test_client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["ffmpeg"]["ok"] is False
    assert "ffmpeg" in body["checks"]["ffmpeg"]["detail"].lower() or body["checks"]["ffmpeg"]["detail"]
