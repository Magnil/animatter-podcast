"""Speaker identity: defaulting, manual creation, assignment, rename, delete.

The interesting cases are all "the model gave us nothing" ones. The real 17.6 s
sample came back with zero ``speaker`` rows and every ``speaker_id`` NULL, so the
manual path is not a fallback in theory — it is the only path that sample takes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version

from animatter.models import Project, Sentence, Speaker, TranscriptVersion
from animatter.services import speakers as speaker_service
from animatter.services import transcribe as transcribe_service


@pytest.fixture()
def version(session: Session) -> TranscriptVersion:
    """Three sentences of three words each, with no diarization at all."""
    created = seed_version(session, text="一二三四五六七八九", sizes=[3, 3, 3])
    session.commit()
    return created


def _project_id(version: TranscriptVersion) -> str:
    return version.project_id


def _annotate(session: Session, version: TranscriptVersion, mapping: dict[int, int]) -> None:
    """Set ``speaker_id`` per sentence ord, bypassing the service on purpose."""
    rows = list(
        session.query(Sentence).filter(Sentence.version_id == version.id).order_by(Sentence.ord)
    )
    for ord_, local_id in mapping.items():
        rows[ord_].speaker_id = local_id
    session.commit()


# --- listing ------------------------------------------------------------


def test_a_transcript_without_diarization_lists_no_speakers(
    client: TestClient, version: TranscriptVersion
) -> None:
    assert client.get(f"/api/projects/{_project_id(version)}/speakers").json() == []


def test_ids_referenced_by_sentences_appear_without_speaker_rows(
    client: TestClient, session: Session, version: TranscriptVersion
) -> None:
    """A diarizer that emitted ids but no config must still show up in the UI."""
    _annotate(session, version, {0: 0, 1: 1, 2: 0})

    speakers = client.get(f"/api/projects/{_project_id(version)}/speakers").json()
    assert [s["local_id"] for s in speakers] == [0, 1]
    assert [s["name"] for s in speakers] == ["说话人1", "说话人2"]
    assert [s["sentence_count"] for s in speakers] == [2, 1]
    assert all(s["color"].startswith("#") for s in speakers)


def test_unknown_project_is_404(client: TestClient) -> None:
    assert client.get("/api/projects/prj_missing/speakers").status_code == 404


def test_speakers_are_scoped_to_their_project(
    client: TestClient, session: Session, version: TranscriptVersion
) -> None:
    client.post(f"/api/projects/{_project_id(version)}/speakers", json={"name": "甲"})

    other = client.post("/api/projects", json={"name": "另一个"}).json()
    assert client.get(f"/api/projects/{other['id']}/speakers").json() == []


# --- manual creation ----------------------------------------------------


def test_create_speaker_is_possible_without_diarization(
    client: TestClient, version: TranscriptVersion
) -> None:
    created = client.post(f"/api/projects/{_project_id(version)}/speakers", json={"name": ""})
    assert created.status_code == 201
    assert created.json() == {
        "local_id": 0,
        "name": "说话人1",
        "color": "#2563eb",
        "sentence_count": 0,
    }

    second = client.post(
        f"/api/projects/{_project_id(version)}/speakers", json={"name": "嘉宾 A"}
    ).json()
    assert second["local_id"] == 1
    assert second["name"] == "嘉宾 A"


def test_created_speakers_do_not_reuse_a_taken_local_id(
    client: TestClient, session: Session, version: TranscriptVersion
) -> None:
    _annotate(session, version, {0: 0, 1: 1})
    created = client.post(
        f"/api/projects/{_project_id(version)}/speakers", json={"name": "主持人"}
    ).json()
    assert created["local_id"] == 2


# --- assignment ---------------------------------------------------------


def test_assign_sentences_then_count_them(
    client: TestClient, version: TranscriptVersion
) -> None:
    client.post(f"/api/projects/{_project_id(version)}/speakers", json={"name": "主持人"})

    response = client.post(
        f"/api/versions/{version.id}/speakers/assign",
        json={"local_id": 0, "sentence_ords": [0, 2]},
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 2, "local_id": 0}

    speakers = client.get(f"/api/projects/{_project_id(version)}/speakers").json()
    assert speakers[0]["sentence_count"] == 2


def test_assign_null_clears_the_annotation(
    client: TestClient, session: Session, version: TranscriptVersion
) -> None:
    _annotate(session, version, {0: 0, 2: 0})

    client.post(
        f"/api/versions/{version.id}/speakers/assign",
        json={"local_id": None, "sentence_ords": [2]},
    )
    speakers = client.get(f"/api/projects/{_project_id(version)}/speakers").json()
    assert [s["sentence_count"] for s in speakers] == [1]


def test_assign_rejects_an_unknown_sentence(
    client: TestClient, version: TranscriptVersion
) -> None:
    client.post(f"/api/projects/{_project_id(version)}/speakers", json={"name": "主持人"})

    response = client.post(
        f"/api/versions/{version.id}/speakers/assign",
        json={"local_id": 0, "sentence_ords": [0, 99]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "speaker_invalid"
    assert "99" in response.json()["error"]["message"]


def test_assign_with_no_sentences_is_rejected(
    client: TestClient, version: TranscriptVersion
) -> None:
    client.post(f"/api/projects/{_project_id(version)}/speakers", json={"name": "主持人"})
    response = client.post(
        f"/api/versions/{version.id}/speakers/assign",
        json={"local_id": 0, "sentence_ords": []},
    )
    assert response.status_code == 400


# --- rename -------------------------------------------------------------


def test_rename_updates_every_sentence_through_the_local_id(
    client: TestClient, session: Session, version: TranscriptVersion
) -> None:
    """Renaming must touch one row, not every sentence."""
    _annotate(session, version, {0: 0, 2: 0})

    renamed = client.patch(
        f"/api/projects/{_project_id(version)}/speakers/0", json={"name": "主持人"}
    )
    assert renamed.status_code == 200
    body = renamed.json()
    assert body["local_id"] == 0
    assert body["name"] == "主持人"
    assert body["sentence_count"] == 2

    # The sentences still carry the integer, so they render the new name.
    rows = list(session.query(Sentence).filter(Sentence.speaker_id == 0))
    assert len(rows) == 2
    session.expire_all()
    assert session.query(Speaker).filter(Speaker.name == "主持人").count() == 1


def test_rename_rejects_a_blank_name(
    client: TestClient, session: Session, version: TranscriptVersion
) -> None:
    _annotate(session, version, {0: 0})
    response = client.patch(
        f"/api/projects/{_project_id(version)}/speakers/0", json={"name": "   "}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "speaker_invalid"


def test_rename_rejects_an_overlong_name(
    client: TestClient, session: Session, version: TranscriptVersion
) -> None:
    _annotate(session, version, {0: 0})
    response = client.patch(
        f"/api/projects/{_project_id(version)}/speakers/0", json={"name": "字" * 40}
    )
    # Pydantic's max_length catches this before the service does.
    assert response.status_code == 422


def test_rename_can_also_set_the_colour(
    client: TestClient, session: Session, version: TranscriptVersion
) -> None:
    _annotate(session, version, {0: 1})
    response = client.patch(
        f"/api/projects/{_project_id(version)}/speakers/1",
        json={"name": "嘉宾", "color": "#ff00aa"},
    )
    assert response.status_code == 200
    assert response.json()["color"] == "#ff00aa"


# --- delete -------------------------------------------------------------


def test_delete_removes_the_row_but_leaves_sentences_annotated(
    client: TestClient, session: Session, version: TranscriptVersion
) -> None:
    _annotate(session, version, {0: 0, 2: 0})

    assert client.delete(f"/api/projects/{_project_id(version)}/speakers/0").status_code == 204
    # The sentences still say 0; the view synthesises a default for it rather
    # than showing "未标注" for two sentences out of three.
    speakers = client.get(f"/api/projects/{_project_id(version)}/speakers").json()
    assert [s["local_id"] for s in speakers] == [0]
    assert speakers[0]["name"] == "说话人1"


def test_delete_unknown_speaker_is_404(
    client: TestClient, version: TranscriptVersion
) -> None:
    assert client.delete(f"/api/projects/{_project_id(version)}/speakers/7").status_code == 404


# --- service contract ---------------------------------------------------


def test_sync_with_diarization_records_every_id(session: Session, version: TranscriptVersion) -> None:
    speaker_service.sync_with_diarization(session, version.project_id, {0, 3})
    session.commit()

    views = speaker_service.list_speakers(session, version.project_id)
    assert [v.local_id for v in views] == [0, 3]
    # 3 is past the default name table, which must not raise.
    assert views[1].name == "说话人4"


def test_ensure_speaker_is_idempotent(session: Session, version: TranscriptVersion) -> None:
    first = speaker_service.ensure_speaker(session, version.project_id, 0)
    second = speaker_service.ensure_speaker(session, version.project_id, 0)
    assert first.id == second.id
    assert session.query(Speaker).count() == 1


def test_sentence_count_ignores_other_projects(
    client: TestClient, session: Session, version: TranscriptVersion
) -> None:
    """A second project's sentences must not inflate the first project's counts."""
    other = client.post("/api/projects", json={"name": "别人的项目"}).json()
    assert other["id"] != version.project_id

    other_version = TranscriptVersion(
        id="ver_other",
        project_id=other["id"],
        media_id="med_other",
        job_id="",
        version=1,
        created_at="2026-01-01T00:00:00Z",
        duration_ms=900,
        token_count=3,
        sentence_count=1,
        is_active=True,
    )
    session.add(other_version)
    session.flush()
    session.add(
        Sentence(
            id="sen_other",
            version_id=other_version.id,
            ord=0,
            token_start=0,
            token_end=3,
            speaker_id=0,
            origin="asr",
            updated_at="2026-01-01T00:00:00Z",
        )
    )
    session.commit()

    assert speaker_service._sentence_counts(session, version.project_id) == {}
    assert speaker_service._sentence_counts(session, other["id"]) == {0: 1}
    assert transcribe_service.load_sentences(session, version.id)[0].speaker_id is None
    assert session.get(Project, other["id"]) is not None
