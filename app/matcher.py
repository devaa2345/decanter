"""
3-layer perfume matching pipeline.

Layer 1: Normalize + exact/substring keyword match (free, instant)
Layer 2: Fuzzy string match via rapidfuzz (free, fast)
Layer 3: Groq LLM classification (paid, slow — last resort)

All prices come from catalog.py, NEVER from the LLM.
"""

import logging
import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.catalog import PERFUMES
from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """Result of the matching pipeline."""

    perfume_id: str | None = None
    layer: str | None = None       # "exact", "fuzzy", "llm", or None
    confidence: float | None = None
    ambiguous: bool = False
    matched_keyword: str | None = None


# Common English words that appear as catalog keywords but are too
# generic to trigger a match on their own.  Without this filter,
# "good morning" matches "Lazy Sunday Morning" and "order kab aayega"
# matches "Mark & Victor Eau De Flora" (which has "order" as a keyword).
# Multi-word keywords containing these words (e.g. "sunday morning",
# "night out") still work fine — only single-word keywords are skipped.
GENERIC_STOPWORDS: set[str] = {
    "amount", "best", "blue", "day", "de", "eau", "flora", "good",
    "hello", "home", "just", "king", "know", "last", "lazy", "light",
    "long", "love", "man", "mark", "morning", "night", "note", "old",
    "one", "open", "order", "play", "pure", "rain", "red", "rich",
    "rose", "royal", "rush", "show", "silk", "star", "story", "sunday",
    "that", "the", "this", "touch", "tree", "very", "want", "warm",
    "what", "wild", "wood", "your",
}


def normalize_message(text: str) -> str:
    """Lowercase, strip punctuation/extra whitespace."""
    text = text.lower().strip()
    # Remove common punctuation but keep spaces and alphanumeric
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _layer1_exact_match(normalized: str) -> MatchResult:
    """
    Layer 1: Check if any perfume's keyword is a substring of the message.

    If multiple perfumes match, prefer the longest/most-specific keyword.
    Only return ambiguous if the customer clearly mentions 2+ distinct
    perfumes with similar specificity — NOT when a short keyword
    accidentally matches multiple entries.
    """
    matches: list[tuple[str, str, int]] = []  # (perfume_id, keyword, keyword_len)

    for pid, data in PERFUMES.items():
        for keyword in data["keywords"]:
            # Only consider keywords that are at least 3 chars
            if len(keyword) < 3:
                continue
            # Skip single-word generic keywords (e.g. "order", "morning")
            if " " not in keyword and keyword in GENERIC_STOPWORDS:
                continue
            if keyword in normalized:
                matches.append((pid, keyword, len(keyword)))

    if not matches:
        return MatchResult()

    # Sort by keyword length descending (most specific first)
    matches.sort(key=lambda x: x[2], reverse=True)

    # Deduplicate by perfume_id, keeping the longest keyword for each
    seen_pids: dict[str, tuple[str, int]] = {}
    for pid, kw, kw_len in matches:
        if pid not in seen_pids or kw_len > seen_pids[pid][1]:
            seen_pids[pid] = (kw, kw_len)

    unique_pids = list(seen_pids.keys())

    if len(unique_pids) == 1:
        pid = unique_pids[0]
        kw, _ = seen_pids[pid]
        return MatchResult(
            perfume_id=pid,
            layer="exact",
            confidence=100.0,
            matched_keyword=kw,
        )

    # Multiple perfumes matched — always pick the longest keyword match.
    # This resolves most cases where a generic word like "sauvage" matches
    # multiple clones, but the specific fragrance name is longer.
    # Sort unique_pids by their best keyword length descending.
    sorted_pids = sorted(unique_pids, key=lambda p: seen_pids[p][1], reverse=True)

    best_pid = sorted_pids[0]
    best_kw, best_len = seen_pids[best_pid]
    second_pid = sorted_pids[1]
    _, second_len = seen_pids[second_pid]

    # If the best match has a significantly longer keyword, it's the clear winner
    if best_len > second_len:
        return MatchResult(
            perfume_id=best_pid,
            layer="exact",
            confidence=95.0,
            matched_keyword=best_kw,
        )

    # Keywords are the same length — check if the customer genuinely
    # mentioned two distinct perfume names (not just two entries that
    # share a common generic word)
    # If both keywords are the same string, it's a shared-keyword collision
    second_kw, _ = seen_pids[second_pid]
    if best_kw == second_kw:
        # Same keyword matched multiple perfumes — just pick the first
        # (this is an ambiguity in the catalog, not in the customer message)
        return MatchResult(
            perfume_id=best_pid,
            layer="exact",
            confidence=85.0,
            matched_keyword=best_kw,
        )

    # Different keywords of similar length — likely the customer genuinely
    # mentioned two different perfumes
    return MatchResult(ambiguous=True)





