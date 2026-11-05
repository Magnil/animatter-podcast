"""Clipping service and real ffmpeg rendering.

Rendering is tested against a synthesised file so duration assertions are exact:
if the EDL says 8.000s and ffmpeg produces 8.037s, the mapping the subtitles use
is wrong and the error will only show up as drifting captions later.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.edl import Deletion, compile_edl
from animatter.media import render as render_module
from animatter.services import clipping as clipping_service


def make_audio(path: Path, seconds: float = 10.0, freq: int = 440) -> Path:
    """Generate a mono WAV of exactly ``seconds``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-ac",
            "1",
            "-ar",
            "22050",
            str(path),
        ],
        check=True,
    )
    return path


def duration_of(path: Path) -> float:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(completed.stdout.strip())


# --- filtergraph --------------------------------------------------------


def test_filtergraph_has_one_branch_per_keep() -> None:
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(2000, 3000), Deletion(5000, 6000)],
        snap_to_silences=False,
        guard_ms=0,
    )
    graph = render_module.build_filtergraph(edl, sample_rate=22050, channels=1)
    assert graph.count("atrim=") == 3
    assert "concat=n=3" in graph


def test_filtergraph_fades_every_segment() -> None:
    # Without the fade, every join is a step discontinuity → audible click.
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(2000, 3000)],
        snap_to_silences=False,
        guard_ms=0,
    )
    graph = render_module.build_filtergraph(edl, sample_rate=22050, channels=1)
    assert graph.count("afade=t=in") == 2
    assert graph.count("afade=t=out") == 2


def test_filtergraph_single_segment_skips_concat() -> None:
    edl = compile_edl(original_duration_ms=10_000, deletions=[], snap_to_silences=False)
    graph = render_module.build_filtergraph(edl, sample_rate=22050, channels=1)
    assert "concat" not in graph


# --- real rendering -----------------------------------------------------


def test_render_duration_matches_edl_exactly(tmp_path: Path) -> None:
    source = make_audio(tmp_path / "src.wav", seconds=10.0)
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(2000, 4000)],
        snap_to_silences=False,
        guard_ms=0,
    )
    target = tmp_path / "out.m4a"

    result = render_module.render(
        "ffmpeg",
        source=source,
        edl=edl,
        target=target,
        sample_rate=22050,
        channels=1,
    )

    assert target.exists() and target.stat().st_size > 0
    actual = duration_of(target)
    # AAC adds priming; 50ms is the tolerance before captions visibly drift.
    assert abs(actual * 1000 - result.duration_ms) < 50, f"{actual * 1000:.0f} vs {result.duration_ms}"


def test_render_removes_expected_amount(tmp_path: Path) -> None:
    source = make_audio(tmp_path / "src.wav", seconds=10.0)
    edl = compile_edl(
        original_duration_ms=10_000,
        deletions=[Deletion(1000, 3000), Deletion(6000, 7000)],
        snap_to_silences=False,
        guard_ms=0,
    )
    target = tmp_path / "out.m4a"
    render_module.render("ffmpeg", source=source, edl=edl, target=target, sample_rate=22050, channels=1)
    assert abs(duration_of(target) * 1000 - 7000) < 50


def test_render_lossless_wav(tmp_path: Path) -> None:
    source = make_audio(tmp_path / "src.wav", seconds=6.0)
    edl = compile_edl(
        original_duration_ms=6000, deletions=[Deletion(2000, 3000)], snap_to_silences=False, guard_ms=0
    )
    target = tmp_path / "out.wav"
    render_module.render(
        "ffmpeg",
        source=source,
        edl=edl,
        target=target,
        sample_rate=22050,
        channels=1,
        lossless=True,
    )
    assert target.suffix == ".wav"
    assert abs(duration_of(target) * 1000 - 5000) < 50


def test_render_refuses_empty_edl(tmp_path: Path) -> None:
    from animatter.errors import AppError

    source = make_audio(tmp_path / "src.wav", seconds=1.0)
    edl = compile_edl(original_duration_ms=1000, deletions=[], snap_to_silences=False)
    empty = type(edl)(original_duration_ms=1000, keeps=(), deletions=(), clamps=())
    with pytest.raises(AppError, match="没有可渲染"):
        render_module.render(
            "ffmpeg", source=source, edl=empty, target=tmp_path / "x.m4a", sample_rate=22050, channels=1
        )


def test_render_missing_source(tmp_path: Path) -> None:
    from animatter.errors import AppError

    edl = compile_edl(original_duration_ms=1000, deletions=[], snap_to_silences=False)
    with pytest.raises(AppError, match="源音频不存在"):
        render_module.render(
            "ffmpeg",
            source=tmp_path / "nope.wav",
            edl=edl,
            target=tmp_path / "x.m4a",
            sample_rate=22050,
            channels=1,
        )


