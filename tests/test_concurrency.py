"""One writer, many readers.

The store takes a lock around writes and reads with `pread`, which carries its
own offset. So a reader never has to coordinate with a writer, and two readers
cannot move each other's file position.
"""

from __future__ import annotations

import threading

from anchor import Anchor


def test_readers_never_see_a_half_written_value(data_dir):
    """A value is only reachable once its record is fully appended and the index
    points at it, so a reader either sees the old value or the new one."""
    keys = [f"k{i:03d}".encode() for i in range(50)]
    with Anchor.open(data_dir, max_segment_bytes=8192) as db:
        for key in keys:
            db.put(key, b"0" * 200)

        stop = threading.Event()
        problems: list[str] = []

        def write() -> None:
            round_no = 1
            while not stop.is_set():
                marker = str(round_no % 10).encode()
                for key in keys:
                    db.put(key, marker * 200)
                round_no += 1

        def read() -> None:
            while not stop.is_set():
                for key in keys:
                    value = db.get(key)
                    if value is None:
                        problems.append(f"{key!r} vanished")
                    elif len(value) != 200 or len(set(value)) != 1:
                        problems.append(f"{key!r} read a torn value: {value[:16]!r}")

        writer = threading.Thread(target=write)
        readers = [threading.Thread(target=read) for _ in range(4)]
        writer.start()
        for reader in readers:
            reader.start()
        stop.wait(1.5)
        stop.set()
        writer.join(timeout=15)
        for reader in readers:
            reader.join(timeout=15)

        assert problems[:5] == []
        assert len(db) == len(keys)


def test_concurrent_writers_do_not_corrupt_the_log(data_dir):
    """Two threads writing at once is not the intended shape, but the lock has
    to hold anyway: the log must stay parseable and every key must resolve."""
    with Anchor.open(data_dir, max_segment_bytes=4096) as db:
        def write(worker: int) -> None:
            for i in range(300):
                db.put(f"w{worker}-k{i:03d}".encode(), f"w{worker}-v{i:03d}".encode())

        threads = [threading.Thread(target=write, args=(w,)) for w in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(db) == 4 * 300

    with Anchor.open(data_dir) as db:
        assert len(db) == 4 * 300
        for worker in range(4):
            for i in (0, 150, 299):
                key = f"w{worker}-k{i:03d}".encode()
                assert db.get(key) == f"w{worker}-v{i:03d}".encode()


def test_a_reader_survives_a_rotation_underneath_it(data_dir):
    """Rotation opens a new file while a reader may be mid-scan of the old one.
    Old segments are never closed until compaction removes them."""
    with Anchor.open(data_dir, max_segment_bytes=1024) as db:
        for i in range(200):
            db.put(f"k{i:03d}".encode(), b"v" * 40)

        stop = threading.Event()
        failures: list[str] = []

        def read() -> None:
            while not stop.is_set():
                for i in range(200):
                    if db.get(f"k{i:03d}".encode()) != b"v" * 40:
                        failures.append(f"k{i:03d} changed under a rotation")
                        return

        reader = threading.Thread(target=read)
        reader.start()
        for i in range(200, 900):
            db.put(f"k{i:03d}".encode(), b"v" * 40)
        stop.set()
        reader.join(timeout=15)

        assert failures == []
        assert db.stats()["segments"] > 5
