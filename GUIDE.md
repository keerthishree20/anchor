# Anchor — Complete Project Guide

## Table of Contents
1. [What is Anchor?](#what-is-anchor)
2. [Quick Start](#quick-start)
3. [Core Concepts](#core-concepts)
4. [Architecture](#architecture)
5. [On-Disk Format](#on-disk-format)
6. [Code Walkthrough](#code-walkthrough)
7. [Using Anchor](#using-anchor)
8. [Testing Strategy](#testing-strategy)
9. [Benchmarks](#benchmarks)
10. [Extending Anchor](#extending-anchor)
11. [Troubleshooting](#troubleshooting)

---

## What is Anchor?

Anchor is an embedded key-value store written in pure Python with no dependencies. It is modelled
on Bitcask, the storage engine behind Riak.

Every write appends to a log file, and nothing on disk is ever modified in place. That single rule
makes crash recovery simple: the only place a file can be damaged is the end of the newest one,
because that is the only place a process can be interrupted mid-write.

The trade-off is stated plainly: **every key must fit in memory.** Values live on disk and do not.

---

## Quick Start

Requires Python 3.10 or newer. On this machine `python3` is 3.6, so the Makefile uses `python3.12`.

```bash
make install        # .venv with pytest; the store itself needs nothing
make test           # the full suite, crash tests included
make demo           # writes two keys and prints them
```

From Python:

```python
from anchor import Anchor

with Anchor.open("./data") as db:
    db.put(b"greeting", b"hello")
    db.get(b"greeting")          # b"hello"
    db.delete(b"greeting")
    db.compact()
```

---

## Core Concepts

### Segment
A data file. Exactly one segment is **active** and receives writes. When it grows past
`max_segment_bytes` (64 MB by default), it is closed and becomes immutable, and a new active segment
starts.

### Keydir
The in-memory index: a Python dict mapping each key to `(file_id, value_pos, value_len, tstamp_ns)`.
A read is one dictionary lookup and one `pread`, no matter how many times the key was rewritten.

### Tombstone
A delete is written as a record whose value length is `2**32 - 1`. The key is removed from the
keydir, and the tombstone stays on disk until compaction.

### Hint file
When a segment closes, a hint file is written next to it holding every key and the position of its
value, without the values. Opening the store later reads hints instead of scanning whole segments.

### Compaction
Rewrites only the live keys into fresh segments and deletes the old ones, reclaiming the space held
by overwritten values and tombstones.

### Fsync policy
How far a write has travelled before `put` returns.

| `fsync` | a returned `put` has reached |
|---|---|
| `always` | the disk |
| `batch` | the kernel, and the disk within `fsync_every` writes |
| `never` | the kernel only |

All three survive a killed process, because the kernel still flushes its page cache. Only `always`
survives a power cut.

---

## Architecture

```
            put / delete                      get
                │                              │
                ▼                              ▼
     ┌─────────────────────┐        ┌────────────────────┐
     │ record.encode()     │        │ keydir lookup      │
     │ append to active    │        │ (file, pos, len)   │
     │ segment, maybe fsync│        └─────────┬──────────┘
     └─────────┬───────────┘                  │ os.pread
               │ update keydir                ▼
               ▼                    ┌────────────────────┐
  data/                             │ segment file       │
    0000000001.data  closed         └────────────────────┘
    0000000001.hint  its index
    0000000002.data  closed
    0000000002.hint
    0000000003.data  active, the only file being written
```

Reads use `os.pread` with a separate read descriptor per segment, so concurrent readers never move
each other's file position and the read path takes no lock. Writes take one lock.

---

## On-Disk Format

Defined in `anchor/record.py`. Big-endian throughout, so a hex dump reads in diagram order.

```
┌──────────┬────────────┬─────────┬───────────┬─────┬───────┐
│ crc32  4 │ tstamp_ns 8│ klen  4 │ vlen    4 │ key │ value │
└──────────┴────────────┴─────────┴───────────┴─────┴───────┘
└── covered by the crc ────────────────────────────────────┘
```

The header is 20 bytes. The checksum turns a torn final record from corruption into a recoverable
event: recovery stops at the first record that fails its checksum or runs past the end of the file.

### Recovery on open
1. Leftover `.tmp` files from an interrupted compaction are deleted.
2. Segments are indexed in ascending id order, so a later record wins over an earlier one.
3. A closed segment with an intact hint is indexed from its hint. Its values are never read.
4. The active segment is always scanned. If the scan stops before the end of the file, the
   remainder was a partial write and is truncated away.
5. A damaged hint is thrown away and its segment is scanned instead.

---

## Code Walkthrough

### `anchor/record.py`
| function | purpose |
|---|---|
| `encode(key, value, tstamp_ns)` | builds one record. `value=None` builds a tombstone |
| `decode(buf, offset)` | parses one record, raising `CorruptRecord` on a bad checksum or short read |
| `value_span(offset, klen, vlen)` | where the value bytes sit inside a record |
| `encode_hint`, `decode_hint` | the hint file format |

### `anchor/store.py`
`Anchor` is the store. The important methods:

| method | what it does |
|---|---|
| `Anchor.open(path, fsync=, max_segment_bytes=, fsync_every=)` | creates or opens a directory and runs recovery |
| `put(key, value)` | appends a record under the lock, updates the keydir, applies the fsync policy |
| `get(key)` | keydir lookup and one `pread`, no lock |
| `delete(key)` | appends a tombstone. Returns `False` if the key was absent |
| `compact()` | merges live data into new segments and returns what it reclaimed |
| `stats()` | keys, segments, live and dead bytes, space amplification |
| `sync()` | forces everything written so far to disk |
| `_recover()`, `_load_hint()`, `_scan_segment()` | the recovery steps above |
| `_rotate()` | fsyncs and closes the active segment, writes its hint, opens the next |
| `_sync_directory()` | fsyncs the directory, so a new file's name is durable too |

Compaction blocks writers for its whole duration. Merged output is written to temporary files,
fsynced and renamed, and only then are the originals deleted. Merged segments get higher ids than
the originals, so if a crash leaves both, the merged data wins.

### `anchor/cli.py`
A thin command line over the same API.

---

## Using Anchor

### Command line
```
anchor --path ./data put greeting hello
anchor --path ./data get greeting
anchor --path ./data del greeting
anchor --path ./data list --values --prefix user:
anchor --path ./data stats
anchor --path ./data compact
anchor --path ./data verify        # read every value back, checksums and all
```

### API
| call | does |
|---|---|
| `put(key, value)` | store bytes |
| `get(key)` | newest value or `None` |
| `delete(key)` | tombstone |
| `keys()`, `items()`, `len(db)`, `key in db` | iterate and inspect |
| `sync()`, `compact()`, `stats()`, `close()` | maintenance |

Keys and values must be `bytes`. Passing a `str` raises `TypeError` telling you to encode it, rather
than guessing an encoding.

### Choosing an fsync policy
- A write that must survive a power cut: `fsync="always"`, and accept the cost.
- Bulk loading you can redo: `fsync="batch"` or `"never"`, then call `sync()` at the end.

### When to compact
Call `compact()` when `stats()` shows high space amplification, meaning much of the disk is dead
bytes from overwrites and deletes. Writers wait until it finishes.

---

## Testing Strategy

| file | what it covers |
|---|---|
| `tests/test_record.py` | encoding, decoding, checksums, corrupt input |
| `tests/test_basics.py` | the API and its edge cases |
| `tests/test_recovery.py` | reopening, torn tails at every offset, flipped bits, damaged hints |
| `tests/test_compaction.py` | merging, tombstone removal, interrupted compactions |
| `tests/test_concurrency.py` | four writer threads and four reader threads, no torn values |
| `tests/test_crash.py` | real child processes killed with SIGKILL and SIGSTOP mid-write |

```bash
make test-fast     # everything except the process-killing tests
make test-crash    # only those
```

What the crash tests prove, and what they do not: SIGKILL ends a process, not a machine, so data
already handed to the kernel survives it. The suite proves the format, recovery and compaction are
sound under an abrupt stop. It cannot prove fsync works, because only cutting power would show that.

---

## Benchmarks

```bash
make bench            # writes at each fsync setting, reads, compaction
make bench-recovery   # reopen time with and without hint files
```

The measured numbers are in the README. Two points matter when quoting them:

- Write throughput depends entirely on the fsync setting. Always name the setting with the number.
- Hint files helped far less than expected for small values, because recovery is bound by decoding
  records in Python, not by reading bytes. They help a lot for large values.

---

## Extending Anchor

These were left out on purpose. The README explains each one.

- **Range scans.** A hash index has no order. Adding a sorted structure is the step from Bitcask to
  an LSM tree.
- **Online compaction.** Merging during live writes needs a second index and a hand-off.
- **A faster decode loop.** Recovery time for small values is bound by per-record Python work.
- **Transactions and replication.** One `put` is atomic, two are not one unit. One process, one
  machine.

---

## Troubleshooting

### `TypeError: keys and values are bytes`
Encode text first: `db.put("name".encode(), "value".encode())`.

### Writes are slow
`fsync="always"` waits for the disk on every write. On a laptop SSD that is a few hundred writes a
second. Use `batch` if you can accept losing the last few writes in a power cut.

### The store grows and never shrinks
Overwrites and deletes only add records. Run `compact()`, or `anchor --path ./data compact`.

### Opening takes a long time
The active segment is always scanned in full. Closed segments with hints load fast. Lowering
`max_segment_bytes` makes the active segment smaller.

### `python3` fails with a syntax error
The system `python3` is 3.6. Use `make install`, then run everything through `.venv/bin/python`.
