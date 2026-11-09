"""The transcription use case: media → uploaded → transcribed → normalised → stored.

One job is one HTTP-independent unit of work (AGENTS.md §6.2). Status transitions
are persisted at every step so a crash mid-flight leaves an inspectable record
rather than a mystery.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.sentence import SentenceSpan
from animatter.core.tokens import TokenSpan
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.media import ffmpeg
from animatter.models import (
    Media,
    Project,
    Sentence,
    Token,
    TranscribeJob,
    TranscriptVersion,
)
from animatter.providers.asr.bailian import BailianAsrProvider
from animatter.providers.asr.base import AsrRequest
from animatter.providers.http import build_client
from animatter.providers.storage.temp_oss import BailianStorage
from animatter.services import normalize as normalizer
from animatter.services import projects as project_service

log = get_logger(__name__)

ProgressFn = Callable[[float, str], None]


@dataclass(frozen=True)
class TranscribeOptions:
    model: str = "qwen-audio-3.1-asr-flash-filetrans"
    language_hints: tuple[str, ...] = ("zh", "en")
    channel_id: int = 0
    diarization: bool = True
    speaker_count: int | None = 2
    vocabulary: dict[str, int] | None = None
    vocabulary_id: str | None = None
    context_text: str | None = None


def _noop(_progress: float, _message: str) -> None:
    return None


def sha256_of(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def project_dir(settings: Settings, project_id: str) -> Path:
    return project_service.project_root(settings, project_id)


def register_media(session: Session, settings: Settings, project_id: str, source: Path) -> Media:
    """Record (or reuse) a media file for a project, probing it for facts."""
    project_service.get_project(session, project_id)
    if not source.exists():
        raise AppError("media_missing", f"找不到文件 {source}", hint="确认路径正确，且文件没有被移动。")

    digest = sha256_of(source)
    # Reuse only within the same project. Matching globally would hand back
    # another project's row, leaving this project with no media of its own.
    existing = session.scalars(
        select(Media).where(Media.project_id == project_id, Media.sha256 == digest)
    ).first()
    if existing is not None:
        return existing

    info = ffmpeg.probe(settings.ffprobe_path, source)
    if info.duration_ms > settings.max_asr_seconds * 1000:
        raise AppError(
            "media_too_long",
            f"音频时长 {info.duration_ms / 3_600_000:.1f} 小时，"
            f"超过单文件上限 {settings.max_asr_seconds // 3600} 小时",
            hint="先裁剪成多期，或等待分段功能（当前按 2 小时自动分段）。",
        )

    root = project_dir(settings, project_id)
    media_dir = root / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    target = media_dir / f"original{source.suffix.lower() or '.bin'}"

    # Copy rather than reference: the project must survive the source being
    # moved or deleted, and editing always reads from this copy.
    if not target.exists():
        target.write_bytes(source.read_bytes())

    media = Media(
        id=new_id("med"),
        project_id=project_id,
        path=str(target),
        sha256=digest,
        duration_ms=info.duration_ms,
        sample_rate=info.sample_rate,
        channels=info.channels,
        codec=info.codec,
        kind="original",
        created_at=utcnow_iso(),
    )
    session.add(media)
    session.flush()
    log.info("media.registered", media_id=media.id, duration_ms=media.duration_ms)
    return media


def transcribe_media(
    session: Session,
    settings: Settings,
    *,
    project_id: str,
    media: Media,
    options: TranscribeOptions,
    progress: ProgressFn = _noop,
) -> TranscriptVersion:
    """Run the full transcription chain and persist a new transcript version."""
    if not settings.dashscope_api_key:
        raise AppError(
            "missing_api_key",
            "未配置 DASHSCOPE_API_KEY",
            hint="在项目根目录的 .env 里填入百炼 API Key（北京地域），然后重启服务。",
            status=503,
        )

    project = project_service.get_project(session, project_id)
    root = project_dir(settings, project_id)
    work_dir = root / "derived" / "asr"
    work_dir.mkdir(parents=True, exist_ok=True)

    # --- 0. build the upload artifact ---------------------------------
    progress(0.05, "转码为识别输入（16kHz 单声道）")
    asr_input = ffmpeg.transcode_for_asr(settings.ffmpeg_path, Path(media.path), work_dir / "asr_input.mp3")

    # --- 1. plan chunks -------------------------------------------------
    plan = chunk_plan_for(settings, Path(media.path), media.duration_ms)
    progress(0.10, f"任务规划：{len(plan.segments)} 段（{plan.quality}）")

    segments = plan.segments
    inputs: list[Path] = []
    for segment in segments:
        if not plan.is_chunked:
            inputs.append(asr_input)
            continue
        target = work_dir / f"seg{segment.index:03d}.mp3"
        inputs.append(
            ffmpeg.extract_segment(
                settings.ffmpeg_path, Path(media.path), target, segment.start_ms, segment.end_ms
            )
        )

    # --- 2. submit + await each segment --------------------------------
    parts: list[normalizer.NormalizedTranscript] = []
    provider = BailianAsrProvider(
        base_url=settings.dashscope_api_base,
        api_key=settings.dashscope_api_key,
        model=options.model,
    )
    storage = BailianStorage(
        base_url=settings.dashscope_api_base,
        api_key=settings.dashscope_api_key,
        model=options.model,
    )

    try:
        with build_client() as http:
            for position, (segment, audio_path) in enumerate(zip(segments, inputs, strict=True)):
                share = 0.75 / len(segments)
                base_progress = 0.15 + share * position
                label = f"第 {position + 1}/{len(segments)} 段"
                progress(base_progress, f"{label}：上传到百炼临时存储")
                uploaded = storage.upload(http, audio_path)

                job = TranscribeJob(
                    id=new_id("job"),
                    project_id=project_id,
                    media_id=media.id,
                    provider=provider.name,
                    model=options.model,
                    params_json=json.dumps(_snapshot(options), ensure_ascii=False),
                    oss_key=uploaded.key,
                    status="submitted",
                    segment_index=segment.index,
                    segment_start_ms=segment.start_ms,
                    segment_end_ms=segment.end_ms,
                    created_at=utcnow_iso(),
                )
                session.add(job)
                session.flush()

                request = AsrRequest(
                    file_url=uploaded.oss_url,
                    model=options.model,
                    language_hints=options.language_hints,
                    channel_id=(options.channel_id,),
                    diarization=options.diarization,
                    speaker_count=options.speaker_count,
                    vocabulary=options.vocabulary or {},
                    vocabulary_id=options.vocabulary_id,
                    context_text=options.context_text,
                )
                submitted = provider.submit(request)
                job.task_id = submitted.task_id
                job.status = "running"
                session.flush()

                progress(base_progress + share * 0.2, f"{label}：识别中（{submitted.task_id[:12]}…）")
                status = provider.wait(submitted.task_id)

                if status.state != "SUCCEEDED":
                    job.status = "failed"
                    job.error_code = status.error_code or status.state
                    job.error_msg = status.error_message
                    job.finished_at = utcnow_iso()
                    raise AppError(
                        "asr_task_failed",
                        f"识别失败：{status.error_code or status.state}",
                        hint=_task_hint(status.error_code, status.error_message),
                        status=502,
                    )

                progress(base_progress + share * 0.8, f"{label}：下载结果")
                result_url = _transcription_url(status)
                raw = provider.fetch_result(result_url)

                raw_path = root / "transcripts" / f"{job.id}.raw.json"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(json.dumps(raw.raw, ensure_ascii=False), encoding="utf-8")

                job.status = "succeeded"
                job.usage_json = json.dumps(status.usage, ensure_ascii=False)
                job.raw_path = str(raw_path)
                job.finished_at = utcnow_iso()

                parts.append(
                    normalizer.normalize(raw, offset_ms=segment.start_ms, channel=options.channel_id)
                )
                session.flush()
    finally:
        provider.close()

    # --- 3. merge and persist ------------------------------------------
    progress(0.93, "归一化字级时间轴")
    merged = normalizer.merge_segments(parts)
    version = _persist_version(session, project, media, merged)
    progress(1.0, f"完成：{version.sentence_count} 句 / {version.token_count} 字")
    return version


def _snapshot(options: TranscribeOptions) -> dict[str, Any]:
    """Immutable record of what was actually requested (reproducibility)."""
    return {
        "model": options.model,
        "language_hints": list(options.language_hints),
        "channel_id": options.channel_id,
        "diarization": options.diarization,
        "speaker_count": options.speaker_count,
        "vocabulary": options.vocabulary or {},
        "vocabulary_id": options.vocabulary_id,
        "context_text": options.context_text,
    }


def _task_hint(code: str, message: str) -> str:
    if code == "FILE_DOWNLOAD_FAILED":
        return "百炼下载不到音频：多为 oss:// 链接超过 48 小时或缺少解析头。重新上传即可。"
    if "limit" in code.lower() or "quota" in message.lower():
        return "可能触发限流或额度不足，稍后重试；长期需要扩容。"
    return f"百炼返回：{message or code}；可在项目 logs 目录查看原始响应。"


def _transcription_url(status: object) -> str:
    results = getattr(status, "subtask_results", None) or []
    if not results:
        raise AppError(
            "asr_no_result", "任务成功但没有结果条目", hint="查看任务详情确认 subtask_status。", status=502
        )
    item = results[0]
    if str(item.get("subtask_status")) != "SUCCEEDED":
        raise AppError(
            "asr_subtask_failed",
            f"子任务失败：{item.get('code')}",
            hint=str(item.get("message") or ""),
            status=502,
        )
    url = item.get("transcription_url")
    if not url:
        raise AppError(
            "asr_no_url",
            "结果里没有 transcription_url",
            hint="结果链接 24 小时失效，可能需要重新识别。",
            status=502,
        )
    return str(url)


def chunk_plan_for(settings: Settings, source: Path, duration_ms: int):  # type: ignore[no-untyped-def]
    from animatter.services import chunking

    return chunking.plan(
        settings.ffmpeg_path,
        source,
        duration_ms,
        threshold_ms=settings.diarization_safe_seconds * 1000,
        target_ms=settings.asr_chunk_target_seconds * 1000,
    )


def _persist_version(
    session: Session,
    project: Project,
    media: Media,
    merged: normalizer.NormalizedTranscript,
) -> TranscriptVersion:
    previous = session.scalars(
        select(TranscriptVersion)
        .where(TranscriptVersion.project_id == project.id)
        .order_by(TranscriptVersion.version.desc())
    ).first()
    next_version = (previous.version + 1) if previous else 1

    version = TranscriptVersion(
        id=new_id("ver"),
        project_id=project.id,
        media_id=media.id,
        job_id="",
        version=next_version,
        created_at=utcnow_iso(),
        duration_ms=media.duration_ms or merged.duration_ms,
        token_count=len(merged.tokens),
        sentence_count=len(merged.sentences),
        is_active=True,
    )
    session.add(version)

    # Only one active version per project: editing targets exactly one timeline.
    for other in session.scalars(
        select(TranscriptVersion).where(
            TranscriptVersion.project_id == project.id, TranscriptVersion.is_active.is_(True)
        )
    ):
        other.is_active = False

    session.add_all(
        [
            Token(
                version_id=version.id,
                idx=token.idx,
                start_ms=token.start_ms,
                end_ms=token.end_ms,
                text=token.text,
                kind=token.kind,
                src_word_idx=token.src_word_idx,
                punc_after=token.punc_after,
                derived=token.derived,
                revised=token.revised,
                hidden=token.hidden,
            )
            for token in merged.tokens
        ]
    )
    session.add_all(
        [
            Sentence(
                id=new_id("sen"),
                version_id=version.id,
                ord=span.ord,
                token_start=span.token_start,
                token_end=span.token_end,
                speaker_id=span.speaker_id,
                origin=span.origin,
                updated_at=utcnow_iso(),
            )
            for span in merged.sentences
        ]
    )
    session.flush()
    log.info(
        "transcript.persisted",
        version_id=version.id,
        tokens=version.token_count,
        sentences=version.sentence_count,
    )
    return version


def load_tokens(session: Session, version_id: str) -> list[TokenSpan]:
    rows = session.scalars(select(Token).where(Token.version_id == version_id).order_by(Token.idx)).all()
    return [
        TokenSpan(
            idx=row.idx,
            start_ms=row.start_ms,
            end_ms=row.end_ms,
            text=row.text,
            kind=row.kind,
            src_word_idx=row.src_word_idx,
            punc_after=row.punc_after,
            derived=row.derived,
            revised=row.revised,
            hidden=row.hidden,
        )
        for row in rows
    ]


def load_sentences(session: Session, version_id: str) -> list[SentenceSpan]:
    rows = session.scalars(
        select(Sentence).where(Sentence.version_id == version_id).order_by(Sentence.ord)
    ).all()
    return [
        SentenceSpan(
            ord=row.ord,
            token_start=row.token_start,
            token_end=row.token_end,
            speaker_id=row.speaker_id,
            origin=row.origin,
        )
        for row in rows
    ]
