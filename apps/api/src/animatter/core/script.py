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

import re
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

#: How far back to look for the host turn a chapter should start on. Ten turns
#: is roughly "the same exchange"; beyond that the host is answering a different
#: question and the chapter would land in the wrong section.
CHAPTER_HOST_WINDOW = 10

#: How far a chapter may be nudged earlier to land on a host turn. Measured on a
#: published episode: the handwritten chapters sit 0–6 s after the host starts
#: speaking, so 12 s covers the real cases without swallowing the previous
#: chapter.
CHAPTER_HOST_MIN_GAP_MS = 12_000


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
    """``00:01:10`` — always three groups, zero-padded.

    Chapter timestamps are pasted into a description field next to other shows'
    timestamps, so the shape has to be stable: a column of ``00:01:10`` and
    ``1:02:03`` mixed together does not align, and a reader scanning for a
    section has to parse two formats. Hours are therefore always printed, even
    when the episode is under an hour.
    """
    total = max(0, int(ms)) // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


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


#: Hard cap on a title, counted the way Chinese editors count it: 汉字 and 全角
#: punctuation are one character each, half-width letters/digits are one each
#: ("MV" is 2). Beyond 22 the title wraps in a podcast app's list view and the
#: ending — which is where the second half of a two-part title carries its
#: meaning — gets cut off.
MAX_TITLE_CHARS = 22

#: How many alternates to ask for. Five is enough to pick from without the model
#: spending its whole budget on titles instead of on the summary.
WANTED_TITLES = 5


@dataclass(frozen=True)
class ShownotesPlan:
    """What the model is allowed to decide about an episode's ShowNotes.

    Deliberately small. Timestamps are *not* in here (they are read back off the
    transcript by :func:`resolve_chapter`), and neither is the section structure
    — only the words.
    """

    summary: str
    titles: list[str]
    chapters: list[tuple[str, int]]


def count_title_chars(title: str) -> int:
    """Length the way a Chinese editor counts a title.

    The house rule is 「汉字 + 全角标点 ≤ 22，半角字母数字按字符计」: every
    visible character is one, so "MV" is 2 and "：" is 1. Whitespace is not
    counted, because 「音乐如何被画出来： 学院与二次元之间的网络创作」 is the
    same title as the version without the space.

    Emoji count by grapheme-ish approximation: a ZWJ sequence or a variation
    selector is *part of* the preceding character, not a character of its own —
    counting code points would make 「🎬」 cost two and reject a legal title.
    """
    total = 0
    for char in title:
        if char.isspace():
            continue
        if char in _ZERO_WIDTH:
            continue
        total += 1
    return total


#: Code points that do not occupy a character cell of their own.
_ZERO_WIDTH = frozenset({"\u200b", "\u200c", "\u200d", "\ufe0e", "\ufe0f", "\u20e3"})


