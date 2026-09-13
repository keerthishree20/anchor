"""The store as an API: put, get, delete, and the edges around them."""

from __future__ import annotations

import pytest

from anchor import Anchor


def test_put_then_get(db):
    db.put(b"greeting", b"hello")
    assert db.get(b"greeting") == b"hello"


def test_a_missing_key_reads_as_none(db):
    assert db.get(b"nothing here") is None


def test_the_newest_write_wins(db):
    for value in (b"first", b"second", b"third"):
        db.put(b"key", value)
    assert db.get(b"key") == b"third"
    assert len(db) == 1


def test_delete_removes_the_key(db):
    db.put(b"key", b"value")
    assert db.delete(b"key") is True
    assert db.get(b"key") is None
    assert b"key" not in db
    assert len(db) == 0


def test_deleting_what_is_not_there_reports_so(db):
    assert db.delete(b"key") is False


def test_a_key_can_come_back_after_deletion(db):
    db.put(b"key", b"first")
    db.delete(b"key")
    db.put(b"key", b"second")
    assert db.get(b"key") == b"second"


def test_an_empty_value_is_stored_not_treated_as_absent(db):
    db.put(b"key", b"")
    assert db.get(b"key") == b""
    assert b"key" in db


def test_binary_keys_and_values_round_trip(db):
    key, value = bytes(range(256)), b"\x00\x01\xff\n"
    db.put(key, value)
    assert db.get(key) == value


def test_a_large_value_round_trips(db):
    value = b"x" * (2 * 1024 * 1024)
    db.put(b"big", value)
    assert db.get(b"big") == value


def test_keys_and_items_agree(db):
    written = {f"k{i}".encode(): f"v{i}".encode() for i in range(20)}
    for key, value in written.items():
        db.put(key, value)
    db.delete(b"k5")

    assert set(db.keys()) == set(written) - {b"k5"}
    assert dict(db.items()) == {k: v for k, v in written.items() if k != b"k5"}


def test_text_is_refused_with_a_message_that_says_what_to_do(db):
    with pytest.raises(TypeError, match="encode text"):
        db.put("key", b"value")
    with pytest.raises(TypeError, match="encode text"):
        db.put(b"key", "value")


def test_using_a_closed_store_is_an_error(data_dir):
    db = Anchor.open(data_dir)
    db.put(b"key", b"value")
    db.close()
    with pytest.raises(ValueError, match="closed"):
        db.get(b"key")
    with pytest.raises(ValueError, match="closed"):
        db.put(b"key", b"other")


def test_closing_twice_is_harmless(data_dir):
    db = Anchor.open(data_dir)
    db.close()
    db.close()


def test_the_context_manager_closes(data_dir):
    with Anchor.open(data_dir) as db:
        db.put(b"key", b"value")
    with pytest.raises(ValueError):
        db.get(b"key")


def test_an_unknown_fsync_policy_is_refused(data_dir):
    with pytest.raises(ValueError, match="always, batch or never"):
        Anchor.open(data_dir, fsync="sometimes")


def test_opening_creates_the_directory(tmp_path):
    target = tmp_path / "nested" / "store"
    with Anchor.open(target) as db:
        db.put(b"key", b"value")
    assert target.is_dir()


def test_stats_describe_an_empty_store(db):
    stats = db.stats()
    assert stats["keys"] == 0
    assert stats["live_bytes"] == 0
    assert stats["total_bytes"] == 0
    assert stats["space_amplification"] == 0.0


def test_overwrites_show_up_as_dead_bytes(db):
    db.put(b"key", b"x" * 100)
    clean = db.stats()
    assert clean["dead_bytes"] == 0

    db.put(b"key", b"y" * 100)
    dirty = db.stats()
    assert dirty["keys"] == 1
    assert dirty["dead_bytes"] > 0
    assert dirty["space_amplification"] > clean["space_amplification"]
