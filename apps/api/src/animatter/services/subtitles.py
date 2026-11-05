"""Subtitle export: SRT, VTT, and plain text, mapped onto the rendered timeline.

The single most important property here is that **every exported timestamp goes
through the project's TimeMap**. A subtitle that references original-audio time
drifts further with each deletion, and the drift is invisible until the file is
played against the render.

Also handled deliberately: a cue whose text survives but whose start falls inside
a deletion is moved to the first surviving instant rather than dropped, because
the user did not delete that line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.edl import TimeMap
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.models import Export, Sentence, Token, TranscriptVersion
from animatter.services import projects as project_service

log = get_logger(__name__)

SUBTITLE_FORMATS = {"srt": "application/x-subrip", "vtt": "text/vtt", "txt": "text/plain"}


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str
    speaker: int | None = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class SubtitleIssue:
    sentence_ord: int
    kind: str
    detail: str


def build_cues(
    session: Session,
    version_id: str,
    *,
    time_map: TimeMap | None,
    max_chars: int = 20,
    max_lines: int = 2,
    max_cps: float = 9.0,
) -> tuple[list[Cue], list[SubtitleIssue]]:
    """Turn sentences into cues, split for readability and mapped to new time.

    Splitting long sentences is not cosmetic: a cue that stays on screen for 30
    seconds while the speaker talks is unusable for viewers, and a single line of
    60 characters is unreadable on a phone.
    """
    rows = list(
        session.scalars(select(Sentence).where(Sentence.version_id == version_id).order_by(Sentence.ord))
    )
    if not rows:
        raise AppError("no_sentences", "这个版本没有句子", hint="先完成转写。")

    tokens = list(session.scalars(select(Token).where(Token.version_id == version_id).order_by(Token.idx)))

    cues: list[Cue] = []
    issues: list[SubtitleIssue] = []
    budget = max_chars * max_lines

    for sentence in rows:
        slice_tokens = tokens[sentence.token_start : sentence.token_end]
        if not slice_tokens:
            continue

        text = "".join(token.text + token.punc_after for token in slice_tokens)
        start = slice_tokens[0].start_ms
        end = slice_tokens[-1].end_ms

        if time_map is not None:
            mapped = time_map.map_range(start, end)
            if mapped is None:
                # Entirely deleted: emit nothing. The text was removed on purpose.
                continue
            new_start, new_end = mapped
            if start < 0 or new_start != time_map.to_new_clamped(start):
                issues.append(
                    SubtitleIssue(
                        sentence_ord=sentence.ord,
                        kind="partially_deleted",
                        detail="部分内容已删除，时间轴已压缩",
                    )
                )
            start, end = new_start, new_end

        if len(text) <= budget:
            cues.append(Cue(start_ms=start, end_ms=end, text=text, speaker=sentence.speaker_id))
            continue

        # Split proportionally by character count: cheap, and accurate enough
        # because the split only redistributes time the sentence already had.
        chunks = _split_text(text, max_chars, max_lines)
        total_chars = sum(len(chunk) for chunk in chunks) or 1
        cursor = start
        span = max(1, end - start)
        for index, chunk in enumerate(chunks):
            share = len(chunk) / total_chars
            chunk_end = end if index == len(chunks) - 1 else min(end, cursor + int(span * share))
            cues.append(
                Cue(
                    start_ms=cursor,
                    end_ms=max(cursor + 1, chunk_end),
                    text=chunk,
                    speaker=sentence.speaker_id,
                )
            )
            cursor = chunk_end

        issues.append(
            SubtitleIssue(
                sentence_ord=sentence.ord,
                kind="split",
                detail=f"长句拆成 {len(chunks)} 条字幕",
            )
        )

    for cue in cues:
        if cue.duration_ms <= 0:
            issues.append(SubtitleIssue(sentence_ord=-1, kind="zero_length", detail="时长为零的字幕"))
        elif cue.duration_ms < 400:
            issues.append(
                SubtitleIssue(
                    sentence_ord=-1,
                    kind="too_brief",
                    detail=f"字幕不足 0.4 秒：{cue.text[:12]}",
                )
            )

    for cue in cues:
        cps = len(cue.text) / max(0.1, cue.duration_ms / 1000)
        if cps > max_cps:
            issues.append(
                SubtitleIssue(
                    sentence_ord=-1,
                    kind="too_fast",
                    detail=f"每秒 {cps:.1f} 字：{cue.text[:12]}",
                )
            )

    return cues, issues


def _split_text(text: str, max_chars: int, max_lines: int) -> list[str]:
    budget = max_chars * max_lines
    chunks: list[str] = []
    remaining = text
    # Prefer breaking at punctuation so a cue does not end mid-clause.
    while len(remaining) > budget:
        window = remaining[:budget]
        cut = max(window.rfind(mark) for mark in "。！？，、；：!?,;:")
        if cut <= 0:
            cut = budget
        else:
            cut += 1  # keep the punctuation with its clause
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


def format_time(ms: int, *, vtt: bool = False) -> str:
    """SRT/VTT timestamp. Both use the same shape; VTT uses a dot separator."""
    ms = max(0, int(ms))
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    separator = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def to_srt(cues: list[Cue]) -> str:
    lines: list[str] = []
    for index, cue in enumerate(cues, start=1):
        lines.append(str(index))
        lines.append(f"{format_time(cue.start_ms)} --> {format_time(cue.end_ms)}")
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines)


def to_vtt(cues: list[Cue]) -> str:
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.append(f"{format_time(cue.start_ms, vtt=True)} --> {format_time(cue.end_ms, vtt=True)}")
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines)


def to_txt(cues: list[Cue]) -> str:
    """Plain script text, one cue per line.

    This is the "auto-organised transcript" the user asked for: readable prose
    with no timestamps, suitable for show notes.
    """
    return "\n".join(cue.text for cue in cues)


def export_subtitles(
    session: Session,
    settings: Settings,
    version_id: str,
    *,
    fmt: str = "srt",
    time_map: TimeMap | None = None,
    max_chars: int = 20,
    max_lines: int = 2,
    max_cps: float = 9.0,
) -> tuple[Export, list[SubtitleIssue]]:
    if fmt not in SUBTITLE_FORMATS:
        raise AppError("export_format_unknown", f"不支持的字幕格式 {fmt}")

    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise AppError("version_not_found", "转写版本不存在", status=404)

    cues, issues = build_cues(
        session,
        version_id,
        time_map=time_map,
        max_chars=max_chars,
        max_lines=max_lines,
        max_cps=max_cps,
    )
    if not cues:
        raise AppError(
            "no_subtitles",
            "没有可导出的字幕",
            hint="所有句子都被删除了，先恢复一部分内容。",
        )

    if fmt == "srt":
        content = to_srt(cues)
    elif fmt == "vtt":
        content = to_vtt(cues)
    else:
        content = to_txt(cues)

    root = project_service.project_root(settings, version.project_id)
    out_dir = root / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    from animatter.ids import new_id, utcnow_iso

    export_id = new_id("exp")
    path = out_dir / f"{export_id}.{fmt}"
    path.write_text(content, encoding="utf-8")

    row = Export(
        id=export_id,
        project_id=version.project_id,
        kind="subtitle",
        path=str(path),
        # The Export table keeps arbitrary per-format facts in options_json; the
        # schema deliberately does not grow a column per subtitle option.
        options_json=json.dumps(
            {
                "version_id": version_id,
                "fmt": fmt,
                "mapped": time_map is not None,
                "cue_count": len(cues),
            },
            ensure_ascii=False,
        ),
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    log.info("subtitle.exported", fmt=fmt, cues=len(cues), mapped=time_map is not None)
    return row, issues


def preview(
    session: Session, version_id: str, *, fmt: str = "srt", limit: int = 40, time_map: TimeMap | None = None
) -> str:
    cues, _ = build_cues(session, version_id, time_map=time_map)
    head = cues[:limit]
    body = to_srt(head) if fmt == "srt" else to_vtt(head) if fmt == "vtt" else to_txt(head)
    if len(cues) > limit:
        body += f"\n… 共 {len(cues)} 条，此处仅显示前 {limit} 条\n"
    return body