def fit_titles(titles: list[str], *, limit: int = MAX_TITLE_CHARS) -> tuple[list[str], list[str]]:
    """Split candidate titles into ``(kept, dropped)``.

    Over-long titles are dropped rather than truncated: cutting a two-part title
    at the limit leaves the 机制 half missing, so the title still parses as
    Chinese but no longer says what the second half was for. Dropping is visible
    (one fewer option); truncating is not.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for raw in titles:
        title = " ".join(str(raw).split())
        if not title:
            continue
        if count_title_chars(title) > limit:
            dropped.append(f"{title}（{count_title_chars(title)} 字，超过 {limit}）")
            continue
        if title not in kept:
            kept.append(title)
    return kept, dropped


#: Phrases that mark the person introducing themselves as the host. Two kinds
#: were observed in real episodes: the greeting (「各位听众大家好，欢迎收听…」)
#: and the self-identification (「我是今天的主持…」). Both are openers, so they
#: only ever earn points in the first few turns.
HOST_INTRO_MARKERS = (
    "欢迎收听",
    "大家好",
    "我是主持",
    "我是今天的主持",
    "主持人",
    "这一期我们",
    "今天我们",
)

#: Turns considered "opening" for the self-introduction rule. An interview's
#: intro is the first thing anyone says; a guest quoting 「欢迎收听」 an hour in
#: is not claiming the host chair.
HOST_INTRO_WINDOW = 8

#: Question markers. A host asks; a guest mostly answers. Counting the final
#: question mark alone misses too much — Chinese speech recognition drops a lot
#: of them — so interrogative words count as well, at a lower weight.
_QUESTION_ENDINGS = ("？", "?", "吗。", "呢。")
_INTERROGATIVES = ("什么", "怎么", "为什么", "是不是", "对不对", "有没有", "如何", "哪一", "的话呢")


def host_speaker(turns: list[Turn]) -> int | None:
    """The speaker who behaves like the host of a conversation.

    Needed because "chapter starts where the host starts talking" is only
    actionable if the model knows *which* id that is, and the transcript carries
    no such label — diarization returns anonymous ids.

    Three signals, because no single one survives contact with real transcripts:

    1. **Self-introduction.** 「各位听众大家好，欢迎收听本期的漫物质，我是今天的
       主持马小褂」 — decisive when present, and it is present in most episodes.
    2. **Question ratio.** The host asks and the guest answers. Measured on a
       real episode: 13 % of the host's turns end in a question mark against
       6 % of the guest's.
    3. **Who opens.** The host says the first word. This is the fallback, and
       the tie-breaker.

    *Not* "who has the most turns": on that same episode the two speakers were
    128 turns to 126. Diarization splits a long answer into as many turns as a
    rapid exchange, so turn count measures talking speed, not role.

    ``None`` for a single-speaker recording: there are no roles to distinguish,
    and nominating a host there would make the chapter rule silently
    inapplicable while looking like it had been applied.
    """
    known = [turn for turn in turns if turn.speaker is not None]
    speakers: set[int] = {speaker for turn in known if (speaker := turn.speaker) is not None}
    if len(speakers) < 2:
        return None

    intro: set[int] = set()
    for turn in known[:HOST_INTRO_WINDOW]:
        if turn.speaker is not None and any(marker in turn.text for marker in HOST_INTRO_MARKERS):
            intro.add(turn.speaker)
    # A marker in the opening window is the strongest evidence available; two
    # different speakers greeting each other is not an intro, so the rule only
    # fires when exactly one speaker claims it.
    if len(intro) == 1:
        return next(iter(intro))

    asked: dict[int, int] = {}
    total: dict[int, int] = {}
    for turn in known:
        speaker = turn.speaker
        if speaker is None:
            continue
        total[speaker] = total.get(speaker, 0) + 1
        text = turn.text.rstrip()
        if text.endswith(_QUESTION_ENDINGS) or any(word in text for word in _INTERROGATIVES):
            asked[speaker] = asked.get(speaker, 0) + 1

    ratio = {speaker: asked.get(speaker, 0) / total[speaker] for speaker in speakers if speaker is not None}
    winner = max(ratio, key=lambda speaker: ratio[speaker])
    runner_up = max((ratio[s] for s in speakers if s != winner), default=0.0)
    # A margin is required: 6 % vs 7 % is noise, and flipping the host would
    # silently move every chapter of the episode.
    if ratio[winner] >= runner_up * 1.4 and asked.get(winner, 0) >= 3:
        return winner

    return known[0].speaker


def refine_chapters(
    chapters: list[tuple[str, int]], turns: list[Turn], host: int | None
) -> list[tuple[str, int]]:
    """Pull each chapter start onto the host turn that opened that stretch.

    The rule from the published episodes: a chapter begins where the host picks
    the conversation up (a new question, a summary of the last answer), not in
    the middle of a guest's sentence. The model has the transcript and can see
    that, but it also reliably anchors on "the interesting bit", which is often
    20 seconds into the guest's answer.

    So the anchor is nudged, not reassigned. The search is **symmetric** around
    the chosen point, because both mistakes happen in real output: sometimes the
    model lands mid-answer (host is behind), sometimes it lands on the guest's
    last «嗯。» a second *before* the host takes over (host is just ahead). A
    backward-only search fixes the first and silently keeps the second.

    Closest host turn wins, so a nudge is never bigger than it has to be; ties
    go to the earlier one, since a chapter opening a beat early is less wrong
    than one that skips the question. Nothing in range means the anchor stands —
    the documented exception, where the topic genuinely turns inside an answer.
    """
    if host is None:
        return chapters
    refined: list[tuple[str, int]] = []
    for heading, index in chapters:
        anchor = turns[index].start_ms
        best: int | None = None
        best_cost = 0
        low = max(0, index - CHAPTER_HOST_WINDOW)
        high = min(len(turns) - 1, index + CHAPTER_HOST_WINDOW)
        for j in range(low, high + 1):
            if turns[j].speaker != host:
                continue
            distance = abs(turns[j].start_ms - anchor)
            if distance > CHAPTER_HOST_MIN_GAP_MS:
                continue
            # `<=` with the later turn considered first would prefer later on a
            # tie; iterating forward and using `<` keeps the earlier one.
            if best is None or distance < best_cost:
                best = j
                best_cost = distance
        refined.append((heading, index if best is None else best))
    return refined


def parse_shownotes_plan(
    payload: object, turns: list[Turn], *, offset: int = 0, host: int | None = None
) -> tuple[ShownotesPlan, list[str]]:
    """Pull the plan out of a model reply, plus the notes a human should see.

    Deliberately forgiving about shapes and strict about validity: a chapter
    whose line number does not exist is dropped rather than snapped to the
    nearest one, because a chapter pointing at the wrong speaker reads as a
    factual error in the published notes.
    """
    data = payload if isinstance(payload, dict) else {}
    summary = str(data.get("summary") or "").strip()

    raw_titles = data.get("titles")
    titles: list[str] = []
    if isinstance(raw_titles, list):
        titles = [str(item) for item in raw_titles]
    titles, dropped_titles = fit_titles(titles)

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
    chapters = refine_chapters(chapters, turns, host)

    return ShownotesPlan(summary=summary, titles=titles, chapters=chapters), dropped_titles


def unverified_numbers(text: str, transcript: str) -> list[str]:
    """Numbers in the copy that do not appear in the transcript.

    The prompt says "do not invent numbers" and the model mostly complies — but
    it reliably upgrades 「今年入围费那奇」 to 「2023 年入围费那奇」, because a
    published ShowNotes *looks like* it should carry a year. That is the worst
    class of error here: it is specific, plausible, and wrong, and nobody
    re-checks a number that reads naturally.

    So the numbers are checked mechanically instead. Anything with a digit whose
    literal string is absent from the transcript is reported for the human to
    confirm. Deliberately a warning and not a rejection: 「60 分钟」 or 「两部作品」
    may be a legitimate derivation, and refusing to publish over it would be a
    worse failure than flagging it.
    """
    found: list[str] = []
    for match in re.finditer(r"\d+(?:\.\d+)?", text):
        token = match.group()
        if token not in transcript and token not in found:
            found.append(token)
    return found


def render_shownotes(
    *,
    title: str,
    summary: str,
    chapters: list[Chapter],
    signature: str,
) -> str:
    """The published ShowNotes block.

    Structure is fixed by this function rather than by the prompt: the model
    decides *what* the episode is about, but a heading that only sometimes
    appears (or a timestamp format that changes between runs) makes the output
    impossible to paste into a description field twice in a row.

    There is no 本期要点 list, on purpose. A bullet list of the episode's
    arguments is a *summary of a podcast*; the job of ShowNotes is to make
    somebody press play, and a reader who has already consumed five bullets does
    not. The prose teaser carries the hook, the chapter list carries the shape.
    """
    lines: list[str] = []
    if title.strip():
        lines.append(f"# {title.strip()}")
        lines.append("")
    if summary.strip():
        lines.append(summary.strip())
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

DEFAULT_SHOWNOTES_PROMPT = """你是中文播客的运营编辑，为这期节目写一份 ShowNotes。
读者已经停在节目页上了，你要做的是让他按下播放，不是让他读完就够。

