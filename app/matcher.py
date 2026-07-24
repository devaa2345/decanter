"""
Perfume matching pipeline — Groq LLM first, deterministic fallback.

Primary: Groq LLM classification + reply phrasing (llama-3.1-8b-instant —
paid, but fast; ~few hundred ms typical). Tried first for every message.
Fallback (whenever Groq didn't return a confident match — genuinely
unreachable, or ran fine but wasn't confident enough on its own; see
result.llm_unavailable and match_perfume's docstring for why BOTH cases
fall through), same order as before this became LLM-first:
  1. Normalize + exact/substring keyword match (free, instant)
  2. Fuzzy string match via rapidfuzz (free, fast)
This means the bot never goes fully silent on a Groq outage, rate limit,
or a case Groq itself hedges on (e.g. a genuinely ambiguous bare "9pm" —
Groq reliably returns one low-confidence guess for this rather than
confidently listing all 4 real candidates, so trusting its "no" as final
would go silent instead of showing the family the customer likely meant).

app.groq_client.classify_and_phrase distinguishes None (Groq itself
couldn't be asked — no key, no candidates, API error) from an empty
GroqClassification() (Groq ran fine, found nothing confident) purely for
logging/diagnostics — see MatchResult.llm_unavailable — but match_perfume
treats both the same way for routing: fall through to the deterministic
layers either way. A real production bug is why the deterministic layers'
own precision matters here rather than trying to gate them out entirely:
"I want to confirm kaaf only" additionally matched an unrelated "Not Only
Intense" product purely because "only" happened to be a standalone
keyword for it — fixed at the root via GENERIC_STOPWORDS below, not by
refusing to ever run the fallback.

A perfume name found by ANY layer only becomes a real match if the message
also looks like an explicit request, not just a mention. Confirmed in
production: a customer naming a perfume mid-conversation (recalling a past
purchase, saying what a friend or the shop owner wears, chit-chat that
happens to name a perfume) was still auto-firing a full price card, which
reads as the bot barging into a human conversation. Groq judges this itself
via classify_and_phrase's explicit_ask field (it sees the whole sentence,
so it can tell a bare "sauvage" apart from "the owner said sauvage is
nice"); the exact/fuzzy fallback layers below use a cheaper deterministic
stand-in (_looks_like_explicit_request) for when Groq itself is down.

All prices come from catalog.py, NEVER from the LLM — Groq only picks
*which* perfume and writes short opening/closing phrasing around the price
card; the numbers themselves are always assembled deterministically (see
app.formatter.build_price_card).
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
    # Populated only by the primary Groq match (layer="llm") — short,
    # natural phrasing to wrap around the deterministic price card. Never
    # contains prices/numbers itself (enforced by the system prompt in
    # app.groq_client) — those always come from catalog.py.
    opening: str | None = None
    closing: str | None = None
    # True only when Groq itself couldn't be reached at all (see
    # app.groq_client.classify_and_phrase's None return) rather than a
    # successful call that simply found nothing confident — kept purely
    # for logging/diagnostics; match_perfume falls through to the
    # deterministic layers in both cases either way (see its docstring).
    llm_unavailable: bool = False


# Common English words that appear as catalog keywords but are too
# generic to trigger a match on their own.  Without this filter,
# "good morning" matches "Lazy Sunday Morning" and "order kab aayega"
# matches "Mark & Victor Eau De Flora" (which has "order" as a keyword).
# Multi-word keywords containing these words (e.g. "sunday morning",
# "night out") still work fine — only single-word keywords are skipped.
# "only"/"most"/"never"/"when" confirmed live: "I want to confirm kaaf
# only" additionally matched "Afnan Supremacy Not Only Intense(SNOI)"
# purely because "only" is a standalone auto-generated keyword for that
# product — nothing to do with what the customer actually asked about.
GENERIC_STOPWORDS: set[str] = {
    "amount", "best", "blue", "day", "de", "eau", "flora", "good",
    "hello", "home", "just", "king", "know", "last", "lazy", "light",
    "long", "love", "man", "mark", "morning", "most", "never", "night",
    "note", "old", "one", "only", "open", "order", "play", "pure",
    "rain", "red", "rich", "rose", "royal", "rush", "show", "silk",
    "star", "story", "sunday", "that", "the", "this", "touch", "tree",
    "very", "want", "warm", "what", "when", "wild", "wood", "your",
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

    A single matched perfume resolves directly. Multiple matches from the
    same shared keyword (e.g. a bare "sauvage" matching several concentration
    variants) are a single ambiguous item — pick the best one. Multiple
    matches from genuinely different keyword strings mean the customer named
    multiple distinct perfumes — every one of those is returned via
    matched_perfume_ids so the reply can show a full card for each, instead
    of guessing one and dropping the rest.
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

    # 2+ different perfumes matched. Two genuinely different situations look
    # the same at this point and need different replies:
    #
    #   1. Every match came from the exact same keyword string (e.g. a bare
    #      "sauvage" matching several concentration variants of one product
    #      line). The customer named ONE thing; we're just unsure which
    #      variant — pick the longest/first match as before.
    #
    #   2. Different keyword strings matched different perfumes (e.g.
    #      "sauvage and eros price" — "sauvage" and "eros" are unrelated
    #      words that each identify a real, distinct product). The customer
    #      clearly asked about multiple perfumes — confirmed in production
    #      that the old length-based tie-break silently kept only the
    #      longest-keyword match and dropped the rest, which is exactly the
    #      "shows one random perfume's detail" bug. Every one of these is a
    #      genuine candidate, so return all of them and let the reply build
    #      a full price card for each.
    sorted_pids = sorted(unique_pids, key=lambda p: seen_pids[p][1], reverse=True)
    distinct_keywords = {seen_pids[pid][0] for pid in unique_pids}

    if len(distinct_keywords) == 1:
        best_pid = sorted_pids[0]
        best_kw, _ = seen_pids[best_pid]
        return MatchResult(
            perfume_id=best_pid,
            layer="exact",
            confidence=85.0,
            matched_keyword=best_kw,
        )

    return MatchResult(ambiguous=True, matched_perfume_ids=sorted_pids)


def has_confident_keyword_match(message_text: str) -> bool:
    """
    True if the deterministic exact-match layer alone already finds a real
    perfume (or a genuine multi-perfume/family-collision candidate list) in
    this message — no LLM call, no fuzzy tolerance, just a precise
    word-boundary keyword hit.

    Used by app.main as a veto: a message that's clearly a catalog request
    (see app.greeting.is_catalog_request) should short-circuit straight to
    the catalog reply UNLESS it also names a specific product this
    precisely — e.g. "show me sauvage price" should still get the Sauvage
    price card, not the catalog link, but "catalogue" or "send me the
    catalogue please" should never reach Groq or the fuzzy matcher at all
    (confirmed in production: Groq occasionally hallucinated a
    plausible-looking candidate out of its "nothing really matches"
    shortlist for bare catalog words, and separately the fuzzy matcher
    false-positived "please" against the keyword "pleasure" at 85.7%
    similarity — a wrong single perfume's price card instead of the
    catalog link).
    """
    normalized = normalize_message(message_text)
    if not normalized:
        return False
    result = _layer1_exact_match(normalized)
    return bool(result.perfume_id or result.matched_perfume_ids)



# Request-intent cues for the deterministic fallback layers only — Groq
# (the primary path, see _primary_llm_match/app.groq_client) makes this same
# judgment itself via classify_and_phrase's explicit_ask field, reading the
# full sentence far better than a keyword list ever could. This is a
# coarser stand-in for the rare case Groq itself is unavailable (API error/
# timeout/no key), so the free exact/fuzzy layers don't regress to the
# original bug: firing a price card on a perfume name merely mentioned in
# passing (recalling a past purchase, naming what a friend/the shop owner
# wears, mid-conversation chit-chat) instead of an actual request.
_REQUEST_CUES: tuple[str, ...] = (
    "price", "prices", "cost", "costs", "rate", "rates", "mrp",
    "how much", "kitna", "kitne", "kitni", "available", "availability",
    "stock", "ml", "size", "sizes", "decant", "sample", "samples",
    "buy", "want", "need", "order", "send", "interested", "info",
    "information", "details", "detail", "quote", "catalog", "catalogue",
)

# A cue word doesn't mean much if it's negated — confirmed in production:
# "the 9pm rebel is really good but I don't want ut" matched the "want" cue
# and fired a price card despite the customer explicitly declining it. Real
# Groq already handles this correctly on its own (it saw the full sentence
# and returned explicit_ask=false for this exact message) — this is only
# needed by the deterministic fallback below, which has no such contextual
# understanding. normalize_message strips apostrophes to spaces, so "don't"
# becomes the two tokens "don"+"t" — "don"/"won"/"doesn"/"didn" cover the
# contracted forms, "dont"/"wont"/etc. cover the ones typed without one.
# Deliberately excludes "can"/"cant": "can" alone is a common genuine-ask
# word ("can I get sauvage"), too ambiguous with "can't" to include here.
_NEGATION_MARKERS: tuple[str, ...] = (
    "dont", "don", "doesnt", "doesn", "didnt", "didn",
    "wont", "won", "not", "never", "no", "nahi",
)

# A message this short that still contains a real keyword match is almost
# always the customer directly naming what they want ("sauvage", "bleu de
# chanel 10ml") rather than a passing mention buried inside a longer,
# unrelated sentence.
_SHORT_MESSAGE_WORD_LIMIT = 5


def _looks_like_explicit_request(normalized: str) -> bool:
    """
    Deterministic stand-in for Groq's explicit_ask judgment (see module
    docstring), used only by the exact/fuzzy fallback layers inside
    match_perfume.

    Negation is checked first, ahead of even the short-message shortcut: a
    short message like "sauvage nahi chahiye" ("don't want sauvage") is
    just as much a decline as a longer one, and erring toward silence is
    the safer failure mode for this coarse, Groq-unavailable-only fallback.
    """
    if not normalized:
        return False
    if any(_keyword_boundary_regex(marker).search(normalized) for marker in _NEGATION_MARKERS):
        return False
    words = normalized.split()
    if len(words) <= _SHORT_MESSAGE_WORD_LIMIT:
        return True
    return any(cue in normalized for cue in _REQUEST_CUES)


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


async def _primary_llm_match(normalized: str) -> MatchResult:
    """
    Primary match: Groq LLM classification + reply phrasing, tried FIRST for
    every message (not a last resort — see match_perfume below).

    Asks the LLM to identify which perfume(s) the message refers to, choosing
    only from a pre-narrowed shortlist (see _top_candidates_for_llm) — never
    the full catalog, which is what keeps every call safely under Groq's
    per-minute token limit. Never generates prices — see app.groq_client and
    app.formatter for how opening/closing phrasing stays separate from the
    deterministic price grid.

    Groq can return 2+ ids when the customer clearly named multiple distinct
    perfumes — that's surfaced the same way the fallback exact matcher
    surfaces it (matched_perfume_ids, ambiguous=True), so app.main builds a
    full price card for each regardless of which layer found them.

    Sets llm_unavailable=True only when Groq itself could not be asked at
    all (classify_and_phrase returned None, or raised) — purely for
    logging/diagnostics (see the module docstring); match_perfume falls
    through to the deterministic layers either way, since a successful-but-
    empty call is treated the same as an outage for routing purposes.
    """
    from app.groq_client import classify_and_phrase

    candidates = _top_candidates_for_llm(normalized)
    if not candidates:
        # Nothing plausible enough to even offer Groq — the deterministic
        # layers below use the same underlying fuzzy scoring, so they
        # wouldn't find anything either. Not an outage, just genuinely
        # nothing here; stays silent rather than falling through.
        return MatchResult()

    try:
        result = await classify_and_phrase(normalized, candidates=candidates)
    except Exception:
        logger.exception("Primary LLM classification failed")
        return MatchResult(llm_unavailable=True)

    if result is None:
        return MatchResult(llm_unavailable=True)

    # Defense in depth even though app.groq_client already validates this —
    # an id outside what was offered must never be trusted.
    valid_pids = [pid for pid in result.perfume_ids if pid in candidates]

    if len(valid_pids) == 1:
        return MatchResult(
            perfume_id=valid_pids[0],
            layer="llm",
            confidence=80.0,
            opening=result.opening,
            closing=result.closing,
        )
    if len(valid_pids) > 1:
        return MatchResult(
            ambiguous=True,
            matched_perfume_ids=valid_pids,
            layer="llm",
            confidence=80.0,
            opening=result.opening,
            closing=result.closing,
        )

    # Groq successfully ran and found nothing confident/explicit —
    # llm_unavailable stays False; match_perfume still falls through to the
    # deterministic layers from here (see its docstring for why).
    return MatchResult()


async def match_perfume(message_text: str) -> MatchResult:
    """
    Run the matching pipeline: Groq LLM first (primary), falling through to
    the free exact/fuzzy matchers whenever Groq didn't return a confident
    match — whether because it was genuinely unreachable (see
    result.llm_unavailable, logged but not gated on: see below) or because
    it successfully ran but wasn't confident/explicit enough on its own.

    Deliberately NOT gated on llm_unavailable alone — an earlier version of
    this fix tried trusting Groq's "no" as final and skipping the
    deterministic layers entirely whenever Groq successfully ran, but that
    regressed a real, valuable existing behavior: a bare "9pm" is
    genuinely ambiguous among 4 real product variants, and Groq reliably
    hedges on this with a single low-confidence guess (explicit_ask=false)
    rather than confidently listing all 4 candidates as the prompt asks —
    trusting that "no" outright would have gone silent instead of showing
    the full family the customer likely meant. The deterministic layers
    are safe to keep running unconditionally now that their real precision
    gaps are fixed at the root (see GENERIC_STOPWORDS and
    _looks_like_explicit_request's negation guard below) rather than by
    refusing to run them at all.

    Returns a MatchResult indicating the match (or lack thereof).
    """
    normalized = normalize_message(message_text)

    if not normalized:
        return MatchResult()

    # Primary: Groq LLM classification + phrasing (async)
    result = await _primary_llm_match(normalized)
    if result.perfume_id or result.matched_perfume_ids:
        logger.info(
            "Primary LLM match: pid=%s, matched_perfume_ids=%s",
            result.perfume_id,
            result.matched_perfume_ids,
        )
        return result

    logger.info(
        "No confident primary match (llm_unavailable=%s) for message: %s",
        result.llm_unavailable,
        message_text[:100],
    )

    # Fallback: exact/substring match — gated by _looks_like_explicit_request
    # (see module docstring): a keyword found inside a long message with no
    # request cue is treated as a passing mention, not a match.
    result = _layer1_exact_match(normalized)
    if (result.perfume_id or result.ambiguous) and _looks_like_explicit_request(normalized):
        logger.info(
            "Fallback exact match: pid=%s, keyword=%s, ambiguous=%s",
            result.perfume_id,
            result.matched_keyword,
            result.ambiguous,
        )
        return result

    # Fallback: fuzzy match — same explicit-request gate as above.
    result = _layer2_fuzzy_match(normalized)
    if (result.perfume_id or result.ambiguous) and _looks_like_explicit_request(normalized):
        logger.info(
            "Fallback fuzzy match: pid=%s, keyword=%s, score=%.1f, ambiguous=%s",
            result.perfume_id,
            result.matched_keyword,
            result.confidence or 0,
            result.ambiguous,
        )
        return result

    # No match found across all methods
    logger.info("No match found for message: %s", message_text[:100])
    return MatchResult()
