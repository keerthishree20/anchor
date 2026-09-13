"""Anchor: an append-only key-value store with crash recovery.

The whole design is one idea. Writes only ever append, and nothing on disk is
ever modified in place. That makes recovery a scan rather than a repair: the
only place damage can be is the very end of the newest file, because that is the
only place a process can be interrupted.

An in-memory index maps every key to where its newest value sits, so a read is
one seek and one read, whatever the history of that key.
"""

from __future__ import annotations

import os
import pathlib
import threading
from dataclasses import dataclass
from typing import Iterator, Literal

from . import record
from .record import CorruptRecord

DATA_SUFFIX = ".data"
HINT_SUFFIX = ".hint"
TMP_SUFFIX = ".tmp"

FsyncPolicy = Literal["always", "batch", "never"]

DEFAULT_SEGMENT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Entry:
    """Where a key's newest value lives. This is the whole index."""

    file_id: int
    value_pos: int
    value_len: int
    tstamp_ns: int

    def record_size(self, key_len: int) -> int:
        # Reconstructed rather than stored: the index holds one of these per
        # key, so every field it does not carry is memory saved on a large store.
        return record.HEADER_SIZE + key_len + self.value_len


class Anchor:
    """An open store. Not a context you can nest; one writer at a time."""

    def __init__(self, path: pathlib.Path, *, fsync: FsyncPolicy,
                 max_segment_bytes: int, fsync_every: int):
        self.path = path
        self.fsync_policy: FsyncPolicy = fsync
        self.max_segment_bytes = max_segment_bytes
        self.fsync_every = fsync_every

        self._lock = threading.Lock()
        self._keydir: dict[bytes, Entry] = {}
        self._readers: dict[int, int] = {}      # file_id -> read fd
        self._sizes: dict[int, int] = {}        # file_id -> bytes on disk
        self._active_id = 0
        self._active_fd = -1
        self._active_size = 0
        self._writes_since_sync = 0
        self._dead_bytes = 0
        self._closed = False

    # ------------------------------------------------------------------ open

    @classmethod
    def open(cls, path: str | os.PathLike, *, fsync: FsyncPolicy = "always",
             max_segment_bytes: int = DEFAULT_SEGMENT_BYTES,
             fsync_every: int = 100) -> "Anchor":
        if fsync not in ("always", "batch", "never"):
            raise ValueError(f"fsync must be always, batch or never, not {fsync!r}")

        directory = pathlib.Path(path)
        directory.mkdir(parents=True, exist_ok=True)
        store = cls(directory, fsync=fsync, max_segment_bytes=max_segment_bytes,
                    fsync_every=fsync_every)
        store._recover()
        return store

    def _recover(self) -> None:
        """Rebuild the index from disk, and repair a torn tail if there is one."""
        self._sweep_temporaries()
        ids = self._segment_ids()

        for file_id in ids:
            data_path = self._data_path(file_id)
            is_active = file_id == ids[-1]
            hint_path = self._hint_path(file_id)

            # A closed segment with an intact hint is indexed without ever
            # touching its values. The active segment is always scanned: it is
            # the only file that can end mid-record.
            if not is_active and hint_path.exists() and self._load_hint(file_id, hint_path):
                self._sizes[file_id] = data_path.stat().st_size
            else:
                good_bytes = self._scan_segment(file_id, data_path)
                if is_active and good_bytes < data_path.stat().st_size:
                    # A partial record from an interrupted write. Cutting it off
                    # is the repair; every record before it is whole.
                    os.truncate(data_path, good_bytes)
                self._sizes[file_id] = good_bytes

            self._readers[file_id] = os.open(data_path, os.O_RDONLY)

        self._active_id = ids[-1] if ids else 1
        self._open_active()
        self._recount_dead_bytes()

    def _load_hint(self, file_id: int, hint_path: pathlib.Path) -> bool:
        """Index a segment from its hint file. False means fall back to a scan."""
        buf = hint_path.read_bytes()
        staged: list[tuple[bytes, Entry | None]] = []
        offset = 0
        try:
            while offset < len(buf):
                key, pos, vlen, ts, offset = record.decode_hint(buf, offset)
                staged.append((key, None if vlen is None else Entry(file_id, pos, vlen, ts)))
        except CorruptRecord:
            # A hint truncated by a crash tells us nothing reliable about the
            # rest of the segment, so throw the whole thing away and scan.
            return False

        for key, entry in staged:
            if entry is None:
                self._keydir.pop(key, None)
            else:
                self._keydir[key] = entry
        return True

    def _scan_segment(self, file_id: int, data_path: pathlib.Path) -> int:
        """Walk every record. Returns the offset of the first bad byte."""
        buf = data_path.read_bytes()
        offset = 0
        while offset < len(buf):
            try:
                rec, end = record.decode(buf, offset)
            except CorruptRecord:
                break
            if rec.deleted:
                self._keydir.pop(rec.key, None)
            else:
                value_pos, value_len = record.value_span(offset, len(rec.key), len(rec.value))
                self._keydir[rec.key] = Entry(file_id, value_pos, value_len, rec.tstamp_ns)
            offset = end
        return offset

    def _recount_dead_bytes(self) -> None:
        live = sum(e.record_size(len(k)) for k, e in self._keydir.items())
        self._dead_bytes = max(sum(self._sizes.values()) - live, 0)

    # ----------------------------------------------------------------- reads

    def get(self, key: bytes) -> bytes | None:
        self._require_open()
        entry = self._keydir.get(key)
        if entry is None:
            return None
        if entry.value_len == 0:
            return b""
        # pread, not seek-then-read: it takes the offset as an argument, so
        # concurrent readers cannot move each other's file position.
        data = os.pread(self._readers[entry.file_id], entry.value_len, entry.value_pos)
        if len(data) != entry.value_len:
            raise CorruptRecord(
                f"key {key!r} points at {entry.value_len} bytes in segment "
                f"{entry.file_id} but only {len(data)} could be read"
            )
        return data

    def __contains__(self, key: bytes) -> bool:
        return key in self._keydir

    def __len__(self) -> int:
        return len(self._keydir)

    def keys(self) -> Iterator[bytes]:
        return iter(list(self._keydir))

    def items(self) -> Iterator[tuple[bytes, bytes]]:
        for key in self.keys():
            value = self.get(key)
            if value is not None:
                yield key, value

    # ---------------------------------------------------------------- writes

    def put(self, key: bytes, value: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or not isinstance(value, (bytes, bytearray)):
            raise TypeError("keys and values are bytes; encode text before storing it")
        self._require_open()
        blob = record.encode(bytes(key), bytes(value))
        with self._lock:
            offset = self._append(blob)
            previous = self._keydir.get(key)
            if previous is not None:
                self._dead_bytes += previous.record_size(len(key))
            value_pos, _ = record.value_span(offset, len(key), len(value))
            self._keydir[bytes(key)] = Entry(self._active_id, value_pos, len(value),
                                             _tstamp_of(blob))
            self._maybe_sync()

    def delete(self, key: bytes) -> bool:
        """Append a tombstone. False means the key was not there to begin with."""
        self._require_open()
        if key not in self._keydir:
            return False
        blob = record.encode(bytes(key), None)
        with self._lock:
            self._append(blob)
            previous = self._keydir.pop(bytes(key))
            # Both the value it shadowed and the tombstone itself are waste until
            # the next compaction.
            self._dead_bytes += previous.record_size(len(key)) + len(blob)
            self._maybe_sync()
        return True

    def _append(self, blob: bytes) -> int:
        if self._active_size and self._active_size + len(blob) > self.max_segment_bytes:
            self._rotate()
        offset = self._active_size
        written = os.write(self._active_fd, blob)
        if written != len(blob):
            raise OSError(f"short write: {written} of {len(blob)} bytes")
        self._active_size += written
        self._sizes[self._active_id] = self._active_size
        return offset

    def _maybe_sync(self) -> None:
        self._writes_since_sync += 1
        if self.fsync_policy == "always":
            os.fsync(self._active_fd)
            self._writes_since_sync = 0
        elif self.fsync_policy == "batch" and self._writes_since_sync >= self.fsync_every:
            os.fsync(self._active_fd)
            self._writes_since_sync = 0

    def sync(self) -> None:
        """Force everything written so far to durable storage."""
        self._require_open()
        with self._lock:
            os.fsync(self._active_fd)
            self._writes_since_sync = 0

    # ------------------------------------------------------------- segments

    def _open_active(self) -> None:
        path = self._data_path(self._active_id)
        existed = path.exists()
        self._active_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self._active_size = self._sizes.get(self._active_id, 0)
        if not existed:
            self._sizes[self._active_id] = 0
            self._readers[self._active_id] = os.open(path, os.O_RDONLY)
            # The file's own directory entry needs fsyncing too, or a crash can
            # lose the fact that the file exists at all.
            self._sync_directory()

    def _rotate(self) -> None:
        """Close the active segment and start a new one. Caller holds the lock."""
        os.fsync(self._active_fd)
        os.close(self._active_fd)
        self._write_hint(self._active_id)
        self._active_id += 1
        self._open_active()
        self._writes_since_sync = 0

    def _write_hint(self, file_id: int) -> None:
        """Index a now-immutable segment so reopening it costs only its keys."""
        entries = sorted(
            ((k, e) for k, e in self._keydir.items() if e.file_id == file_id),
            key=lambda pair: pair[1].value_pos,
        )
        blob = b"".join(record.encode_hint(k, e.value_pos, e.value_len, e.tstamp_ns)
                        for k, e in entries)
        _write_file_durably(self._hint_path(file_id), blob)
        self._sync_directory()

    def _segment_ids(self) -> list[int]:
        ids = []
        for entry in self.path.glob(f"*{DATA_SUFFIX}"):
            try:
                ids.append(int(entry.name[:-len(DATA_SUFFIX)]))
            except ValueError:
                continue  # not ours
        return sorted(ids)

    def _sweep_temporaries(self) -> None:
        """Remove half-written compaction output from an interrupted merge."""
        for leftover in self.path.glob(f"*{TMP_SUFFIX}"):
            leftover.unlink(missing_ok=True)

    def _data_path(self, file_id: int) -> pathlib.Path:
        return self.path / f"{file_id:010d}{DATA_SUFFIX}"

    def _hint_path(self, file_id: int) -> pathlib.Path:
        return self.path / f"{file_id:010d}{HINT_SUFFIX}"

    def _sync_directory(self) -> None:
        fd = os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # ------------------------------------------------------------ compaction

    def compact(self) -> dict[str, int]:
        """Rewrite the live set into fresh segments and drop everything else.

        Writers are blocked for the duration. That is the honest trade for an
        embedded single-writer store: the alternative, merging alongside live
        writes, needs a second index and a hand-off protocol, and it is the part
        of an LSM engine where the bugs live.
        """
        self._require_open()
        with self._lock:
            before_bytes = sum(self._sizes.values())
            before_segments = len(self._sizes)

            os.fsync(self._active_fd)
            os.close(self._active_fd)
            self._active_fd = -1
            self._write_hint(self._active_id)
            retired = sorted(self._sizes)

            merged_id = self._active_id + 1
            new_keydir, merged_ids = self._merge_into(merged_id)

            # Only now is it safe to remove the originals: every live value has
            # been written and fsynced somewhere else first.
            for file_id in retired:
                os.close(self._readers.pop(file_id))
                self._data_path(file_id).unlink(missing_ok=True)
                self._hint_path(file_id).unlink(missing_ok=True)
                self._sizes.pop(file_id, None)
            self._sync_directory()

            self._keydir = new_keydir
            self._active_id = (merged_ids[-1] if merged_ids else merged_id) + 1
            self._open_active()
            self._writes_since_sync = 0
            self._recount_dead_bytes()

            after_bytes = sum(self._sizes.values())
            return {
                "reclaimed_bytes": before_bytes - after_bytes,
                "before_bytes": before_bytes,
                "after_bytes": after_bytes,
                "before_segments": before_segments,
                "after_segments": len(self._sizes),
                "keys": len(self._keydir),
            }

    def _merge_into(self, first_id: int) -> tuple[dict[bytes, Entry], list[int]]:
        """Write every live key into new segments. Caller holds the lock.

        Deleted keys are not carried over at all. Their tombstones are dropped
        with them, because every file that could still hold an older value for
        them is about to be removed.
        """
        new_keydir: dict[bytes, Entry] = {}
        written: list[int] = []
        file_id = first_id
        buffer = bytearray()
        hints = bytearray()

        def flush() -> None:
            if not buffer:
                return
            _write_file_durably(self._data_path(file_id), bytes(buffer))
            _write_file_durably(self._hint_path(file_id), bytes(hints))
            self._sizes[file_id] = len(buffer)
            self._readers[file_id] = os.open(self._data_path(file_id), os.O_RDONLY)
            written.append(file_id)

        for key in sorted(self._keydir):
            entry = self._keydir[key]
            value = self.get(key)
            if value is None:
                continue
            blob = record.encode(key, value, entry.tstamp_ns)
            if buffer and len(buffer) + len(blob) > self.max_segment_bytes:
                flush()
                buffer, hints = bytearray(), bytearray()
                file_id += 1
            offset = len(buffer)
            buffer += blob
            value_pos, _ = record.value_span(offset, len(key), len(value))
            hints += record.encode_hint(key, value_pos, len(value), entry.tstamp_ns)
            new_keydir[key] = Entry(file_id, value_pos, len(value), entry.tstamp_ns)

        flush()
        self._sync_directory()
        return new_keydir, written

    # ---------------------------------------------------------------- status

    def stats(self) -> dict[str, float | int]:
        live = sum(e.record_size(len(k)) for k, e in self._keydir.items())
        total = sum(self._sizes.values())
        return {
            "keys": len(self._keydir),
            "segments": len(self._sizes),
            "live_bytes": live,
            "total_bytes": total,
            "dead_bytes": max(total - live, 0),
            # 1.0 means every byte on disk is a byte someone can still read.
            "space_amplification": round(total / live, 3) if live else 0.0,
            "fsync": self.fsync_policy,
        }

    # ----------------------------------------------------------------- close

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            os.fsync(self._active_fd)
            os.close(self._active_fd)
            for fd in self._readers.values():
                os.close(fd)
            self._readers.clear()
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise ValueError("store is closed")

    def __enter__(self) -> "Anchor":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"{len(self._keydir)} keys"
        return f"<Anchor {self.path} {state} fsync={self.fsync_policy}>"


def _tstamp_of(blob: bytes) -> int:
    _crc, tstamp_ns, _klen, _vlen = record.HEADER.unpack_from(blob, 0)
    return tstamp_ns


def _write_file_durably(path: pathlib.Path, blob: bytes) -> None:
    """Write, fsync, then rename. A reader never sees a half-written file."""
    scratch = path.with_name(path.name + TMP_SUFFIX)
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, blob)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(scratch, path)