## 输出的三样东西

### 1. titles：5 个备选标题

**目标 16–20 字，绝对不超过 22 字。** 留出余量：写的时候按 18 字收，
这样即使数错两三个字也不会越线。
计数方式：汉字、全角标点、半角字母数字各记 1 字（"MV" 记 2 字，"：" 记 1 字）。
超过 22 字的标题会被程序丢弃，你就白写了。

写法（按优先级）：

- **双段式：位置 + 机制**。前半段说这件事发生在哪条缝隙里（谁、在哪、处在什么生态位），
  后半段说它到底怎么运作。两段必须承担不同职能。
  自检：把两段各改写成一句完整的话，如果两句说的是同一件事，就是同义重复，换掉一段。
  反例：「画面就是声轨：音乐如何被画出来」——前后同义，信息增量为零。
- **用事实，不用命题**。"甲方开始买这种画法"是描述；"甲方毁了创作"是立场，会招来预期错位。
- **留一个隐含悬念**，让前半段自然引出后半段。
- 慎用「最」「唯一」「颠覆」。

5 个标题要给不同方向，不要五个说法讲同一件事。至少包含：
一个双段式（位置+机制）、一个观点/落差型、一个意象型；其余自由发挥。

**不要用**「甲方」作为卖点（除非节目里真在吐槽），也不要写成「XX 的 N 种方式」这种清单体。
**不要自创节目里没出现的英文词**（glitch pop、互联网 core 这类），除非转写稿里就有这个说法。

