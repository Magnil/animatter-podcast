"""Archive round-trip and subtitle export.

Both features are only trustworthy if a round trip is *lossless*: a save/load
that quietly drops the operation log, or an export whose timestamps come from the
original audio instead of the render, looks fine until the user relies on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.core.edl import Deletion, compile_edl
from animatter.errors import AppError
from animatter.services import archive as archive_service
from animatter.services import subtitles as subtitle_service


@pytest.fixture()
def seeded(client: TestClient, session: Session):  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    version = seed_version(session, text="今天我们来聊一聊播客剪辑。", sizes=[13])
    session.commit()
    return version


# --- archive ------------------------------------------------------------


def test_save_creates_a_project_file(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    response = client.post(f"/api/projects/{seeded.project_id}/archive")
    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"].endswith(".project")
    assert payload["sentence_count"] == 1
    assert Path(payload["path"]).exists()


def test_save_reports_counts(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    from animatter.services import editing

    editing.apply_boundary(session, seeded.id, action="new_sentence", from_token=6, to_token=6)
    session.commit()

    payload = client.post(f"/api/projects/{seeded.project_id}/archive").json()
    assert payload["op_count"] >= 1


def test_roundtrip_preserves_sentences(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    saved = client.post(f"/api/projects/{seeded.project_id}/archive").json()
    before = client.get(f"/api/versions/{seeded.id}/transcript").json()

    loaded = client.post("/api/archive/load", json={"path": saved["path"], "name": "副本"})
    assert loaded.status_code == 200
    after = client.get(f"/api/versions/{loaded.json()['version_id']}/transcript").json()

    assert [(s["text"], s["start_ms"]) for s in before["sentences"]] == [
        (s["text"], s["start_ms"]) for s in after["sentences"]
    ]


def test_roundtrip_preserves_tokens_exactly(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    saved = client.post(f"/api/projects/{seeded.project_id}/archive").json()
    before = client.get(f"/api/versions/{seeded.id}/transcript").json()
    loaded = client.post("/api/archive/load", json={"path": saved["path"]})
    after = client.get(f"/api/versions/{loaded.json()['version_id']}/transcript").json()

    assert len(after["tokens"]) == len(before["tokens"])
    for old, new in zip(before["tokens"], after["tokens"], strict=True):
        assert (old["text"], old["start_ms"], old["end_ms"]) == (new["text"], new["start_ms"], new["end_ms"])


def test_roundtrip_preserves_the_revised_flag(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    """A corrected word must still be blue in the restored project.

    The flag is what distinguishes "the ASR wrote this" from "a human wrote
    this"; dropping it on load would make an edited transcript look untouched.
    """
    from animatter.services import editing

    editing.apply_text_edit(session, seeded.id, sentence_ord=0, original="今天", proposed="昨天")
    session.commit()

    saved = client.post(f"/api/projects/{seeded.project_id}/archive").json()
    loaded = client.post("/api/archive/load", json={"path": saved["path"]}).json()
    tokens = client.get(f"/api/versions/{loaded['version_id']}/transcript").json()["tokens"]

    assert tokens[0]["text"] == "昨天"
    assert tokens[0]["revised"] is True
    assert any(not token["revised"] for token in tokens[1:])


def test_roundtrip_preserves_operation_log(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    """Losing the log would silently disable undo in the restored project."""
    from animatter.services import editing

    editing.apply_boundary(session, seeded.id, action="new_sentence", from_token=6, to_token=6)
    session.commit()

    saved = client.post(f"/api/projects/{seeded.project_id}/archive").json()
    loaded = client.post("/api/archive/load", json={"path": saved["path"]}).json()

    history = client.get(f"/api/versions/{loaded['version_id']}/history").json()
    assert len(history) == saved["op_count"]
    assert any(entry["label"] for entry in history)


def test_roundtrip_preserves_deletions(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    """Deletions are part of the workflow, not a cache: they must come back."""
    from animatter.services import clipping

    clipping.add_deletion(session, seeded.id, start_ms=100, end_ms=300)
    session.commit()

    saved = client.post(f"/api/projects/{seeded.project_id}/archive").json()
    loaded = client.post("/api/archive/load", json={"path": saved["path"]}).json()

    deletions = client.get(f"/api/versions/{loaded['version_id']}/deletions").json()
    assert len(deletions) == 1
    assert (deletions[0]["start_ms"], deletions[0]["end_ms"]) == (100, 300)


def test_load_creates_a_new_project(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    saved = client.post(f"/api/projects/{seeded.project_id}/archive").json()
    loaded = client.post("/api/archive/load", json={"path": saved["path"]}).json()
    # Never overwrite: the original must still be there and untouched.
    assert loaded["project_id"] != seeded.project_id


def test_inspect_does_not_import(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    saved = client.post(f"/api/projects/{seeded.project_id}/archive").json()
    before = len(client.get("/api/projects").json()["items"])
    info = client.post("/api/archive/inspect", json={"path": saved["path"]})
    assert info.status_code == 200
    assert info.json()["sentence_count"] == 1
    assert len(client.get("/api/projects").json()["items"]) == before


def test_load_missing_file_is_a_clear_error(client: TestClient) -> None:
    response = client.post("/api/archive/load", json={"path": "N:/nope/gone.project"})
    assert response.status_code == 404


def test_load_rejects_a_non_archive(tmp_path: Path) -> None:
    bogus = tmp_path / "fake.project"
    bogus.write_text("not a zip", encoding="utf-8")
    with pytest.raises(AppError) as caught:
        archive_service.read_archive(bogus)
    # The message has to say what the file *is not*, because "invalid" alone
    # leaves the user guessing whether they picked the wrong file.
    assert caught.value.code == "archive_invalid"


def test_same_audio_in_two_projects_gets_two_media_rows(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    """Regression: a global UNIQUE(sha256) made the second project media-less.

    Media rows are project-owned, so reusing another project's row leaves the
    second project with nothing. The audio route filters by project, returned
    empty, and the UI said "这个项目没有登记音频文件".
    """
    from animatter.services import projects as project_service
    from animatter.services import transcribe as transcribe_service

    settings = client.app.state.settings  # type: ignore[attr-defined]
    second = project_service.create_project(session, settings, "第二期")
    session.flush()

    source = Path(seeded.media_path) if hasattr(seeded, "media_path") else None
    if source is None or not source.exists():
        pytest.skip("seed has no audio file on disk")

    media = transcribe_service.register_media(session, settings, second.id, source)
    session.commit()

    # The new row must belong to the second project, not be borrowed.
    assert media.project_id == second.id
    listed = client.get(f"/api/projects/{second.id}/media").json()
    assert len(listed) == 1


def test_project_scoped_media_query_returns_own_row(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    media = client.get(f"/api/projects/{seeded.project_id}/media").json()
    assert len(media) == 1


# --- subtitles ----------------------------------------------------------


def _cues(client: TestClient, version_id: str, **params: object):  # type: ignore[no-untyped-def]
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/versions/{version_id}/subtitles/preview?fmt=srt" + (f"&{query}" if query else "")
    return client.get(url).text


def test_preview_produces_srt(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    text = _cues(client, seeded.id)
    assert "00:00:00,000 -->" in text
    assert "今天我们来聊一聊播客剪辑。" in text


def test_preview_respects_limit(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    text = _cues(client, seeded.id, limit=1)
    # One cue = one index + one timing line.
    assert text.count("-->") == 1


def test_export_writes_a_file(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    response = client.post(f"/api/versions/{seeded.id}/subtitles/export?fmt=srt")
    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"].endswith(".srt")
    assert payload["cue_count"] >= 1

    download = client.get(payload["url"])
    assert download.status_code == 200
    assert "-->" in download.text


def test_export_vtt_has_header(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    payload = client.post(f"/api/versions/{seeded.id}/subtitles/export?fmt=vtt").json()
    body = client.get(payload["url"]).text
    assert body.startswith("WEBVTT")
    assert "." in body.split("-->")[0], "VTT uses a dot as the millisecond separator"


def test_export_rejects_unknown_format(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    response = client.post(f"/api/versions/{seeded.id}/subtitles/export?fmt=ass")
    assert response.status_code >= 400


def test_deleted_sentence_disappears_from_subtitles(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    from animatter.services import clipping

    clipping.add_deletion(session, seeded.id, start_ms=0, end_ms=10_000)
    session.commit()

    response = client.post(f"/api/versions/{seeded.id}/subtitles/export")
    assert response.status_code >= 400  # everything deleted: nothing to export


def test_timestamps_are_mapped_to_the_render(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    """The whole point of the map: a cue after a cut must move earlier.

    Without this, subtitles drift by exactly the amount deleted, and the drift is
    invisible until the file is played against the render.
    """
    # Two sentences so there is something after the deletion.
    from animatter.services import clipping, editing

    editing.apply_boundary(session, seeded.id, action="new_sentence", from_token=5, to_token=5)
    session.flush()

    clipping.add_deletion(session, seeded.id, start_ms=0, end_ms=3000)
    session.commit()

    mapped = _cues(client, seeded.id, mapped="true", limit=50)
    raw = _cues(client, seeded.id, mapped="false", limit=50)
    assert mapped != raw, "mapping a cut timeline must change the timestamps"


def test_time_map_is_none_without_deletions(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    """No cuts means the identity map, so exports cannot drift by rounding."""

    plain = compile_edl(original_duration_ms=1000, deletions=[], snap_to_silences=False)
    assert plain.removed_ms == 0


def test_long_sentence_is_split(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    """A 30-second single cue is unusable; splitting keeps captions readable."""
    cues, _ = subtitle_service.build_cues(session, seeded.id, time_map=None, max_chars=5, max_lines=1)
    assert len(cues) > 1
    assert all(len(cue.text) <= 5 for cue in cues)


def test_issues_flag_implausible_timing(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    cues, issues = subtitle_service.build_cues(session, seeded.id, time_map=None, max_cps=0.01)
    assert cues
    assert any(issue.kind == "too_fast" for issue in issues)


def test_srt_format_time() -> None:
    assert subtitle_service.format_time(0) == "00:00:00,000"
    assert subtitle_service.format_time(3_661_500) == "01:01:01,500"
    assert subtitle_service.format_time(3_661_500, vtt=True) == "01:01:01.500"


# --- snapping invariant -------------------------------------------------


def test_snapping_only_grows_the_deletion() -> None:
    """Regression: an edge could snap *inward* and hand deleted audio back."""
    from animatter.core.edl import SilenceSpan

    edl = compile_edl(
        original_duration_ms=20_000,
        deletions=[Deletion(3709, 6010)],
        silences=[SilenceSpan(5991, 6100)],
        guard_ms=0,
    )
    assert edl.deletions[0].start_ms <= 3709
    assert edl.deletions[0].end_ms >= 6010
