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
    """These numbers are the whole point of §19.28 / §19.34; a silent change is a regression.

    ``video_filter_threads`` **used to be** the memory lever (16 slices peaked at 5.6–6.1 GB and
    OOMed); after still pre-compositing and the plate bake the peak is flat from 4 to 16 slices,
    so it is now a speed lever and 12 wins by 6.6 % over the whole film (§19.34). Both are read
    by the export and by nothing else, so a wrong default shows up as either an OOM 20 minutes
    into a render or a render that quietly takes 7 % longer than it should.
    """
    settings = Settings(data_dir=tmp_path)
    assert settings.video_filter_threads == 12
    assert settings.video_still_composite is True
    assert settings.video_chunk_workers_max == 2
