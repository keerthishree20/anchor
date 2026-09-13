"""Write and read throughput at each durability setting.

The fsync policy is the whole story here. Writes per second with a flush to disk
on every record and writes per second without one are two different numbers
about two different guarantees, and quoting the second while claiming the first
is the easiest way to publish a benchmark that is a lie.

    python -m bench.throughput --records 50000 --value-bytes 200
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import tempfile
import time

from anchor import Anchor


def _fill(db, records: int, value_bytes: int) -> float:
    value = b"x" * value_bytes
    start = time.perf_counter()
    for i in range(records):
        db.put(f"key{i:012d}".encode(), value)
    db.sync()
    return time.perf_counter() - start


def measure_writes(records: int, value_bytes: int, fsync: str, segment_bytes: int) -> dict:
    directory = tempfile.mkdtemp(prefix="anchor-bench-")
    try:
        with Anchor.open(directory, fsync=fsync, max_segment_bytes=segment_bytes) as db:
            elapsed = _fill(db, records, value_bytes)
            stats = db.stats()
        return {
            "fsync": fsync,
            "records": records,
            "seconds": round(elapsed, 3),
            "writes_per_s": round(records / elapsed),
            "bytes_on_disk": stats["total_bytes"],
        }
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def measure_reads(records: int, value_bytes: int, samples: int, segment_bytes: int) -> dict:
    directory = tempfile.mkdtemp(prefix="anchor-bench-")
    try:
        with Anchor.open(directory, fsync="never", max_segment_bytes=segment_bytes) as db:
            _fill(db, records, value_bytes)
            keys = [f"key{i:012d}".encode() for i in range(records)]
            rng = random.Random(20260912)
            picks = [rng.choice(keys) for _ in range(samples)]

            start = time.perf_counter()
            for key in picks:
                db.get(key)
            elapsed = time.perf_counter() - start

        return {
            "records": records,
            "samples": samples,
            "seconds": round(elapsed, 3),
            "reads_per_s": round(samples / elapsed),
            "microseconds_per_read": round(elapsed / samples * 1e6, 2),
        }
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def measure_compaction(records: int, value_bytes: int, rounds: int, segment_bytes: int) -> dict:
    directory = tempfile.mkdtemp(prefix="anchor-bench-")
    try:
        with Anchor.open(directory, fsync="never", max_segment_bytes=segment_bytes) as db:
            value = b"x" * value_bytes
            for _ in range(rounds):
                for i in range(records):
                    db.put(f"key{i:012d}".encode(), value)
            before = db.stats()

            start = time.perf_counter()
            result = db.compact()
            elapsed = time.perf_counter() - start
            after = db.stats()

        return {
            "keys": records,
            "overwrite_rounds": rounds,
            "seconds": round(elapsed, 3),
            "before_bytes": before["total_bytes"],
            "after_bytes": after["total_bytes"],
            "space_amplification_before": before["space_amplification"],
            "space_amplification_after": after["space_amplification"],
            "reclaimed_bytes": result["reclaimed_bytes"],
            "megabytes_per_s": round(before["total_bytes"] / elapsed / 1e6, 1),
        }
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench.throughput", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--records", type=int, default=50_000)
    p.add_argument("--value-bytes", type=int, default=200)
    p.add_argument("--read-samples", type=int, default=200_000)
    p.add_argument("--overwrite-rounds", type=int, default=4)
    p.add_argument("--segment-bytes", type=int, default=16 * 1024 * 1024)
    p.add_argument("--fsync-records", type=int, default=5_000,
                   help="fewer records for fsync=always, which is far slower")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    result = {
        "writes": [
            measure_writes(args.fsync_records, args.value_bytes, "always", args.segment_bytes),
            measure_writes(args.records, args.value_bytes, "batch", args.segment_bytes),
            measure_writes(args.records, args.value_bytes, "never", args.segment_bytes),
        ],
        "reads": measure_reads(args.records, args.value_bytes, args.read_samples,
                               args.segment_bytes),
        "compaction": measure_compaction(args.records // 5, args.value_bytes,
                                         args.overwrite_rounds, args.segment_bytes),
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"value size   {args.value_bytes} bytes\n")
    print("writes")
    for row in result["writes"]:
        print(f"  fsync={row['fsync']:<7} {row['writes_per_s']:>9,}/s   "
              f"({row['records']:,} records in {row['seconds']}s)")
    r = result["reads"]
    print(f"\nreads        {r['reads_per_s']:,}/s   "
          f"({r['microseconds_per_read']} microseconds each, {r['records']:,} keys)")
    c = result["compaction"]
    print(f"\ncompaction   {c['megabytes_per_s']} MB/s   "
          f"amplification {c['space_amplification_before']} to "
          f"{c['space_amplification_after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
