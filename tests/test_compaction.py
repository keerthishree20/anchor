"""Compaction: reclaiming the space that overwrites and deletions leave behind."""

from __future__ import annotations

from anchor import Anchor

from .conftest import data_files, hint_files


def _churn(db, keys: int = 40, rounds: int = 5) -> dict[bytes, bytes]:
    """Write every key several times so most of the log is superseded."""
    latest: dict[bytes, bytes] = {}
    for round_no in range(rounds):
        for i in range(keys):
            key = f"k{i:03d}".encode()
            value = f"round{round_no}-value{i}".encode()
            db.put(key, value)
            latest[key] = value
    return latest


def test_compaction_reclaims_the_superseded_bytes(data_dir):
    with Anchor.open(data_dir, max_segment_bytes=4096) as db:
        _churn(db)
        before = db.stats()
        assert before["space_amplification"] > 2

        result = db.compact()
        after = db.stats()

    assert result["reclaimed_bytes"] > 0
    assert after["total_bytes"] < before["total_bytes"]
    assert after["space_amplification"] == 1.0
    assert after["dead_bytes"] == 0


def test_compaction_keeps_every_live_value(data_dir):
    with Anchor.open(data_dir, max_segment_bytes=4096) as db:
        expected = _churn(db)
        db.compact()
        assert dict(db.items()) == expected

    with Anchor.open(data_dir) as db:
        assert dict(db.items()) == expected


def test_compaction_drops_deleted_keys_and_their_tombstones(data_dir):
    with Anchor.open(data_dir, max_segment_bytes=4096) as db:
        expected = _churn(db)
        for i in range(0, 40, 2):
            key = f"k{i:03d}".encode()
            db.delete(key)
            expected.pop(key)

        db.compact()
        assert dict(db.items()) == expected

    # The deletions must still hold after a reopen, even though no tombstone
    # was carried into the merged segments.
    with Anchor.open(data_dir) as db:
        assert dict(db.items()) == expected
        assert db.get(b"k000") is None
        assert len(db) == 20


def test_compacting_a_clean_store_changes_nothing_visible(data_dir):
    with Anchor.open(data_dir) as db:
        for i in range(10):
            db.put(f"k{i}".encode(), f"v{i}".encode())
        expected = dict(db.items())

        db.compact()
        first = db.stats()
        db.compact()
        second = db.stats()

    assert first["total_bytes"] == second["total_bytes"]
    assert first["keys"] == second["keys"] == 10
    with Anchor.open(data_dir) as db:
        assert dict(db.items()) == expected


def test_compacting_an_empty_store_is_safe(data_dir):
    with Anchor.open(data_dir) as db:
        result = db.compact()
        assert result["keys"] == 0
        db.put(b"after", b"compaction")
        assert db.get(b"after") == b"compaction"

    with Anchor.open(data_dir) as db:
        assert db.get(b"after") == b"compaction"


def test_writes_continue_after_compaction(data_dir):
    with Anchor.open(data_dir, max_segment_bytes=4096) as db:
        _churn(db)
        db.compact()
        for i in range(20):
            db.put(f"new{i}".encode(), b"written after the merge")
        assert db.get(b"new19") == b"written after the merge"

    with Anchor.open(data_dir) as db:
        assert db.get(b"new0") == b"written after the merge"
        assert db.get(b"k000") is not None


def test_merged_segments_carry_hint_files(data_dir):
    with Anchor.open(data_dir, max_segment_bytes=4096) as db:
        _churn(db)
        db.compact()

    merged = {p.stem for p in data_files(data_dir)}
    hinted = {p.stem for p in hint_files(data_dir)}
    # Every merged segment is hinted; only the fresh active file is not.
    assert len(merged - hinted) == 1


def test_compaction_splits_output_at_the_segment_limit(data_dir):
    with Anchor.open(data_dir, max_segment_bytes=1024) as db:
        for i in range(100):
            db.put(f"k{i:03d}".encode(), b"v" * 60)
        db.compact()
        assert db.stats()["segments"] > 2

    with Anchor.open(data_dir) as db:
        assert len(db) == 100
        assert db.get(b"k099") == b"v" * 60


def test_compaction_leaves_no_temporary_files(data_dir):
    with Anchor.open(data_dir, max_segment_bytes=1024) as db:
        _churn(db)
        db.compact()
    assert not list(data_dir.glob("*.tmp"))


def test_reads_are_served_from_the_merged_segments(data_dir):
    """After the originals are unlinked, every read must resolve against a file
    that still exists."""
    with Anchor.open(data_dir, max_segment_bytes=4096) as db:
        expected = _churn(db)
        db.compact()
        surviving = {p.name for p in data_files(data_dir)}
        for key, value in expected.items():
            assert db.get(key) == value
        assert len(surviving) == db.stats()["segments"]
