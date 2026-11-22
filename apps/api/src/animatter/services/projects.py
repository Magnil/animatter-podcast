"""Project lifecycle: rows, directories, and the on-disk layout from AGENTS.md §5.2."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.models import Project

log = get_logger(__name__)

PROJECT_SUBDIRS: tuple[str, ...] = ("media", "transcripts", "peaks", "edits", "exports", "logs", "derived")


def project_root(settings: Settings, project_id: str) -> Path:
    return settings.projects_dir / project_id


def create_project(
    session: Session, settings: Settings, name: str, *, with_template: bool = True
) -> Project:
    """Create a project row plus its directory layout.

    ``with_template`` installs the bundled default template (§19.36). On by default because a
    新项目 that opens the 视频 panel to 「还没有模板」 has nothing to do there — the default *is*
    the starting point, and a user who wants a different poster imports one over it.

    A failure here never fails the project: the template is a convenience, and losing a
    project because a PNG could not be written would be the worse trade. ``False`` is for
    tests and for callers that know there will be no video (an archive that carries no
    template still gets the default; see ``load_project``).
    """
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
    if with_template:
        _install_default_template(session, settings, project.id)
    return project


def _install_default_template(session: Session, settings: Settings, project_id: str) -> None:
    """Best-effort: a missing bundled file must not cost the user their project.

    Imported here rather than at module level: ``video_template_package`` imports
    ``video_templates``, which imports this module. A local import keeps the cycle out of the
    import graph instead of relying on import order at runtime.
    """
    from animatter.services import video_template_package as package_service

    try:
        package_service.install_default(session, settings, project_id)
    except Exception as exc:  # a broken install is worth a log line, not a failed POST
        log.warning("project.default_template_failed", project=project_id, error=str(exc))


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
