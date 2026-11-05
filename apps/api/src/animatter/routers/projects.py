"""Project CRUD. Thin by design: the router validates and delegates."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from animatter.db import Database
from animatter.schemas import ProjectCreate, ProjectListOut, ProjectOut
from animatter.services import projects as project_service

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _session_dependency(request: Request) -> Iterator[Session]:
    """One session per request; committed by ``Database.session`` on success."""
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectCreate,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> ProjectOut:
    project = project_service.create_project(session, request.app.state.settings, payload.name)
    return ProjectOut(
        id=project.id,
        name=project.name,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.get("", response_model=ProjectListOut)
def list_projects(session: Session = Depends(_session_dependency)) -> ProjectListOut:
    items = project_service.list_projects(session)
    return ProjectListOut(
        items=[
            ProjectOut(id=p.id, name=p.name, created_at=p.created_at, updated_at=p.updated_at) for p in items
        ],
        total=len(items),
    )


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, session: Session = Depends(_session_dependency)) -> ProjectOut:
    project = project_service.get_project(session, project_id)
    return ProjectOut(
        id=project.id,
        name=project.name,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )
