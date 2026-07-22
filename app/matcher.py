"""
3-layer perfume matching pipeline.

Layer 1: Normalize + exact/substring keyword match (free, instant)
Layer 2: Fuzzy string match via rapidfuzz (free, fast)
Layer 3: Groq LLM classification (paid, slow — last resort)

All prices come from catalog.py, NEVER from the LLM.
"""

import functools
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
    # Populated only when ambiguous=True — the actual candidate perfume_ids,
    # so the reply can list them instead of just asking "which one?" with
    # no information to go on.
    matched_perfume_ids: list[str] | None = None


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

# Family/series terms that are genuinely ambiguous when mentioned alone,
# with no distinguishing word — see the "not matches" branch in
# _layer1_exact_match for the confirmed "9pm" case.
_AMBIGUOUS_FAMILY_TERMS: tuple[str, ...] = ("9pm", "9 pm")


def normalize_message(text: str) -> str:
    """Lowercase, strip punctuation/extra whitespace."""
    text = text.lower().strip()
    # Remove common punctuation but keep spaces and alphanumeric
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


@functools.lru_cache(maxsize=None)
def _keyword_boundary_regex(keyword: str) -> re.Pattern:
    """
    Cached compiled word-boundary pattern for a keyword. Word-boundary, not
    raw substring: confirmed in production that plain `keyword in normalized`
    lets a keyword match *inside* an unrelated word — the typo "fregrance"
    contains "egra" (a real keyword for an unrelated perfume, "Rasasi Egra")
    and matched it. `\\b` ensures a keyword only matches as a whole word (or
    whole phrase, for multi-word keywords), never embedded in a longer one.
    """
    return re.compile(r"\b" + re.escape(keyword) + r"\b")


def _layer1_exact_match(normalized: str) -> MatchResult:
    """
    Layer 1: Check if any perfume's keyword appears as a whole word/phrase
    in the message (word-boundary match, not raw substring — see
    _keyword_boundary_regex).

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
            if _keyword_boundary_regex(keyword).search(normalized):
                matches.append((pid, keyword, len(keyword)))

    if not matches:
        # "9pm" alone (no distinguishing word) is genuinely ambiguous among
        # 4 different Afnan products — Rebel, Night Out, Elixir, and the
        # base "Afnan 9PM" — confirmed in production ("9pm fragrance" got
        # no match at all). None of them has a bare "9pm" keyword by
        # design: adding one would hit the "same keyword -> pick first"
        # branch below and silently guess one, risking the wrong price. If
        # the message actually contained one of their real distinguishing
        # keywords ("rebel", "night out", "9pm elixir", "afnan 9pm", ...),
        # `matches` wouldn't be empty here at all — this only fires for the
        # genuinely ambiguous bare mention. Scoped to this one reported,
        # confirmed case rather than guessing at other series names across
        # 1200+ perfumes without real customer reports to ground them.
        if any(
            _keyword_boundary_regex(term).search(normalized)
            for term in _AMBIGUOUS_FAMILY_TERMS
        ):
            family_pids = [
                pid
                for pid, data in PERFUMES.items()
                if any(
                    _keyword_boundary_regex(term).search(kw)
                    for kw in data["keywords"]
                    for term in _AMBIGUOUS_FAMILY_TERMS
                )
            ]
            return MatchResult(ambiguous=True, matched_perfume_ids=family_pids)
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
    # mentioned two different perfumes. List every pid tied for the longest
    # keyword — those are the genuine competing candidates; anything with a
    # strictly shorter keyword was already outranked and isn't a real option.
    tied_pids = [pid for pid in sorted_pids if seen_pids[pid][1] == best_len]
    return MatchResult(ambiguous=True, matched_perfume_ids=tied_pids)





def _build_ngram_candidates(normalized: str) -> list[str]:
    """
    Words plus 2-word and 3-word sliding windows from the message — shared
    phrase-extraction logic used by both fuzzy matching (Layer 2) and LLM
    candidate shortlisting (Layer 3).
    """
    words = normalized.split()
    candidates: list[str] = list(words)

    for n in (2, 3):
        for i in range(len(words) - n + 1):
            candidates.append(" ".join(words[i : i + n]))

    return candidates


def _layer2_fuzzy_match(normalized: str) -> MatchResult:
    """
    Layer 2: Fuzzy string matching against known keywords.

    Extracts individual words and sliding n-grams (2-word, 3-word) from
    the message, compares each against all keywords using partial_ratio.
    """
    threshold = settings.FUZZY_THRESHOLD
    candidates = _build_ngram_candidates(normalized)

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


def _top_candidates_for_llm(normalized: str, limit: int = 25) -> dict[str, dict]:
    """
    Build a small shortlist of the most plausible perfumes for Layer 3 to
    choose from, instead of handing the LLM the entire catalog.

    With 1200+ perfumes, listing all of them in the prompt runs to ~23K
    tokens — confirmed in production to blow straight through Groq's
    per-minute token limit (6000 TPM on the current tier), failing Layer 3
    on every single call, not just occasionally. Reuses the same word/n-gram
    fuzzy scoring as Layer 2, but ranks by each perfume's BEST keyword score
    rather than picking one overall winner, so a reasonably wide (but still
    small and cheap) net gets cast for the LLM to reason over.
    """
    candidates = _build_ngram_candidates(normalized)

    best_score_per_pid: dict[str, float] = {}
    for pid, data in PERFUMES.items():
        for keyword in data["keywords"]:
            if len(keyword) < 4:
                continue
            if " " not in keyword and keyword in GENERIC_STOPWORDS:
                continue
            for candidate in candidates:
                if len(candidate) < 4:
                    continue
                if " " not in candidate and candidate in GENERIC_STOPWORDS:
                    continue
                score = fuzz.ratio(candidate, keyword)
                if score > best_score_per_pid.get(pid, -1.0):
                    best_score_per_pid[pid] = score

    ranked = sorted(best_score_per_pid.items(), key=lambda kv: kv[1], reverse=True)
    top_pids = [pid for pid, _ in ranked[:limit]]
    return {pid: PERFUMES[pid] for pid in top_pids}


async def _layer3_llm_match(normalized: str) -> MatchResult:
    """
    Layer 3: Groq LLM classification (only called when Layers 1 & 2 fail).

    Asks the LLM to identify which perfume_id the message refers to, choosing
    only from a pre-narrowed shortlist (see _top_candidates_for_llm) — never
    the full catalog. Returns only a known ID or None — never generates
    prices or reply text.
    """
    from app.groq_client import classify_perfume

    candidates = _top_candidates_for_llm(normalized)
    if not candidates:
        return MatchResult()

    try:
        result = await classify_perfume(normalized, candidates=candidates)
        if result and result in candidates:
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
