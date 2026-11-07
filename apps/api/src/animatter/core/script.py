"""Transcript → published text: ShowNotes and 文稿.

Pure by design — no database, no network. The three things that go wrong in
document generation are all decidable here and nowhere else:

* **Speaker turns, not sentences.** 1074 sentences handed to a model as 1074
  lines produces a summary of a list, not of a conversation. Consecutive
  sentences from one speaker are merged into one turn first.
* **Chapter timestamps come from the transcript, not from the model.** Models
  invent plausible-looking times. The model is only asked which *line* a chapter
  starts at, and the millisecond value is read back off the sentence it named.
* **Long transcripts are split.** A 105-minute episode is 24k characters and fits
  one call; a 12-hour one does not. Blocks are cut at turn boundaries so a
  sentence is never split across two prompts.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Characters of transcript per body call. Above this the model starts
#: summarising instead of rewriting, which is the failure mode that looks fine
#: in the preview and ruins the article.
MAX_BODY_CHARS = 6_000

#: One chapter-planning call may carry this much. A 105-minute episode is 24k,
#: so the multi-block path only engages on genuinely long files.
MAX_PLAN_CHARS = 40_000

#: A turn longer than this is split, so one filibuster cannot become a
#: 3000-character paragraph the model paraphrases away.
MAX_TURN_CHARS = 400


@dataclass(frozen=True)
class ScriptSentence:
    """One sentence of the transcript, on whichever timeline the caller wants."""

    ord: int
    start_ms: int
    end_ms: int
    speaker: int | None
    text: str


@dataclass(frozen=True)
class Turn:
    """Consecutive sentences from one speaker, as the model should see them."""

    speaker: int | None
    start_ms: int
    end_ms: int
    first_ord: int
    text: str


@dataclass(frozen=True)
class Chapter:
    heading: str
    start_ms: int
    first_ord: int


def format_clock(ms: int) -> str:
    """``1:02:03`` / ``02:03`` — the shape podcast apps print."""
    total = max(0, int(ms)) // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def speaker_name(speaker: int | None, names: dict[int, str]) -> str:
    """Display name for a diarizer id, falling back to the editor's default.

    A transcript whose speakers were never named must still produce a readable
    document, so an unknown id becomes 说话人N rather than disappearing.
    """
    if speaker is None:
        return "说话人"
    known = names.get(speaker)
    if known:
        return known
    return f"说话人{speaker + 1}"


def build_turns(sentences: list[ScriptSentence], *, max_turn_chars: int = MAX_TURN_CHARS) -> list[Turn]:
    """Merge consecutive same-speaker sentences into turns.

    Also splits a turn that grows past ``max_turn_chars``: the model treats a
    3000-character turn as "this whole stretch is one idea" and collapses it,
    which is exactly the content the user wanted kept.
    """
    turns: list[Turn] = []
    for sentence in sentences:
        text = sentence.text.strip()
        if not text:
            continue
        last = turns[-1] if turns else None
        if (
            last is not None
            and last.speaker == sentence.speaker
            and len(last.text) + len(text) <= max_turn_chars
        ):
            turns[-1] = Turn(
                speaker=last.speaker,
                start_ms=last.start_ms,
                end_ms=sentence.end_ms,
                first_ord=last.first_ord,
                text=last.text + text,
            )
            continue
        turns.append(
            Turn(
                speaker=sentence.speaker,
                start_ms=sentence.start_ms,
                end_ms=sentence.end_ms,
                first_ord=sentence.ord,
                text=text,
            )
        )
    return turns


def plan_blocks(turns: list[Turn], *, max_chars: int) -> list[tuple[int, int]]:
    """Half-open turn ranges, each under ``max_chars``.

    Always returns at least one (possibly empty) block so callers do not need a
    special case for "nothing to do".
    """
    if not turns:
        return [(0, 0)]
    blocks: list[tuple[int, int]] = []
    start = 0
    size = 0
    for index, turn in enumerate(turns):
        cost = len(turn.text)
        if index > start and size + cost > max_chars:
            blocks.append((start, index))
            start = index
            size = 0
        size += cost
    blocks.append((start, len(turns)))
    return blocks


def numbered_lines(turns: list[Turn], names: dict[int, str], *, offset: int = 0) -> str:
    """One line per turn, prefixed with a number the model can cite back.

    The number is the turn index, not the sentence ord: it is what
    :func:`resolve_chapter` maps back, and it keeps the prompt short.
    """
    lines = []
    for index, turn in enumerate(turns):
        lines.append(f"[{index + offset}] {speaker_name(turn.speaker, names)}：{turn.text}")
    return "\n".join(lines)


def spoken_lines(turns: list[Turn], names: dict[int, str]) -> str:
    """Turn list without line numbers, for prompts that only rewrite text."""
    return "\n".join(f"{speaker_name(turn.speaker, names)}：{turn.text}" for turn in turns)


def parse_chapters(
    payload: object, turns: list[Turn], *, offset: int = 0
) -> tuple[str, list[str], list[tuple[str, int]]]:
    """Pull ``(summary, points, [(heading, turn_index)])`` out of a model reply.

    Deliberately forgiving about shapes and strict about validity: a chapter
    whose line number does not exist is dropped rather than snapped to the
    nearest one, because a chapter pointing at the wrong speaker reads as a
    factual error in the published notes.
    """
    data = payload if isinstance(payload, dict) else {}
    summary = str(data.get("summary") or "").strip()

    points: list[str] = []
    raw_points = data.get("points")
    if isinstance(raw_points, list):
        for item in raw_points:
            text = str(item).strip()
            if text:
                points.append(text)

    chapters: list[tuple[str, int]] = []
    raw_chapters = data.get("chapters")
    if isinstance(raw_chapters, list):
        for item in raw_chapters:
            if not isinstance(item, dict):
                continue
            heading = str(item.get("heading") or "").strip()
            raw_index = item.get("line", item.get("sentence"))
            try:
                index = int(raw_index)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            index -= offset
            if not heading or not 0 <= index < len(turns):
                continue
            chapters.append((heading, index))

    chapters.sort(key=lambda pair: pair[1])
    return summary, points, chapters


def render_shownotes(
    *,
    title: str,
    summary: str,
    points: list[str],
    chapters: list[Chapter],
    signature: str,
) -> str:
    """The published ShowNotes block.

    Structure is fixed by this function rather than by the prompt: the model
    decides *what* the episode is about, but a heading that only sometimes
    appears (or a timestamp format that changes between runs) makes the output
    impossible to paste into a description field twice in a row.
    """
    lines: list[str] = []
    if title.strip():
        lines.append(f"# {title.strip()}")
        lines.append("")
    if summary.strip():
        lines.append(summary.strip())
        lines.append("")
    if points:
        lines.append("## 本期要点")
        lines.append("")
        for point in points:
            lines.append(f"- {point}")
        lines.append("")
    if chapters:
        lines.append("## 时间轴")
        lines.append("")
        for chapter in chapters:
            lines.append(f"- **{format_clock(chapter.start_ms)}** {chapter.heading}")
        lines.append("")
    if signature.strip():
        lines.append("---")
        lines.append("")
        lines.append(signature.strip())
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_dialogue(*, title: str, body: str) -> str:
    """Lightly cleaned conversation, speakers kept.

    No chapter headings: this style exists for people who want to *read what was
    said*, and a heading the speaker never said breaks that promise.
    """
    lines: list[str] = []
    if title.strip():
        lines.append(f"# {title.strip()}")
        lines.append("")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def render_article(*, title: str, sections: list[tuple[str, str]]) -> str:
    """Rewritten article: speaker-free paragraphs under chapter headings."""
    lines: list[str] = []
    if title.strip():
        lines.append(f"# {title.strip()}")
        lines.append("")
    for heading, body in sections:
        text = body.strip()
        if not text:
            continue
        if heading.strip():
            lines.append(f"## {heading.strip()}")
            lines.append("")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# --- prompts -------------------------------------------------------------
#
# These are the *system* prompts. The transcript is handed over in the user
# message, so editing a prompt here can change voice, structure and strictness
# without being able to break the data handoff — which is the failure a
# placeholder-based template invites.

DEFAULT_SHOWNOTES_PROMPT = """你是中文播客的运营编辑。根据这期节目的转写稿写一份 ShowNotes。

