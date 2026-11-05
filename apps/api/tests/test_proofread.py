"""Proof-reading proposals: the guard rules are the actual product here.

A model that proposes anything is worse than no model at all, because every
false candidate costs a human decision. These tests pin the rules that make the
queue trustworthy (AGENT.md §6.5):

* only changes justified by a glossary term survive;
* the quoted original must exist verbatim in the token stream;
* nothing is applied automatically.
"""

from __future__ import annotations

import httpx
import pytest

from animatter.core.sentence import SentenceSpan
from animatter.core.tokens import TokenSpan
from animatter.providers.llm.bailian import BailianLlm
from animatter.services import proofread as proofread_service

HOTWORDS = {"句级标签": 3, "百炼": 4, "paraformer-v2": 3}


def build_tokens(text: str, *, step: int = 100) -> list[TokenSpan]:
    return [
        TokenSpan(
            idx=i,
            start_ms=i * step,
            end_ms=(i + 1) * step,
            text=ch,
            kind="char",
            src_word_idx=0,
            punc_after="",
            derived=True,
        )
        for i, ch in enumerate(text)
    ]


def sentences_for(tokens: list[TokenSpan], sizes: list[int]) -> list[SentenceSpan]:
    spans: list[SentenceSpan] = []
    cursor = 0
    for ord_, size in enumerate(sizes):
        spans.append(SentenceSpan(ord=ord_, token_start=cursor, token_end=cursor + size))
        cursor += size
    return spans


def fake_llm(monkeypatch, payload: dict[str, object]) -> BailianLlm:
    llm = BailianLlm(base_url="https://example.test/compatible-mode/v1", api_key="k", model="qwen-plus")

    def fake_chat_json(**_kwargs):  # type: ignore[no-untyped-def]
        from animatter.providers.llm.bailian import LlmReply

        return payload, LlmReply(text="", prompt_tokens=100, completion_tokens=20, model="qwen-plus")

    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    return llm


def no_network_client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)), trust_env=False)


def run(monkeypatch, payload: dict[str, object], text: str, sizes: list[int]):  # type: ignore[no-untyped-def]
    tokens = build_tokens(text)
    sentences = sentences_for(tokens, sizes)
    llm = fake_llm(monkeypatch, payload)
    return proofread_service.review(
        tokens=tokens,
        sentences=sentences,
        hotwords=HOTWORDS,
        llm=llm,
        client=no_network_client(),
    )


# --- acceptance rules ---------------------------------------------------


def test_valid_correction_accepted(monkeypatch) -> None:
    outcome = run(
        monkeypatch,
        {
            "corrections": [
                {
                    "sentence_ord": 0,
                    "original": "剧集标签",
                    "proposed": "句级标签",
                    "matched_hotword": "句级标签",
                    "confidence": 0.9,
                    "reason": "同音误写",
                }
            ]
        },
        "剧集标签很好用",
        [7],
    )
    assert len(outcome.proposals) == 1
    proposal = outcome.proposals[0]
    assert (proposal.token_start, proposal.token_end) == (0, 4)
    assert proposal.confidence == 0.9


def test_change_without_hotword_justification_rejected(monkeypatch) -> None:
    outcome = run(
        monkeypatch,
        {
            "corrections": [
                {
                    "sentence_ord": 0,
                    "original": "很好用",
                    "proposed": "非常好用",
                    "matched_hotword": "非常好用",  # not in the glossary
                    "confidence": 0.95,
                }
            ]
        },
        "剧集标签很好用",
        [7],
    )
    assert outcome.proposals == []
    assert any("不在热词表" in reason for _, reason in outcome.rejected)


def test_original_not_in_text_rejected(monkeypatch) -> None:
    outcome = run(
        monkeypatch,
        {
            "corrections": [
                {
                    "sentence_ord": 0,
                    "original": "根本不存在的片段",
                    "proposed": "句级标签",
                    "matched_hotword": "句级标签",
                    "confidence": 0.9,
                }
            ]
        },
        "剧集标签很好用",
        [7],
    )
    assert outcome.proposals == []
    assert any("找不到原文" in reason for _, reason in outcome.rejected)


def test_original_in_another_sentence_rejected(monkeypatch) -> None:
    """A match outside the claimed sentence means the model lost its place."""
    outcome = run(
        monkeypatch,
        {
            "corrections": [
                {
                    "sentence_ord": 1,  # wrong sentence
                    "original": "剧集标签",
                    "proposed": "句级标签",
                    "matched_hotword": "句级标签",
                    "confidence": 0.9,
                }
            ]
        },
        "剧集标签很好用",
        [4, 3],
    )
    assert outcome.proposals == []


def test_identical_original_and_proposed_rejected(monkeypatch) -> None:
    outcome = run(
        monkeypatch,
        {
            "corrections": [
                {
                    "sentence_ord": 0,
                    "original": "剧集标签",
                    "proposed": "剧集标签",
                    "matched_hotword": "句级标签",
                }
            ]
        },
        "剧集标签很好用",
        [7],
    )
    assert outcome.proposals == []


def test_unknown_sentence_ord_rejected(monkeypatch) -> None:
    outcome = run(
        monkeypatch,
        {
            "corrections": [
                {
                    "sentence_ord": 99,
                    "original": "剧集标签",
                    "proposed": "句级标签",
                    "matched_hotword": "句级标签",
                }
            ]
        },
        "剧集标签很好用",
        [7],
    )
    assert outcome.proposals == []
    assert any("不在本次校对范围" in reason for _, reason in outcome.rejected)


