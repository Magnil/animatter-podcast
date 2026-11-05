"""Speaker endpoints: list, create, rename, assign, delete."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from animatter.db import Database
from animatter.services import speakers as speaker_service

router = APIRouter(prefix="/api", tags=["speakers"])


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class SpeakerOut(BaseModel):
    local_id: int
    name: str
    color: str
    sentence_count: int


class SpeakerCreateIn(BaseModel):
    name: str = Field(default="", max_length=24)


class SpeakerRenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    color: str | None = None


class AssignIn(BaseModel):
    #: ``null`` clears the assignment (sentence shows no speaker).
    local_id: int | None = None
    sentence_ords: list[int]


@router.get("/projects/{project_id}/speakers", response_model=list[SpeakerOut])
def list_speakers(project_id: str, session: Session = Depends(_session_dependency)) -> list[SpeakerOut]:
    return [
        SpeakerOut(
            local_id=view.local_id,
            name=view.name,
            color=view.color,
            sentence_count=view.sentence_count,
        )
        for view in speaker_service.list_speakers(session, project_id)
    ]


@router.post("/projects/{project_id}/speakers", response_model=SpeakerOut, status_code=201)
def create_speaker(
    project_id: str,
    payload: SpeakerCreateIn,
    session: Session = Depends(_session_dependency),
) -> SpeakerOut:
    """Create a speaker by hand.

    This is the fallback when the ASR model cannot separate voices: the user
    marks who is talking, sentence by sentence.
    """
    view = speaker_service.create_speaker(session, project_id, payload.name)
    return SpeakerOut(
        local_id=view.local_id,
        name=view.name,
        color=view.color,
        sentence_count=view.sentence_count,
    )


@router.patch("/projects/{project_id}/speakers/{local_id}", response_model=SpeakerOut)
def rename_speaker(
    project_id: str,
    local_id: int,
    payload: SpeakerRenameIn,
    session: Session = Depends(_session_dependency),
) -> SpeakerOut:
    view = speaker_service.rename_speaker(session, project_id, local_id, payload.name, payload.color)
    return SpeakerOut(
        local_id=view.local_id,
        name=view.name,
        color=view.color,
        sentence_count=view.sentence_count,
    )


@router.delete("/projects/{project_id}/speakers/{local_id}", status_code=204)
def delete_speaker(project_id: str, local_id: int, session: Session = Depends(_session_dependency)) -> None:
    speaker_service.delete_speaker(session, project_id, local_id)


@router.post("/versions/{version_id}/speakers/assign")
def assign_sentences(
    version_id: str,
    payload: AssignIn,
    session: Session = Depends(_session_dependency),
) -> dict[str, object]:
    """Point sentences at a speaker (or clear them with ``local_id: null``)."""
    count = speaker_service.assign_sentences(session, version_id, payload.sentence_ords, payload.local_id)
    return {"updated": count, "local_id": payload.local_id}
