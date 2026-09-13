"""Reopening: segments, hint files, and repairing a torn tail."""

from __future__ import annotations

import os

import pytest

from anchor import Anchor, record

from .conftest import data_files, hint_files


def test_everything_written_survives_a_reopen(data_dir):
    with Anchor.open(data_dir) as db:
        for i in range(50):
            db.put(f"k{i}".encode(), f"v{i}".encode())
        db.delete(b"k7")
        db.put(b"k9", b"changed")

    with Anchor.open(data_dir) as db:
        assert len(db) == 49
        assert db.get(b"k0") == b"v0"
        assert db.get(b"k7") is None
        assert db.get(b"k9") == b"changed"


def test_segments_rotate_at_the_size_limit(data_dir):
    with Anchor.open(data_dir, max_segment_bytes=256) as db:
        for i in range(40):
            db.put(f"k{i:03d}".encode(), b"v" * 40)
        assert len(data_files(data_dir)) > 1
        for segment in data_files(data_dir)[:-1]:
            assert segment.stat().st_size <= 256 + 128


def test_a_closed_segment_gets_a_hint_file(data_dir):
    with Anchor.open(data_dir, max_segment_bytes=256) as db:
        for i in range(40):
            db.put(f"k{i:03d}".encode(), b"v" * 40)

    hints = {p.stem for p in hint_files(data_dir)}
    datas = {p.stem for p in data_files(data_dir)}
    assert hints, "closed segments should be indexed"
    # The newest segment is still open for writing, so it has no hint.
    assert max(datas) not in hints


def test_the_hint_path_and_the_scan_path_agree(data_dir):
    """The index must not depend on which route recovery took."""
    with Anchor.open(data_dir, max_segment_bytes=256) as db:
        for i in range(60):
            db.put(f"k{i:03d}".encode(), f"value-{i}".encode())
        db.delete(b"k004")
        db.put(b"k010", b"replaced")

    with Anchor.open(data_dir) as from_hints:
        with_hints = dict(from_hints.items())

    for hint in hint_files(data_dir):
        hint.unlink()
    with Anchor.open(data_dir) as from_scan:
        from_scan_items = dict(from_scan.items())

    assert with_hints == from_scan_items
    assert b"k004" not in with_hints
    assert with_hints[b"k010"] == b"replaced"


def test_a_damaged_hint_falls_back_to_scanning(data_dir):
    with Anchor.open(data_dir, max_segment_bytes=256) as db:
        for i in range(40):
            db.put(f"k{i:03d}".encode(), f"value-{i}".encode())
        expected = dict(db.items())

    victim = hint_files(data_dir)[0]
    victim.write_bytes(victim.read_bytes()[:-3] + b"\x00\x00\x00")

    with Anchor.open(data_dir) as db:
        assert dict(db.items()) == expected


def test_a_torn_tail_is_cut_off_and_the_rest_survives(data_dir):
    """Exactly what a process killed mid-write leaves behind."""
    with Anchor.open(data_dir) as db:
        for i in range(20):
            db.put(f"k{i:02d}".encode(), f"v{i}".encode())

    active = data_files(data_dir)[-1]
    full = active.stat().st_size
    partial = record.encode(b"k99", b"never finished")[:11]
    with open(active, "ab") as handle:
        handle.write(partial)

    with Anchor.open(data_dir) as db:
        assert len(db) == 20
        assert db.get(b"k99") is None
        assert db.get(b"k19") == b"v19"

    assert active.stat().st_size == full, "the partial record should be truncated away"


@pytest.mark.parametrize("keep", [1, 7, 19, 25])
def test_a_tail_torn_at_any_offset_still_opens(data_dir, keep):
    with Anchor.open(data_dir) as db:
        for i in range(10):
            db.put(f"k{i}".encode(), f"v{i}".encode())

    active = data_files(data_dir)[-1]
    with open(active, "ab") as handle:
        handle.write(record.encode(b"torn", b"x" * 40)[:keep])

    with Anchor.open(data_dir) as db:
        assert len(db) == 10
        assert db.get(b"torn") is None


def test_writing_continues_cleanly_after_a_repair(data_dir):
    with Anchor.open(data_dir) as db:
        db.put(b"before", b"1")

    active = data_files(data_dir)[-1]
    with open(active, "ab") as handle:
        handle.write(record.encode(b"torn", b"x")[:9])

    with Anchor.open(data_dir) as db:
        db.put(b"after", b"2")

    with Anchor.open(data_dir) as db:
        assert db.get(b"before") == b"1"
        assert db.get(b"after") == b"2"
        assert db.get(b"torn") is None


def test_corruption_in_the_middle_stops_the_scan_there(data_dir):
    """A flipped bit is not a torn tail. Recovery keeps what it has verified and
    stops, rather than guessing where the next record starts."""
    with Anchor.open(data_dir) as db:
        for i in range(10):
            db.put(f"k{i}".encode(), f"v{i}".encode())

    active = data_files(data_dir)[-1]
    raw = bytearray(active.read_bytes())
    midpoint = len(raw) // 2
    raw[midpoint] ^= 0xFF
    active.write_bytes(bytes(raw))

    with Anchor.open(data_dir) as db:
        assert 0 < len(db) < 10
        for key in db.keys():
            assert db.get(key) is not None


def test_an_empty_directory_opens(data_dir):
    with Anchor.open(data_dir) as db:
        assert len(db) == 0
        db.put(b"first", b"write")
    with Anchor.open(data_dir) as db:
        assert db.get(b"first") == b"write"


def test_unrelated_files_are_ignored(data_dir):
    (data_dir / "README.txt").write_text("not a segment")
    (data_dir / "notanumber.data").write_bytes(b"junk")
    with Anchor.open(data_dir) as db:
        db.put(b"key", b"value")
        assert db.get(b"key") == b"value"


def test_leftover_temporary_files_are_swept(data_dir):
    with Anchor.open(data_dir) as db:
        db.put(b"key", b"value")
    (data_dir / "0000000099.data.tmp").write_bytes(b"half a merge")

    with Anchor.open(data_dir) as db:
        assert db.get(b"key") == b"value"
    assert not list(data_dir.glob("*.tmp"))


def test_the_directory_entry_itself_is_synced(data_dir, monkeypatch):
    """A new segment is worthless if a crash loses the directory entry for it."""
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])

    with Anchor.open(data_dir) as db:
        db.put(b"key", b"value")
    assert synced, "nothing was fsynced at all"
