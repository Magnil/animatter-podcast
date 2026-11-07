"""ShowNotes / 文稿 endpoints.

One endpoint generates, one streams the result as a file. Deliberately *not*
the async render pattern used for audio: a model call is one blocking HTTP
request that either answers or does not, so there is no progress to poll and
nothing to resume. What it does need is a timeout the user can see, which is
why the client shows elapsed time rather than a fake percentage.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.script import DEFAULT_PROMPTS
from animatter.db import Database
from animatter.errors import AppError
from animatter.providers.http import build_client
from animatter.providers.llm.bailian import BailianLlm
from animatter.services import script as script_service
from animatter.services import settings_store

router = APIRouter(prefix="/api", tags=["script"])


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


def _llm(request: Request, session: Session) -> BailianLlm:
    settings: Settings = request.app.state.settings
    if not settings.dashscope_api_key:
        raise AppError(
            "missing_api_key",
            "未配置 DASHSCOPE_API_KEY",
            hint="在 .env 里填入百炼 API Key，重启服务后再试。",
            status=503,
        )
    # The model is a *setting*, not a process constant: the menu lets the user
    # pick qwen-max for a flagship episode without a restart.
    chosen = str(settings_store.load(session).get("llm_model") or settings.llm_model)
    return BailianLlm(
        base_url=settings.dashscope_compatible_base,
        api_key=settings.dashscope_api_key,
        model=chosen,
    )


class GenerateIn(BaseModel):
    format: str = Field(default="shownotes", description="shownotes | dialogue | article")
    #: Map chapter timestamps onto the rendered audio. Same default as subtitles,
    #: and the same reason: published notes sit next to the published file.
    mapped: bool = True
    #: Overrides the project name as the document title.
    title: str = ""


class ChapterOut(BaseModel):
    heading: str
    start_ms: int
    clock: str


class GenerateOut(BaseModel):
    format: str
    title: str
    body: str
    filename: str
    chapters: list[ChapterOut]
    turns: int
    blocks: int
    warnings: list[str]


class PromptOut(BaseModel):
    format: str
    label: str
    default: str
    current: str


@router.get("/script/prompts", response_model=list[PromptOut])
def list_prompts(session: Session = Depends(_session_dependency)) -> list[PromptOut]:
    """Built-in prompts plus whatever the user has saved.

    Served so 设置 can show the default next to the editable copy: editing a
    prompt you cannot see the original of is editing blind.
    """
    labels = {"shownotes": "ShowNotes", "dialogue": "文稿 · 对话式", "article": "文稿 · 文章式"}
    stored = settings_store.load(session)
    return [
        PromptOut(
            format=fmt,
            label=labels[fmt],
            default=DEFAULT_PROMPTS[fmt],
            current=script_service.prompt_for(stored, fmt),
        )
        for fmt in ("shownotes", "dialogue", "article")
    ]


@router.post("/versions/{version_id}/script/generate", response_model=GenerateOut)
def generate_script(
    version_id: str,
    payload: GenerateIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> GenerateOut:
    settings: Settings = request.app.state.settings
    llm = _llm(request, session)
    with build_client() as client:
        try:
            result = script_service.generate(
                session=session,
                app_settings=settings,
                version_id=version_id,
                fmt=payload.format,
                llm=llm,
                client=client,
                mapped=payload.mapped,
                title=payload.title,
            )
        except AppError:
            raise
        except Exception as error:
            # A transport failure inside a long model call is the most likely
            # error here, and httpx's own message ("ReadTimeout") tells the user
            # nothing about what to do.
            raise AppError(
                "script_failed",
                f"生成失败：{error}",
                hint="稍后重试；长节目需要几分钟，超时可调低「最大输出长度」。",
                status=502,
            ) from error

    chapters = result.chapters
    from animatter.core.script import format_clock

    return GenerateOut(
        format=result.format,
        title=result.title,
        body=result.body,
        filename=_filename(result.title, result.format),
        chapters=[
            ChapterOut(
                heading=chapter.heading,
                start_ms=chapter.start_ms,
                clock=format_clock(chapter.start_ms),
            )
            for chapter in chapters
        ],
        turns=result.turns,
        blocks=result.blocks,
        warnings=result.warnings,
    )


class FileIn(BaseModel):
    body: str
    filename: str


@router.post("/script/file")
def script_file(payload: FileIn) -> Response:
    """Download what was just generated.

    Round-trips the body through the client instead of caching it on the server:
    the document is the user's, generation is repeatable, and keeping a copy
    would mean another table, another cleanup path, and another thing that can
    go stale against the transcript.
    """
    return Response(
        content=payload.body.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"script.md\"; filename*=UTF-8''{quote(payload.filename)}"
            ),
        },
    )


def _filename(title: str, fmt: str) -> str:
    labels = {"shownotes": "ShowNotes", "dialogue": "文稿-对话式", "article": "文稿-文章式"}
    safe = "".join(ch for ch in title.strip() if ch not in '\\/:*?"<>|').strip() or "未命名"
    return f"{safe}_{labels.get(fmt, fmt)}.md"
