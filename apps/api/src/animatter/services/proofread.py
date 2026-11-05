"""LLM proof-reading: propose text repairs constrained to known hotwords.

The design constraint that makes this safe (AGENT.md §6.5):

1. The model may only propose changes **justified by a term in the glossary**.
   Anything else is discarded before it reaches the review queue, which is what
   keeps free-form "improvements" and hallucinations out.
2. Each proposal must **locate exactly** in the token stream. If the quoted
   original text cannot be found, the candidate is dropped rather than guessed
   at — a fuzzy match would rewrite the wrong span.
3. Nothing is applied here. Output is a list of candidates for a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from animatter.core.realign import locate_sentence
from animatter.core.sentence import SentenceSpan
from animatter.core.tokens import TokenSpan
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.providers.llm.bailian import BailianLlm

log = get_logger(__name__)

#: Sentences per model call. Small enough that the model keeps its place in the
#: text, large enough that context survives; ~2k chars in practice.
SENTENCES_PER_CHUNK = 25

#: Hard cap per call so a runaway completion cannot blow the budget.
MAX_CANDIDATES_PER_CALL = 60

SYSTEM_PROMPT = """你是中文播客字幕的校对助手。
用户会给你：一段连续的句子文本（带编号）、以及一份「热词表」（节目里的专有名词、人名、产品名的正确写法）。

你的唯一任务：找出文本里**写错了的热词**，并给出正确写法。

严格规则：
1. 只报告「明显应该是热词表中某个词，但被识别成了别的写法」的位置。
   典型情况是同音字/近音字错误，例如热词是「句级」而文本写成「剧集」。
2. 如果文本里的词与热词表无关，或者你不确定，**必须不报告**。
   宁可漏报，也绝不能臆造修改。
3. 不要改标点、不要改语序、不要润色表达、不要删口水词。只改错词。
4. original 必须是文本中**逐字存在**的片段（可直接复制），长度尽量短（2-6 字）。
5. proposed 必须是热词表中的原词。
6. confidence 取 0-1，表示你有多确定这是个同音错写。

