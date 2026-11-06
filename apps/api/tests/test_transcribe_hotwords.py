"""Hotwords must actually reach the recognition request.

This file exists because of a real regression: the new-project dialog sent
``use_hotwords: true``, pydantic had no such field, silently dropped it, and
every transcription ran with ``vocabulary = {}``. The UI said "热词已启用", the
glossary was populated, and the audio still came back as 漫无日 / 马小挂.

The provider-level test (``test_bailian_provider.py``) proves the wire format;
this one proves the router hands a non-empty map to the service.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.core.hotwords import HotwordCandidate
from animatter.ids import new_id, utcnow_iso
from animatter.models import Media, Project
from animatter.routers import transcribe as transcribe_router
from animatter.services import hotwords as hotword_service
from animatter.services import transcribe as transcribe_service


@pytest.fixture()
def project_with_media(session: Session) -> tuple[str, str]:
    project = Project(id=new_id("prj"), name="热词测试", created_at=utcnow_iso(), updated_at=utcnow_iso())
    session.add(project)
    session.flush()

    media = Media(
        id=new_id("med"),
        project_id=project.id,
        path="x.wav",
        sha256=new_id("sha"),
        duration_ms=1000,
        sample_rate=16000,
        channels=1,
        codec="mp3",
        kind="original",
        created_at=utcnow_iso(),
    )
    session.add(media)
    session.flush()
    session.commit()
    return project.id, media.id


@pytest.fixture()
def captured_options(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the long-running service call with a recorder.

    Patching the *service* rather than httpx keeps this test about the router's
    contract: what vocabulary did it decide to send.
    """
    captured: dict[str, Any] = {}

    def fake_transcribe(
        session: Session,
        settings: Any,
        *,
        project_id: str,
        media: Media,
        options: transcribe_service.TranscribeOptions,
        progress: Any = None,
    ) -> Any:
        captured["options"] = options
        captured["project_id"] = project_id
        return SimpleNamespace(
            id=new_id("ver"),
            version=1,
            duration_ms=1000,
            token_count=10,
            sentence_count=2,
            created_at=utcnow_iso(),
        )

    monkeypatch.setattr(transcribe_router.transcribe_service, "transcribe_media", fake_transcribe)
    return captured


def test_use_hotwords_merges_the_glossary(
    client: TestClient,
    session: Session,
    project_with_media: tuple[str, str],
    captured_options: dict[str, Any],
) -> None:
    project_id, media_id = project_with_media
    hotword_service.add_global(
        session, [HotwordCandidate("漫物质", weight=3), HotwordCandidate("马小褂", weight=3)]
    )
    hotword_service.replace_episode(
        session, project_id=project_id, media_id=media_id, candidates=[HotwordCandidate("张三", weight=4)]
    )
    session.commit()

    response = client.post(
        f"/api/projects/{project_id}/transcribe",
        json={"media_id": media_id, "use_hotwords": True},
    )
    assert response.status_code == 201, response.text

    options = captured_options["options"]
    assert options.vocabulary == {"漫物质": 3, "马小褂": 3, "张三": 4}
    assert response.json()["hotword_count"] == 3


def test_hotwords_absent_when_flag_is_off(
    client: TestClient,
    session: Session,
    project_with_media: tuple[str, str],
    captured_options: dict[str, Any],
) -> None:
    project_id, media_id = project_with_media
    hotword_service.add_global(session, [HotwordCandidate("漫物质", weight=3)])
    session.commit()

    response = client.post(
        f"/api/projects/{project_id}/transcribe",
        json={"media_id": media_id, "use_hotwords": False},
    )
    assert response.status_code == 201
    assert captured_options["options"].vocabulary is None
    # Off means off: reporting a count here would be a lie the UI would show.
    assert response.json()["hotword_count"] == 0


def test_explicit_vocabulary_wins_over_the_glossary(
    client: TestClient,
    session: Session,
    project_with_media: tuple[str, str],
    captured_options: dict[str, Any],
) -> None:
    """Replaying a historical run must send the recorded map, not today's list."""
    project_id, media_id = project_with_media
    hotword_service.add_global(session, [HotwordCandidate("新词", weight=3)])
    session.commit()

    response = client.post(
        f"/api/projects/{project_id}/transcribe",
        json={"media_id": media_id, "use_hotwords": True, "vocabulary": {"旧词": 5}},
    )
    assert response.status_code == 201
    assert captured_options["options"].vocabulary == {"旧词": 5}
    assert response.json()["hotword_count"] == 1


def test_empty_glossary_still_transcribes(
    client: TestClient,
    session: Session,
    project_with_media: tuple[str, str],
    captured_options: dict[str, Any],
) -> None:
    """An empty glossary is not an error — it is just an empty map."""
    project_id, media_id = project_with_media
    response = client.post(
        f"/api/projects/{project_id}/transcribe",
        json={"media_id": media_id, "use_hotwords": True},
    )
    assert response.status_code == 201
    assert captured_options["options"].vocabulary == {}
    assert response.json()["hotword_count"] == 0


def test_other_projects_episode_terms_are_not_leaked(
    client: TestClient,
    session: Session,
    project_with_media: tuple[str, str],
    captured_options: dict[str, Any],
) -> None:
    project_id, media_id = project_with_media
    hotword_service.replace_episode(
        session,
        project_id="prj_other",
        media_id="med_other",
        candidates=[HotwordCandidate("别期嘉宾", weight=5)],
    )
    session.commit()

    response = client.post(
        f"/api/projects/{project_id}/transcribe",
        json={"media_id": media_id, "use_hotwords": True},
    )
    assert response.status_code == 201
    assert captured_options["options"].vocabulary == {}
