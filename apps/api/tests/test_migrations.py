"""Migration tests.

The word-level migration rewrites the whole token table, so a bug here is silent:
the editor keeps working, but every sentence boundary and every saved deletion now
points at the wrong audio. These tests pin the failure modes we actually hit:

* characters that share one ASR word must collapse into one word token;
* re-running the migration must be a no-op — the rebuild is additive, so a second
  pass used to append a duplicate copy of the entire transcript.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from itertools import pairwise
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config

#: ``apps/api`` — the directory holding ``alembic.ini``.
API_ROOT = Path(__file__).resolve().parents[1]

CHAR_REVISION = "8b3eb15ba250"
WORD_REVISION = "3c1a7d90e5b2"


def _config(db_path: Path) -> Config:
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path.as_posix()}")
    return cfg


#: Char tokens shaped like the real sample: the ASR numbers words from 0 inside
#: each sentence, so ``src_word_idx`` repeats and the merge key has to be the
#: contiguous run *within* a sentence, never the raw index.
CHARS = [
    # sentence 0: words 0 ("你好"), 1 ("第一次"), 2 ("用"), 3 ("这")
    (0, 0, 500, "你", 0, ""),
    (1, 500, 760, "好", 0, ""),
    (2, 760, 1280, "第", 1, ""),
    (3, 1280, 1600, "一", 1, ""),
    (4, 1600, 1960, "次", 1, ""),
    (5, 1960, 2120, "用", 2, ""),
    (6, 2120, 2400, "这", 3, "。"),
    # sentence 1 restarts numbering at 0
    (7, 3709, 4100, "个", 0, ""),
    (8, 4100, 4360, "人", 0, ""),
    (9, 4360, 5160, "真", 1, ""),
    (10, 5160, 5560, "好", 2, "？"),
]

SENTENCES = [(0, 0, 7), (1, 7, 11)]


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _seed(db_path: Path) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT INTO project (id, name, created_at, updated_at, schema_version) "
            "VALUES ('prj_t', '迁移测试', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1)"
        )
        con.execute(
            "INSERT INTO transcript_version (id, project_id, media_id, job_id, version, "
            "created_at, duration_ms, token_count, sentence_count, is_active) "
            "VALUES ('ver_t', 'prj_t', 'med_t', '', 1, '2026-01-01T00:00:00Z', 5560, 11, 2, 1)"
        )
        con.executemany(
            "INSERT INTO token (version_id, idx, start_ms, end_ms, text, kind, "
            "src_word_idx, punc_after, derived) VALUES ('ver_t', ?, ?, ?, ?, 'char', ?, ?, 1)",
            [(i, s, e, t, w, p) for i, s, e, t, w, p in CHARS],
        )
        con.executemany(
            "INSERT INTO sentence (id, version_id, ord, token_start, token_end, "
            "speaker_id, origin, updated_at) VALUES (?, 'ver_t', ?, ?, ?, NULL, 'asr', "
            "'2026-01-01T00:00:00Z')",
            [(f"sen_{o}", o, a, b) for o, a, b in SENTENCES],
        )
        con.commit()
    finally:
        con.close()


def _read(db_path: Path) -> tuple[list[tuple[int, int, int, str]], list[tuple[int, int, int]]]:
    con = _connect(db_path)
    try:
        tokens = [
            (int(r["idx"]), int(r["start_ms"]), int(r["end_ms"]), str(r["text"]))
            for r in con.execute("SELECT * FROM token ORDER BY idx")
        ]
        sentences = [
            (int(r["ord"]), int(r["token_start"]), int(r["token_end"]))
            for r in con.execute("SELECT * FROM sentence ORDER BY ord")
        ]
        return tokens, sentences
    finally:
        con.close()


@pytest.fixture()
def char_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "migration.db"
    command.upgrade(_config(db_path), CHAR_REVISION)
    _seed(db_path)
    return db_path


def test_merges_characters_that_share_one_asr_word(char_db: Path) -> None:
    command.upgrade(_config(char_db), "head")

    tokens, sentences = _read(char_db)
    # Punctuation lives in ``punc_after`` and is deliberately *not* folded into
    # ``text``: it belongs to the word, and a later text edit must not be able to
    # swallow or duplicate it.
    assert [text for _i, _s, _e, text in tokens] == [
        "你好",
        "第一次",
        "用",
        "这",
        "个人",
        "真",
        "好",
    ]
    # 11 characters over 7 words: the two sentences shrink from 7/4 to 4/3.
    assert sentences == [(0, 0, 4), (1, 4, 7)]


def test_merged_word_span_covers_first_start_to_last_end(char_db: Path) -> None:
    command.upgrade(_config(char_db), "head")

    tokens, _sentences = _read(char_db)
    assert tokens[0] == (0, 0, 760, "你好")
    assert tokens[1] == (1, 760, 1960, "第一次")


def test_punctuation_survives_on_the_merged_word(char_db: Path) -> None:
    """``punc_after`` comes from the run's last character, not the first."""
    command.upgrade(_config(char_db), "head")

    con = _connect(char_db)
    try:
        puncs = [
            (str(r["text"]), str(r["punc_after"]))
            for r in con.execute("SELECT * FROM token ORDER BY idx")
        ]
    finally:
        con.close()
    assert puncs[3] == ("这", "。")
    assert puncs[6] == ("好", "？")
    assert puncs[0] == ("你好", "")


