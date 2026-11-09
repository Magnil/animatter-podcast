"""ShowNotes and 文稿 generation.

The service owns three decisions the pure core deliberately does not:

**Which timeline the timestamps are on.** Chapters quote the *rendered* audio,
because the ShowNotes are published next to the exported episode: a chapter
pointing into a deleted stretch sends listeners to a hole. The text itself is
read from the original transcript, so the mapping is applied once, at the end,
through the project's TimeMap.

**How a long transcript is split.** Body work goes block by block, cut at turn
boundaries. A 105-minute episode is one block; the multi-block path exists so a
12-hour one does not silently get one 24k-character prompt that the model
answers with a summary.

**That a failure is per-block.** One bad block must not throw away the other
nine: the document comes back with what succeeded and names what did not.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core import script as script_core
from animatter.core.edl import TimeMap
from animatter.core.script import (
    DEFAULT_PROMPTS,
    Chapter,
    ScriptSentence,
    Turn,
    build_turns,
    host_speaker,
    numbered_lines,
    parse_shownotes_plan,
    plan_blocks,
    render_article,
    render_dialogue,
    render_shownotes,
    spoken_lines,
)
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.models import Sentence, Token, TranscriptVersion
from animatter.providers.llm.bailian import BailianLlm, parse_json_object
from animatter.services import clipping as clipping_service
from animatter.services import projects as project_service
from animatter.services import settings_store
from animatter.services import speakers as speaker_service

log = get_logger(__name__)

FORMAT_DIALOGUE = "dialogue"
FORMAT_ARTICLE = "article"
SHOWNOTES_FORMAT = "shownotes"

_PROMPT_KEY = {
    SHOWNOTES_FORMAT: "script_prompt_shownotes",
    FORMAT_DIALOGUE: "script_prompt_dialogue",
    FORMAT_ARTICLE: "script_prompt_article",
}


def prompt_for(settings: dict[str, object], fmt: str) -> str:
    """The prompt the user edited, or the built-in one.

    Empty means "unset" rather than "empty prompt": a blank system prompt makes
    the model answer whatever it likes, and the settings UI cannot tell an
    untouched field from a deliberately emptied one.
    """
    key = _PROMPT_KEY[fmt]
    override = str(settings.get(key) or "").strip()
    return override if override else DEFAULT_PROMPTS[fmt]


def _version_or_raise(session: Session, version_id: str) -> TranscriptVersion:
    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise AppError("version_not_found", "转写版本不存在", hint="刷新版本列表。", status=404)
    return version


def _sentences(session: Session, version_id: str, time_map: TimeMap | None) -> list[ScriptSentence]:
    """The version's sentences on the timeline the caller wants.

    A sentence whose every word was deleted is dropped: it is not in the
    published audio, so a ShowNotes summary that discusses it would be
    describing something the listener never hears.
    """
    rows = list(
        session.scalars(select(Sentence).where(Sentence.version_id == version_id).order_by(Sentence.ord))
    )
    tokens = list(session.scalars(select(Token).where(Token.version_id == version_id).order_by(Token.idx)))
    if not rows or not tokens:
        raise AppError(
            "no_transcript",
            "这个版本还没有可用的转写内容",
            hint="先完成转写，或换一个已经转写好的版本。",
        )

    out: list[ScriptSentence] = []
    for row in rows:
        slice_tokens = tokens[row.token_start : row.token_end]
        if not slice_tokens:
            continue
        # Hidden filler words are dropped from the text handed to the model, so a
        # ShowNotes summary cannot quote a word the published subtitle omits.
        # Timing still comes from the full slice: the audio is unchanged.
        text = "".join(
            token.text + token.punc_after for token in slice_tokens if not token.hidden
        ).strip()
        if not text:
            continue
        start = slice_tokens[0].start_ms
        end = slice_tokens[-1].end_ms
        if time_map is not None:
            mapped = time_map.map_range(start, end)
            if mapped is None:
                continue
            start, end = mapped
        out.append(
            ScriptSentence(ord=row.ord, start_ms=start, end_ms=end, speaker=row.speaker_id, text=text)
        )
    if not out:
        raise AppError(
            "no_surviving_text",
            "所有内容都被删除了，没有可生成文稿的文字",
            hint="先恢复一部分删除，再生成。",
        )
    return out


def _time_map(session: Session, settings: Settings, version: TranscriptVersion) -> TimeMap | None:
    """Rendered timeline when anything was cut, otherwise ``None``.

    ``None`` (not an identity map) keeps the common case exact: mapping a
    timeline onto itself can only introduce rounding.
    """
    try:
        edl = clipping_service.build_edl(session, settings, version)
    except AppError:
        # A broken EDL must not block publishing the text. The un-mapped
        # timeline is wrong by the deleted amount but still readable, and the
        # error the user needs to see is the one from the EDL, not this.
        log.warning("script.edl_unavailable", version_id=version.id)
        return None
    if edl.removed_ms == 0:
        return None
    return edl.time_map()


def _speaker_names(session: Session, project_id: str) -> dict[int, str]:
    try:
        return {view.local_id: view.name for view in speaker_service.list_speakers(session, project_id)}
    except AppError:
        return {}


def _params(stored: dict[str, object]) -> tuple[float, int]:
    temperature = stored.get("text_temperature", 0.6)
    max_tokens = stored.get("text_max_tokens", 8000)
    return (
        float(temperature) if isinstance(temperature, (int, float)) else 0.6,
        int(max_tokens) if isinstance(max_tokens, int) else 8000,
    )


def _chat(
    llm: BailianLlm,
    client: httpx.Client,
    *,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> str:
    reply = llm.chat(
        system=system,
        user=user,
        client=client,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )
    return reply.text


def _plan(
    llm: BailianLlm,
    client: httpx.Client,
    *,
    turns: list[Turn],
    names: dict[int, str],
    title_hint: str,
    system: str,
    temperature: float,
    max_tokens: int,
) -> tuple[script_core.ShownotesPlan, list[str]]:
    """One call for the titles, the teaser and the chapter line numbers.

    A transcript longer than the planning budget is truncated here and nowhere
    else. Truncating the *planning* input costs a chapter; truncating the *body*
    input costs the content itself, which is why the body path splits into
    blocks instead.

    The host's name is handed over explicitly. "Chapter starts where the host
    picks the conversation up" is unactionable without it — diarization returns
    anonymous ids, and a model guessing which of 说话人1 / 说话人2 is the host
    gets it wrong about half the time, which is indistinguishable from a model
    that ignored the rule.
    """
    host = host_speaker(turns)
    host_line = ""
    if host is not None:
        host_line = f"主持人：{script_core.speaker_name(host, names)}（章节从这一位开口处开始）\n"

    listing = numbered_lines(turns, names)
    note = ""
    if len(listing) > script_core.MAX_PLAN_CHARS:
        listing = listing[: script_core.MAX_PLAN_CHARS]
        note = "\n（转写稿过长，以上只是前一部分；章节按能看到的内容划分。）"
    user = (
        f"节目名称：{title_hint}\n"
        f"{host_line}\n"
        f"=== 转写稿开始 ===\n{listing}\n=== 转写稿结束 ==={note}"
    )
    text = _chat(
        llm,
        client,
        system=system,
        user=user,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=True,
    )
    return parse_shownotes_plan(parse_json_object(text) or {}, turns, host=host)


@dataclass(frozen=True)
class GeneratedDocument:
    """What one generation produced, with everything the UI reports about it."""

    format: str
    title: str
    body: str
    chapters: list[Chapter]
    #: Candidate titles the model offered, already filtered to ≤ 22 chars. The
    #: first one is the H1; the rest are what the user picks from when the
    #: model's own pick is not the best one.
    titles: list[str]
    #: What the *file* is called, before the format label. Kept apart from
    #: ``title`` because the headline is model-written and changes on every run.
    filename_base: str
    turns: int
    blocks: int
    warnings: list[str]


def generate(
    *,
    session: Session,
    app_settings: Settings,
    version_id: str,
    fmt: str,
    llm: BailianLlm,
    client: httpx.Client,
    mapped: bool = True,
    title: str = "",
) -> GeneratedDocument:
    """Build one document. Returns the export payload the router renders."""
    if fmt not in {SHOWNOTES_FORMAT, FORMAT_DIALOGUE, FORMAT_ARTICLE}:
        raise AppError("bad_format", f"不支持的格式：{fmt}")

    version = _version_or_raise(session, version_id)
    project = project_service.get_project(session, version.project_id)
    stored = settings_store.load(session)
    temperature, max_tokens = _params(stored)

    time_map = _time_map(session, app_settings, version) if mapped else None
    sentences = _sentences(session, version_id, time_map)
    names = _speaker_names(session, project.id)
    turns = build_turns(sentences)

    document_title = title.strip() or project.name
    warnings: list[str] = []
    blocks = plan_blocks(turns, max_chars=script_core.MAX_BODY_CHARS)
    # Only the body path actually splits. ShowNotes makes one planning call and
    # truncates its input instead, so announcing "分 5 段生成" there would be a
    # warning about something that did not happen.
    if fmt != SHOWNOTES_FORMAT and len(blocks) > 1:
        warnings.append(f"转写稿较长，分 {len(blocks)} 段生成")

    if fmt == SHOWNOTES_FORMAT:
        plan, dropped_titles = _plan(
            llm,
            client,
            turns=turns,
            names=names,
            title_hint=document_title,
            system=prompt_for(stored, SHOWNOTES_FORMAT),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        chapters = [
            Chapter(heading=heading, start_ms=turns[index].start_ms, first_ord=turns[index].first_ord)
            for heading, index in plan.chapters
            if 0 <= index < len(turns)
        ]
        if not plan.summary:
            raise AppError(
                "empty_summary",
                "模型没有给出概述",
                hint="换个模型或降低温度后重试；内置提示词里 summary 是必填项。",
                status=502,
            )
        if dropped_titles:
            # Reported rather than silently discarded: the model ignoring the
            # 22-character rule is the one failure a human must adjudicate, and
            # a shorter-than-asked list with no explanation reads like a bug.
            warnings.append(f"{len(dropped_titles)} 个备选标题超过 22 字，已隐藏：{dropped_titles[0]}")
        if not plan.titles:
            warnings.append("模型没有给出合规的备选标题，标题沿用项目名")
        if len(chapters) < 3:
            warnings.append(f"只识别出 {len(chapters)} 个章节，可能没有找到主持人接话的转折点")

        # Chapters must reach the end. A plan that stops at 60 % of a 62-minute
        # episode reads like a deliberate choice and is in fact a half-finished
        # list — the single most common failure on long transcripts, and one the
        # user cannot see without comparing the last timestamp to the duration.
        span = 0.0
        if chapters and turns:
            total = max(1, turns[-1].start_ms - turns[0].start_ms)
            span = (chapters[-1].start_ms - turns[0].start_ms) / total
        if chapters and span < 0.75:
            warnings.append(
                f"章节只覆盖到 {span:.0%}，后面可能漏了转折点——可以手动补，或调高最大输出长度后重生成"
            )

        # Numbers are checked against the transcript instead of trusted.
        transcript_text = "".join(turn.text for turn in turns)
        invented = script_core.unverified_numbers(plan.summary + "".join(plan.titles), transcript_text)
        if invented:
            warnings.append(f"概述里的这些数字在转写稿里找不到，发布前请核对：{'、'.join(invented[:6])}")

        # Length is a hard requirement in the prompt and the model overshoots it
        # by ~50 % anyway: measured 405 字 against a 260 字 cap, because "be
        # vivid" and "be short" pull in opposite directions. Reported rather
        # than trimmed — cutting a teaser at an arbitrary character is how you
        # end up with a sentence that stops mid-clause.
        summary_chars = len(plan.summary.replace("\n", ""))
        if summary_chars > 300:
            warnings.append(f"概述 {summary_chars} 字，比 260 字的上限长，建议删到两段以内好读")

        # The H1 is the first compliant title, so a generated document is
        # publishable without a manual edit. An explicit `title` argument still
        # wins: that is a human overriding the model on purpose.
        headline = title.strip() or (plan.titles[0] if plan.titles else project.name)
        body = render_shownotes(
            title=headline,
            summary=plan.summary,
            chapters=chapters,
            signature=str(stored.get("shownotes_signature") or ""),
        )
        return GeneratedDocument(
            format=fmt,
            title=headline,
            # Filenames follow the project, not the headline: the headline
            # changes every time the model is re-run, and a folder of
            # 「音乐如何被画出来_ShowNotes.md」 and 「网络动画MV的中间态_ShowNotes.md」
            # is the same episode saved twice.
            filename_base=project.name,
            body=body,
            chapters=chapters,
            titles=plan.titles,
            turns=len(turns),
            blocks=len(blocks),
            warnings=warnings,
        )

    pieces: list[str] = []
    failures: list[str] = []
    for index, (start, end) in enumerate(blocks):
        segment = turns[start:end]
        if not segment:
            continue
        if fmt == FORMAT_DIALOGUE:
            user = (
                f"节目名称：{document_title}\n\n"
                f"=== 转写稿开始（第 {index + 1}/{len(blocks)} 段）===\n"
                f"{spoken_lines(segment, names)}\n=== 转写稿结束 ==="
            )
        else:
            user = (
                f"节目名称：{document_title}\n\n"
                f"=== 转写稿开始（第 {index + 1}/{len(blocks)} 段）===\n"
                f"{spoken_lines(segment, names)}\n=== 转写稿结束 ===\n"
                "请改写为书面文章正文。"
            )
        try:
            pieces.append(
                _chat(
                    llm,
                    client,
                    system=prompt_for(stored, fmt),
                    user=user,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=False,
                ).strip()
            )
        except AppError as error:
            failures.append(f"第 {index + 1} 段：{error.message}")

    if not pieces:
        raise AppError(
            "all_blocks_failed",
            "每一段都生成失败，没有可导出的内容",
            hint="；".join(failures[:3]) or "检查模型参数与网络后重试。",
            status=502,
        )
    if failures:
        warnings.append(f"{len(failures)} 段失败：{'；'.join(failures[:3])}")

    text = "\n\n".join(piece for piece in pieces if piece)
    if fmt == FORMAT_DIALOGUE:
        body = render_dialogue(title=document_title, body=text)
    else:
        body = render_article(title=document_title, sections=[("", text)])

    return GeneratedDocument(
        format=fmt,
        title=document_title,
        body=body,
        chapters=[],
        titles=[],
        filename_base=project.name,
        turns=len(turns),
        blocks=len(blocks),
        warnings=warnings,
    )
