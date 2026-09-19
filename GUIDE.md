# Anchor — Complete Project Guide

A complete guide from zero to a crash-safe key-value store. Covers every feature, every design
decision and the reason behind it, with the real code. It is self-contained: you can paste it into
any AI chat and ask questions about the project without sharing the repository.

**Repository:** https://github.com/keerthishree20/anchor

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Tech Stack & Why](#2-tech-stack--why)
3. [Project Setup from Scratch](#3-project-setup-from-scratch)
4. [Core Ideas in Plain Words](#4-core-ideas-in-plain-words)
5. [Project Structure](#5-project-structure)
6. [The Record Format](#6-the-record-format)
7. [Writing: put and delete](#7-writing-put-and-delete)
8. [Reading: get](#8-reading-get)
9. [Segments and Rotation](#9-segments-and-rotation)
10. [Hint Files](#10-hint-files)
11. [Crash Recovery](#11-crash-recovery)
12. [Compaction](#12-compaction)
13. [Durability: The Three fsync Settings](#13-durability-the-three-fsync-settings)
14. [Concurrency](#14-concurrency)
15. [Statistics](#15-statistics)
16. [Python API](#16-python-api)
17. [Command Line](#17-command-line)
18. [Testing & the Crash Suite](#18-testing--the-crash-suite)
19. [Benchmarks & Results](#19-benchmarks--results)
20. [Deliberately Not Built](#20-deliberately-not-built)
21. [Troubleshooting](#21-troubleshooting)
22. [Complete Feature Summary](#22-complete-feature-summary)

---

## 1. Project Overview

Anchor is an **embedded key-value store** written in pure Python with no dependencies. You open a
folder, store bytes under keys, and read them back, even after the program crashes.

It follows the design of **Bitcask**, the storage engine behind the Riak database. One rule shapes
everything: **every write appends, and nothing on disk is ever changed in place.** Because of that,
the only place a file can be damaged by a crash is the end of the newest file, since that is the only
place a program can be interrupted mid-write. Recovery becomes a simple scan instead of a repair.

The honest trade-off: **every key must fit in memory.** Values live on disk and do not. A store with a
hundred million keys needs a different design. A store with a few million keys and huge values is
exactly what this is for.

```python
from anchor import Anchor

with Anchor.open("./data") as db:
    db.put(b"greeting", b"hello")
    db.get(b"greeting")          # b"hello"
    db.delete(b"greeting")
    db.compact()
```

**Status:** complete. 75 tests pass, including a crash suite that kills real processes.

---

## 2. Tech Stack & Why

| Technology | Role | Why We Chose It |
|---|---|---|
| **Python 3.10+** | Language | the storage rules are easy to read and check |
| **Standard library only** | Runtime | `os`, `struct` and `zlib` are all a log-structured store needs |
| **`os.pread`** | Reads | reads at an offset without moving a shared file position, so readers need no lock |
| **CRC32 (`zlib`)** | Checksums | turns a torn write into a detectable, recoverable event |
| **pytest** | Tests | including tests that SIGKILL real writer processes |
| **GitHub Actions** | CI | Python 3.10, 3.11 and 3.12 |

### Why Bitcask and not an LSM tree?
An LSM tree with levelled compaction is the more impressive answer, and its merge logic is where all
its difficulty lives. Bitcask keeps the whole on-disk story simple enough to explain completely,
which is the better trade for a store meant to be read and understood.

---

## 3. Project Setup from Scratch

```bash
git clone https://github.com/keerthishree20/anchor.git
cd anchor
make install        # .venv with pytest (uses python3.12; python3 here is 3.6)
make test           # all 75 tests, crash tests included
make demo           # writes two keys and prints them
```

Other targets: `make test-fast` (no process killing), `make test-crash`, `make bench`,
`make bench-recovery`.

---

## 4. Core Ideas in Plain Words

| Idea | Meaning |
|---|---|
| **Segment** | one data file. Exactly one is *active* and receives writes |
| **Record** | one write: a checksum, a timestamp, the key and the value |
| **Tombstone** | a record that means "this key was deleted" |
| **Keydir** | the in-memory index: key → where its newest value is on disk |
| **Hint file** | a small index written next to a closed segment, so reopening is fast |
| **Compaction** | rewriting only the live data and deleting the old files |

### How a folder looks
```
data/
  0000000001.data   closed segment, never changes again
  0000000001.hint   its index, values left out
  0000000002.data   closed segment
  0000000002.hint
  0000000003.data   active segment, the only file being written
```

---

## 5. Project Structure

```
anchor/
  record.py    the record and hint formats, checksums, CorruptRecord
  store.py     Anchor: keydir, segments, rotation, recovery, compaction, stats
  cli.py       the command line
bench/
  throughput.py  writes at each fsync setting, reads, compaction
  recovery.py    reopen time with and without hint files
tests/
  test_record.py       encoding, decoding, corruption
  test_basics.py       the API and its edge cases
  test_recovery.py     reopening, torn tails, flipped bits, damaged hints
  test_compaction.py   merging and interrupted compactions
  test_concurrency.py  4 writer threads, 4 reader threads
  test_crash.py        real processes killed mid-write
Makefile  pyproject.toml  requirements-dev.txt  .github/workflows/tests.yml
```

---

## 6. The Record Format

Defined in `anchor/record.py`. Big-endian, so a hex dump reads in diagram order.

```
┌──────────┬────────────┬─────────┬───────────┬─────┬───────┐
│ crc32  4 │ tstamp_ns 8│ klen  4 │ vlen    4 │ key │ value │
└──────────┴────────────┴─────────┴───────────┴─────┴───────┘
└── covered by the crc ────────────────────────────────────┘
```

The header is 20 bytes. A value length of `2**32 - 1` marks a tombstone.

```python
def encode(key: bytes, value: bytes | None, tstamp_ns: int | None = None) -> bytes:
    tstamp_ns = time.time_ns() if tstamp_ns is None else tstamp_ns
    vlen = TOMBSTONE if value is None else len(value)
    body = HEADER.pack(0, tstamp_ns, len(key), vlen)[4:] + key + (value or b"")
    return struct.pack(">I", zlib.crc32(body)) + body
```

`decode()` reads one record and raises `CorruptRecord` if the record runs past the end of the data
or its checksum does not match. The recovery scan treats that as "end of usable data", not an error.

### Why a checksum?
It turns a torn tail from corruption into a recoverable event. Without it, half a record could be
read as garbage values.

---

## 7. Writing: put and delete

```python
def put(self, key: bytes, value: bytes) -> None:
    if not isinstance(key, (bytes, bytearray)) or not isinstance(value, (bytes, bytearray)):
        raise TypeError("keys and values are bytes; encode text before storing it")
    blob = record.encode(bytes(key), bytes(value))
    with self._lock:
        offset = self._append(blob)
        previous = self._keydir.get(key)
        if previous is not None:
            self._dead_bytes += previous.record_size(len(key))     # the old value is now waste
        value_pos, _ = record.value_span(offset, len(key), len(value))
        self._keydir[bytes(key)] = Entry(self._active_id, value_pos, len(value), _tstamp_of(blob))
        self._maybe_sync()
```

`delete(key)` appends a tombstone and removes the key from the keydir. It returns `False` if the key
was not there. Both the old value and the tombstone count as dead bytes until compaction.

### Why only bytes?
Passing a `str` raises a `TypeError` telling you to encode it, rather than guessing an encoding.

---

## 8. Reading: get

```python
def get(self, key: bytes) -> bytes | None:
    entry = self._keydir.get(key)
    if entry is None:
        return None
    if entry.value_len == 0:
        return b""
    data = os.pread(self._readers[entry.file_id], entry.value_len, entry.value_pos)
    ...
    return data
```

A read is **one dictionary lookup and one `pread`**, no matter how many times the key was rewritten.

### Why `pread`?
`pread` takes the offset as an argument. Seek-then-read would move a shared file position, so two
readers could interfere. With `pread`, the read path needs no lock at all.

---

## 9. Segments and Rotation

When the active segment would grow past `max_segment_bytes` (64 MB by default), Anchor rotates:

```python
def _rotate(self) -> None:
    os.fsync(self._active_fd)
    os.close(self._active_fd)
    self._write_hint(self._active_id)      # index of the segment just closed
    self._active_id += 1
    self._open_active()                     # new file; its directory entry is fsynced too
```

### Why fsync the directory?
A file whose contents are on disk but whose *name* is not may not exist after a crash. Creating a
segment fsyncs the folder so the file's existence is durable too.

---

## 10. Hint Files

A hint file holds every key of a closed segment and the position of its value, without the values.
Reopening the store reads hints instead of scanning whole segments.

```python
def _load_hint(self, file_id, hint_path) -> bool:
    ...
    except CorruptRecord:
        return False     # a damaged hint tells us nothing reliable; scan the segment instead
```

### What they are actually worth
Hints help far less than expected for **small** values: only 1.3× faster reopening at 200-byte
values, because recovery is limited by decoding records one at a time in Python, not by reading bytes.
They help a lot for **large** values: 10.4× at 16 KB, where a scan reads half a gigabyte and the hint
read touches a few megabytes. See section 19.

---

## 11. Crash Recovery

`Anchor.open()` rebuilds the keydir:

1. Delete leftover `.tmp` files from an interrupted compaction.
2. Index segments in ascending id order, so a later record wins over an earlier one.
3. A **closed** segment with an intact hint is indexed from the hint. Its values are never read.
4. The **active** segment is always scanned, because it is the only file that can end mid-record.
   If the scan stops before the end, the rest was a partial write and is truncated away:

```python
good_bytes = self._scan_segment(file_id, data_path)
if is_active and good_bytes < data_path.stat().st_size:
    os.truncate(data_path, good_bytes)    # cutting off the torn record is the repair
```

5. A damaged hint is thrown away and its segment scanned instead.

Because records are only appended, every record before the torn one is whole by construction.

---

## 12. Compaction

Overwrites and deletes only add records, so the folder grows. `compact()` rewrites only the live data:

1. fsync and close the active segment; write its hint.
2. Write every live key into **new** segments with higher ids, via temporary files that are fsynced
   and renamed into place:

```python
def _write_file_durably(path, blob):
    scratch = path.with_name(path.name + TMP_SUFFIX)
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.write(fd, blob); os.fsync(fd); os.close(fd)
    os.replace(scratch, path)            # a reader never sees a half-written file
```

3. Only then delete the old segments and hints, and fsync the folder.
4. Tombstones are dropped too, because every file that could hold an older value is gone.

### Why is it safe to crash in the middle?
- Merged segments have **higher ids** than the originals, so if a crash leaves both, the merged data
  wins on reopen.
- Leftover temporary files are swept on the next open.

### Why do writers wait?
Merging alongside live writes needs a second index and a hand-off protocol, which is exactly where
storage engine bugs live. Blocking writers is the honest trade for a single-writer embedded store.

`compact()` returns what it reclaimed: bytes before and after, segments before and after, and keys.

---

## 13. Durability: The Three fsync Settings

```python
def _maybe_sync(self) -> None:
    self._writes_since_sync += 1
    if self.fsync_policy == "always":
        os.fsync(self._active_fd)
    elif self.fsync_policy == "batch" and self._writes_since_sync >= self.fsync_every:
        os.fsync(self._active_fd)
```

| `fsync` | a returned `put` has reached | survives |
|---|---|---|
| `always` | the disk | a killed process and a power cut |
| `batch` | the kernel, and the disk within `fsync_every` writes (100) | a killed process |
| `never` | the kernel only | a killed process |

Every setting survives a killed process, because the kernel still writes out its memory afterwards.
Only `always` survives losing the machine.

### Choosing
- Data that must survive a power cut: `always`, and accept the speed.
- Bulk loading you could redo: `batch` or `never`, then call `sync()` at the end.

---

## 14. Concurrency

- **One writer lock** guards appends and the keydir update.
- **Reads take no lock**, because `pread` does not share a file position.
- The concurrency test runs four writer threads and four reader threads and checks that no torn value
  is ever read and no key is lost.

---

## 15. Statistics

```python
db.stats()
# {"keys": ..., "segments": ..., "live_bytes": ..., "total_bytes": ...,
#  "dead_bytes": ..., "space_amplification": 4.0, "fsync": "always"}
```

`space_amplification` is total bytes on disk divided by live bytes. `1.0` means every byte on disk can
still be read. A high number means it is time to compact.

---

## 16. Python API

| Call | Does |
|---|---|
| `Anchor.open(path, fsync="always", max_segment_bytes=64 MB, fsync_every=100)` | open or create |
| `put(key, value)` | append a record, update the index |
| `get(key)` | the newest value, or `None` |
| `delete(key)` | append a tombstone; `False` if absent |
| `keys()`, `items()`, `len(db)`, `key in db` | iterate and inspect |
| `sync()` | force everything so far to disk |
| `compact()` | merge, returning what it reclaimed |
| `stats()` | keys, segments, live and dead bytes, space amplification |
| `close()` / `with` block | close file descriptors |

---

## 17. Command Line

```
anchor --path ./data put greeting hello
anchor --path ./data get greeting
anchor --path ./data del greeting
anchor --path ./data list --values --prefix user:
anchor --path ./data stats
anchor --path ./data compact
anchor --path ./data verify        # read every value back, checksums and all
```

---

## 18. Testing & the Crash Suite

| Failure | What is asserted |
|---|---|
| writer SIGKILLed mid-write | surviving keys form an unbroken prefix, no holes, every value matches its key |
| killed at all three fsync settings | same prefix property each time |
| killed repeatedly | each recovery keeps everything the previous one had |
| writer SIGSTOPped | another process opens the folder and reads a consistent prefix |
| killed during compaction, at three points | every live value present, no unreadable file, no leftover temporaries |
| torn tail | truncated away, rest intact, writing continues |
| torn at every offset inside a record | still opens, at 1, 7, 19 and 25 bytes in |
| flipped bit mid-file | the scan stops there rather than guessing |
| damaged hint file | discarded, segment scanned, identical index |
| 4 writer + 4 reader threads | no torn value, no lost key |

### What these prove, and what they don't
SIGKILL ends a process, not a machine. Anything already handed to the kernel survives it. So the
suite proves the format, recovery and compaction are sound under an abrupt stop. It **cannot** prove
fsync does its job, because only cutting the power would show that.

---

## 19. Benchmarks & Results

Intel Core i5-11320H, Linux 6.8, Python 3.12, laptop SSD. Values 200 bytes, keys 15 bytes.

| Operation | Measured |
|---|---:|
| Writes, `fsync=always` | 247 /s |
| Writes, `fsync=batch` every 100 | 21,876 /s |
| Writes, `fsync=never` | 198,911 /s |
| Reads, random key | 932,989 /s |
| Read latency | 1.07 µs |
| Compaction | 86.2 MB/s |
| Space amplification before and after compaction | 4.0 to 1.0 |

**Always name the fsync setting with a write number.** 247 and 198,911 writes a second are the same
code with a different promise attached.

### Hint files
| Value size | Records | On disk | With hints | Scanning | Speedup |
|---|---:|---:|---:|---:|---:|
| 200 B | 200,000 | 47 MB | 793 ms | 1,064 ms | 1.3× |
| 4 KB | 60,000 | 242 MB | 123 ms | 431 ms | 3.5× |
| 16 KB | 30,000 | 481 MB | 61 ms | 632 ms | 10.4× |

---

## 20. Deliberately Not Built

| Feature | Why not |
|---|---|
| keys larger than memory | the keydir is a dictionary; this is the design's defining limit |
| range scans | a hash index has no order; adding one is the step to an LSM tree |
| transactions | one `put` is atomic, two are not one unit |
| online compaction | writers wait during a merge (section 12) |
| a faster decode loop | recovery for small values is bound by Python per-record work |
| replication | one process, one folder, one machine |

---

## 21. Troubleshooting

### `TypeError: keys and values are bytes`
Encode text first: `db.put("name".encode(), "value".encode())`.

### Writes are slow
`fsync="always"` waits for the disk every write, a few hundred a second on a laptop. Use `batch` if
losing the last few writes in a power cut is acceptable.

### The folder keeps growing
Run `db.compact()` or `anchor --path ./data compact`.

### Opening takes a long time
The active segment is always scanned in full. A smaller `max_segment_bytes` keeps it small.

### `python3` fails with a syntax error
The system `python3` is 3.6. Use `make install` and `.venv/bin/python`.

---

## 22. Complete Feature Summary

### All Features Built

| # | Feature | Type | Key Files |
|---|---|---|---|
| 1 | Checksummed record format | Format | `record.py` |
| 2 | Append-only put and tombstone delete | Store | `store.py` |
| 3 | Lock-free `pread` reads | Store | `store.py` |
| 4 | Segment rotation with directory fsync | Store | `store.py` |
| 5 | Hint files | Format / Store | `record.py`, `store.py` |
| 6 | Crash recovery with torn-tail truncation | Store | `store.py` |
| 7 | Crash-safe compaction | Store | `store.py` |
| 8 | Three fsync policies | Store | `store.py` |
| 9 | Statistics and space amplification | Store | `store.py` |
| 10 | Command line with verify | Tooling | `cli.py` |
| 11 | Crash suite with real processes | Testing | `tests/test_crash.py` |
| 12 | Throughput and recovery benchmarks | Tooling | `bench/` |

### Data Flow Architecture

```
put(key, value)
  └── record.encode() ──► os.write to active segment ──► keydir[key] = (file, pos, len, ts)
          └── fsync policy: always / every N / never
          └── segment full ──► fsync, close, write hint, open next (fsync directory)

get(key)
  └── keydir lookup ──► os.pread(file, len, pos) ──► value

open(path)
  └── sweep .tmp ──► for each segment in id order:
          closed + good hint ──► load hint
          otherwise          ──► scan records (stop at first bad checksum)
          active             ──► always scan, truncate torn tail

compact()
  └── write live keys to new higher-id segments (tmp, fsync, rename) ──► delete old ──► fsync dir
```

### Tech Stack at a Glance

```
Language:  Python 3.10+ (standard library only)
Design:    Bitcask: append-only segments + in-memory hash index + hint files
Integrity: CRC32 per record, atomic renames, directory fsync
Testing:   pytest, crash suite with SIGKILL and SIGSTOP, concurrency tests
CI:        GitHub Actions on Python 3.10, 3.11, 3.12
```
