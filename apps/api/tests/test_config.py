"""Configuration invariants that protect the local media store."""

from __future__ import annotations

import pytest

from animatter.config import Settings


def test_defaults_bind_loopback(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path)
    assert settings.host == "127.0.0.1"
    assert settings.db_path == tmp_path / "app.db"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com"])
def test_public_bind_is_rejected(tmp_path, host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        Settings(data_dir=tmp_path, host=host)


def test_dashscope_env_alias(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-123")
    settings = Settings(data_dir=tmp_path)
    assert settings.dashscope_api_key == "sk-test-123"


def test_ensure_dirs(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "nested" / "data")
    settings.ensure_dirs()
    assert settings.db_path.parent.is_dir()
    assert settings.projects_dir.is_dir()


def test_unknown_log_level_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="log level"):
        Settings(data_dir=tmp_path, log_level="verbose")


def test_export_memory_settings_have_the_measured_defaults(tmp_path) -> None:
    """These two numbers are the whole point of §19.28; a silent default change is a regression.

    ``video_filter_threads`` is the memory lever (ffmpeg's own default is one slice per logical
    CPU, i.e. 16 here, and that is what ran the machine out of memory), and
    ``video_still_composite`` is the time lever. Both are read by the export and by nothing else,
    so a wrong default shows up as an OOM 20 minutes into a full-film render.
    """
    settings = Settings(data_dir=tmp_path)
    assert settings.video_filter_threads == 4
    assert settings.video_still_composite is True
    assert settings.video_chunk_workers == 1
