"""One place that builds a model client (§19.16).

The model is a *setting*, not a process constant: the menu lets the user pick ``qwen-max`` for
a flagship episode without a restart. That read, plus the "no key" case, was written out
identically in three routers; a fourth copy would have been the point where one of them
silently used a different model from the others.

Returns ``None`` rather than raising when no key is configured. The two callers want opposite
things from that state: 文稿 must fail with an explanation, while the deck generator falls back
to its deterministic planner and says so.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.providers.llm.bailian import BailianLlm
from animatter.services import settings_store


def build_llm(settings: Settings, session: Session) -> BailianLlm | None:
    if not settings.dashscope_api_key:
        return None
    chosen = str(settings_store.load(session).get("llm_model") or settings.llm_model)
    return BailianLlm(
        base_url=settings.dashscope_compatible_base,
        api_key=settings.dashscope_api_key,
        model=chosen,
    )
