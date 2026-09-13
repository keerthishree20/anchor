"""How long reopening takes, as the log grows.

Two curves, and the gap between them is the entire argument for hint files. A
scan reads every byte of every segment; a hint read touches only the keys. On a
store whose values dwarf its keys, that is the difference between recovery
costing the size of the data and costing the size of the index.

    python -m bench.recovery --sizes 10000,50000,200000
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import tempfile
import time

from anchor import Anchor


def _build(directory: str, records: int, value_bytes: int, segment_bytes: int) -> int:
    value = b"x" * value_bytes
    with Anchor.open(directory, fsync="never", max_segment_bytes=segment_bytes) as db:
        for i in range(records):
            db.put(f"key{i:012d}".encode(), value)
        return db.stats()["total_bytes"]


def _time_open(directory: str) -> tuple[float, int]:
    start = time.perf_counter()
    with Anchor.open(directory) as db:
        keys = len(db)
    return time.perf_counter() - start, keys


def measure(records: int, value_bytes: int, segment_bytes: int) -> dict:
    directory = tempfile.mkdtemp(prefix="anchor-recovery-")
    try:
        on_disk = _build(directory, records, value_bytes, segment_bytes)

        with_hints, keys = _time_open(directory)

        # Same data, no index: recovery has to read every record.
        for hint in pathlib.Path(directory).glob("*.hint"):
            hint.unlink()
        scanning, keys_again = _time_open(directory)
        assert keys == keys_again, "the two routes disagreed about the key count"

        return {
            "records": records,
            "bytes_on_disk": on_disk,
            "megabytes_on_disk": round(on_disk / 1e6, 1),
            "with_hints_ms": round(with_hints * 1000, 1),
            "scanning_ms": round(scanning * 1000, 1),
            "speedup": round(scanning / with_hints, 1) if with_hints else None,
        }
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench.recovery", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sizes", default="10000,50000,200000",
                   help="comma separated record counts")
    p.add_argument("--value-bytes", type=int, default=200)
    p.add_argument("--segment-bytes", type=int, default=4 * 1024 * 1024)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    rows = [measure(int(size), args.value_bytes, args.segment_bytes)
            for size in args.sizes.split(",")]

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(f"value size {args.value_bytes} bytes\n")
    print(f"{'records':>10} {'on disk':>10} {'with hints':>12} {'scanning':>10} {'speedup':>8}")
    for row in rows:
        print(f"{row['records']:>10,} {row['megabytes_on_disk']:>8} MB "
              f"{row['with_hints_ms']:>10} ms {row['scanning_ms']:>8} ms "
              f"{row['speedup']:>7}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
