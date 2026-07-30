"""
Unit tests for the in-memory message-ID dedup cache (app/dedup.py).

Time-based behavior (TTL expiry, LRU max-size eviction) is tested by
directly manipulating the cache's internal timestamps rather than real
sleeping — deterministic and fast.
"""

import time

from app.dedup import DedupCache


class TestBasicDuplicateDetection:
    def test_new_id_is_not_a_duplicate(self):
        cache = DedupCache(ttl_seconds=300, max_size=100)
        assert cache.is_duplicate("msg1") is False

    def test_same_id_seen_again_is_a_duplicate(self):
        cache = DedupCache(ttl_seconds=300, max_size=100)
        cache.is_duplicate("msg1")
        assert cache.is_duplicate("msg1") is True

    def test_different_ids_are_independent(self):
        cache = DedupCache(ttl_seconds=300, max_size=100)
        cache.is_duplicate("msg1")
        assert cache.is_duplicate("msg2") is False

    def test_size_reflects_entry_count(self):
        cache = DedupCache(ttl_seconds=300, max_size=100)
        cache.is_duplicate("a")
        cache.is_duplicate("b")
        cache.is_duplicate("a")  # duplicate, shouldn't add a second entry
        assert cache.size == 2

    def test_clear_empties_the_cache(self):
        cache = DedupCache(ttl_seconds=300, max_size=100)
        cache.is_duplicate("msg1")
        cache.clear()
        assert cache.size == 0
        assert cache.is_duplicate("msg1") is False


class TestTTLExpiry:
    def test_expired_entry_is_treated_as_new(self):
        cache = DedupCache(ttl_seconds=100, max_size=100)
        cache.is_duplicate("msg1")

        # Backdate it past the TTL window without real sleeping.
        cache._cache["msg1"] = time.time() - 200

        assert cache.is_duplicate("msg1") is False

    def test_expired_entry_is_actually_evicted_not_just_bypassed(self):
        cache = DedupCache(ttl_seconds=100, max_size=100)
        cache.is_duplicate("msg1")
        cache._cache["msg1"] = time.time() - 200

        cache.is_duplicate("msg1")  # re-adds it fresh
        assert cache.size == 1

    def test_non_expired_entry_survives_eviction_pass(self):
        cache = DedupCache(ttl_seconds=100, max_size=100)
        cache.is_duplicate("old_but_valid")
        cache.is_duplicate("fresh")

        # Trigger an eviction pass (via any is_duplicate call) — neither
        # entry is old enough to be evicted.
        cache.is_duplicate("trigger")
        assert cache.size == 3

    def test_repeated_sightings_do_not_refresh_position_or_timestamp(self):
        """_evict_expired only pops from the front and stops at the first
        non-expired entry — correct only because insertion order always
        tracks timestamp order, which in turn holds only because re-seeing
        an already-known id never moves it or updates its timestamp. This
        guards the invariant the eviction logic depends on."""
        cache = DedupCache(ttl_seconds=10_000, max_size=100)
        cache.is_duplicate("first")
        original_ts = cache._cache["first"]
        cache.is_duplicate("second")

        cache.is_duplicate("first")  # a duplicate sighting, not a fresh add

        assert cache._cache["first"] == original_ts
        assert list(cache._cache.keys()) == ["first", "second"]


class TestMaxSizeEviction:
    def test_oldest_entry_evicted_when_over_capacity(self):
        cache = DedupCache(ttl_seconds=10_000, max_size=3)
        cache.is_duplicate("a")
        cache.is_duplicate("b")
        cache.is_duplicate("c")
        cache.is_duplicate("d")  # pushes size to 4, over the cap

        assert cache.size == 3
        assert cache.is_duplicate("a") is False  # evicted, treated as new
        assert cache.is_duplicate("d") is True  # still present

    def test_capacity_never_exceeded_across_many_inserts(self):
        cache = DedupCache(ttl_seconds=10_000, max_size=5)
        for i in range(50):
            cache.is_duplicate(f"msg{i}")
        assert cache.size == 5

    def test_most_recent_entries_survive(self):
        cache = DedupCache(ttl_seconds=10_000, max_size=3)
        for i in range(5):
            cache.is_duplicate(f"msg{i}")
        # Only the last 3 (msg2, msg3, msg4) should remain. Check via
        # membership, not is_duplicate() — is_duplicate() itself adds
        # missing keys as a side effect, which would corrupt the very
        # state this test is trying to inspect.
        assert list(cache._cache.keys()) == ["msg2", "msg3", "msg4"]


class TestDefaultsFromSettings:
    def test_constructs_with_settings_defaults_when_unspecified(self):
        from app.config import settings

        cache = DedupCache()
        assert cache._ttl == settings.DEDUP_TTL_SECONDS
        assert cache._max_size == settings.DEDUP_MAX_SIZE