def test_sentence_ranges_tile_the_token_stream(char_db: Path) -> None:
    command.upgrade(_config(char_db), "head")

    tokens, sentences = _read(char_db)
    assert sentences[0][1] == 0
    assert sentences[-1][2] == len(tokens)
    for (_o, _start, end), (_o2, start2, _end2) in pairwise(sentences):
        assert end == start2


def test_migration_marks_tokens_as_words_and_updates_the_count(char_db: Path) -> None:
    command.upgrade(_config(char_db), "head")

    con = _connect(char_db)
    try:
        kinds = [str(r[0]) for r in con.execute("SELECT DISTINCT kind FROM token")]
        row = con.execute("SELECT token_count FROM transcript_version").fetchone()
    finally:
        con.close()
    assert kinds == ["word"]
    assert row is not None and int(row[0]) == 7


def test_running_the_migration_twice_does_not_duplicate_tokens(char_db: Path) -> None:
    """A failed-then-retried deploy must not append a second copy.

    Also asserts the ``token`` table's shape is unchanged by the replay. Two
    separate migrations add a boolean to it (``revised``, then ``hidden``), and
    adding the second one made this replay fail with ``CircularDependencyError``:
    ``batch_alter_table`` rebuilds the table from the *current* schema, so by the
    time it re-added ``revised`` the column was already there and its column
    ordering developed a cycle. Both migrations now guard on
    ``PRAGMA table_info``, the way ``deletion_batch_id`` always did.

    The rewind point is ``CHAR_REVISION`` rather than the initial schema: earlier
    revisions create tables without guards, so replaying them is a different (and
    larger) promise than "this deploy can be retried".
    """
    columns_before = _token_columns(char_db)
    cfg = _config(char_db)
    command.upgrade(cfg, "head")
    first = _read(char_db)
    columns_head = _token_columns(char_db)

    con = _connect(char_db)
    try:
        con.execute(f"UPDATE alembic_version SET version_num = '{CHAR_REVISION}'")
        con.commit()
    finally:
        con.close()
    command.upgrade(cfg, "head")

    assert _read(char_db) == first
    assert _token_columns(char_db) == columns_head
    # Both flags must actually be there, or the replay "passing" would just mean
    # the migrations silently did nothing.
    assert {"revised", "hidden"} <= set(columns_head), columns_head
    assert columns_before != columns_head  # the upgrade really added a column


def _token_columns(db_path: Path) -> list[str]:
    con = _connect(db_path)
    try:
        return [str(row[1]) for row in con.execute("PRAGMA table_info(token)")]
    finally:
        con.close()


