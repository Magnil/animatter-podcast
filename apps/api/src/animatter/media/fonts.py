"""Font discovery for rendered text: installed families **and** 字由's download store.

Why this module exists
----------------------
Text the user can edit (期号, 当期标题, 头像名) is drawn by us, so we need the actual
font file. libass finds fonts by family name through the OS, but Pillow needs a path — and
the two must agree, or the preview and the export disagree about what the film looks like.

The awkward part is 字由 (HelloFont). Fonts activated through it are **not** installed into
``C:/Windows/Fonts``; the agent app keeps them in a per-user store under
``%APPDATA%/hellofont/f/zy/<guid>/`` with opaque names like ``0-10921-20220913094457000``.
There is no registry entry and no extension — but the bytes are real TTF/OTF files, so the
family name can be read out of the file itself. That is what :func:`discover` does.

Why not just require the user to install the font
-------------------------------------------------
Because on this machine the two fonts the design calls for (阿里妈妈数黑体, Baigo) only exist
inside 字由's store: the registry reports 175 families, neither of them among them. Treating
"not in C:/Windows/Fonts" as "missing" would make the feature unusable on the machine it was
specified for.

Caching
-------
``discover()`` walks ~200 font files and opens each one to read its ``name`` table — measured
at ~1.4 s. The result is cached by a cheap fingerprint (directory mtimes + file counts) so a
page load does not pay for it twice, while a newly downloaded font still shows up as soon as
字由 finishes writing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import ImageFont

from animatter.log import get_logger

log = get_logger(__name__)

#: Where the OS keeps fonts. Checked, not assumed, so the error message can list what is
#: actually there.
SYSTEM_DIRS: Final[tuple[Path, ...]] = (
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts"),
    Path("/System/Library/Fonts"),
)

#: Per-user Windows fonts, plus 字由's own store. The 字由 path is the only one that is
#: nested per account; font family is what we key on either way.
USER_DIRS: Final[tuple[Path, ...]] = (
    Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
)

#: ``%APPDATA%/hellofont/f/<channel>/<user guid>/<font id>-<timestamp>``. The channel is
#: ``zy`` for the personal product; the guid is per user and deliberately not hard-coded.
ZIYOU_ROOT: Final[Path] = Path.home() / "AppData/Roaming/hellofont/f"

#: Extensions an installed font can have. 字由's store has **no** extensions at all, which
#: is why discovery also sniffs the first bytes instead of trusting the suffix.
_FONT_SUFFIXES: Final[frozenset[str]] = frozenset({".ttf", ".ttc", ".otf"})

#: SFNT magic numbers. ``true``/``typ1`` are old Mac variants of the same container.
_SFNT_MAGIC: Final[tuple[bytes, ...]] = (b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"true", b"typ1")

#: Families the design calls for. Kept as constants so a typo is a name error, not a film
#: that silently renders in the wrong face.
TITLE_FONT: Final[str] = "Alimama ShuHeiTi"
EPISODE_FONT: Final[str] = "Baigo"
NAME_FONT: Final[str] = "Alimama ShuHeiTi"


@dataclass(frozen=True)
class FontFace:
    """One font file that resolves to one family.

    ``path`` is what Pillow opens; ``family`` is what the UI shows and what libass would
    look for. When they disagree (字由's store), the file is still usable — that is the
    whole point of carrying the path.
    """

    family: str
    style: str
    path: Path

    @property
    def is_bold(self) -> bool:
        return any(word in self.style.lower() for word in ("bold", "heavy", "black", "semibold"))


def _is_font_file(path: Path) -> bool:
    """True for anything that could be a font.

    Suffix first for normal installs; then a magic-byte sniff, because 字由's downloads have
    no extension and skipping them would make the store look empty.
    """
    if path.suffix.lower() in _FONT_SUFFIXES:
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(4) in _SFNT_MAGIC
    except OSError:
        return False


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for directory in (*SYSTEM_DIRS, *USER_DIRS):
        if not directory.is_dir():
            continue
        try:
            files.extend(item for item in directory.iterdir() if item.is_file() and _is_font_file(item))
        except OSError:
            continue
    if ZIYOU_ROOT.is_dir():
        # Two levels: channel, then user guid. ``glob`` rather than a fixed path so a
        # different account (or a future channel) is picked up without a code change.
        for path in ZIYOU_ROOT.glob("*/*/*"):
            if path.is_file() and _is_font_file(path):
                files.append(path)
    return files


def _fingerprint(files: list[Path]) -> tuple[int, int]:
    """Cheap change detector for the cache: file count and newest mtime.

    Not a hash of contents — reading 300 MB of fonts to decide whether to read 300 MB of
    fonts is the wrong shape. Count + newest mtime catches "a font was downloaded" and
    "a font was installed", which are the only two events that matter.
    """
    newest = 0
    for path in files:
        try:
            newest = max(newest, int(path.stat().st_mtime))
        except OSError:
            continue
    return len(files), newest


_cache: tuple[tuple[int, int], list[FontFace]] | None = None


def discover() -> list[FontFace]:
    """Every resolvable font face, de-duplicated by (family, style)."""
    global _cache
    files = _candidate_files()
    key = _fingerprint(files)
    if _cache is not None and _cache[0] == key:
        return _cache[1]

    found: dict[tuple[str, str], FontFace] = {}
    for path in sorted(files):
        try:
            font = ImageFont.truetype(str(path), 12, index=0)
            family, style = font.getname()
        except Exception:
            continue
        family = (family or "").strip()
        if not family:
            continue
        style = (style or "Regular").strip()
        found.setdefault((family, style), FontFace(family=family, style=style, path=path))
    faces = sorted(found.values(), key=lambda item: (item.family.lower(), item.style))
    _cache = (key, faces)
    log.info("fonts.discovered", files=len(files), faces=len(faces))
    return faces


def families() -> list[str]:
    """Distinct family names, for the picker."""
    return sorted({face.family for face in discover()}, key=str.lower)


def resolve(family: str, *, bold: bool | None = None) -> FontFace | None:
    """The best face for ``family``, or ``None``.

    Matching is case- and space-insensitive and falls back to a substring match: the user
    typed (or the PSD reported) 「Alimama ShuHeiTi」 while the file says
    「Alimama ShuHeiTi Bold」, and a strict equality check would report a font as missing
    while it is sitting right there.
    """
    wanted = family.strip().lower()
    if not wanted:
        return None
    faces = discover()
    exact = [face for face in faces if face.family.lower() == wanted]
    if not exact:
        compact = wanted.replace(" ", "")
        exact = [
            face
            for face in faces
            if face.family.lower().replace(" ", "") == compact
            or compact in face.family.lower().replace(" ", "")
        ]
    if not exact:
        return None
    if bold is True:
        bold_faces = [face for face in exact if face.is_bold]
        if bold_faces:
            return bold_faces[0]
    if bold is False:
        plain = [face for face in exact if not face.is_bold]
        if plain:
            return plain[0]
    return exact[0]


def require(family: str, *, bold: bool | None = None) -> FontFace:
    """Like :func:`resolve`, but raises with the families that *are* available.

    libass substitutes a missing family silently, so any path that draws text has to fail
    before an encode rather than produce a film with the wrong face in it.
    """
    from animatter.errors import AppError

    face = resolve(family, bold=bold)
    if face is not None:
        return face
    installed = "、".join(families()[:10]) or "（一个都没找到）"
    raise AppError(
        "font_missing",
        f"找不到字体「{family}」",
        hint=(
            f"装一个这个名字的字体，或者在界面上换一个。这台机器上已有：{installed}…"
            "（字由里已下载的字体也会被找到，不需要再安装一遍）"
        ),
        status=503,
    )
