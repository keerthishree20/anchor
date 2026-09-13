"""The on-disk record format.

    ┌──────────┬────────────┬─────────┬───────────┬─────┬───────┐
    │ crc32  4 │ tstamp_ns 8│ klen  4 │ vlen    4 │ key │ value │
    └──────────┴────────────┴─────────┴───────────┴─────┴───────┘
    └── covered by the crc ────────────────────────────────────┘

Big-endian throughout, so a hex dump of a file reads left to right in the same
order as this diagram.

The crc is what makes a torn tail recoverable rather than fatal. A process
killed mid-write leaves a partial record at the end of the log; on reopen the
scan reaches it, the checksum fails or the record runs past end of file, and
recovery stops there. Everything before it is intact by construction, because
records are only ever appended and never rewritten in place.
"""

from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass

HEADER = struct.Struct(">IQII")
HEADER_SIZE = HEADER.size  # 20

#: A value length of 2**32-1 marks a deletion. Real values are capped just below
#: it, which is far past any value this store is meant to hold.
TOMBSTONE = 0xFFFFFFFF
MAX_VALUE_SIZE = TOMBSTONE - 1
MAX_KEY_SIZE = 0xFFFFFFFF


class CorruptRecord(Exception):
    """The bytes at this offset are not a record this store wrote intact."""


@dataclass(frozen=True)
class Record:
    key: bytes
    value: bytes | None  # None is a tombstone
    tstamp_ns: int

    @property
    def deleted(self) -> bool:
        return self.value is None


def encode(key: bytes, value: bytes | None, tstamp_ns: int | None = None) -> bytes:
    if len(key) > MAX_KEY_SIZE:
        raise ValueError(f"key of {len(key)} bytes exceeds the {MAX_KEY_SIZE} byte limit")
    if value is not None and len(value) > MAX_VALUE_SIZE:
        raise ValueError(f"value of {len(value)} bytes exceeds the {MAX_VALUE_SIZE} byte limit")

    tstamp_ns = time.time_ns() if tstamp_ns is None else tstamp_ns
    vlen = TOMBSTONE if value is None else len(value)
    body = HEADER.pack(0, tstamp_ns, len(key), vlen)[4:] + key + (value or b"")
    return struct.pack(">I", zlib.crc32(body)) + body


def decode(buf: bytes, offset: int = 0) -> tuple[Record, int]:
    """Read one record. Returns it and the offset just past it.

    Raises CorruptRecord for a truncated or checksum-failing record, which the
    recovery scan treats as the end of usable data rather than as an error.
    """
    if offset + HEADER_SIZE > len(buf):
        raise CorruptRecord(f"header runs past end of data at offset {offset}")

    crc, tstamp_ns, klen, vlen = HEADER.unpack_from(buf, offset)
    deleted = vlen == TOMBSTONE
    payload = 0 if deleted else vlen
    end = offset + HEADER_SIZE + klen + payload
    if end > len(buf):
        raise CorruptRecord(
            f"record at offset {offset} claims {klen + payload} bytes of payload "
            f"but only {len(buf) - offset - HEADER_SIZE} remain"
        )

    body = buf[offset + 4:end]
    if zlib.crc32(body) != crc:
        raise CorruptRecord(f"checksum mismatch at offset {offset}")

    key_at = offset + HEADER_SIZE
    key = buf[key_at:key_at + klen]
    value = None if deleted else buf[key_at + klen:end]
    return Record(key=key, value=value, tstamp_ns=tstamp_ns), end


def value_span(offset: int, klen: int, vlen: int) -> tuple[int, int]:
    """Where a record's value sits, for a read that skips the key entirely."""
    return offset + HEADER_SIZE + klen, vlen


# --------------------------------------------------------------------- hints
#
# A hint file is the index of a closed segment with the values left out. Reopen
# reads hints instead of scanning whole data files, which is the difference
# between recovery costing the size of the data and costing the size of the
# keys. Only closed segments get one: the active segment can have a torn tail,
# and a hint would claim it does not.

HINT = struct.Struct(">IQIQI")
HINT_HEADER_SIZE = HINT.size  # 28


def encode_hint(key: bytes, value_pos: int, value_len: int | None, tstamp_ns: int) -> bytes:
    vlen = TOMBSTONE if value_len is None else value_len
    body = HINT.pack(0, tstamp_ns, len(key), value_pos, vlen)[4:] + key
    return struct.pack(">I", zlib.crc32(body)) + body


def decode_hint(buf: bytes, offset: int = 0) -> tuple[bytes, int, int | None, int, int]:
    """Returns (key, value_pos, value_len or None, tstamp_ns, next_offset)."""
    if offset + HINT_HEADER_SIZE > len(buf):
        raise CorruptRecord(f"hint header runs past end of data at offset {offset}")

    crc, tstamp_ns, klen, value_pos, vlen = HINT.unpack_from(buf, offset)
    end = offset + HINT_HEADER_SIZE + klen
    if end > len(buf):
        raise CorruptRecord(f"hint at offset {offset} is truncated")

    body = buf[offset + 4:end]
    if zlib.crc32(body) != crc:
        raise CorruptRecord(f"hint checksum mismatch at offset {offset}")

    key = buf[offset + HINT_HEADER_SIZE:end]
    return key, value_pos, (None if vlen == TOMBSTONE else vlen), tstamp_ns, end
