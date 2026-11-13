"""Deck outline: ask the model where the topics turn, then repair the answer (§19.15).

The model is asked one thing — **which line a page starts on, and what that page says** — and
:mod:`animatter.core.deck` does everything else. That split is the same one the ShowNotes
generator uses, and for the same reason: a model asked for a timestamp produces a
plausible-looking number that is wrong often enough to make the feature untrustworthy, while
a model asked "does the topic change at line 42" is answering a question it can actually see.

Two things this module decides and the core cannot:

**How a long transcript is split for planning.** One call per ~18k characters, cut at turn
boundaries. A 61-minute episode is one block; a three-hour one is four, and each block numbers
its own lines from 0, so a page can never point at a line the prompt did not show.

**That one failed block is not a failed deck.** The blocks that answered keep their pages and
the failure is reported. Yesterday's deck is not worth losing to one timeout.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session

from animatter.core.deck import (
    DEFAULT_OUTLINE_PROMPT,
    PagePlan,
    parse_outline,
    settle_pages,
)
from animatter.core.script import (
    Chapter,
    ScriptSentence,
    Turn,
    build_turns,
    numbered_lines,
    plan_blocks,
)
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.providers.llm.bailian import BailianLlm, parse_json_object
from animatter.services import speakers as speaker_service

log = get_logger(__name__)

#: Characters of transcript per planning call. Measured: a 61-minute episode is 17.3k, so the
#: common case is one call. Bigger blocks save calls but let the model lose the thread in the
#: middle of a long prompt and answer with fewer pages than the material deserves.
OUTLINE_BLOCK_CHARS = 18_000

#: Output budget per call. ~40 pages of outline is ~1.5k tokens of JSON; 3000 leaves room for
#: the thinking that precedes it without letting a runaway reply cost real money.
OUTLINE_MAX_TOKENS = 3000

#: Zero, and measured rather than assumed. Going from 0.3 to 0.0 raised the share of *identical
#: page starts* between two runs on the sample episode from 3/25 to 13/29 — the boundaries are a
#: structural judgement, and sampling only blurs them. It does **not** make the call
#: reproducible: the same prompt at 0.0 still returned 29 and 31 pages, because the endpoint is
#: not deterministic even then. Title wording stays specific at 0.0 (「《め》创作契机」), so
#: nothing is traded away for the extra stability.
OUTLINE_TEMPERATURE = 0.0


@dataclass(frozen=True)
class OutlineResult:
    """Pages, where they came from, and what to tell the user."""

    pages: list[PagePlan]
    #: ``"model"`` when the model planned the pages, ``"transcript"`` when it did not and the
    #: deterministic planner stood in.
    source: str
    warnings: list[str] = field(default_factory=list)


def outline_pages(
    session: Session,
    *,
    llm: BailianLlm,
    client: httpx.Client,
    sentences: list[ScriptSentence],
    chapters: list[Chapter] | None = None,
    total_ms: int,
    title_hint: str = "",
    project_id: str | None = None,
) -> OutlineResult:
    """Plan pages with the model, then settle them into something renderable.

    Returns ``source="transcript"`` with **no pages** when every call failed, which is the
    caller's cue to run the fallback planner. It does not raise: an outline that could not be
    written is a deck with a warning, not a broken button.
    """
    turns = build_turns(sentences)
    if not turns:
        return OutlineResult(pages=[], source="transcript")

    names = _speaker_names(session, project_id)
    blocks = plan_blocks(turns, max_chars=OUTLINE_BLOCK_CHARS)

    pages: list[PagePlan] = []
    warnings: list[str] = []
    failures: list[str] = []
    for index, (start, end) in enumerate(blocks):
        segment = turns[start:end]
        if not segment:
            continue
        try:
            payload = _call(
                llm,
                client,
                segment=segment,
                names=names,
                title_hint=title_hint,
                part=(index + 1, len(blocks)),
            )
        except AppError as error:
            failures.append(error.message)
            continue
        block_pages, block_warnings = parse_outline(parse_json_object(payload) or {}, segment)
        pages.extend(block_pages)
        warnings.extend(block_warnings)

    if failures:
        warnings.append(f"{len(failures)} 段规划失败：{'；'.join(failures[:3])}")
    if not pages:
        return OutlineResult(pages=[], source="transcript", warnings=warnings)

    settled, settle_warnings = settle_pages(
        pages, sentences=sentences, total_ms=total_ms, chapters=chapters
    )
    log.info(
        "deck.outlined",
        blocks=len(blocks),
        pages=len(settled),
        failures=len(failures),
        turns=len(turns),
    )
    return OutlineResult(
        pages=settled, source="model", warnings=[*warnings, *settle_warnings]
    )


def _call(
    llm: BailianLlm,
    client: httpx.Client,
    *,
    segment: list[Turn],
    names: dict[int, str],
    title_hint: str,
    part: tuple[int, int],
) -> str:
    listing = numbered_lines(segment, names)
    where = f"（第 {part[0]}/{part[1]} 段）" if part[1] > 1 else ""
    user = (
        f"节目名称：{title_hint}\n\n"
        f"=== 转写稿开始{where} ===\n{listing}\n=== 转写稿结束 ===\n"
        "请按话题转折分段，输出 JSON。"
    )
    reply = llm.chat(
        system=DEFAULT_OUTLINE_PROMPT,
        user=user,
        client=client,
        temperature=OUTLINE_TEMPERATURE,
        max_tokens=OUTLINE_MAX_TOKENS,
        json_mode=True,
    )
    return reply.text


def _speaker_names(session: Session, project_id: str | None) -> dict[int, str]:
    """Display names for the prompt. Anonymous ids are fine — the page plan does not need them.

    Wrapped because a project whose speakers were never named is the normal case, and a
    lookup failure there must not cost the whole outline.
    """
    if project_id is None:
        return {}
    try:
        return {
            view.local_id: view.name
            for view in speaker_service.list_speakers(session, project_id)
        }
    except AppError:
        return {}