严格按 JSON 输出：
{"corrections": [
  {"sentence_ord": 12, "original": "剧集标签", "proposed": "句级标签",
   "matched_hotword": "句级标签", "confidence": 0.9, "reason": "同音误写"}
]}
没有发现任何问题时输出 {"corrections": []}。"""


@dataclass(frozen=True)
class Proposal:
    sentence_ord: int
    original: str
    proposed: str
    matched_hotword: str
    confidence: float
    reason: str
    token_start: int
    token_end: int


@dataclass
class ProofreadOutcome:
    proposals: list[Proposal] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (description, reason)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    chunks: int = 0


def build_prompt(
    tokens: list[TokenSpan],
    sentences: list[SentenceSpan],
    hotwords: dict[str, int],
    *,
    start_ord: int,
    end_ord: int,
) -> str:
    """Render one chunk as numbered sentences plus the glossary."""
    lines = ["=== 热词表（正确写法）==="]
    lines.append("、".join(sorted(hotwords)) if hotwords else "（空）")
    lines.append("")
    lines.append("=== 待校对文本 ===")
    for span in sentences[start_ord:end_ord]:
        text = "".join(t.text + t.punc_after for t in tokens[span.token_start : span.token_end])
        lines.append(f"[{span.ord}] {text}")
    return "\n".join(lines)


def _coerce_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _coerce_int(value: object, default: int = -1) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _extract_items(payload: dict[str, object]) -> list[dict[str, object]]:
    for key in ("corrections", "items", "results", "terms"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def review_chunk(
    *,
    tokens: list[TokenSpan],
    sentences: list[SentenceSpan],
    hotwords: dict[str, int],
    llm: BailianLlm,
    client: httpx.Client,
    start_ord: int,
    end_ord: int,
) -> tuple[list[Proposal], list[tuple[str, str]], int, int]:
    """Run one chunk through the model and keep only verifiable proposals."""
    if not hotwords:
        return [], [], 0, 0

    prompt = build_prompt(tokens, sentences, hotwords, start_ord=start_ord, end_ord=end_ord)
    payload, reply = llm.chat_json(system=SYSTEM_PROMPT, user=prompt, client=client, max_tokens=1500)

    by_ord = {span.ord: span for span in sentences}
    proposals: list[Proposal] = []
    rejected: list[tuple[str, str]] = []
    hotword_set = set(hotwords)

    for item in _extract_items(payload):
        original = str(item.get("original") or "").strip()
        proposed = str(item.get("proposed") or "").strip()
        matched = str(item.get("matched_hotword") or proposed).strip()
        sentence_ord = _coerce_int(item.get("sentence_ord"), -1)

        label = f"{sentence_ord}:{original}→{proposed}"

        if not original or not proposed:
            rejected.append((label, "缺少 original 或 proposed"))
            continue
        if original == proposed:
            rejected.append((label, "原文与建议相同"))
            continue
        # Rule 1: the change must be justified by a known term.
        if matched not in hotword_set:
            rejected.append((label, f"「{matched}」不在热词表中，拒绝无依据的改动"))
            continue
        if proposed not in original and matched not in proposed:
            # The proposed text must at least be the hotword it claims.
            rejected.append((label, "建议写法与所引热词不一致"))
            continue

        span = by_ord.get(sentence_ord)
        if span is None:
            rejected.append((label, f"句子 {sentence_ord} 不在本次校对范围内"))
            continue

        # Rule 2: the quoted original must exist verbatim inside that sentence.
        located = locate_sentence(tokens, span.token_start, span.token_end, original)
        if located is None:
            rejected.append((label, "在对应句子中找不到原文片段，拒绝模糊匹配"))
            continue

        proposals.append(
            Proposal(
                sentence_ord=sentence_ord,
                original=original,
                proposed=proposed,
                matched_hotword=matched,
                confidence=max(0.0, min(1.0, _coerce_float(item.get("confidence"), 0.5))),
                reason=str(item.get("reason") or "")[:200],
                token_start=located.start,
                token_end=located.end,
            )
        )
        if len(proposals) >= MAX_CANDIDATES_PER_CALL:
            rejected.append((label, f"超过单次上限 {MAX_CANDIDATES_PER_CALL}，其余丢弃"))
            break

    return proposals, rejected, reply.prompt_tokens, reply.completion_tokens


def review(
    *,
    tokens: list[TokenSpan],
    sentences: list[SentenceSpan],
    hotwords: dict[str, int],
    llm: BailianLlm,
    client: httpx.Client,
    chunk_size: int = SENTENCES_PER_CHUNK,
) -> ProofreadOutcome:
    """Proof-read the whole transcript chunk by chunk.

    Overlapping proposals for the same span are collapsed, keeping the highest
    confidence: two chunks may both notice the same error near a boundary.
    """
    if not hotwords:
        raise AppError(
            "no_hotwords",
            "热词表为空，无法校对",
            hint="校对必须有热词作为依据，否则模型只能凭空猜测。先在词库或大纲里添加词条。",
        )

    outcome = ProofreadOutcome()
    seen: dict[tuple[int, int], Proposal] = {}

    for start in range(0, len(sentences), chunk_size):
        end = min(start + chunk_size, len(sentences))
        proposals, rejected, prompt_tokens, completion_tokens = review_chunk(
            tokens=tokens,
            sentences=sentences,
            hotwords=hotwords,
            llm=llm,
            client=client,
            start_ord=start,
            end_ord=end,
        )
        outcome.chunks += 1
        outcome.prompt_tokens += prompt_tokens
        outcome.completion_tokens += completion_tokens
        outcome.rejected.extend(rejected)

        for proposal in proposals:
            key = (proposal.token_start, proposal.token_end)
            existing = seen.get(key)
            if existing is None or proposal.confidence > existing.confidence:
                seen[key] = proposal

    outcome.proposals = sorted(seen.values(), key=lambda p: (p.token_start, -p.confidence))
    log.info(
        "proofread.done",
        chunks=outcome.chunks,
        proposals=len(outcome.proposals),
        rejected=len(outcome.rejected),
        prompt_tokens=outcome.prompt_tokens,
    )
    return outcome
