"""Project lifecycle: rows, directories, and the on-disk layout from AGENT.md §5.2."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.models import Project

PROJECT_SUBDIRS: tuple[str, ...] = ("media", "transcripts", "peaks", "edits", "exports", "logs", "derived")


def project_root(settings: Settings, project_id: str) -> Path:
    return settings.projects_dir / project_id


def create_project(session: Session, settings: Settings, name: str) -> Project:
    clean = name.strip()
    if not clean:
        raise AppError(
            "project_name_empty", "项目名不能为空", hint="给这一期播客起一个名字，例如「S02E07 对话某某」。"
        )

    project = Project(
        id=new_id("prj"),
        name=clean,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    session.add(project)
    session.flush()

    root = project_root(settings, project.id)
    for sub in PROJECT_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return project


def list_projects(session: Session) -> list[Project]:
    stmt = select(Project).order_by(Project.created_at.desc())
    return list(session.scalars(stmt))


def get_project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise AppError(
            "project_not_found",
            f"项目 {project_id} 不存在",
            hint="回到项目列表刷新；若文件夹被手动删除，项目需要重建。",
            status=404,
        )
    return project
