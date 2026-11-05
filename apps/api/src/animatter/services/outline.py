"""Outline → per-episode hotwords (AGENT.md §6.1).

The LLM proposes; this module decides. Every candidate must survive
:func:`animatter.core.hotwords.sanitize` and every accepted term keeps its
supporting quote from the outline, so a human can judge it in one glance instead
of trusting an opaque list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from animatter.core.hotwords import DEFAULT_WEIGHT, HotwordCandidate, SanitizeReport, sanitize
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.models import Outline
from animatter.providers.llm.bailian import BailianLlm

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

log = get_logger(__name__)

#: Beyond this the outline gets truncated: extraction quality plateaus, and cost
#: grows linearly. 8000 chars is a long show-notes document.
MAX_OUTLINE_CHARS = 8000
MAX_CANDIDATES = 120

SYSTEM_PROMPT = """你是中文播客的转写校对助手。
你的任务是从节目大纲中提取「语音识别容易听错、需要提前告诉识别引擎正确写法」的词。

只提取以下几类：
1. 人名（嘉宾、提及的人物）
2. 机构、公司、品牌、产品名
3. 专业术语、行话、作品名
4. 中英混用的专有表达

必须遵守：
- 每个词是一个「词」，不是句子或短语；长度不超过 15 个中文字符（纯英文不超过 5 个单词）
- 必须是实际会被念出来的词，抽象概念不要提（如「思考」「内容」「价值」）
- 通用大词不要提（如「人工智能」「互联网」「行业」）
- 不要提纯数字、日期、标点符号
- 宁可少提，也不要提一堆没用的词：热词太杂会干扰识别

对每个词给出：
- text: 词的准确写法
- weight: 1-5 的整数。专有名词用 3-4；极可能被听错且写法唯一的用 5；泛用术语用 2
- kind: person / org / product / term / title 之一
- evidence: 大纲中支持这个词的原句片段（不超过 30 字）

严格按 JSON 输出，格式为 {"terms": [...]}，不要输出任何其他内容。"""


@dataclass(frozen=True)
class ExtractResult:
    candidates: list[HotwordCandidate]
    report: SanitizeReport
    prompt_tokens: int
    completion_tokens: int
    truncated: bool = False
    raw_count: int = 0
    warnings: list[str] = field(default_factory=list)


def _user_prompt(outline: str, meta: str) -> str:
    parts = ["以下是播客节目大纲，请提取热词："]
    if meta.strip():
        parts.append(f"\n节目信息：{meta.strip()}")
    parts.append(f"\n=== 大纲开始 ===\n{outline}\n=== 大纲结束 ===")
    return "".join(parts)


def _coerce_terms(payload: dict[str, object]) -> list[dict[str, object]]:
    terms = payload.get("terms")
    if isinstance(terms, list):
        return [item for item in terms if isinstance(item, dict)]
    # Some models return {"hotwords": ...} or a bare mapping; accept the shapes
    # that carry the same information rather than failing the whole run.
    for key in ("hotwords", "keywords", "result"):
        alternative = payload.get(key)
        if isinstance(alternative, list):
            return [item for item in alternative if isinstance(item, dict)]
    return []


def extract(
    *,
    outline_text: str,
    llm: BailianLlm,
    client: httpx.Client,
    meta: str = "",
    limit: int = MAX_CANDIDATES,
) -> ExtractResult:
    """Turn an outline into sanitised hotword candidates."""
    outline = outline_text.strip()
    if not outline:
        raise AppError(
            "outline_empty",
            "大纲内容为空",
            hint="先粘贴节目大纲或 show notes，再生成热词。",
        )

    truncated = len(outline) > MAX_OUTLINE_CHARS
    if truncated:
        outline = outline[:MAX_OUTLINE_CHARS]

    payload, reply = llm.chat_json(
        system=SYSTEM_PROMPT,
        user=_user_prompt(outline, meta),
        client=client,
        max_tokens=3000,
    )

    raw_terms = _coerce_terms(payload)
    warnings: list[str] = []
    if not raw_terms:
        warnings.append("模型没有返回任何词条，可能大纲信息量太少或格式异常")

    candidates: list[HotwordCandidate] = []
    for item in raw_terms:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        raw_weight = item.get("weight", DEFAULT_WEIGHT)
        try:
            weight = int(raw_weight) if isinstance(raw_weight, (int, float, str)) else DEFAULT_WEIGHT
        except (TypeError, ValueError):
            weight = DEFAULT_WEIGHT
        candidates.append(
            HotwordCandidate(
                text=text,
                weight=weight,
                lang=str(item.get("lang") or "zh"),
                kind=str(item.get("kind") or "term"),
                evidence=str(item.get("evidence") or "")[:60],
                origin="outline",
            )
        )

    report = sanitize(candidates, limit=limit)
    if report.rejected:
        warnings.append(f"{len(report.rejected)} 条候选未通过校验，已剔除")
    if report.downweighted:
        warnings.append(f"{len(report.downweighted)} 条通用词已自动降权")

    log.info(
        "outline.extracted",
        raw=len(raw_terms),
        accepted=len(report.accepted),
        rejected=len(report.rejected),
    )
    return ExtractResult(
        candidates=report.accepted,
        report=report,
        prompt_tokens=reply.prompt_tokens,
        completion_tokens=reply.completion_tokens,
        truncated=truncated,
        raw_count=len(raw_terms),
        warnings=warnings,
    )


def save_outline(session: Session, *, project_id: str, media_id: str, content: str) -> Outline:
    """Persist the outline so hotwords can be regenerated without re-pasting."""
    from sqlalchemy import select

    from animatter.ids import new_id, utcnow_iso

    existing = session.scalars(
        select(Outline).where(Outline.project_id == project_id, Outline.media_id == media_id)
    ).first()
    if existing is not None:
        existing.content = content
        session.flush()
        return existing

    row = Outline(
        id=new_id("out"),
        project_id=project_id,
        media_id=media_id,
        content=content,
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def load_outline(session: Session, *, project_id: str, media_id: str) -> str:
    from sqlalchemy import select

    row = session.scalars(
        select(Outline).where(Outline.project_id == project_id, Outline.media_id == media_id)
    ).first()
    return row.content if row is not None else ""
