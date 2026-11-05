"""Real LLM proof-reading (deselected by default).

    pytest -m live tests/test_live_proofread.py -s

The interesting question is not "does the API work" but "does the model actually
find a planted homophone error while leaving correct text alone". A model that
reports nothing is useless; one that reports everything is worse than useless,
because every false candidate costs a human decision.
"""

from __future__ import annotations

import pytest

from animatter.config import load_settings
from animatter.core.sentence import SentenceSpan
from animatter.core.tokens import WordSpan, expand_words
from animatter.providers.http import build_client
from animatter.providers.llm.bailian import BailianLlm
from animatter.services import proofread as proofread_service

pytestmark = pytest.mark.live

HOTWORDS = {"句级标签": 3, "字级标签": 3, "百炼": 4, "paraformer-v2": 3}

# Sentence 1 contains a planted homophone error: 剧集 should be 句级.
SENTENCES = [
    "今天我们聊聊播客剪辑的流程。",
    "第一步是把音频转成文字，我们用的是百炼平台。",
    "然后是剧集标签和字级标签的校正。",
    "最后根据校正结果导出文稿。",
]


def build_stream() -> tuple[list, list[SentenceSpan]]:
    """Build a token stream from synthetic sentences, one word per sentence chunk."""
    tokens: list = []
    spans: list[SentenceSpan] = []
    clock = 0
    for ord_, text in enumerate(SENTENCES):
        start_idx = len(tokens)
        # Split into 2-char pseudo-words so the stream resembles ASR output.
        for i in range(0, len(text), 2):
            chunk = text[i : i + 2]
            tokens.extend(expand_words([WordSpan(chunk, clock, clock + 200)]))
            clock += 200
        spans.append(SentenceSpan(ord=ord_, token_start=start_idx, token_end=len(tokens)))
    return tokens, spans


@pytest.fixture(scope="module")
def settings():  # type: ignore[no-untyped-def]
    s = load_settings()
    if not s.dashscope_api_key:
        pytest.skip("DASHSCOPE_API_KEY not configured")
    return s


def test_proofread_finds_planted_error(settings) -> None:  # type: ignore[no-untyped-def]
    tokens, spans = build_stream()
    llm = BailianLlm(
        base_url=settings.dashscope_compatible_base,
        api_key=settings.dashscope_api_key,
        model=settings.llm_model,
    )
    with build_client() as client:
        outcome = proofread_service.review(
            tokens=tokens, sentences=spans, hotwords=HOTWORDS, llm=llm, client=client
        )

    print(f"\nproposals={len(outcome.proposals)} rejected={len(outcome.rejected)}")
    for proposal in outcome.proposals:
        print(
            f"  句{proposal.sentence_ord}: {proposal.original} → {proposal.proposed} "
            f"(w={proposal.matched_hotword}, conf={proposal.confidence})"
        )
    for item, reason in outcome.rejected:
        print(f"  [rejected] {item}: {reason}")

    # Plumbing.
    assert outcome.prompt_tokens > 0
    assert outcome.chunks >= 1

    # Every surviving proposal must be justified by a glossary term and must
    # have located exactly in the token stream.
    for proposal in outcome.proposals:
        assert proposal.matched_hotword in HOTWORDS
        assert proposal.original in SENTENCES[proposal.sentence_ord]
        assert proposal.token_start < proposal.token_end

    # The planted error should be caught. This is the whole point of the feature.
    caught = [p for p in outcome.proposals if p.original == "剧集" or "剧集" in p.original]
    assert caught, f"模型没有发现植入的同音错写「剧集」；实际发现：{[p.original for p in outcome.proposals]}"
    assert any("句级" in c.proposed for c in caught), caught

    # Precision guard: it must not carpet-bomb correct sentences.
    assert len(outcome.proposals) <= 3, (
        f"候选过多（{len(outcome.proposals)}），说明模型在臆造修改："
        f"{[(p.original, p.proposed) for p in outcome.proposals]}"
    )
