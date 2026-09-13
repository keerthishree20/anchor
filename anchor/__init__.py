"""Anchor: an append-only key-value store with crash recovery.

    from anchor import Anchor

    with Anchor.open("./data") as db:
        db.put(b"greeting", b"hello")
        db.get(b"greeting")     # b"hello"
        db.delete(b"greeting")
        db.compact()
"""

from .record import CorruptRecord, Record
from .store import Anchor, Entry, FsyncPolicy

__version__ = "1.0.0"
__all__ = ["Anchor", "Entry", "Record", "CorruptRecord", "FsyncPolicy"]
