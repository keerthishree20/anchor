"""Command line interface.

    anchor --path ./data put greeting hello
    anchor --path ./data get greeting
    anchor --path ./data del greeting
    anchor --path ./data list
    anchor --path ./data stats
    anchor --path ./data compact
    anchor --path ./data verify

Keys and values are bytes on disk. The command line takes and prints UTF-8;
anything that is not valid UTF-8 is shown as a Python bytes repr.
"""

from __future__ import annotations

import argparse
import json
import sys

from .record import CorruptRecord
from .store import Anchor


def _show(raw: bytes) -> str:
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return repr(raw)


def cmd_put(db: Anchor, args) -> int:
    db.put(args.key.encode(), args.value.encode())
    return 0


def cmd_get(db: Anchor, args) -> int:
    value = db.get(args.key.encode())
    if value is None:
        print(f"{args.key}: not found", file=sys.stderr)
        return 1
    print(_show(value))
    return 0


def cmd_del(db: Anchor, args) -> int:
    if db.delete(args.key.encode()):
        return 0
    print(f"{args.key}: not found", file=sys.stderr)
    return 1


def cmd_list(db: Anchor, args) -> int:
    shown = 0
    for key in sorted(db.keys()):
        if args.prefix and not key.startswith(args.prefix.encode()):
            continue
        if args.values:
            print(f"{_show(key)}\t{_show(db.get(key) or b'')}")
        else:
            print(_show(key))
        shown += 1
        if args.limit and shown >= args.limit:
            break
    return 0


def cmd_stats(db: Anchor, args) -> int:
    stats = db.stats()
    if args.json:
        print(json.dumps(stats))
        return 0
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"{key.ljust(width)}  {value}")
    return 0


def cmd_compact(db: Anchor, args) -> int:
    result = db.compact()
    if args.json:
        print(json.dumps(result))
    else:
        print(f"reclaimed {result['reclaimed_bytes']} bytes, "
              f"{result['before_segments']} segments to {result['after_segments']}, "
              f"{result['keys']} keys kept")
    return 0


def cmd_verify(db: Anchor, args) -> int:
    """Read every value back, so a checksum or a bad offset shows up now."""
    checked = 0
    for key in db.keys():
        try:
            if db.get(key) is None:
                print(f"index points at nothing for {_show(key)}", file=sys.stderr)
                return 1
        except CorruptRecord as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        checked += 1
    print(f"{checked} keys read back cleanly")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="anchor", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", default="./anchor-data", help="the store directory")
    parser.add_argument("--fsync", choices=("always", "batch", "never"), default="always")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("put"); p.add_argument("key"); p.add_argument("value")
    p.set_defaults(fn=cmd_put)

    p = sub.add_parser("get"); p.add_argument("key"); p.set_defaults(fn=cmd_get)
    p = sub.add_parser("del"); p.add_argument("key"); p.set_defaults(fn=cmd_del)

    p = sub.add_parser("list")
    p.add_argument("--prefix", default=None)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--values", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("stats"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("compact"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_compact)

    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with Anchor.open(args.path, fsync=args.fsync) as db:
        return args.fn(db, args)


if __name__ == "__main__":
    raise SystemExit(main())
