"""Burned-in subtitles for the video pipeline (§19.5).

Why not just reuse the SRT exporter
-----------------------------------
The subtitle *exporter* answers "give me a file a player can show". The video
pipeline asks something stricter: the same words, at the same instants, with a
look that must not change between the preview frame and the exported film. Three
things are needed for that and none of them come from SRT:

* ASS with an explicit style (font, size, weight, outline, position);
* ``PlayResX/PlayResY`` **always set** — libass assumes 384×288 otherwise and a
  1080p render comes out with 5.6× oversized text and no warning;
* a font check before rendering, because libass substitutes silently when the
  family is missing. There is no log line, no error, just a film that looks wrong.

Timing
------
Cues are produced in **output** time: they are mapped through the project's
TimeMap, exactly like the exported subtitles, so a cue cannot drift away from the
speech when audio is cut. Entries whose span is entirely deleted vanish; entries
whose head was cut are clamped to the surviving part rather than dropped, because
the words were not deleted even if the silence before them was.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from animatter.errors import AppError
from animatter.log import get_logger

log = get_logger(__name__)

#: Fonts known to ship with Chinese Windows. The first one that exists is used;
#: the list is ordered by how well it reads at 1080p under a busy poster.
_CJK_FONT_CANDIDATES: Final[tuple[str, ...]] = (
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "SimHei",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
)

#: Family name → the file names Windows ships it under. Substring matching alone is
#: not enough: 「Microsoft YaHei」 is ``msyh.ttc``, and ``"microsoftyahei" in "msyh"`` is
#: false — so the guard rejected a font that *was* installed and told the user to
#: install it, which is worse than saying nothing. This table exists because the
#: mapping is not derivable; the fallback below still handles anything not listed.
_FONT_FILES: Final[dict[str, tuple[str, ...]]] = {
    "microsoftyahei": ("msyh", "msyhbd"),
    "microsoftyaheiui": ("msyh", "msyhbd"),
    "simhei": ("simhei",),
    "simsun": ("simsun", "simsunb"),
    "simkai": ("simkai",),
    "fangsong": ("simfang",),
    "pingfangsc": ("pingfang",),
    "notosanscjksc": ("notosanscjksc", "notosanscjk"),
    "sourcehansanssc": ("sourcehansanssc", "sourcehansans"),
    "microsoftjhenghei": ("msjh", "msjhbd"),
    "microsoftyaheiui-light": ("msyhl",),
}

#: Where Windows keeps its fonts. Checked rather than assumed so the error message
#: can say what was looked for.
_FONT_DIRS: Final[tuple[Path, ...]] = (
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts"),
    Path("/System/Library/Fonts"),
)


@dataclass(frozen=True)
class SubtitleStyle:
    """Everything the user can change in the 「字幕」 tab."""

    font_name: str = "Microsoft YaHei"
    font_size: int = 52
    #: ``&H00BBGGRR`` is ASS's byte order — blue and red are swapped versus HTML.
    primary_colour: str = "&H00FFFFFF"
    outline_colour: str = "&H00000000"
    outline: float = 2.4
    shadow: float = 1.0
    #: Distance from the bottom of the frame, in the 1920×1080 design space.
    margin_v: int = 56
    margin_l: int = 120
    margin_r: int = 120
    bold: bool = True

    def as_ass_style(self, *, play_w: int, play_h: int) -> str:
        # ``play_w``/``play_h`` are accepted for symmetry with the header writer and
        # because libass needs them there, but the style itself carries no resolution
        # — it is expressed in PlayRes units by definition.
        del play_w, play_h
        bold = -1 if self.bold else 0
        return (
            f"Style: Default,{self.font_name},{self.font_size},"
            f"{self.primary_colour},{self.primary_colour},{self.outline_colour},"
            f"&H80000000,{bold},0,0,0,100,100,0,0,1,{self.outline},{self.shadow},2,"
            f"{self.margin_l},{self.margin_r},{self.margin_v},1"
        )


@dataclass(frozen=True)
class AssCue:
    start_ms: int
    end_ms: int
    text: str

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def ass_timestamp(ms: int) -> str:
    """``H:MM:SS.cc`` — ASS uses centiseconds and a single-digit hour."""
    clamped = max(0, ms)
    hours, rest = divmod(clamped, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{millis // 10:02d}"


def escape_text(text: str) -> str:
    """Make user text safe for an ASS ``Dialogue`` line.

    Braces start an override block and a backslash starts an escape, so an
    unescaped ``{`` in someone's transcript silently swallows the rest of the line
    — the subtitle just stops mid-sentence with no error anywhere.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r\n", "\\N")
        .replace("\n", "\\N")
        .replace("\r", "\\N")
    )