要求：
- summary：一句话概述，60–120 字，说清这期聊了什么、为什么值得听。不要用「本期节目」「我们将」这类空话开头。
- points：4–6 条要点，每条 15–40 字，覆盖主要话题。不要写成「讨论了 X」这种元描述，直接写观点。
- chapters：4–10 个章节，按内容转折划分，每章标题 6–14 字，概括这一段在讲什么。
  line 字段填该章内容第一行前面的编号（整数），必须是输入中真实存在的编号。

只依据转写稿的内容，不要编造事实、人名、数字。严格按 JSON 输出：
{"summary": "...", "points": ["..."], "chapters": [{"heading": "...", "line": 0}]}
不要输出任何其他内容。"""

DEFAULT_DIALOGUE_PROMPT = """你是中文播客的文字编辑。给你一段播客转写稿，做「轻度整理」。

必须做：
- 删掉纯语气词（嗯、呃、啊、哦、那个、就是说、然后呢、对对对这类），保留有语义的口语
- 修掉口误、重复词、说了一半的话、明显的语法断裂
- 补全标点，按语义断句，一句话不要跨行

不要做：
- 不要改写观点，不要书面化，不要概括，不要删减内容
- 不要改变说话人，不要合并不同人的发言，不要调整顺序
- 不要添加转写稿里没有的信息

输出格式：每位说话人一次发言占一段，段与段之间空一行，每段以「**说话人名字**：」开头。
说话人名字用输入里给你的那个，不要改名。只输出正文，不要任何解释或标题。"""

DEFAULT_ARTICLE_PROMPT = """你是中文播客的资深编辑。把给定的转写稿改写成一篇可读的书面文章。

要求：
- 不区分说话人，不出现「说话人1」这类标签，读起来是一个人在讲述
- 按语义重新组织成自然段，每段 3–6 句，段与段之间有逻辑推进
- 书面化：去掉语气词、口头禅、重复、跑题、寒暄
- 保留关键观点、例子、比喻、人名、作品名、数字，不要编造或改动
- 不要输出标题或小标题，只输出这一段的正文
- 不要输出任何解释、开场白或结束语"""

DEFAULT_PROMPTS: dict[str, str] = {
    "shownotes": DEFAULT_SHOWNOTES_PROMPT,
    "dialogue": DEFAULT_DIALOGUE_PROMPT,
    "article": DEFAULT_ARTICLE_PROMPT,
}
