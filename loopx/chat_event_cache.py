"""Resident rows of a chat Turn's event log, with bounded finished retention.

A chat Turn's event log is written once and then replayed: the SSE endpoint
re-reads the same rows on every reconnect, and the Turn stops being written the
moment its terminal event lands. Two obligations meet here.

- Replaying a finished Turn must not re-read its log per request, so the rows a
  reader just read stay resident.
- Retention must stay bounded, so only the most recent finished Turns stay
  resident and anything older falls out of the budget and is read once more.

The store keeps file I/O, sequence ordering and the file lock; this module owns
which rows are resident and which finished Turns fall out of the budget, so both
obligations are stated once instead of being inferred from a cache miss.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Sequence


TERMINAL_EVENT_CACHE_TURNS = 8

EventKey = tuple[str, str]
EventRevision = tuple[int, int, int] | None


def event_revision(path: Path) -> EventRevision:
    """Identify one event log's on-disk revision, or ``None`` when it is absent."""

    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


class ChatEventCache:
    """Rows of chat Turns that stay resident, bounding finished Turns.

    ``rows`` and ``revisions`` stay public mappings: a caller legitimately reads
    and seeds them, and an operator surface can count resident Turns without a
    second accessor.
    """

    def __init__(
        self,
        *,
        lock: threading.RLock,
        budget: int = TERMINAL_EVENT_CACHE_TURNS,
    ) -> None:
        self.rows: dict[EventKey, list[dict[str, Any]]] = {}
        self.revisions: dict[EventKey, EventRevision] = {}
        self._lock = lock
        self._budget = max(1, int(budget))
        self._finished: dict[EventKey, None] = {}

    def get(self, key: EventKey, path: Path) -> list[dict[str, Any]] | None:
        """Return resident rows only while they still match the log on disk."""

        revision = event_revision(path)
        with self._lock:
            rows = self.rows.get(key)
            if rows is not None and self.revisions.get(key) == revision:
                return rows
        return None

    def put(self, key: EventKey, rows: list[dict[str, Any]], path: Path) -> None:
        with self._lock:
            self.rows[key] = rows
            self.revisions[key] = event_revision(path)

    def load(
        self,
        key: EventKey,
        path: Path,
        read: Callable[[Path], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Return one Turn's rows, reading the log only when it cannot be served."""

        cached = self.get(key, path)
        if cached is not None:
            return cached
        rows = read(path)
        self.put(key, rows, path)
        return rows

    def drop(self, key: EventKey) -> None:
        with self._lock:
            self.rows.pop(key, None)
            self.revisions.pop(key, None)
            self._finished.pop(key, None)

    def retain_finished(
        self,
        key: EventKey,
        rows: Sequence[dict[str, Any]],
        *,
        terminal_kinds: frozenset[str],
    ) -> tuple[EventKey, ...]:
        """Keep a finished Turn resident and evict past the budget.

        Returns the keys that left the cache, so a caller can report or audit the
        eviction instead of guessing which Turn a later read paid for. Only the
        last row is read: a replay must not walk the rows it is about to skip.
        """

        if not rows or rows[-1].get("kind") not in terminal_kinds:
            return ()
        with self._lock:
            self._finished.pop(key, None)
            self._finished[key] = None
            evicted: list[EventKey] = []
            while len(self._finished) > self._budget:
                oldest = next(iter(self._finished))
                self._finished.pop(oldest, None)
                self.rows.pop(oldest, None)
                self.revisions.pop(oldest, None)
                evicted.append(oldest)
        return tuple(evicted)

    def resident_finished_keys(self) -> tuple[EventKey, ...]:
        with self._lock:
            return tuple(self._finished)
