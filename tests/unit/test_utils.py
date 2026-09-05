from __future__ import annotations

from datetime import timezone

from trajweave.utils.hashing import file_sha256, stable_short_id, text_sha256
from trajweave.utils.timeparse import parse_timestamp, to_iso


def test_stable_short_id_is_deterministic():
    a = stable_short_id("tw_proj_", "/home/user/work/repo-a")
    b = stable_short_id("tw_proj_", "/home/user/work/repo-a")
    c = stable_short_id("tw_proj_", "/home/user/work/repo-b")
    assert a == b
    assert a != c
    assert a.startswith("tw_proj_") and len(a) == len("tw_proj_") + 12


def test_file_sha256(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"hello world")
    assert file_sha256(p) == text_sha256("hello world")


def test_parse_timestamp_iso_and_epoch():
    dt = parse_timestamp("2026-09-02T08:41:16.123Z")
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026 and dt.month == 9

    # epoch seconds and milliseconds both land on the same instant
    assert parse_timestamp(1787318372) == parse_timestamp(1787318372000)
    assert parse_timestamp(None) is None
    assert parse_timestamp("not a date") is None


def test_to_iso_roundtrip():
    assert to_iso("2026-09-02T08:41:16Z") == "2026-09-02T08:41:16+00:00"
