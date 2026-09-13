"""The record format. Everything above it assumes these properties hold."""

from __future__ import annotations

import struct

import pytest

from anchor import record
from anchor.record import CorruptRecord


def test_a_record_survives_a_round_trip():
    blob = record.encode(b"key", b"value")
    rec, end = record.decode(blob)
    assert rec.key == b"key"
    assert rec.value == b"value"
    assert end == len(blob)
    assert rec.tstamp_ns > 0


def test_records_pack_back_to_back():
    blob = record.encode(b"a", b"1") + record.encode(b"bb", b"22") + record.encode(b"ccc", b"333")
    seen, offset = [], 0
    while offset < len(blob):
        rec, offset = record.decode(blob, offset)
        seen.append((rec.key, rec.value))
    assert seen == [(b"a", b"1"), (b"bb", b"22"), (b"ccc", b"333")]


def test_a_tombstone_decodes_as_a_deletion():
    rec, _ = record.decode(record.encode(b"gone", None))
    assert rec.deleted is True
    assert rec.value is None


def test_an_empty_value_is_not_a_deletion():
    rec, _ = record.decode(record.encode(b"present", b""))
    assert rec.deleted is False
    assert rec.value == b""


def test_binary_keys_and_values_are_safe():
    key, value = bytes(range(256)), b"\x00\xff\n\r\x00"
    rec, _ = record.decode(record.encode(key, value))
    assert (rec.key, rec.value) == (key, value)


def test_a_flipped_bit_is_caught():
    blob = bytearray(record.encode(b"key", b"value"))
    blob[-1] ^= 0x01
    with pytest.raises(CorruptRecord, match="checksum"):
        record.decode(bytes(blob))


def test_a_corrupted_header_is_caught():
    blob = bytearray(record.encode(b"key", b"value"))
    blob[8] ^= 0xFF  # inside the timestamp, which the checksum covers
    with pytest.raises(CorruptRecord):
        record.decode(bytes(blob))


@pytest.mark.parametrize("cut", [1, 5, 12, 19, 20, 22])
def test_a_truncated_record_is_caught_at_every_cut(cut):
    """The torn tail a killed process leaves behind, at each place it can tear."""
    blob = record.encode(b"key", b"value")
    with pytest.raises(CorruptRecord):
        record.decode(blob[:cut])


def test_a_length_field_that_lies_does_not_read_past_the_buffer():
    blob = bytearray(record.encode(b"key", b"value"))
    struct.pack_into(">I", blob, 16, 1_000_000)  # value length
    with pytest.raises(CorruptRecord):
        record.decode(bytes(blob))


def test_an_oversized_value_is_refused_before_it_is_written():
    class Pretend(bytes):
        def __len__(self):
            return record.MAX_VALUE_SIZE + 1

    with pytest.raises(ValueError, match="exceeds"):
        record.encode(b"key", Pretend())


# ------------------------------------------------------------------- hints


def test_a_hint_survives_a_round_trip():
    blob = record.encode_hint(b"key", 128, 64, 1234)
    key, pos, vlen, ts, end = record.decode_hint(blob)
    assert (key, pos, vlen, ts) == (b"key", 128, 64, 1234)
    assert end == len(blob)


def test_a_hint_can_carry_a_deletion():
    _key, _pos, vlen, _ts, _end = record.decode_hint(record.encode_hint(b"gone", 0, None, 1))
    assert vlen is None


def test_a_damaged_hint_is_caught():
    blob = bytearray(record.encode_hint(b"key", 8, 4, 99))
    blob[-1] ^= 0x40
    with pytest.raises(CorruptRecord):
        record.decode_hint(bytes(blob))
