"""Real LLM outline extraction (deselected by default).

    pytest -m live tests/test_live_outline.py -s

Asserts *shape and plumbing*, not extraction quality: what the model picks is a
product judgement, but "call succeeds, junk is filtered, tokens are reported"
must hold on every run.
"""

from __future__ import annotations

import pytest

from animatter.config import load_settings
from animatter.providers.http import build_client
from animatter.providers.llm.bailian import BailianLlm
from animatter.services import outline as outline_service

pytestmark = pytest.mark.live

OUTLINE = """
本期节目：漫谈播客制作流程

嘉宾：陈默（「深夜漫谈」主播，做了七年播客）

0:00 开场，聊为什么播客剪辑这么痛苦
0:08 陈默介绍他用过的三款工具：Audition、Descript、剪映
0:15 讨论「句级标签」和「字级标签」的区别
0:22 提到阿里云百炼平台的 paraformer-v2 模型
0:30 陈默推荐了他的节目《深夜漫谈》，还有合作的《城市观察》
0:38 聊聊 AI 转写对播客行业的影响
0:45 结尾预告下一期会请到「老周」
"""


@pytest.fixture(scope="module")
def settings():  # type: ignore[no-untyped-def]
    s = load_settings()
    if not s.dashscope_api_key:
        pytest.skip("DASHSCOPE_API_KEY not configured")
    return s


def test_outline_extraction_end_to_end(settings) -> None:  # type: ignore[no-untyped-def]
    llm = BailianLlm(
        base_url=settings.dashscope_compatible_base,
        api_key=settings.dashscope_api_key,
        model=settings.llm_model,
    )
    with build_client() as client:
        result = outline_service.extract(outline_text=OUTLINE, llm=llm, client=client)

    print(f"\nraw={result.raw_count} accepted={len(result.candidates)}")
    for candidate in result.candidates[:15]:
        print(f"  {candidate.text} w={candidate.weight} kind={candidate.kind} | {candidate.evidence}")

    # Plumbing assertions.
    assert result.prompt_tokens > 0
    assert result.completion_tokens > 0

    # The outline names concrete entities; a working extraction must find some.
    assert len(result.candidates) >= 3, f"only got: {[c.text for c in result.candidates]}"

    texts = {c.text for c in result.candidates}

    # Named entities from the outline should appear in some form.
    expected_hits = [name for name in ("陈默", "百炼", "paraformer", "深夜漫谈", "城市观察") if name in texts]
    assert expected_hits, f"none of the named entities were extracted; got {texts}"

    # Sanitising must have removed obvious junk.
    for term in result.candidates:
        assert len(term.text) >= 1
        assert term.text.strip() == term.text
        assert 1 <= term.weight <= 50
        assert term.origin == "outline"
