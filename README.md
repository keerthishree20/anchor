# Anchor

An append-only key-value store with crash recovery. Pure Python, no
dependencies, one file format.

Every write appends. Nothing on disk is ever modified in place. That single
constraint is what makes recovery a scan rather than a repair: the only place a
file can be damaged is the very end of the newest one, because that is the only
place a process can be interrupted.

```python
from anchor import Anchor

with Anchor.open("./data") as db:
    db.put(b"greeting", b"hello")
    db.get(b"greeting")          # b"hello"
    db.delete(b"greeting")
    db.compact()
```

---

## The design

Bitcask, not a log-structured merge tree. The choice is deliberate and worth
saying out loud: an LSM with levelled compaction is the more impressive answer,
and the merge logic is where all of its difficulty lives. Bitcask puts the whole
key index in memory and keeps the on-disk story simple enough to explain
completely, which is the better trade for a store meant to be read and
understood.

The consequence is the one real limit here: **every key must fit in memory**.
Values do not. A store with a hundred million keys needs a different design; a
store with a hundred million megabytes and a few million keys is exactly what
this is for.

### On disk

```
data/
  0000000001.data   closed segment, immutable
  0000000001.hint   its index, values omitted
  0000000002.data   closed segment
  0000000002.hint
  0000000003.data   active segment, the only file being written
```

A record:

```
┌──────────┬────────────┬─────────┬───────────┬─────┬───────┐
│ crc32  4 │ tstamp_ns 8│ klen  4 │ vlen    4 │ key │ value │
└──────────┴────────────┴─────────┴───────────┴─────┴───────┘
└── covered by the crc ────────────────────────────────────┘
```

A value length of `2**32-1` marks a deletion. The checksum is what turns a torn
tail from a corruption into a recoverable event.

### In memory

One dictionary, key to `(file_id, value_pos, value_len, tstamp_ns)`. A read is
one dictionary lookup and one `pread`, whatever the history of that key. Reads
use `pread` rather than seek-then-read so concurrent readers cannot move each
other's file position, and no lock is needed on the read path at all.

### Recovery

On open, segments are indexed in ascending order, so a later record naturally
wins over an earlier one for the same key.

- **A closed segment with an intact hint** is indexed from the hint. Its values
  are never touched.
- **The active segment is always scanned**, because it is the only file that can
  end mid-record. If the scan stops before the end of the file, the remainder
  was a partial write and is truncated away.
- **A damaged hint is discarded entirely** and the segment is scanned. A hint
  truncated by a crash tells you nothing reliable about the rest of it.

### Compaction

Rewrites the live set into fresh segments and drops everything else, including
the tombstones, since every file that could hold an older value for a deleted
key is removed in the same pass.

Merged output is written to temporary files, fsynced, renamed, and only then are
the originals unlinked. Interrupted anywhere in that sequence the store still
opens: merged segments carry higher ids than the originals, so if both are
present the merged data wins, and leftover temporary files are swept on the next
open.

Writers are blocked for the duration. That is the honest trade for an embedded
single-writer store. Merging alongside live writes needs a second index and a
hand-off protocol, and that is precisely the part of a storage engine where the
bugs live.

---

## Durability

Three settings, and they are three different promises:

| `fsync` | Promise |
|---|---|
| `always` | a returned `put` has reached the disk |
| `batch` | a returned `put` has reached the kernel; the disk gets it within `fsync_every` writes |
| `never` | a returned `put` has reached the kernel, and nothing more |

Every setting survives a killed process, because the kernel still writes out its
page cache afterwards. Only `always` survives losing the machine.

The directory entry is fsynced too when a segment is created. A file whose
contents are durable but whose name is not is a file that may not exist after a
crash.

---

## Measured

Run with `make bench` and `make bench-recovery`. Nothing here is a target.

**Hardware.** Intel Core i5-11320H, 8 threads, 15 GB RAM, Linux 6.8, Python
3.12.13, on a laptop SSD. Values 200 bytes, keys 15 bytes.

| Operation | Measured |
|---|---:|
| Writes, `fsync=always` | 247 /s |
| Writes, `fsync=batch` every 100 | 21,876 /s |
| Writes, `fsync=never` | 198,911 /s |
| Reads, random key | 932,989 /s |
| Read latency | 1.07 microseconds |
| Compaction | 86.2 MB/s |
| Space amplification, before and after | 4.0 to 1.0 |

The write column is the point of the table. Two hundred and forty seven writes
per second and two hundred thousand writes per second are the same code with a
different promise attached, and quoting the second while claiming the first is
the easiest way to publish a benchmark that is a lie.

### What hint files are actually worth