### 2. summary：两段，共 200–240 字（硬上限 260）

**要有人味儿。** 这不是节目简介模板，是一个刚听完的人在跟你说这期值不值得听。

**长度很容易写超，写完自己数一遍。** 两段加起来超过 260 字就删：
优先删形容词、删并列项（A、B、C、D 留三个）、删「如何」「怎样」这类从句的铺陈。

- **第一段（90–110 字）：这件事为什么值得聊。**
  从一个具体的现象、数字或矛盾切入，不要从"本期节目"开头。
  允许有判断、有情绪、有一点点不平（比如某类创作者长期被忽视）。
  这一段决定他会不会往下读。**超过 120 字就是在铺陈，收住。**
- **第二段（110–140 字）：这期请了谁、聊了什么。**
  用「本期节目特别邀请了……」这类实句开头没问题，因为第一段已经把钩子给足了。
  嘉宾要有可验证的身份和具体作品（作品名、平台、提名、奖项任选，**只能用转写稿里出现过的**）。
  结尾用「以……为切入点，我们聊了 A、B、C。欢迎大家收听。」收口。
  A/B/C 是三个具体的话题短语，不是「创作」「行业」这种大词，**也不要超过三个**。

**不要这样写**（模型的通病，逐条避免）：

- 「本期节目我们将探讨……」——预告片腔，读者不知道你在说什么。
- 「深入剖析」「全方位解读」「精彩纷呈」——形容词代替内容。
- 把章节标题堆成一段话——那是目录，不是摘要。
- **编造数字、年份、奖项、播放量。** 这是最严重的一种：转写稿里没有的，
  一个字都不能写。转写稿说「今年入围费那奇」，就不要补成「2023 年入围」；
  说「在油管上出圈」，就不要补成「破百万播放」。**宁可少一个细节，不可多一个。**
- 把嘉宾没说过的话塞进他嘴里（"她拒绝高清 3D"），除非转写稿里确有这句意思。

### 3. chapters：按节目长度给章节

**分章规则：一个章节 = 主持人重新接过话头、且话题真的换了的地方。**

主持人会做这些事：问新问题、总结上一位的回答、说「那我们接下来聊聊……」、把话题拉回来。
嘉宾自己越聊越远不算转折——哪怕内容多精彩，那也是同一章的延续。

- 我已经在提示里告诉你哪位说话人是主持人（见「主持人」一行）。
- **line 填主持人那一段发言前面的编号**，不是嘉宾开始回答的编号。
- 只有在主持人完全没参与、话题由嘉宾自己硬转的情况下，才允许填嘉宾的编号。
- **必须覆盖到节目结尾。** 输入里最大的编号就是全长；最后一个章节不能停在节目
  一半的地方。**如果最后一章超过全长的三分之一，说明后面还有转折被漏掉了，回去找。**
- **开场和嘉宾介绍算同一章。** 前 5 分钟通常只有一到两章。不要因为主持人在第 2 分钟
  说了句「下面请嘉宾自我介绍」就切一刀——那是同一章的推进。
- 平均每 8–12 分钟一章，所以：60 分钟 5–8 章，90 分钟 7–10 章，3 小时 10–14 章。
- 章节标题 4–16 字，**名词短语**：写这一段在讲什么（「《幸福的悖论》的创作构思」），
  不要写成一个句子或一个判断（不要「嘉宾聊了幸福的悖论」）。不要以「讨论」「关于」开头。
- 相邻章节的主题必须真的不同；说不清区别就合并。

## 输入

转写稿每行格式：`[编号] 说话人名字：内容`。编号是整数，line 字段必须填输入里真实存在的编号。

## 输出

只依据转写稿，不要编造事实、人名、作品名、数字。严格按这个 JSON 输出，不要任何其他内容：

{"titles": ["…", "…", "…", "…", "…"],
 "summary": "第一段。\\n\\n第二段。",
 "chapters": [{"heading": "…", "line": 0}]}"""

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