def _layer2_fuzzy_match(normalized: str) -> MatchResult:
    """
    Layer 2: Fuzzy string matching against known keywords.

    Extracts individual words and sliding n-grams (2-word, 3-word) from
    the message, compares each against all keywords using partial_ratio.
    """
    threshold = settings.FUZZY_THRESHOLD

    # Build candidate phrases from the message
    words = normalized.split()
    candidates: list[str] = list(words)

    # Add 2-word and 3-word sliding windows
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            candidates.append(" ".join(words[i : i + n]))

    best_score = 0.0
    best_pid: str | None = None
    best_keyword: str | None = None
    second_best_score = 0.0
    second_best_pid: str | None = None

    for pid, data in PERFUMES.items():
        for keyword in data["keywords"]:
            # Skip very short keywords for fuzzy matching (too noisy)
            if len(keyword) < 4:
                continue
            # Skip single-word keywords that are common English words
            if " " not in keyword and keyword in GENERIC_STOPWORDS:
                continue
            for candidate in candidates:
                if len(candidate) < 4:
                    continue
                # Skip single-word candidates that are stopwords
                if " " not in candidate and candidate in GENERIC_STOPWORDS:
                    continue
                # Use fuzz.ratio (full string similarity) — NOT partial_ratio
                # which is too loose with 1200+ catalog entries and causes
                # false positives like "morning" matching "lazy sunday morning"
                score = fuzz.ratio(candidate, keyword)

                if score > best_score:
                    second_best_score = best_score
                    second_best_pid = best_pid
                    best_score = score
                    best_pid = pid
                    best_keyword = keyword
                elif score > second_best_score and pid != best_pid:
                    second_best_score = score
                    second_best_pid = pid

    if best_score < threshold or best_pid is None:
        return MatchResult()

    # Even if there's a close runner-up, pick the best match.
    # With 1200+ perfumes, fuzzy score ties are common and don't
    # mean the customer genuinely mentioned two different perfumes.
    # Ambiguity detection is handled better at Layer 1 (exact match).

    return MatchResult(
        perfume_id=best_pid,
        layer="fuzzy",
        confidence=best_score,
        matched_keyword=best_keyword,
    )


async def _layer3_llm_match(normalized: str) -> MatchResult:
    """
    Layer 3: Groq LLM classification (only called when Layers 1 & 2 fail).

    Asks the LLM to identify which perfume_id the message refers to.
    Returns only a known ID or None — never generates prices or reply text.
    """
    from app.groq_client import classify_perfume

    try:
        result = await classify_perfume(normalized)
        if result and result in PERFUMES:
            return MatchResult(
                perfume_id=result,
                layer="llm",
                confidence=80.0,
            )
    except Exception:
        logger.exception("Layer 3 LLM classification failed")

    return MatchResult()


async def match_perfume(message_text: str) -> MatchResult:
    """
    Run the full 3-layer matching pipeline.

    Returns a MatchResult indicating the match (or lack thereof).
    """
    normalized = normalize_message(message_text)

    if not normalized:
        return MatchResult()

    # Layer 1: Exact/substring match
    result = _layer1_exact_match(normalized)
    if result.perfume_id or result.ambiguous:
        logger.info(
            "Layer 1 match: pid=%s, keyword=%s, ambiguous=%s",
            result.perfume_id,
            result.matched_keyword,
            result.ambiguous,
        )
        return result

    # Layer 2: Fuzzy match
    result = _layer2_fuzzy_match(normalized)
    if result.perfume_id or result.ambiguous:
        logger.info(
            "Layer 2 fuzzy match: pid=%s, keyword=%s, score=%.1f, ambiguous=%s",
            result.perfume_id,
            result.matched_keyword,
            result.confidence or 0,
            result.ambiguous,
        )
        return result

    # Layer 3: LLM classification (async)
    result = await _layer3_llm_match(normalized)
    if result.perfume_id:
        logger.info("Layer 3 LLM match: pid=%s", result.perfume_id)
        return result

    # No match found across all layers
    logger.info("No match found for message: %s", message_text[:100])
    return MatchResult()
