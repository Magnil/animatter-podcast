"""Hotword sanitising and merging.

Every rule here comes from a documented or measured constraint (AGENT.md §2.1,
§6.1). The most important one is the weight cap: ``weight=5`` was measured to
push a *correct* word ("句级") into a hotword ("剧集"), so the default is 3 and
5 is treated as a deliberate, warned-about choice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Bailian text rules (§2.1): non-ASCII words ≤15 chars; pure ASCII ≤7 segments.
MAX_NON_ASCII_CHARS = 15
MAX_ASCII_SEGMENTS = 7

#: Weights. 5 is legal but measurably harmful unless the word is unmistakable.
MIN_WEIGHT = 1
DEFAULT_WEIGHT = 3
MAX_WEIGHT = 5
SUPER_WEIGHT = 50

#: Client-side safety margin under the model ceilings (500 / 2000).
DEFAULT_LIMIT = 450

_ASCII_SEGMENT_RE = re.compile(r"\s+")

#: Words that are always common enough to hurt more than help.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "我们",
        "你们",
        "他们",
        "这个",
        "那个",
        "什么",
        "怎么",
        "因为",
        "所以",
        "但是",
        "而且",
        "然后",
        "就是",
        "可以",
        "这样",
        "那样",
        "一个",
        "现在",
        "时候",
        "如果",
        "还是",
        "已经",
        "知道",
        "觉得",
        "应该",
        "可能",
        "其实",
        "今天",
        "明天",
        "昨天",
        "大家",
        "自己",
        "东西",
        "事情",
        "问题",
        "地方",
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "you",
        "are",
        "was",
    }
)

#: Words that carry almost no domain signal on their own; keeping them at full
#: weight crowds out the words that actually matter.
_GENERIC_DOWNWEIGHT: frozenset[str] = frozenset(
    {
        "人工智能",
        "机器学习",
        "深度学习",
        "大模型",
        "区块链",
        "云计算",
        "互联网",
        "数字化",
        "元宇宙",
        "大数据",
    }
)


class HotwordError(ValueError):
    """A term violates a documented provider constraint."""


@dataclass(frozen=True, slots=True)
class HotwordCandidate:
    """One term on its way into, or out of, the glossary."""

    text: str
    weight: int = DEFAULT_WEIGHT
    lang: str = "zh"
    kind: str = "term"
    evidence: str = ""
    origin: str = "manual"

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "weight": self.weight,
            "lang": self.lang,
            "kind": self.kind,
            "evidence": self.evidence,
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class SanitizeReport:
    """What sanitising did, so the UI can explain itself instead of silently dropping."""

    accepted: list[HotwordCandidate] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (text, reason)
    downweighted: list[tuple[str, int, int]] = field(default_factory=list)  # (text, old, new)

    @property
    def rejected_texts(self) -> set[str]:
        return {text for text, _ in self.rejected}


def text_length_ok(text: str) -> bool:
    """Apply Bailian's documented length limits."""
    trimmed = text.strip()
    if not trimmed:
        return False
    if trimmed.isascii():
        segments = [segment for segment in _ASCII_SEGMENT_RE.split(trimmed) if segment]
        return len(segments) <= MAX_ASCII_SEGMENTS
    return len(trimmed) <= MAX_NON_ASCII_CHARS


def _reject_reason(text: str) -> str | None:
    trimmed = text.strip()
    if not trimmed:
        return "空词条"
    if len(trimmed) < 2 and trimmed.isascii():
        return "单个字母没有纠错价值"
    if not text_length_ok(trimmed):
        limit = (
            f"{MAX_ASCII_SEGMENTS} 个空格分隔片段" if trimmed.isascii() else f"{MAX_NON_ASCII_CHARS} 个字符"
        )
        return f"超出百炼热词长度限制（{limit}）"
    if trimmed.lower() in _STOPWORDS:
        return "常见停用词，加了会挤占其他词"
    if not any(ch.isalnum() for ch in trimmed):
        return "不含字母或数字"
    if trimmed.isdigit():
        return "纯数字"
    # A full sentence is not a term; a long phrase with punctuation almost always is.
    if any(ch in "，。！？；、,.!?;" for ch in trimmed):
        return "包含句读，应拆成独立词条"
    return None


def clamp_weight(weight: int) -> int:
    if weight == SUPER_WEIGHT:
        return SUPER_WEIGHT
    return max(MIN_WEIGHT, min(MAX_WEIGHT, weight))


def sanitize(
    candidates: list[HotwordCandidate],
    *,
    limit: int = DEFAULT_LIMIT,
) -> SanitizeReport:
    """Deduplicate, validate, down-weight generic terms, and cap the list.

    Deduplication keeps the highest weight seen: the same term proposed by both
    the outline and the global glossary should not lose its stronger signal.
    """
    accepted: dict[str, HotwordCandidate] = {}
    rejected: list[tuple[str, str]] = []
    downweighted: list[tuple[str, int, int]] = []

    for candidate in candidates:
        text = candidate.text.strip()
        reason = _reject_reason(text)
        if reason is not None:
            rejected.append((text or candidate.text, reason))
            continue

        weight = clamp_weight(candidate.weight)
        if text in _GENERIC_DOWNWEIGHT and weight > 2:
            downweighted.append((text, weight, 2))
            weight = 2

        existing = accepted.get(text)
        if existing is not None:
            if weight > existing.weight:
                accepted[text] = HotwordCandidate(
                    text=text,
                    weight=weight,
                    lang=candidate.lang or existing.lang,
                    kind=candidate.kind or existing.kind,
                    evidence=candidate.evidence or existing.evidence,
                    origin=existing.origin,
                )
            continue

        accepted[text] = HotwordCandidate(
            text=text,
            weight=weight,
            lang=candidate.lang or "zh",
            kind=candidate.kind or "term",
            evidence=candidate.evidence,
            origin=candidate.origin,
        )

    # Sort before truncating so the cut is deterministic and keeps the strongest:
    # weight first, then a longer term (more specific), then lexicographic.
    ordered = sorted(accepted.values(), key=lambda c: (-c.weight, -len(c.text), c.text))
    kept = ordered[:limit]
    dropped = ordered[limit:]
    for candidate in dropped:
        rejected.append((candidate.text, f"超出上限 {limit} 条，已丢弃（权重较低）"))

    return SanitizeReport(accepted=kept, rejected=rejected, downweighted=downweighted)


def to_instant_vocabulary(candidates: list[HotwordCandidate]) -> dict[str, int]:
    """Build the ``parameters.vocabulary`` payload."""
    return {candidate.text: clamp_weight(candidate.weight) for candidate in candidates}


def merge_vocabularies(*groups: dict[str, int]) -> dict[str, int]:
    """Merge hotword maps, keeping the maximum weight for repeated terms."""
    merged: dict[str, int] = {}
    for group in groups:
        for text, weight in group.items():
            merged[text] = max(merged.get(text, 0), clamp_weight(weight))
    return merged


def truncate_to_limit(vocabulary: dict[str, int], limit: int = DEFAULT_LIMIT) -> tuple[dict[str, int], int]:
    """Trim a merged vocabulary, returning the trimmed map and how many were dropped.

    The provider silently drops a *random* subset past its ceiling, so trimming
    here is what keeps behaviour reproducible (AGENT.md §2.1).
    """
    if len(vocabulary) <= limit:
        return dict(vocabulary), 0
    ordered = sorted(vocabulary.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    kept = dict(ordered[:limit])
    return kept, len(vocabulary) - limit
