"""Settings endpoints backing the workbench menu."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from animatter.db import Database
from animatter.services import settings_store

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class PatchIn(BaseModel):
    values: dict[str, Any]


class ModelsOut(BaseModel):
    asr: list[dict[str, Any]]
    llm: list[str]
    defaults: dict[str, Any]


@router.get("")
def get_settings(session: Session = Depends(_session_dependency)) -> dict[str, Any]:
    return settings_store.load(session)


@router.patch("")
def patch_settings(payload: PatchIn, session: Session = Depends(_session_dependency)) -> dict[str, Any]:
    return settings_store.save(session, payload.values)


@router.post("/reset")
def reset_settings(session: Session = Depends(_session_dependency)) -> dict[str, Any]:
    return settings_store.reset(session)


@router.get("/models", response_model=ModelsOut)
def get_models(session: Session = Depends(_session_dependency)) -> ModelsOut:
    """Available models with capability flags.

    The flags matter because choosing an ASR model silently decides whether
    hotwords and speaker separation work at all — the menu has to say so.
    """
    return ModelsOut(
        asr=settings_store.list_asr_models(session),
        llm=sorted(settings_store._ENUMS["llm_model"]),
        defaults=settings_store.DEFAULTS,
    )
