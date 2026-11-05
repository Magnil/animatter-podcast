"""Per-episode hotword add/remove must be narrow operations.

The UI's "添加" used to POST to the replace endpoint, which deletes the whole
episode list first. The visible symptom: adding a second guest name silently
dropped the first. These tests pin the two verbs apart — append keeps the list,
replace rebuilds it — because they are one careless refactor away from merging
back together.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.core.hotwords import HotwordCandidate
from animatter.ids import new_id, utcnow_iso
from animatter.models import Media, Project
from animatter.services import hotwords as hotword_service


@pytest.fixture()
def episode(session: Session) -> tuple[str, str]:
    project = Project(id=new_id("prj"), name="热词", created_at=utcnow_iso(), updated_at=utcnow_iso())
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


def test_append_keeps_existing_terms(session: Session, episode: tuple[str, str]) -> None:
    project_id, media_id = episode
    hotword_service.append_episode(
        session, project_id=project_id, media_id=media_id, candidates=[HotwordCandidate("马小褂")]
    )
    hotword_service.append_episode(
        session, project_id=project_id, media_id=media_id, candidates=[HotwordCandidate("漫物质")]
    )
    texts = {row.text for row in hotword_service.list_episode(session, project_id, media_id)}
    assert texts == {"马小褂", "漫物质"}


def test_append_updates_weight_in_place(session: Session, episode: tuple[str, str]) -> None:
    project_id, media_id = episode
    hotword_service.append_episode(
        session, project_id=project_id, media_id=media_id, candidates=[HotwordCandidate("马小褂", weight=2)]
    )
    hotword_service.append_episode(
        session, project_id=project_id, media_id=media_id, candidates=[HotwordCandidate("马小褂", weight=5)]
    )
    rows = hotword_service.list_episode(session, project_id, media_id)
    assert len(rows) == 1
    assert rows[0].weight == 5


def test_append_does_not_duplicate(session: Session, episode: tuple[str, str]) -> None:
    project_id, media_id = episode
    for _ in range(3):
        hotword_service.append_episode(
            session, project_id=project_id, media_id=media_id, candidates=[HotwordCandidate("马小褂")]
        )
    assert len(hotword_service.list_episode(session, project_id, media_id)) == 1


def test_remove_episode_deletes_only_the_named_term(
    session: Session, episode: tuple[str, str]
) -> None:
    project_id, media_id = episode
    hotword_service.append_episode(
        session,
        project_id=project_id,
        media_id=media_id,
        candidates=[HotwordCandidate("马小褂"), HotwordCandidate("漫物质")],
    )
    hotword_service.remove_episode(session, project_id=project_id, media_id=media_id, text="马小褂")
    texts = [row.text for row in hotword_service.list_episode(session, project_id, media_id)]
    assert texts == ["漫物质"]


def test_remove_episode_is_idempotent(session: Session, episode: tuple[str, str]) -> None:
    project_id, media_id = episode
    hotword_service.remove_episode(session, project_id=project_id, media_id=media_id, text="没有这个词")
    # Must not raise: a double click on "×" is a normal user action.
    assert hotword_service.list_episode(session, project_id, media_id) == []


def test_remove_episode_ignores_other_projects(session: Session, episode: tuple[str, str]) -> None:
    project_id, media_id = episode
    hotword_service.append_episode(
        session, project_id="prj_other", media_id=media_id, candidates=[HotwordCandidate("别人的词")]
    )
    hotword_service.remove_episode(session, project_id=project_id, media_id=media_id, text="别人的词")
    assert len(hotword_service.list_episode(session, "prj_other", media_id)) == 1


def test_append_endpoint_does_not_wipe_the_list(
    client: TestClient, session: Session, episode: tuple[str, str]
) -> None:
    project_id, media_id = episode
    base = f"/api/projects/{project_id}/media/{media_id}/hotwords"

    first = client.post(f"{base}/append", json=[{"text": "马小褂", "weight": 3}])
    assert first.status_code == 201, first.text
    second = client.post(f"{base}/append", json=[{"text": "漫物质", "weight": 3}])
    assert second.status_code == 201, second.text

    listed = client.get(base).json()
    assert {item["text"] for item in listed} == {"马小褂", "漫物质"}


def test_replace_endpoint_still_replaces(
    client: TestClient, session: Session, episode: tuple[str, str]
) -> None:
    """Outline extraction depends on this; do not "fix" it into an append."""
    project_id, media_id = episode
    base = f"/api/projects/{project_id}/media/{media_id}/hotwords"
    client.post(f"{base}/append", json=[{"text": "旧词", "weight": 3}])
    client.post(base, json=[{"text": "新词", "weight": 3}])
    assert [item["text"] for item in client.get(base).json()] == ["新词"]


def test_delete_endpoint_removes_one_term(
    client: TestClient, session: Session, episode: tuple[str, str]
) -> None:
    project_id, media_id = episode
    base = f"/api/projects/{project_id}/media/{media_id}/hotwords"
    client.post(f"{base}/append", json=[{"text": "马小褂", "weight": 3}, {"text": "漫物质", "weight": 3}])

    removed = client.delete(f"{base}/%E9%A9%AC%E5%B0%8F%E8%A4%82")  # 马小褂
    assert removed.status_code == 204, removed.text
    assert [item["text"] for item in client.get(base).json()] == ["漫物质"]
