"""Identifier shape and uniqueness."""

from __future__ import annotations

import re

from animatter.ids import new_id, utcnow_iso

ID_PATTERN = re.compile(r"^prj_[0-9a-z]{19}$")


def test_id_shape() -> None:
    value = new_id("prj")
    assert ID_PATTERN.match(value), value


def test_ids_are_unique() -> None:
    values = {new_id("tok") for _ in range(2000)}
    assert len(values) == 2000


def test_timestamps_are_utc_z() -> None:
    stamp = utcnow_iso()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", stamp), stamp