The interesting result, because it is not the one I expected.

| Value size | Records | On disk | With hints | Scanning | Speedup |
|---|---:|---:|---:|---:|---:|
| 200 B | 200,000 | 47 MB | 793 ms | 1,064 ms | 1.3x |
| 4 KB | 60,000 | 242 MB | 123 ms | 431 ms | 3.5x |
| 16 KB | 30,000 | 481 MB | 61 ms | 632 ms | 10.4x |

With small values, hints barely help. Recovery is not bound by reading bytes off
the disk, it is bound by decoding one record at a time in Python, and both routes
decode the same number of records. The hint only removes the value bytes, which
were never the expensive part.

The gap opens as values grow, exactly where the theory says it should: at 16 KB a
scan reads half a gigabyte and the hint read touches a few megabytes of keys. So
hint files are a large-value optimisation here, and on a store of small values
the honest thing to say is that they buy almost nothing until the decode loop
itself is made faster.

---

## Tests

75 tests. The ones that matter kill things.

| Failure | What is asserted |
|---|---|
| Writer `SIGKILL`ed mid-write | surviving keys form an unbroken prefix, no holes, every value matching its key |
| Killed at all three fsync settings | same prefix property in each case |
| Killed repeatedly | each recovery keeps everything the previous one had |
| Writer `SIGSTOP`ped | another process opens the same directory and reads a consistent prefix |
| Killed during compaction, at three points | every live value still present, no unreadable file, no leftover temporaries |
| Torn tail | truncated away, the rest intact, and writing continues cleanly afterwards |
| Torn at every offset in a record | still opens, at 1, 7, 19 and 25 bytes in |
| Flipped bit mid-file | the scan stops there rather than guessing where the next record starts |
| Damaged hint file | discarded, the segment scanned, identical index either way |
| Four writer threads, four reader threads | no torn value ever read, no key lost |

**What these prove, and what they do not.** `SIGKILL` ends a process, not a
machine. Anything already handed to the kernel survives it. So the suite
establishes that the format, the recovery scan and the compaction hand-off are
sound under an abrupt stop. It does not establish that `fsync` is doing its job,
because only cutting the power would show that. The fsync policy is the claim;
this suite is not its evidence.

```
$ .venv/bin/python -m pytest -q
...........................................................................  [100%]
75 passed in 31.98s
```

---

## Quick start

```bash
make install     # .venv, pytest only; the store itself has no dependencies
make test        # 75 tests, crash tests included
make bench       # throughput at each fsync setting
make bench-recovery
```

Python 3.12 is required and is not always what `python3` points at. The Makefile
uses `python3.12` explicitly for that reason.

## Command line

```
anchor --path ./data put greeting hello
anchor --path ./data get greeting
anchor --path ./data del greeting
anchor --path ./data list --values --prefix user:
anchor --path ./data stats
anchor --path ./data compact
anchor --path ./data verify        # read every value back, checksums and all
```

## API

| Call | Does |
|---|---|
| `Anchor.open(path, fsync=, max_segment_bytes=, fsync_every=)` | open or create |
| `put(key, value)` | append a record, update the index |
| `get(key)` | the newest value, or `None` |
| `delete(key)` | append a tombstone; `False` if the key was absent |
| `keys()`, `items()`, `len()`, `in` | iterate and inspect |
| `sync()` | force everything written so far to disk |
| `compact()` | merge, returning what it reclaimed |
| `stats()` | keys, segments, live and dead bytes, space amplification |

Keys and values are `bytes`. Passing `str` raises with a message that says to
encode it, rather than guessing an encoding.

---

## Not built

Named because leaving them out was a choice.

- **Keys larger than memory.** The index is a dictionary. This is the design's
  defining limit, not an oversight.
- **Range scans.** A hash index has no ordering. Ordered iteration would need a
  sorted structure, which is the point where Bitcask becomes an LSM.
- **Transactions.** A single `put` is atomic. Two of them are not one unit.
- **Online compaction.** Writers block during a merge. See the design section.
- **A faster decode loop.** The recovery benchmark says this, not intuition:
  with small values the scan is bound by per-record Python work, so parsing in a
  C extension or with `memoryview` slicing would move recovery time more than
  any I/O change.
- **Replication.** One process, one directory, one machine.

## Layout

```
anchor/
  record.py    the record and hint formats, checksums, decode errors
  store.py     the store: index, segments, rotation, recovery, compaction
  cli.py       the command line
bench/
  throughput.py  writes at each fsync setting, reads, compaction
  recovery.py    reopen time with and without hints, across value sizes
tests/         75 tests, including the crash suite
```
