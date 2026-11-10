"""Keep video anchors honest when an edit can move the sentence grid (§19.3).

One call site per *entry point*, not per service function. A cleanup writes hundreds
of deletion rows in one transaction; refreshing inside ``add_deletion`` would recompile
the EDL four hundred times and, worse, do it while the batch is half-written — the
grid it computed would be a grid that never existed for the user.

**Which edits need this at all.** Only the ones that move a token:

* ``boundaries`` (``new_sentence`` / ``to_prev`` / ``to_next`` / ``merge``) — can add
  or delete a sentence start;
* ``time-shift`` — moves every token at once, but rigidly, so blocks move with it
  instead of being re-seated (see ``routers/align.py``).

Deleting or restoring audio moves no token, so the grid is untouched and a block's
shortened length is derived on read. That is why the deletion path is *not* wired
here: the delete keystroke has to stay instant.

The grid is captured **before** the edit and handed back in, so an edge that was never
on a boundary (the user dragged with snapping off) is left exactly where they put it
instead of being snapped along with the genuinely stale ones.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.services import video as video_service


def grid_before(session: Session, version_id: str) -> tuple[int, ...] | None:
    """The snap grid as it is right now, for passing to :func:`after_boundary_edit`."""
    from animatter.models import TranscriptVersion

    version = session.get(TranscriptVersion, version_id)
    if version is None:
        return None
    return video_service.build_grid(session, version).boundaries


def after_boundary_edit(
    session: Session,
    settings: Settings,
    version_id: str,
    *,
    previous_grid: tuple[int, ...] | None = None,
) -> dict[str, Any] | None:
    """Re-seat block edges the edit invalidated, and report it, or ``None``."""
    report = video_service.refresh_after_audio_edit(
        session, settings, version_id, previous=previous_grid
    )
    return report.as_dict() if report.changed else None
