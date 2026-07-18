"""
In-memory message-ID deduplication cache with TTL.

Prevents replying twice to the same inbound message when AiSensy
retries webhook delivery. Ephemeral by design — Render restarts
clear this, which is fine (worst case: one duplicate reply on redeploy).
"""

import time
from collections import OrderedDict

from app.config import settings


class DedupCache:
    """Thread-safe-ish ordered dict with TTL eviction and max-size cap."""

    def __init__(
        self,
        ttl_seconds: int | None = None,
        max_size: int | None = None,
    ):
        self._ttl = ttl_seconds or settings.DEDUP_TTL_SECONDS
        self._max_size = max_size or settings.DEDUP_MAX_SIZE
        self._cache: OrderedDict[str, float] = OrderedDict()

    def _evict_expired(self) -> None:
        """Remove entries older than TTL."""
        now = time.time()
        cutoff = now - self._ttl
        # Pop from the front (oldest) until we hit a non-expired entry
        while self._cache:
            key, ts = next(iter(self._cache.items()))
            if ts < cutoff:
                self._cache.pop(key)
            else:
                break

    def _enforce_max_size(self) -> None:
        """Evict oldest entries if cache exceeds max size."""
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def is_duplicate(self, message_id: str) -> bool:
        """
        Check if a message ID has been seen recently.

        Returns True if already in cache (duplicate).
        Returns False and adds to cache if new.
        """
        self._evict_expired()

        if message_id in self._cache:
            return True

        self._cache[message_id] = time.time()
        self._enforce_max_size()
        return False

    def clear(self) -> None:
        """Clear the entire cache (for testing)."""
        self._cache.clear()

    @property
    def size(self) -> int:
        """Current number of entries in cache."""
        return len(self._cache)


# Singleton instance
dedup_cache = DedupCache()
