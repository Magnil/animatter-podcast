"""The silence map is cached, and every caller shares it.

Why this matters beyond speed: `build_edl` runs on *every edit*. Before the cache
it called `ffmpeg silencedetect` over the whole file each time — measured at 9.1 s
on a 105-minute episode. The first paint of `GET /edl` therefore took nine
seconds, `edl` stayed `null` in the client, and every control gated on
`edl.deletions.length > 0` — 删除, 导出成片 — sat there disabled with no
explanation. That is the "点击后无反应" report.

The tests below pin the two things that made it a one-line fix and could silently
regress:

* a second call does not touch ffmpeg;
* the cache key includes the detector parameters, so changing the noise floor in
  settings invalidates rather than returning a map built with the old one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.media import silences as silence_cache
from animatter.models import Media
from animatter.services import settings_store, silence_map


@pytest.fixture()
def project_with_media(client: TestClient, session: Session, settings: Settings, monkeypatch):  # type: ignore[no-untyped-def]
    """A version whose media exists on disk, with ffmpeg's detector stubbed out."""
    from tests.test_corrections import seed_version
    from tests.test_render import make_audio

    version = seed_version(session, text="一二三四五六七八", sizes=[8])
    media = session.get(Media, version.media_id)
    assert media is not None
    source = make_audio(settings.data_dir / "tones.wav", seconds=2.0)
    media.path = str(source)
    media.duration_ms = 2000
    session.flush()
    session.commit()

    calls: list[tuple[float, float]] = []

    def fake_detect(ffmpeg: str, path: Path, *, noise_db: float = -35.0, min_duration_s: float = 0.4):  # type: ignore[no-untyped-def]
        calls.append((noise_db, min_duration_s))
        return [silence_cache.ffmpeg.SilenceSpan(start_ms=500, end_ms=900)]

    monkeypatch.setattr(silence_cache.ffmpeg, "detect_silence", fake_detect)
    return version, media, calls


def test_the_second_call_does_not_re_run_the_detector(
    client: TestClient, session: Session, settings: Settings, project_with_media  # type: ignore[no-untyped-def]
) -> None:
    version, media, calls = project_with_media

    first = silence_map.for_media(session, settings, version, media)
    second = silence_map.for_media(session, settings, version, media)

    assert len(calls) == 1, calls
    assert [(span.start_ms, span.end_ms) for span in first] == [(500, 900)]
    assert [(span.start_ms, span.end_ms) for span in second] == [(500, 900)]


def test_cache_key_follows_the_detector_parameters(
    client: TestClient, session: Session, settings: Settings, project_with_media  # type: ignore[no-untyped-def]
) -> None:
    """Moving the noise floor must not hand back a map built at the old one."""
    version, media, calls = project_with_media
    silence_map.for_media(session, settings, version, media)

    defaults = settings_store.load(session)
    settings_store.save(session, {"silence_noise_db": -30})
    session.commit()
    assert settings_store.load(session)["silence_noise_db"] == -30
    assert defaults["silence_noise_db"] != -30

    silence_map.for_media(session, settings, version, media)

    assert len(calls) == 2, calls
    assert calls[0][0] != calls[1][0]
    # Every parameter set gets its own file, so flipping the setting back is a
    # cache hit rather than another two-hour decode.
    folder = settings.data_dir / "projects" / version.project_id / "silences"
    assert len(list(folder.glob("*.json"))) == 2


def test_build_edl_through_the_api_is_not_a_minute_long(
    client: TestClient, session: Session, settings: Settings, project_with_media  # type: ignore[no-untyped-def]
) -> None:
    """The first EDL request pays for detection; the next must be immediate."""
    import time

    from animatter.services import clipping as clipping_service

    version, _media, calls = project_with_media
    clipping_service.add_deletion(session, version.id, start_ms=200, end_ms=400)
    session.commit()

    started = time.perf_counter()
    client.get(f"/api/versions/{version.id}/edl")
    first = time.perf_counter() - started
    started = time.perf_counter()
    client.get(f"/api/versions/{version.id}/edl")
    second = time.perf_counter() - started

    assert len(calls) == 1, "第二次请求又跑了一次全片静音检测"
    assert second <= max(first, 1.0), (first, second)