def test_missing_fields_rejected(monkeypatch) -> None:
    outcome = run(
        monkeypatch,
        {"corrections": [{"sentence_ord": 0, "original": "", "proposed": "句级标签"}]},
        "剧集标签",
        [4],
    )
    assert outcome.proposals == []


# --- coercion and tolerance --------------------------------------------


def test_confidence_clamped(monkeypatch) -> None:
    outcome = run(
        monkeypatch,
        {
            "corrections": [
                {
                    "sentence_ord": 0,
                    "original": "剧集",
                    "proposed": "句级标签",
                    "matched_hotword": "句级标签",
                    "confidence": 5.0,
                }
            ]
        },
        "剧集标签",
        [4],
    )
    assert outcome.proposals[0].confidence == 1.0


def test_string_confidence_parsed(monkeypatch) -> None:
    outcome = run(
        monkeypatch,
        {
            "corrections": [
                {
                    "sentence_ord": "0",
                    "original": "剧集",
                    "proposed": "句级标签",
                    "matched_hotword": "句级标签",
                    "confidence": "0.7",
                }
            ]
        },
        "剧集标签",
        [4],
    )
    assert outcome.proposals[0].confidence == 0.7


def test_alternate_payload_key(monkeypatch) -> None:
    outcome = run(
        monkeypatch,
        {
            "items": [
                {
                    "sentence_ord": 0,
                    "original": "剧集",
                    "proposed": "句级标签",
                    "matched_hotword": "句级标签",
                }
            ]
        },
        "剧集标签",
        [4],
    )
    assert len(outcome.proposals) == 1


def test_empty_corrections_list(monkeypatch) -> None:
    outcome = run(monkeypatch, {"corrections": []}, "剧集标签", [4])
    assert outcome.proposals == []
    assert outcome.rejected == []


# --- chunking -----------------------------------------------------------


def test_multiple_chunks_are_merged(monkeypatch) -> None:
    text = "剧集标签" * 10
    tokens = build_tokens(text)
    sentences = [SentenceSpan(ord=i, token_start=i * 4, token_end=(i + 1) * 4) for i in range(10)]
    llm = fake_llm(
        monkeypatch,
        {
            "corrections": [
                {
                    "sentence_ord": i,
                    "original": "剧集",
                    "proposed": "句级标签",
                    "matched_hotword": "句级标签",
                    "confidence": 0.8,
                }
                for i in range(10)
            ]
        },
    )
    outcome = proofread_service.review(
        tokens=tokens,
        sentences=sentences,
        hotwords=HOTWORDS,
        llm=llm,
        client=no_network_client(),
        chunk_size=3,
    )
    assert outcome.chunks == 4  # 10 sentences / 3 per chunk
    assert len(outcome.proposals) == 10


def test_duplicate_spans_collapse_to_best(monkeypatch) -> None:
    tokens = build_tokens("剧集标签")
    sentences = [SentenceSpan(ord=0, token_start=0, token_end=4)]
    llm = fake_llm(
        monkeypatch,
        {
            "corrections": [
                {
                    "sentence_ord": 0,
                    "original": "剧集",
                    "proposed": "句级标签",
                    "matched_hotword": "句级标签",
                    "confidence": 0.4,
                },
                {
                    "sentence_ord": 0,
                    "original": "剧集",
                    "proposed": "句级标签",
                    "matched_hotword": "句级标签",
                    "confidence": 0.9,
                },
            ]
        },
    )
    outcome = proofread_service.review(
        tokens=tokens,
        sentences=sentences,
        hotwords=HOTWORDS,
        llm=llm,
        client=no_network_client(),
    )
    assert len(outcome.proposals) == 1
    assert outcome.proposals[0].confidence == 0.9


def test_no_hotwords_raises() -> None:
    from animatter.errors import AppError

    llm = BailianLlm(base_url="https://example.test/compatible-mode/v1", api_key="k", model="qwen-plus")
    with pytest.raises(AppError, match="热词表为空"):
        proofread_service.review(
            tokens=build_tokens("文字"),
            sentences=[SentenceSpan(ord=0, token_start=0, token_end=2)],
            hotwords={},
            llm=llm,
            client=no_network_client(),
        )


# --- prompt construction ------------------------------------------------


def test_prompt_contains_hotwords_and_numbered_sentences() -> None:
    tokens = build_tokens("剧集标签")
    sentences = [SentenceSpan(ord=0, token_start=0, token_end=4)]
    prompt = proofread_service.build_prompt(tokens, sentences, HOTWORDS, start_ord=0, end_ord=1)
    assert "句级标签" in prompt
    assert "[0] 剧集标签" in prompt


def test_prompt_includes_punctuation() -> None:
    tokens = [
        TokenSpan(0, 0, 100, "你", "char", 0, "", True),
        TokenSpan(1, 100, 200, "好", "char", 0, "。", True),
    ]
    prompt = proofread_service.build_prompt(
        tokens, [SentenceSpan(ord=0, token_start=0, token_end=2)], HOTWORDS, start_ord=0, end_ord=1
    )
    assert "你好。" in prompt


def test_prompt_marks_empty_glossary() -> None:
    tokens = build_tokens("文字")
    prompt = proofread_service.build_prompt(
        tokens, [SentenceSpan(ord=0, token_start=0, token_end=2)], {}, start_ord=0, end_ord=1
    )
    assert "（空）" in prompt