def build_ass(
    cues: list[AssCue],
    *,
    style: SubtitleStyle,
    play_w: int = 1920,
    play_h: int = 1080,
    title: str = "Animatter",
) -> str:
    """Serialise cues into a complete ASS file.

    ``PlayResX``/``PlayResY`` are not optional: without them libass falls back to
    384×288 and scales the style up by ~5.6× at 1080p, which looks like the font
    size setting is broken.
    """
    header = [
        "[Script Info]",
        f"Title: {title}",
        "ScriptType: v4.00+",
        f"PlayResX: {play_w}",
        f"PlayResY: {play_h}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        style.as_ass_style(play_w=play_w, play_h=play_h),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines = list(header)
    for cue in cues:
        if cue.end_ms <= cue.start_ms:
            continue
        lines.append(
            f"Dialogue: 0,{ass_timestamp(cue.start_ms)},{ass_timestamp(cue.end_ms)},"
            f"Default,,0,0,0,,{escape_text(cue.text)}"
        )
    return "\n".join(lines) + "\n"


def write_ass(path: Path, cues: list[AssCue], *, style: SubtitleStyle, title: str = "Animatter") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 **with BOM**: libass on Windows otherwise guesses the codepage and
    # renders Chinese as boxes.
    path.write_text(
        build_ass(cues, style=style, title=title), encoding="utf-8-sig", newline="\n"
    )
    return path


def find_font(name: str) -> Path | None:
    """Locate a font file by family name, or ``None``.

    Two passes, because neither alone is right:

    1. the known family → file-name table (「Microsoft YaHei」 is ``msyh.ttc``, and no
       amount of substring matching derives that);
    2. a substring match, for any font not in the table.

    Its only job is to answer "will libass find something for this family, or will it
    silently substitute?" — it is not a font database.
    """
    key = name.lower().replace(" ", "").replace("-", "")
    stems = _FONT_FILES.get(key, (key,))
    files = [
        candidate
        for directory in _FONT_DIRS
        if directory.is_dir()
        for candidate in directory.iterdir()
        if candidate.suffix.lower() in {".ttf", ".ttc", ".otf"}
    ]
    for stem in stems:
        for candidate in files:
            if stem in candidate.stem.lower().replace(" ", ""):
                return candidate
    return None


def first_available_font() -> str | None:
    for family in _CJK_FONT_CANDIDATES:
        if find_font(family) is not None:
            return family
    return None


def ensure_font(style: SubtitleStyle) -> None:
    """Fail loudly *before* an encode when the family is not installed.

    libass substitutes a missing family with no warning at all, so the only symptom
    is a finished film that looks wrong. A three-minute encode is far too expensive
    to discover that at the end.
    """
    if find_font(style.font_name) is not None:
        return
    fallback = first_available_font()
    searched = ", ".join(str(directory) for directory in _FONT_DIRS if directory.is_dir())
    if fallback is None:
        raise AppError(
            "subtitle_font_missing",
            f"找不到字体「{style.font_name}」，也没有任何可用的中文字体",
            hint=f"在 {searched} 里装一个中文字体（微软雅黑或思源黑体），再重新导出。",
            status=503,
        )
    raise AppError(
        "subtitle_font_missing",
        f"找不到字体「{style.font_name}」",
        hint=f"这台机器上有「{fallback}」，把字幕字体改成它，或者先安装「{style.font_name}」。",
    )