def test_single_character_words_are_not_glued_together(tmp_path: Path) -> None:
    """Three one-character words stay three tokens, not one."""
    db_path = tmp_path / "single.db"
    command.upgrade(_config(db_path), CHAR_REVISION)
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT INTO project (id, name, created_at, updated_at, schema_version) "
            "VALUES ('prj_s', 'p', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1)"
        )
        con.execute(
            "INSERT INTO transcript_version (id, project_id, media_id, job_id, version, "
            "created_at, duration_ms, token_count, sentence_count, is_active) "
            "VALUES ('ver_s', 'prj_s', 'med_s', '', 1, '2026-01-01T00:00:00Z', 900, 3, 1, 1)"
        )
        con.executemany(
            "INSERT INTO token (version_id, idx, start_ms, end_ms, text, kind, "
            "src_word_idx, punc_after, derived) VALUES ('ver_s', ?, ?, ?, ?, 'char', ?, '', 1)",
            [(0, 0, 300, "我", 0), (1, 300, 600, "的", 1), (2, 600, 900, "书", 2)],
        )
        con.execute(
            "INSERT INTO sentence (id, version_id, ord, token_start, token_end, "
            "speaker_id, origin, updated_at) VALUES ('sen_s', 'ver_s', 0, 0, 3, NULL, "
            "'asr', '2026-01-01T00:00:00Z')"
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(_config(db_path), "head")

    tokens, sentences = _read(db_path)
    assert [text for _i, _s, _e, text in tokens] == ["我", "的", "书"]
    assert sentences == [(0, 0, 3)]


def test_tokens_outside_every_sentence_are_kept(tmp_path: Path) -> None:
    """INV-1 says this cannot happen; if it does, the audio keeps its transcript."""
    db_path = tmp_path / "orphan.db"
    command.upgrade(_config(db_path), CHAR_REVISION)
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT INTO project (id, name, created_at, updated_at, schema_version) "
            "VALUES ('prj_o', 'p', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1)"
        )
        con.execute(
            "INSERT INTO transcript_version (id, project_id, media_id, job_id, version, "
            "created_at, duration_ms, token_count, sentence_count, is_active) "
            "VALUES ('ver_o', 'prj_o', 'med_o', '', 1, '2026-01-01T00:00:00Z', 1200, 4, 1, 1)"
        )
        con.executemany(
            "INSERT INTO token (version_id, idx, start_ms, end_ms, text, kind, "
            "src_word_idx, punc_after, derived) VALUES ('ver_o', ?, ?, ?, ?, 'char', ?, '', 1)",
            [
                (0, 0, 300, "甲", 0),
                (1, 300, 600, "乙", 0),
                (2, 600, 900, "丙", 1),
                (3, 900, 1200, "丁", 2),  # idx 3 is outside the sentence
            ],
        )
        con.execute(
            "INSERT INTO sentence (id, version_id, ord, token_start, token_end, "
            "speaker_id, origin, updated_at) VALUES ('sen_o', 'ver_o', 0, 0, 2, NULL, "
            "'asr', '2026-01-01T00:00:00Z')"
        )
        con.commit()
    finally:
        con.close()

    command.upgrade(_config(db_path), "head")

    tokens, sentences = _read(db_path)
    assert [text for _i, _s, _e, text in tokens] == ["甲乙", "丙", "丁"]
    assert sentences[0][1] == 0 and sentences[0][2] == 3


def test_downgrade_refuses_to_invent_timings() -> None:
    module = _load_revision()
    with pytest.raises(NotImplementedError):
        module.downgrade()


def _load_revision() -> ModuleType:
    """Import the revision module; its filename starts with a digit."""
    path = API_ROOT / "alembic" / "versions" / "3c1a7d90e5b2_merge_tokens_to_word_level.py"
    spec = importlib.util.spec_from_file_location("_word_revision", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == WORD_REVISION
    return module
