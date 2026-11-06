"""The label endpoint, end to end against a seeded project.

Pins the things that would silently ship a broken file:

* the response is a downloadable ``.txt`` whose name is the project's, not
  ``exp_...``;
* the body parses as Audacity labels line by line;
* the route works with **no deletions at all** — that is the whole point of it;
* silence detection failing must not take the word labels down with it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.ids import new_id, utcnow_iso
from animatter.models import Media, Project, Sentence, Token, TranscriptVersion


@pytest.fixture()
def seeded(session: Session, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A 4-second episode: 今天 / 嗯 / 我们 / 聊聊, then a 1 s pause."""
    from animatter.media import ffmpeg as ffmpeg_module

    project = Project(id=new_id("prj"), name="漫物质", created_at=utcnow_iso(), updated_at=utcnow_iso())
    session.add(project)
    session.flush()

    media = Media(
        id=new_id("med"),
        project_id=project.id,
        path="x.wav",
        sha256=new_id("sha"),
        duration_ms=4000,
        sample_rate=16000,
        channels=1,
        codec="mp3",
        kind="original",
        created_at=utcnow_iso(),
    )
    session.add(media)
    session.flush()

    version = TranscriptVersion(
        id=new_id("ver"),
        project_id=project.id,
        media_id=media.id,
        job_id="job",
        version=1,
        created_at=utcnow_iso(),
        duration_ms=4000,
        token_count=4,
        sentence_count=1,
        is_active=True,
    )
    session.add(version)
    session.flush()

    for idx, (text, start, end) in enumerate(
        [("今天", 0, 500), ("嗯", 500, 800), ("我们", 800, 1400), ("聊聊", 1400, 2000)]
    ):
        session.add(
            Token(
                version_id=version.id,
                idx=idx,
                start_ms=start,
                end_ms=end,
                text=text,
                kind="word",
                src_word_idx=idx,
                punc_after="",
                derived=False,
                revised=False,
            )
        )
    session.add(
        Sentence(
            id=new_id("sen"),
            version_id=version.id,
            ord=0,
            token_start=0,
            token_end=4,
            speaker_id=None,
            origin="asr",
            updated_at=utcnow_iso(),
        )
    )
    session.commit()

    monkeypatch.setattr(
        ffmpeg_module,
        "detect_silence",
        lambda *_a, **_k: [ffmpeg_module.SilenceSpan(2000, 3000)],
    )
    return {"project": project.id, "media": media.id, "version": version.id}


def _parse(body: str) -> list[tuple[float, float, str]]:
    out = []
    for line in body.splitlines():
        parts = line.split("\t")
        assert len(parts) == 3, f"not a label line: {line!r}"
        out.append((float(parts[0]), float(parts[1]), parts[2]))
    return out


def test_download_works_without_any_deletions(client: TestClient, seeded: dict[str, str]) -> None:
    # The point of the feature: 导出成片音频 is disabled until something is cut,
    # and this must not inherit that gate.
    response = client.get(f"/api/versions/{seeded['version']}/labels/file")
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"0.000\t0.500\t")


def test_filename_header_is_the_project_name(client: TestClient, seeded: dict[str, str]) -> None:
    from urllib.parse import unquote

    response = client.get(f"/api/versions/{seeded['version']}/labels/file")
    name = unquote(response.headers["X-Labels-Filename"])
    assert name == "漫物质_audacity_labels.txt"
    # latin-1 headers cannot carry the raw name; percent-encoding is the contract.
    assert "漫" not in response.headers["X-Labels-Filename"]


def test_body_is_crlf_utf8_with_no_bom(client: TestClient, seeded: dict[str, str]) -> None:
    raw = client.get(f"/api/versions/{seeded['version']}/labels/file").content
    assert raw.endswith(b"\r\n")
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8").splitlines()[0] == "0.000\t0.500\t今天"


def test_filler_is_tagged(client: TestClient, seeded: dict[str, str]) -> None:
    body = client.get(f"/api/versions/{seeded['version']}/labels/file").text
    texts = [row[2] for row in _parse(body)]
    assert "[FILLER]嗯" in texts


def test_silence_labels_are_included(client: TestClient, seeded: dict[str, str]) -> None:
    body = client.get(f"/api/versions/{seeded['version']}/labels/file").text
    # Not 2.000 but 2.050: the span touches the previous word, so the 50 ms
    # speech-pad is applied. Cutting the raw detector span would clip the tail
    # of 「聊聊」. This is the same geometry one-click cleanup uses.
    assert (2.05, 3.0, "[SILENCE]") in _parse(body)


def test_silence_can_be_turned_off(client: TestClient, seeded: dict[str, str]) -> None:
    body = client.get(f"/api/versions/{seeded['version']}/labels/file?include_silence=false").text
    assert "[SILENCE]" not in body
    assert len(_parse(body)) == 4


def test_missing_version_is_404(client: TestClient, seeded: dict[str, str]) -> None:
    assert client.get("/api/versions/ver_nope/labels/file").status_code == 404


def test_info_counts_match_the_file(client: TestClient, seeded: dict[str, str]) -> None:
    info = client.get(f"/api/versions/{seeded['version']}/labels/info").json()
    body = client.get(f"/api/versions/{seeded['version']}/labels/file").text
    words_and_tags = _parse(body)
    assert info["words"] == 3  # the 嗯 is counted as a filler, not a word
    assert info["fillers"] == 1
    # `info` skips silence detection on purpose, so it under-reports `lines`
    # relative to a full export. Word counts are the numbers it can promise.
    assert info["tokens"] == 4
    assert len(words_and_tags) >= 4


def test_preview_shows_real_lines(client: TestClient, seeded: dict[str, str]) -> None:
    text = client.get(f"/api/versions/{seeded['version']}/labels/preview?limit=2").text
    assert text.splitlines()[0] == "0.000\t0.500\t今天"
    assert len(text.splitlines()) == 2


def test_silence_detector_failure_does_not_kill_the_export(
    client: TestClient, seeded: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Labels are the fallback path; it must not depend on ffmpeg succeeding."""
    from animatter.media import ffmpeg as ffmpeg_module

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(ffmpeg_module, "detect_silence", boom)
    response = client.get(f"/api/versions/{seeded['version']}/labels/file?include_silence=true")
    assert response.status_code == 200, response.text
    # Degrades to word labels rather than failing: the words are the part the
    # user cannot reconstruct by hand.
    assert "[SILENCE]" not in response.text
    assert "[FILLER]嗯" in response.text


def test_deleted_tokens_are_skipped_on_request(client: TestClient, seeded: dict[str, str]) -> None:
    client.post(
        f"/api/versions/{seeded['version']}/deletions",
        json={"token_ids": [1]},
    )
    skipped = client.get(f"/api/versions/{seeded['version']}/labels/file?skip_deleted=true").text
    kept = client.get(f"/api/versions/{seeded['version']}/labels/file?skip_deleted=false").text
    assert "[FILLER]嗯" not in skipped
    assert "[FILLER]嗯" in kept
