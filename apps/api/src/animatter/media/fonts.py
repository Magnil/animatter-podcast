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
Because the two fonts the design calls for (阿里妈妈数黑体, Baigo) were, on this machine, only
inside 字由's store: the registry reported 175 families and neither was among them. Treating
"not in C:/Windows/Fonts" as "missing" would have made the feature unusable on the machine it was
specified for.

**Both locations are scanned.** Since 2026-11-12 the same two files also exist in the per-user
Windows font directory (byte-identical, sha256 verified), and :func:`discover` de-duplicates by
``(family, style)`` with the earlier directory winning — so :func:`resolve` now returns the
user-directory copy. That is fine and deliberate: the point of scanning several directories is
that it does not matter *where* the font came from, only that it can be found. It does mean the
honest claim is "the registry does not know about these fonts, but the filesystem can find them",
not "they exist nowhere else".

Caching
-------
``discover()`` walks ~200 font files and opens each one to read its ``name`` table — measured
at ~1.4 s. The result is cached by a cheap fingerprint (directory mtimes + file counts) so a
page load does not pay for it twice, while a newly downloaded font still shows up as soon as
字由 finishes writing it.
"""

from __future__ import annotations

import time
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

#: A directory this app owns, holding fonts that arrived inside a ``.template`` package.
#: Not a constant path because only the caller knows ``settings.data_dir``; :func:`set_private_dir`
#: is called once at startup and again whenever a package installs a font. A template that
#: needs 「Baigo」 has to work on a machine that never installed it, and the only alternative —
#: threading a per-template font path through ``_fit``/``_face``/libass — is a much larger change.
_private_dir: Path | None = None

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

#: A common character and a private-use code point used only to tell "this face has Chinese"
#: from "this face draws .notdef". See :func:`draws_chinese`.
_CJK_SAMPLE: Final[str] = "中"
_NOTDEF_SAMPLE: Final[str] = "\ue000"


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


def set_private_dir(directory: Path | None) -> None:
    """Tell discovery about the app's own font directory (``data/fonts``).

    Called from the app's lifespan and again after a package installs a font. Setting the
    same directory twice is cheap; the walk is cached anyway. Passing ``None`` forgets it,
    which is what the tests want between cases.
    """
    global _private_dir
    resolved = Path(directory) if directory is not None else None
    if resolved == _private_dir:
        return
    _private_dir = resolved
    forget()


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    extra = (_private_dir,) if _private_dir is not None else ()
    for directory in (*SYSTEM_DIRS, *USER_DIRS, *extra):
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
#: Same key as ``_cache``; invalidated together with it.
_cjk_cache: tuple[tuple[int, int], list[str]] | None = None
#: ``(family, bold) -> face or None``. Cleared whenever ``discover`` rebuilds.
_resolve_cache: dict[tuple[str, bool | None], FontFace | None] = {}
#: When the last directory walk happened, on the monotonic clock.
_checked_at: float = 0.0

#: How long a directory walk is trusted. ``discover`` stats 222 files (~35–47 ms measured), and
#: drawing one slide asks for ~29 font sizes — without this the shrink loop re-walked the font
#: directories 29 times and one 320×180 page cost **1.4 s**. Two seconds is long enough that a
#: single render (or a whole PATCH→purge→redraw cycle) pays once, and short enough that a font
#: downloaded in 字由 shows up while the user is still looking at the picker. A test can call
#: :func:`forget` to drop the caches outright.
_FINGERPRINT_TTL: Final[float] = 2.0


def forget() -> None:
    """Drop every font cache, so the next call re-walks the filesystem.

    Only useful when a test installs or removes a font mid-process; the normal path relies on
    the TTL plus the fingerprint.
    """
    global _cache, _cjk_cache, _checked_at
    _cache = None
    _cjk_cache = None
    _resolve_cache.clear()
    _checked_at = 0.0


def discover() -> list[FontFace]:
    """Every resolvable font face, de-duplicated by (family, style).

    Re-walks the font directories at most once per :data:`_FINGERPRINT_TTL`, then trusts the
    fingerprint (file count + newest mtime) to notice an install.
    """
    global _cache, _checked_at
    if _cache is not None and (time.monotonic() - _checked_at) < _FINGERPRINT_TTL:
        return _cache[1]
    files = _candidate_files()
    key = _fingerprint(files)
    _checked_at = time.monotonic()
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
    _resolve_cache.clear()
    log.info("fonts.discovered", files=len(files), faces=len(faces))
    return faces


def families() -> list[str]:
    """Distinct family names, for the picker."""
    return sorted({face.family for face in discover()}, key=str.lower)


def draws_chinese(family: str) -> bool:
    """True when ``family`` actually has Chinese glyphs.

    Being installed is not the same as being able to draw 中文: 68 of the 105 families on this
    machine are Latin-only or icon faces. A slide set in one of those renders a page of blank
    boxes, and the user blames the app rather than the font they picked — so a picker for
    Chinese text has to ask this, not just ``resolve()``.

    Two signatures, because neither is universal: the glyph's box differs from U+E000's (the
    private-use .notdef box) for most faces, and 中's advance width differs for the rest.
    ~0.04 s for the whole machine, cached beside :func:`discover`.
    """
    return family in chinese_families()


def chinese_families() -> list[str]:
    """Families that can draw Chinese — what a font picker for 中文 should offer."""
    global _cjk_cache
    faces = discover()
    # The key comes from ``discover()``'s own fingerprint, so installing a font invalidates
    # this list too. Reading ``_cache`` directly is deliberate: recomputing the fingerprint
    # means another full directory scan on every picker request.
    key = _cache[0] if _cache is not None else (0, 0)
    if _cjk_cache is not None and _cjk_cache[0] == key:
        return _cjk_cache[1]

    names: list[str] = []
    for face in faces:
        if not _readable(face.family):
            # One 字由 download reports its family as 「?????-??」 — PIL could not decode the
            # name table. It draws fine, but nobody can select a string they cannot read, and
            # it would sit at the top of an alphabetically sorted picker.
            continue
        try:
            font = ImageFont.truetype(str(face.path), 24, index=0)
            if (
                font.getbbox(_CJK_SAMPLE) != font.getbbox(_NOTDEF_SAMPLE)
                or font.getlength(_CJK_SAMPLE) != font.getlength(_NOTDEF_SAMPLE)
            ):
                names.append(face.family)
        except Exception:  # pragma: no cover - a font file Pillow cannot open
            continue
    unique = sorted(set(names), key=str.lower)
    _cjk_cache = (key, unique)
    log.info("fonts.chinese", families=len(unique), total=len(families()))
    return unique


def _readable(family: str) -> bool:
    """False for a family name that is mostly undecoded bytes (``?????-??``)."""
    stripped = family.strip()
    if not stripped:
        return False
    return stripped.count("?") * 2 < len(stripped)


def resolve(family: str, *, bold: bool | None = None) -> FontFace | None:
    """The best face for ``family``, or ``None``.

    Matching is case- and space-insensitive and falls back to a substring match: the user
    typed (or the PSD reported) 「Alimama ShuHeiTi」 while the file says
    「Alimama ShuHeiTi Bold」, and a strict equality check would report a font as missing
    while it is sitting right there.

    Memoised: drawing one slide asks for the same family at ~29 sizes, and every one of those
    used to redo the match over 220 faces. The cache is cleared whenever :func:`discover`
    rebuilds.
    """
    key = (family.strip().lower(), bold)
    if key in _resolve_cache:
        return _resolve_cache[key]
    _resolve_cache[key] = _resolve_uncached(key[0], bold)
    return _resolve_cache[key]


def _resolve_uncached(wanted: str, bold: bool | None) -> FontFace | None:
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
