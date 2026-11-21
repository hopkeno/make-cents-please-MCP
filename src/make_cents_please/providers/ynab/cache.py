"""In-memory response cache with YNAB delta (server_knowledge) merging.

Nothing here touches disk: the cache lives for the lifetime of the server process.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CacheEntry:
    data: dict[str, Any]
    fetched_at: float
    server_knowledge: int | None = None
    wall_time: float = field(default_factory=time.time)


MAX_ENTRIES = 256


class ResponseCache:
    def __init__(
        self,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._entries: dict[Hashable, CacheEntry] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> CacheEntry | None:
        with self._lock:
            return self._entries.get(key)

    def is_fresh(self, entry: CacheEntry) -> bool:
        return (self._clock() - entry.fetched_at) < self.ttl_seconds

    def put(
        self, key: Hashable, data: dict[str, Any], server_knowledge: int | None = None
    ) -> CacheEntry:
        entry = CacheEntry(data=data, fetched_at=self._clock(), server_knowledge=server_knowledge)
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = entry
            # Dicts keep insertion order, so the first key is the least recently stored.
            while len(self._entries) > self.max_entries:
                del self._entries[next(iter(self._entries))]
        return entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _key_of(item: dict[str, Any]) -> Any:
    # Months are identified by their "month" date; everything else has an "id".
    return item.get("id", item.get("month"))


def merge_entities(
    existing: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    nested: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Merge a delta list into an existing list by id.

    Changed entities replace existing ones; entities flagged ``deleted`` are removed.
    ``nested`` maps a child-list field (e.g. ``"categories"``) to merge recursively,
    because YNAB deltas for category groups only include the changed categories.
    """
    nested = nested or {}
    merged: dict[Any, dict[str, Any]] = {_key_of(item): item for item in existing}
    for change in changes:
        key = _key_of(change)
        if change.get("deleted"):
            merged.pop(key, None)
            continue
        previous = merged.get(key)
        if previous is not None:
            change = dict(change)
            for child_field in nested:
                if child_field in change and child_field in previous:
                    change[child_field] = merge_entities(
                        previous[child_field] or [], change[child_field] or []
                    )
        merged[key] = change
    return list(merged.values())