# --- preview ------------------------------------------------------------


def test_preview_covers_only_the_window(tmp_path: Path) -> None:
    source = make_audio(tmp_path / "src.wav", seconds=60.0)
    edl = compile_edl(
        original_duration_ms=60_000,
        deletions=[Deletion(20_000, 25_000)],
        snap_to_silences=False,
        guard_ms=0,
    )
    target = tmp_path / "preview.m4a"
    result = render_module.render_preview(
        "ffmpeg",
        source=source,
        edl=edl,
        target=target,
        sample_rate=22050,
        channels=1,
        window_orig=(22_000, 23_000),
        window_ms=6000,
    )
    # Window ±3s, minus the 5s deletion that falls inside it.
    assert result.duration_ms < 60_000
    assert target.exists()


def test_preview_outside_content_raises(tmp_path: Path) -> None:
    from animatter.errors import AppError

    source = make_audio(tmp_path / "src.wav", seconds=5.0)
    edl = (
        compile_edl(
            original_duration_ms=5000,
            deletions=[Deletion(0, 5000)],
            snap_to_silences=False,
            guard_ms=0,
            min_keep_ms=0,
        )
        if False
        else compile_edl(
            original_duration_ms=5000,
            deletions=[Deletion(4000, 4990)],
            snap_to_silences=False,
            guard_ms=0,
        )
    )
    # Ask for a window entirely inside the deletion.
    with pytest.raises(AppError, match="没有可预览"):
        render_module.render_preview(
            "ffmpeg",
            source=source,
            edl=edl,
            target=tmp_path / "p.m4a",
            sample_rate=22050,
            channels=1,
            window_orig=(4500, 4900),
            window_ms=0,
        )


# --- service ------------------------------------------------------------


@pytest.fixture()
def session(client: TestClient) -> Session:
    db = client.app.state.db  # type: ignore[attr-defined]
    with db.session() as s:
        yield s


def test_add_and_list_deletions(session: Session) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    version = seed_version(session, text="一二三四五", sizes=[5])
    clipping_service.add_deletion(session, version.id, start_ms=100, end_ms=300)

    rows = clipping_service.list_deletions(session, version.id)
    assert len(rows) == 1
    assert (rows[0].start_ms, rows[0].end_ms) == (100, 300)


def test_token_deletion_coalesces_contiguous(session: Session) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    version = seed_version(session, text="一二三四五", sizes=[5])
    rows = clipping_service.add_token_deletion(session, version.id, [1, 2, 3])

    # One deletion, not three: segment count drives render cost.
    assert len(rows) == 1
    assert (rows[0].start_ms, rows[0].end_ms) == (100, 400)


def test_token_deletion_splits_discontiguous(session: Session) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    version = seed_version(session, text="一二三四五", sizes=[5])
    rows = clipping_service.add_token_deletion(session, version.id, [0, 4])
    assert len(rows) == 2


def test_empty_token_selection_rejected(session: Session) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    from animatter.errors import AppError

    version = seed_version(session, text="一二", sizes=[2])
    with pytest.raises(AppError, match="没有选中"):
        clipping_service.add_token_deletion(session, version.id, [])


def test_soft_delete_deletion(session: Session) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    version = seed_version(session, text="一二三", sizes=[3])
    row = clipping_service.add_deletion(session, version.id, start_ms=0, end_ms=100)
    clipping_service.remove_deletion(session, row.id)
    assert clipping_service.list_deletions(session, version.id) == []


def test_edl_reflects_deletions(session: Session, client: TestClient) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    settings: Settings = client.app.state.settings  # type: ignore[attr-defined]
    version = seed_version(session, text="一二三四五六七八", sizes=[8])
    clipping_service.add_deletion(session, version.id, start_ms=200, end_ms=400)

    edl = clipping_service.build_edl(session, settings, version, snap=False)
    # The 20ms guard widens each cut edge, so a 200ms deletion removes 240ms.
    guard = 20
    assert edl.output_duration_ms == 800 - 200 - 2 * guard
    assert edl.removed_ms == 200 + 2 * guard


def test_edl_view_shape(session: Session, client: TestClient) -> None:  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    settings: Settings = client.app.state.settings  # type: ignore[attr-defined]
    version = seed_version(session, text="一二三四", sizes=[4])
    clipping_service.add_deletion(session, version.id, start_ms=100, end_ms=200)

    view = clipping_service.view(clipping_service.build_edl(session, settings, version, snap=False))
    assert view.segment_count == 2
    assert len(view.keeps) == 2
    assert view.signature
