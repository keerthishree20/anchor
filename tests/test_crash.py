"""Killing things.

A note on what these prove. `SIGKILL` ends a process, not a machine: anything
already handed to the kernel stays in the page cache and reaches the disk
afterwards. So these tests establish that the format, the recovery scan and the
compaction hand-off are sound under an abrupt stop. They do not establish that
`fsync` is doing its job, because only pulling the power would show that. The
fsync policy is the claim; this suite is not its evidence.
"""

from __future__ import annotations

import signal
import time

import pytest

from anchor import Anchor
from anchor.record import CorruptRecord

from .conftest import data_files, wait_for

WRITER = """
import sys
from anchor import Anchor

path, fsync, total = sys.argv[1], sys.argv[2], int(sys.argv[3])
db = Anchor.open(path, fsync=fsync, max_segment_bytes=8192)
for i in range(total):
    db.put(f"k{i:08d}".encode(), f"v{i:08d}".encode())
db.close()
"""

COMPACTOR = """
import sys
from anchor import Anchor

path = sys.argv[1]
db = Anchor.open(path, max_segment_bytes=4096)
while True:
    db.compact()
"""


def assert_prefix(db) -> int:
    """Keys must be k0..kN with no holes, and each value must match its key.

    A hole would mean a write was lost while a later one survived, which
    append-only recovery must never produce.
    """
    keys = sorted(db.keys())
    for position, key in enumerate(keys):
        assert key == f"k{position:08d}".encode(), (
            f"gap at position {position}: found {key!r}")
        assert db.get(key) == f"v{position:08d}".encode(), f"wrong value for {key!r}"
    return len(keys)


@pytest.mark.parametrize("fsync", ["always", "batch", "never"])
def test_a_killed_writer_leaves_a_readable_prefix(data_dir, spawn, fsync):
    proc = spawn(WRITER, fsync, "20000")
    wait_for(lambda: any(p.stat().st_size > 4096 for p in data_files(data_dir)),
             what="the writer to get going")
    time.sleep(0.05)
    proc.kill()
    proc.wait(timeout=10)

    with Anchor.open(data_dir) as db:
        survived = assert_prefix(db)
    assert survived > 0, "nothing at all was recovered"


def test_the_store_keeps_working_after_a_kill(data_dir, spawn):
    proc = spawn(WRITER, "always", "20000")
    # Wait for a rotation, so the kill lands with a closed segment plus a
    # partly written active one rather than a single small file.
    wait_for(lambda: len(data_files(data_dir)) >= 2,
             what="the writer to rotate a segment")
    proc.kill()
    proc.wait(timeout=10)

    with Anchor.open(data_dir) as db:
        recovered = assert_prefix(db)
        db.put(b"written-after-the-crash", b"yes")
        assert db.get(b"written-after-the-crash") == b"yes"

    with Anchor.open(data_dir) as db:
        assert db.get(b"written-after-the-crash") == b"yes"
        assert len(db) == recovered + 1


def test_repeated_kills_never_lose_earlier_writes(data_dir, spawn):
    """Crash, recover, crash again. Each round must keep everything the last
    round had."""
    high_water = 0
    for round_no in range(3):
        proc = spawn(WRITER, "always", "20000")
        wait_for(lambda: any(p.stat().st_size > 2048 for p in data_files(data_dir)),
                 what=f"writer {round_no} to start")
        time.sleep(0.08)
        proc.kill()
        proc.wait(timeout=10)

        with Anchor.open(data_dir) as db:
            survived = assert_prefix(db)
        assert survived >= high_water, "a later crash lost what an earlier one kept"
        high_water = max(high_water, survived)
    assert high_water > 0


def test_a_frozen_writer_holds_nothing_the_reader_needs(data_dir, spawn):
    """SIGSTOP mid-run. A reader in another process opens the same directory and
    sees a consistent prefix, because nothing on disk is ever half-updated."""
    proc = spawn(WRITER, "always", "20000")
    wait_for(lambda: any(p.stat().st_size > 4096 for p in data_files(data_dir)),
             what="the writer to get going")
    proc.send_signal(signal.SIGSTOP)
    try:
        with Anchor.open(data_dir) as db:
            assert assert_prefix(db) > 0
    finally:
        proc.send_signal(signal.SIGCONT)
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.parametrize("delay", [0.05, 0.15, 0.4])
def test_a_kill_during_compaction_loses_nothing(data_dir, spawn, delay):
    """The merge writes new segments and fsyncs them before unlinking any
    original. Interrupted anywhere, the store still holds every live value."""
    with Anchor.open(data_dir, max_segment_bytes=4096) as db:
        expected = {}
        for round_no in range(4):
            for i in range(120):
                key, value = f"k{i:04d}".encode(), f"r{round_no}-{i}".encode()
                db.put(key, value)
                expected[key] = value

    proc = spawn(COMPACTOR)
    time.sleep(delay)
    proc.kill()
    proc.wait(timeout=10)

    with Anchor.open(data_dir) as db:
        assert dict(db.items()) == expected
        for key in db.keys():
            assert db.get(key) is not None


def test_a_kill_during_compaction_leaves_no_unreadable_files(data_dir, spawn):
    with Anchor.open(data_dir, max_segment_bytes=4096) as db:
        for i in range(300):
            db.put(f"k{i:04d}".encode(), b"value" * 10)
        expected = dict(db.items())

    proc = spawn(COMPACTOR)
    time.sleep(0.2)
    proc.kill()
    proc.wait(timeout=10)

    with Anchor.open(data_dir) as db:
        for key in db.keys():
            try:
                db.get(key)
            except CorruptRecord as exc:
                pytest.fail(f"unreadable after an interrupted merge: {exc}")
        assert dict(db.items()) == expected
    assert not list(data_dir.glob("*.tmp"))
