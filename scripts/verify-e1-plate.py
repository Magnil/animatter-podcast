"""验收：E1 烘焙 plate 是否**逐位**等价于旧路径（§19.30）。

常驻脚本（和 `verify-p1-consistency.py` 同一类）：`.tmp/` 会被清空，验收依据不能只活在临时
目录里，否则下次没人能重跑。

    .\\.venv\\Scripts\\python.exe scripts\\verify-e1-plate.py            # 单帧（约 2 s）
    .\\.venv\\Scripts\\python.exe scripts\\verify-e1-plate.py --chunk 3  # 一个真 chunk（约 1 min）

判据是 **PSNR `inf`**，不是「≥45 dB」：烘焙把三层全画布的 blend 搬到了离线，只要有一处顺序或
重采样不同，PSNR 就会掉到 40 出头（文字被头像压住时实测 41.74 dB）。**`inf` 以外都算失败。**

`--chunk N` 用生产路径（`_render_picture` 同款调用）把那一个 chunk 编两遍：一遍旧图（`layers`
+ `text`），一遍烘焙图（`layers=[]`、`text=None`、补丁头像）。x264 是确定性的，所以两条路径
喂同样的帧就会产出同样的字节——脚本额外报帧数，因为 **`-map [out]` 漏掉时 ffmpeg 会静默丢弃
`psnr` 的输出、一行不打印**，那时「没有坏帧」和「没有帧」长得一模一样。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

PROJECT = "prj_1m6v2m8anfpyqq3w8qt"
VERSION = "ver_1m6v2q5javqm1j82xsg"


def _project_path() -> None:
    """Make ``animatter`` importable **after** ``sys.path`` is set up.

    The imports are local to the functions on purpose: ``ruff`` flags module-level imports that
    follow a ``sys.path`` mutation, and ``verify-p1-consistency.py`` does the same thing.
    """
    src = str(ROOT / "apps" / "api" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def psnr(left: Path, right: Path) -> dict[str, object]:
    """逐帧 PSNR。``stats_file=-`` 写到 **stdout**，汇总行在 stderr，两边都要收。"""
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "info",
            "-i", str(left), "-i", str(right),
            "-filter_complex", "[0:v][1:v]psnr=stats_file=-[out]",
            "-map", "[out]", "-f", "null", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    text = result.stdout + "\n" + result.stderr
    values: list[float] = []
    for line in text.splitlines():
        if not line.startswith("n:"):
            continue
        fields = dict(part.split(":", 1) for part in line.split() if ":" in part)
        raw = fields.get("psnr_avg")
        values.append(float("inf") if raw in (None, "inf") else float(raw))
    finite = [value for value in values if value != float("inf")]
    return {
        "frames": len(values),
        "all_inf": bool(values) and not finite,
        "finite_frames": len(finite),
        "mean_finite": round(statistics.mean(finite), 3) if finite else None,
        "min_finite": min(finite) if finite else None,
        "summary": next((t.strip() for t in text.splitlines() if "Parsed_psnr" in t), ""),
    }


def _patched_avatar(pair: tuple[Path, Path], *, lit: bool) -> Path:
    return pair[0] if lit else pair[1]


def check_still(settings: Any, out: Path) -> int:
    """单帧：旧图 vs 烘焙图。这是最便宜、也最能定位问题的一档。"""
    from animatter.db import Database
    from animatter.media import video as video_media
    from animatter.services import video_render as vr

    with Database(settings).session() as session:
        template = vr.template_service.require(session, PROJECT)
        version = vr.clipping_service.get_version(session, VERSION)
        edl = vr.clipping_service.build_edl(session, settings, version)
        tracks = vr.avatar_tracks(session, template, version, edl)
        plates = vr.video_avatars.name_plates(session, template)
    canvas_w = template.canvas_w or 1920
    canvas_h = template.canvas_h or 1080
    text_png = vr.text_png_for(
        template, work_dir=out, width=canvas_w, height=canvas_h,
        visible_avatars={track.name for track in tracks} or None, avatar_names=plates,
    )
    if text_png is None or not tracks:
        print("这个项目没有文字或没有绑定的头像，单帧这一档不适用")
        return 0
    base = vr._base_background(template, tracks)
    plate = vr._plate_for(
        settings, template, text_png=text_png, tracks=tracks, work_dir=out,
        canvas_w=canvas_w, canvas_h=canvas_h,
        box_rect=vr.template_service.box_rect_of(template),
    )
    if plate is None:
        print("烘焙被关掉了（video_plate_bake=False），没有可比的图")
        return 1
    print(
        f"plate {plate.path.name}: text_baked={plate.text_baked} "
        f"patched={plate.patched_pixels}px skipped={plate.skipped or '-'} "
        f"slots={sorted(plate.avatar_sources)}"
    )

    at_ms = 60_000
    old = out / "old.png"
    video_media.render_still(
        settings.ffmpeg_path, target=old, at_ms=at_ms, width=canvas_w, height=canvas_h,
        background=base, layers=vr._layers(template),
        avatars=vr._avatar_stills(tracks, at_ms=at_ms),
        text=video_media.Layer(path=text_png, x=0, y=0),
    )
    new = out / "plate.png"
    swapped: list[video_media.AvatarLayer] = []
    for track, layer in zip(tracks, vr._avatar_stills(tracks, at_ms=at_ms), strict=True):
        pair = plate.avatar_sources.get(track.name)
        if pair is None:
            swapped.append(layer)
            continue
        lit = Path(layer.path) == Path(track.lit_path)
        swapped.append(
            video_media.AvatarLayer(
                path=_patched_avatar(pair, lit=lit), x=layer.x, y=layer.y,
                width=layer.width, height=layer.height, is_video=False,
            )
        )
    video_media.render_still(
        settings.ffmpeg_path, target=new, at_ms=at_ms, width=canvas_w, height=canvas_h,
        background=plate.path, layers=[], avatars=swapped,
        text=None if plate.text is None else video_media.Layer(path=plate.text, x=0, y=0),
    )
    result = psnr(old, new)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    ok = result["all_inf"] and result["frames"] == 1
    print("单帧验收：", "PASS（PSNR inf）" if ok else "FAIL")
    return 0 if ok else 1


def check_chunk(settings: Any, out: Path, index: int) -> int:
    """一个真 chunk：两条路径都真编码，比字节。"""
    from animatter.db import Database
    from animatter.media import video as video_media
    from animatter.media import video_chunks
    from animatter.services import video_render as vr

    with Database(settings).session() as session:
        prepared = vr.prepare(session, settings, PROJECT, VERSION)
        plan = vr._box(
            session,
            settings=settings,
            template=prepared.template,
            box_rect=prepared.box_rect,
            items=prepared.items,
            edl=prepared.edl,
        )
    canvas_w = prepared.template.canvas_w or 1920
    canvas_h = prepared.template.canvas_h or 1080
    lower = list(plan.segments if plan.split is None else plan.split.lower)
    sources = list(plan.sources if plan.split is None else plan.split.lower_sources)
    chunks = video_chunks.split(
        lower,
        max_segments=settings.video_chunk_segments,
        max_ms=settings.video_chunk_seconds * 1000,
    )
    if not 0 <= index < len(chunks):
        print(f"chunk 序号超出范围：0–{len(chunks) - 1}")
        return 2
    chunk = chunks[index]
    print(
        f"chunk {index}/{len(chunks)}: {chunk.duration_ms / 1000:.1f}s, "
        f"{chunk.frames(video_media.FPS)} 帧, 段 {chunk.first}..{chunk.last}"
    )
    text_png = vr.text_png_for(
        prepared.template, work_dir=out, width=canvas_w, height=canvas_h,
        visible_avatars={track.name for track in prepared.avatars} or None,
        avatar_names=prepared.avatar_names,
    )
    layers = vr._layers(prepared.template)
    stills = (
        vr._composite_stills(
            settings, plan, lower[chunk.first : chunk.last], sources,
            work_dir=out, tag=f"chunk-{index:04d}",
        )
        if settings.video_still_composite
        else {}
    )
    chain, parts, inputs = vr._chain_for(
        plan, lower[chunk.first : chunk.last], sources,
        out_label="boxout", start_ms=chunk.start_ms, stills=stills,
    )
    plate = vr._plate_for(
        settings, prepared.template, text_png=text_png, tracks=prepared.avatars,
        work_dir=out, canvas_w=canvas_w, canvas_h=canvas_h, box_rect=prepared.box_rect,
    )
    if plate is None:
        print("烘焙被关掉了（video_plate_bake=False），没有可比的图")
        return 1
    print(
        f"plate {plate.path.name}: text_baked={plate.text_baked} "
        f"patched={plate.patched_pixels}px skipped={plate.skipped or '-'}"
    )

    timings: dict[str, float] = {}
    targets: dict[str, Path] = {}
    for label in ("old", "plate"):
        baked = plate if label == "plate" else None
        avatars = vr._avatar_layers(
            settings, prepared.avatars, duration_ms=prepared.duration_ms, work_dir=out,
            start_ms=chunk.start_ms, end_ms=chunk.end_ms, tag=f"-{index:04d}-{label}",
            sources=plate.avatar_sources if baked is not None else None,
        )
        target = out / f"chunk-{index:04d}-{label}.mp4"
        started = time.monotonic()
        video_media.render(
            settings.ffmpeg_path,
            target=target,
            duration_ms=chunk.duration_ms,
            width=canvas_w,
            height=canvas_h,
            background=plate.path if baked is not None else prepared.background_path,
            layers=[] if baked is not None else layers,
            box=chain,
            box_segments=parts,
            box_sources=inputs,
            avatars=avatars,
            text=None
            if (baked is not None and baked.text is None) or text_png is None
            else video_media.Layer(path=text_png, x=0, y=0),
            subtitles=None,
            start_ms=chunk.start_ms,
            frames=chunk.frames(video_media.FPS),
            filter_threads=settings.video_filter_threads,
        )
        timings[label] = round(time.monotonic() - started, 2)
        targets[label] = target
        print(f"  {label}: {timings[label]:.2f} s")

    result = psnr(targets["old"], targets["plate"])
    saved = (1 - timings["plate"] / timings["old"]) * 100
    print(json.dumps({**result, "saved_pct": round(saved, 2)}, ensure_ascii=False, indent=1))
    expected = chunk.frames(video_media.FPS)
    ok = result["all_inf"] and result["frames"] == expected
    print(
        f"chunk 验收：{'PASS' if ok else 'FAIL'}（期望 {expected} 帧全 inf）"
        f"，耗时 {timings['old']:.1f} → {timings['plate']:.1f} s（省 {saved:.1f} %）"
    )
    return 0 if ok else 1


def main() -> int:
    from animatter.config import Settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", type=int, default=None, help="跑一个真 chunk（更慢、更接近验收）")
    parser.add_argument("--out", default=".tmp/verify-e1", help="产物目录")
    args = parser.parse_args()

    _project_path()
    settings = Settings()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.chunk is None:
        return check_still(settings, out)
    return check_chunk(settings, out, args.chunk)


if __name__ == "__main__":
    sys.exit(main())
