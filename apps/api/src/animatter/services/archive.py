"""Project archiving: save the whole editing workflow to a ``.project`` file.

The point of this module is that a project is *resumable*, not just exportable.
That means the archive has to carry everything the workbench reads back: the
transcript, every sentence boundary, every text correction, every deletion, the
operation log that makes undo meaningful, and the hotwords.

What it deliberately does **not** carry is the audio. A two-hour WAV is gigabytes
and the archive is meant to be a small file you can keep next to the episode. The
archive records the media's sha256 instead and re-links on load; if the file
moved, load still succeeds and reports that the audio needs relinking, because
losing the audio path must not cost the user their editing work.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core import sentence as sentence_core
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.models import (
    CorrectionCandidate,
    Deletion,
    EditOp,
    EpisodeHotword,
    Media,
    Outline,
    Project,
    Sentence,
    Token,
    TranscriptVersion,
)
from animatter.services import projects as project_service

log = get_logger(__name__)

#: Format version. Bumping it means an older archive is still readable *if* the
#: loader handles both, so never bump without extending the loader.
ARCHIVE_VERSION = 1
ARCHIVE_SUFFIX = ".project"


@dataclass(frozen=True)
class ArchiveInfo:
    path: Path
    version: int
    saved_at: str
    project_name: str
    duration_ms: int
    sentence_count: int
    deletion_count: int
    op_count: int
    media_filename: str
    media_sha256: str


def _dump_sentences(version_id: str, session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(Sentence).where(Sentence.version_id == version_id).order_by(Sentence.ord))
    return [
        {
            "ord": row.ord,
            "token_start": row.token_start,
            "token_end": row.token_end,
            "speaker_id": row.speaker_id,
        }
        for row in rows
    ]


def _dump_tokens(version_id: str, session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(Token).where(Token.version_id == version_id).order_by(Token.idx))
    return [
        {
            "idx": row.idx,
            "text": row.text,
            "punc_after": row.punc_after,
            "start_ms": row.start_ms,
            "end_ms": row.end_ms,
            "derived": row.derived,
            "kind": row.kind,
            "src_word_idx": row.src_word_idx,
            "revised": row.revised,
            "hidden": row.hidden,
        }
        for row in rows
    ]


def save_project(
    session: Session,
    settings: Settings,
    project_id: str,
    *,
    target: Path | None = None,
) -> ArchiveInfo:
    """Write the project to ``target`` (or its own directory) and return metadata."""
    project = project_service.get_project(session, project_id)
    version = session.scalars(
        select(TranscriptVersion)
        .where(TranscriptVersion.project_id == project_id)
        .order_by(TranscriptVersion.version.desc())
    ).first()
    if version is None:
        raise AppError(
            "nothing_to_save",
            "这个项目还没有转写结果",
            hint="先转写音频，再保存项目。",
        )

    media = session.get(Media, version.media_id)
    sentences = _dump_sentences(version.id, session)
    tokens = _dump_tokens(version.id, session)

    deletions = [
        {
            "start_ms": row.start_ms,
            "end_ms": row.end_ms,
            "source": row.source,
            "token_ids": json.loads(row.token_ids_json or "[]"),
            "removed": row.removed,
        }
        for row in session.scalars(
            select(Deletion).where(Deletion.version_id == version.id).order_by(Deletion.start_ms)
        )
    ]

    ops = [
        {
            "seq": row.seq,
            "op_type": row.op_type,
            "payload": json.loads(row.payload_json or "{}"),
            "undone": row.undone,
            "created_at": row.created_at,
        }
        for row in session.scalars(select(EditOp).where(EditOp.version_id == version.id).order_by(EditOp.seq))
    ]

    episode_hotwords = []
    if media is not None:
        episode_hotwords = [
            {"text": row.text, "weight": row.weight, "lang": row.lang, "enabled": row.enabled}
            for row in session.scalars(select(EpisodeHotword).where(EpisodeHotword.media_id == media.id))
        ]

    outline = session.scalars(
        select(Outline).where(Outline.project_id == project_id).order_by(Outline.created_at.desc())
    ).first()

    candidates = [
        {
            "sentence_ord": row.sentence_ord,
            "token_start": row.token_start,
            "token_end": row.token_end,
            "original_text": row.original_text,
            "proposed_text": row.proposed_text,
            "matched_hotword": row.matched_hotword,
            "confidence": row.confidence,
            "reason": row.reason,
            "status": row.status,
            "source": row.source,
        }
        for row in session.scalars(
            select(CorrectionCandidate).where(CorrectionCandidate.version_id == version.id)
        )
    ]

    payload: dict[str, Any] = {
        "archive_version": ARCHIVE_VERSION,
        "saved_at": utcnow_iso(),
        "project": {"name": project.name, "created_at": project.created_at},
        "media": (
            {
                "filename": Path(media.path).name,
                "path": media.path,
                "sha256": media.sha256,
                "duration_ms": media.duration_ms,
                "sample_rate": media.sample_rate,
                "channels": media.channels,
                "codec": media.codec,
            }
            if media is not None
            else None
        ),
        "version": {
            "version": version.version,
            "duration_ms": version.duration_ms,
            "token_count": version.token_count,
            "sentence_count": version.sentence_count,
        },
        "sentences": sentences,
        "tokens": tokens,
        "deletions": deletions,
        "ops": ops,
        "episode_hotwords": episode_hotwords,
        "outline": ({"content": outline.content} if outline is not None else None),
        "candidates": candidates,
    }

    if target is None:
        root = project_service.project_root(settings, project_id)
        target = root / f"{_safe_name(project.name)}{ARCHIVE_SUFFIX}"
    target.parent.mkdir(parents=True, exist_ok=True)

    # JSON inside a zip: readable with any tool, and compresses a transcript well.
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project.json", json.dumps(payload, ensure_ascii=False, indent=1))

    log.info("project.saved", project=project_id, path=str(target), bytes=target.stat().st_size)
    return ArchiveInfo(
        path=target,
        version=ARCHIVE_VERSION,
        saved_at=payload["saved_at"],
        project_name=project.name,
        duration_ms=version.duration_ms,
        sentence_count=len(sentences),
        deletion_count=sum(1 for d in deletions if not d["removed"]),
        op_count=len(ops),
        media_filename=Path(media.path).name if media else "",
        media_sha256=media.sha256 if media else "",
    )


def read_archive(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AppError("archive_missing", f"找不到文件 {path}", status=404)
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read("project.json")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise AppError(
            "archive_invalid",
            "这不是有效的项目存档",
            hint="确认选择的是 .project 文件，且没有被截断。",
        ) from exc

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppError("archive_invalid", "项目存档内容损坏") from exc
    if not isinstance(loaded, dict):
        raise AppError("archive_invalid", "项目存档结构不对：期望一个对象")
    payload: dict[str, Any] = loaded

    version = int(payload.get("archive_version", 0))
    if version < 1 or version > ARCHIVE_VERSION:
        raise AppError(
            "archive_version_unsupported",
            f"存档版本 {version} 不受支持",
            hint=f"当前版本可读 1–{ARCHIVE_VERSION}。",
        )
    return payload


def inspect_archive(path: Path) -> ArchiveInfo:
    payload = read_archive(path)
    return ArchiveInfo(
        path=path,
        version=int(payload["archive_version"]),
        saved_at=str(payload.get("saved_at", "")),
        project_name=str(payload.get("project", {}).get("name", "")),
        duration_ms=int(payload.get("version", {}).get("duration_ms", 0)),
        sentence_count=len(payload.get("sentences", [])),
        deletion_count=sum(1 for d in payload.get("deletions", []) if not d.get("removed")),
        op_count=len(payload.get("ops", [])),
        media_filename=str(payload.get("media", {}).get("filename", "")),
        media_sha256=str(payload.get("media", {}).get("sha256", "")),
    )


def load_project(
    session: Session,
    settings: Settings,
    path: Path,
    *,
    name: str | None = None,
    media_path: str | None = None,
) -> tuple[Project, TranscriptVersion, bool]:
    """Restore a project. Returns ``(project, version, media_relinked)``.

    Loading always creates a *new* project rather than overwriting an existing
    one: silently replacing a project the user is currently editing is
    unrecoverable, and the archive can always be loaded twice.
    """
    payload = read_archive(path)

    archive_name = str(payload.get("project", {}).get("name", "载入的项目"))
    project = project_service.create_project(session, settings, name or f"{archive_name}（载入）")

    media_payload = payload.get("media")
    media: Media | None = None
    relinked = False
    if isinstance(media_payload, dict):
        media, relinked = _restore_media(session, settings, project, media_payload, media_path)

    version_payload = payload.get("version", {})
    version = TranscriptVersion(
        id=new_id("ver"),
        project_id=project.id,
        media_id=media.id if media else (media_payload or {}).get("id", "") or "",
        version=int(version_payload.get("version", 1)),
        duration_ms=int(version_payload.get("duration_ms", 0)),
        token_count=int(version_payload.get("token_count", 0)),
        sentence_count=int(version_payload.get("sentence_count", 0)),
        job_id="",
        is_active=True,
        created_at=utcnow_iso(),
    )
    if media is None:
        raise AppError(
            "archive_media_missing",
            "存档里没有音频信息",
            hint="这个存档无法恢复时间轴；请用原始音频重新转写。",
        )
    session.add(version)
    session.flush()

    for token in payload.get("tokens", []):
        session.add(
            Token(
                # token.id is an autoincrementing INTEGER; assigning a string id
                # here fails as "datatype mismatch".
                version_id=version.id,
                idx=int(token["idx"]),
                text=str(token["text"]),
                punc_after=str(token.get("punc_after", "")),
                start_ms=int(token["start_ms"]),
                end_ms=int(token["end_ms"]),
                derived=bool(token.get("derived", False)),
                kind=str(token.get("kind", "char")),
                src_word_idx=int(token.get("src_word_idx", -1)),
                revised=bool(token.get("revised", False)),
                hidden=bool(token.get("hidden", False)),
            )
        )

    for item in payload.get("sentences", []):
        session.add(
            Sentence(
                id=new_id("sen"),
                version_id=version.id,
                ord=int(item["ord"]),
                token_start=int(item["token_start"]),
                token_end=int(item["token_end"]),
                speaker_id=item.get("speaker_id"),
                origin=str(item.get("origin", "asr")),
                updated_at=utcnow_iso(),
            )
        )
    session.flush()

    # The sentence layout must be internally consistent before anything else is
    # restored; a corrupted archive must not produce a half-editable project.
    spans = [
        sentence_core.SentenceSpan(
            ord=int(item["ord"]),
            token_start=int(item["token_start"]),
            token_end=int(item["token_end"]),
        )
        for item in payload.get("sentences", [])
    ]
    sentence_core.validate(spans, len(payload.get("tokens", [])))

    for deletion in payload.get("deletions", []):
        session.add(
            Deletion(
                id=new_id("del"),
                version_id=version.id,
                start_ms=int(deletion["start_ms"]),
                end_ms=int(deletion["end_ms"]),
                source=str(deletion.get("source", "token")),
                token_ids_json=json.dumps(deletion.get("token_ids", [])),
                created_at=utcnow_iso(),
                removed=bool(deletion.get("removed", False)),
            )
        )

    for op in payload.get("ops", []):
        session.add(
            EditOp(
                id=new_id("edt"),
                project_id=project.id,
                version_id=version.id,
                seq=int(op["seq"]),
                op_type=str(op["op_type"]),
                payload_json=json.dumps(op.get("payload", {}), ensure_ascii=False),
                undone=bool(op.get("undone", False)),
                created_at=str(op.get("created_at", utcnow_iso())),
            )
        )

    if media is not None:
        for item in payload.get("episode_hotwords", []):
            session.add(
                EpisodeHotword(
                    id=new_id("ehw"),
                    project_id=project.id,
                    media_id=media.id,
                    text=str(item["text"]),
                    weight=int(item.get("weight", 3)),
                    lang=str(item.get("lang", "zh")),
                    enabled=bool(item.get("enabled", True)),
                    origin="manual",
                )
            )

        outline_payload = payload.get("outline")
        if isinstance(outline_payload, dict):
            session.add(
                Outline(
                    id=new_id("out"),
                    project_id=project.id,
                    media_id=media.id,
                    content=str(outline_payload.get("content", "")),
                    created_at=utcnow_iso(),
                )
            )

    for item in payload.get("candidates", []):
        session.add(
            CorrectionCandidate(
                id=new_id("cor"),
                version_id=version.id,
                sentence_ord=int(item["sentence_ord"]),
                token_start=int(item["token_start"]),
                token_end=int(item["token_end"]),
                original_text=str(item["original_text"]),
                proposed_text=str(item["proposed_text"]),
                matched_hotword=str(item.get("matched_hotword", "")),
                confidence=float(item.get("confidence", 0.0)),
                reason=str(item.get("reason", "")),
                status=str(item.get("status", "pending")),
                source=str(item.get("source", "llm")),
                created_at=utcnow_iso(),
            )
        )

    session.flush()
    log.info(
        "project.loaded",
        project=project.id,
        sentences=len(payload.get("sentences", [])),
        relinked=relinked,
    )
    return project, version, relinked


def _restore_media(
    session: Session,
    settings: Settings,
    project: Project,
    media_payload: dict[str, Any],
    override_path: str | None,
) -> tuple[Media | None, bool]:
    """Re-link the media, hashing the file when one is available.

    Three cases, in order of how much the user has to do:
    1. The file is present — register it (which hashes it) and report whether the
       hash still matches the archive.
    2. The file is gone but a media row with that hash already exists — reuse it.
       ``media.sha256`` is globally unique, so inserting a second row for the same
       audio would fail, and the existing row already carries the right duration
       and channel facts.
    3. Neither — insert a placeholder keyed by a synthetic hash. It must not be
       the recorded one, or a second restore of the same archive would collide on
       the unique constraint.
    """
    from animatter.services.transcribe import register_media, sha256_of

    candidate = override_path or str(media_payload.get("path") or "")
    path = Path(candidate).expanduser() if candidate else None
    recorded = str(media_payload.get("sha256", ""))

    if path is not None and path.exists():
        media = register_media(session, settings, project.id, path)
        matches = sha256_of(path) == recorded if recorded else False
        return media, matches

    if recorded:
        # Only this project's own rows may be reused. Matching globally would hand
        # back another project's media, leaving this project with none — the bug
        # that produced "这个项目没有登记音频文件".
        existing = session.scalars(
            select(Media).where(Media.project_id == project.id, Media.sha256 == recorded)
        ).first()
        if existing is not None:
            return existing, False

    media = Media(
        id=new_id("med"),
        project_id=project.id,
        path=candidate,
        duration_ms=int(media_payload.get("duration_ms", 0)),
        sample_rate=int(media_payload.get("sample_rate", 44_100)),
        channels=int(media_payload.get("channels", 2)),
        codec=str(media_payload.get("codec", "")),
        kind="missing",
        sha256=f"missing:{new_id('sha')}",
        created_at=utcnow_iso(),
    )
    session.add(media)
    session.flush()
    return media, False


def _safe_name(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip()
    return cleaned or "project"
