"""
Perfume matching pipeline.

TWO STAGES, IN THIS ORDER
-------------------------
1. app.name_index decides WHICH perfumes the message could be naming. It
   scores every catalog entry by weighted token overlap and returns the ones
   that clear a real bar — complete (nothing is lost to a shortlist cutoff),
   deterministic, free, and about half a millisecond.
2. Groq decides WHETHER the customer is actually asking, narrows a
   multi-candidate list using the recent conversation, and writes the
   phrasing around the price card.

This is the reverse of the previous arrangement, where Groq ran first and
picked from a top-25 shortlist built out of raw n-gram fuzzy scores. That
shortlist was the weak link: with 1,200+ entries it routinely contained
nothing relevant (short unrelated words score high on string similarity
alone — "please"/"pleasure" is 85.7, "tell"/"stellar" 72.7), and a small
fast model handed a noisy list does not reliably answer "none of these".
The whole family of defensive patches that grew around it — a plausibility
floor, a stopword blocklist, a catalog-request veto, a keyword-quality
filter — existed to contain that one design decision. Scoring the catalog
properly and only then asking the model to judge intent removes the cause
instead of the symptoms; benchmarked strictly over the full catalog
(scripts/benchmark_matcher.py), it moved top-1 accuracy on misspelled names
from 53.9% to well above 90% while cutting false positives.

WHAT GROQ MAY AND MAY NOT DO
----------------------------
It may: drop candidates, pick one candidate out of several, decide the
message is not a request at all, and write an opening/closing line.
It may NOT: introduce a perfume the index did not find, or produce a price.
Prices are always assembled from catalog.py by app.formatter. Both limits
are enforced in code (see _refine_with_llm), not just asked for in the
prompt.

If Groq is unreachable the deterministic result stands on its own, gated by
_looks_like_explicit_request — a coarser stand-in for the same intent
judgment, so an outage degrades the bot's manners rather than silencing it.

MENTION vs. ASK
---------------
A perfume name found in a message is not automatically a request for its
price. Confirmed in production: a customer recalling a past purchase, or
saying what a friend or the shop owner wears, was getting a price card
fired at them mid-conversation. Whichever layer makes the final call, that
judgment happens before any card is built.
"""

import logging
import re
from dataclasses import dataclass, field

from app import name_index
from app.catalog import PERFUMES

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """Result of the matching pipeline."""

    perfume_id: str | None = None
    # "exact"  — the customer typed the name cleanly
    # "fuzzy"  — recovered through typo tolerance
    # "llm"    — Groq narrowed several index candidates down to this one
    # "context"— resolved from the recent conversation, not this message
    layer: str | None = None
    confidence: float | None = None
    ambiguous: bool = False
    matched_keyword: str | None = None
    # Populated whenever the message resolved to 2+ perfumes — either the
    # customer named several, or one name genuinely fits several products.
    # The reply shows a full card for each rather than guessing.
    matched_perfume_ids: list[str] | None = None
    # Short natural phrasing from Groq to wrap around the deterministic
    # price card. Never contains prices — enforced in app.groq_client.
    opening: str | None = None
    closing: str | None = None
    # True only when Groq could not be reached at all (no key, API error),
    # as opposed to running fine and judging the message not to be an ask.
    # Kept for logging/diagnostics.
    llm_unavailable: bool = False
    # Debug/diagnostic view of what the index scored, best first.
    scores: list[tuple[str, float]] = field(default_factory=list)


# --- Message normalization --------------------------------------------------

def normalize_message(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Kept for callers
    that want a stable, human-readable normalized form (analytics, the
    size parser below); token-level matching goes through
    app.name_index.tokenize_message instead."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_ML_SIZE_PATTERN = re.compile(r"\b(\d{1,4})\s*ml\b")


def extract_requested_size_ml(normalized: str) -> int | None:
    """
    Pull the ml quantity out of a normalized message ("9pm rebel 3ml",
    "sauvage 10 ml price"). Naming a size does NOT narrow the price card —
    every card shows the full grid (see app.formatter._build_card_block).
    This is read purely as an intent signal: "sauvage 3ml" with no other
    request words is still plainly someone asking to buy.

    Takes the first ml number mentioned; a message naming more than one size
    is rare enough, and ambiguous enough about which should win, that
    guessing further would be worse than not.
    """
    match = _ML_SIZE_PATTERN.search(normalized)
    return int(match.group(1)) if match else None


# --- Intent: is this an ask, or just a mention? -----------------------------

# Words that make a message a request rather than a remark. Used only when
# Groq is unavailable — Groq reads the whole sentence (and the recent
# conversation) and judges this far better than any word list can.
#
# Whole words, not substrings — "rate" is inside "accurate" and "corporate",
# "cost" inside "costume", "ml" inside anything at all.
_REQUEST_CUES: frozenset[str] = frozenset(
    {
        "price", "prices", "cost", "costs", "rate", "rates", "mrp",
        "much", "kitna", "kitne", "kitni", "keemat", "available",
        "availability", "stock", "size", "sizes", "decant", "decants",
        "sample", "samples", "buy", "want", "need", "order", "send",
        "interested", "info", "information", "details", "detail", "quote",
        "chahiye", "milega", "milegi", "bhejo", "dedo",
    }
)

# A cue means nothing if it is negated — confirmed in production: "the 9pm
# rebel is really good but I don't want ut" hit the "want" cue and fired a
# price card at a customer who had just declined it. normalize_message turns
# apostrophes into spaces, so "don't" arrives as "don" + "t"; both the
# contracted and uncontracted spellings are listed. "can"/"cant" are
# deliberately absent — "can I get sauvage" is a genuine ask, and the two
# are too easily confused to risk.
_NEGATION_MARKERS: frozenset[str] = frozenset(
    {
        "dont", "don", "doesnt", "doesn", "didnt", "didn",
        "wont", "won", "not", "never", "no", "nahi", "mat",
    }
)

# Above this fraction of the message's content words being the perfume name
# itself, the message IS the name — no request cue needed. This is what
# makes a bare "Issey Miyake L'Eau d'Issey Solar Lavender" work: eight words
# long, no cue word anywhere, and unmistakably someone naming a perfume. The
# previous word-count rule capped this at five words and silently dropped
# every longer product name in the catalog.
_NAME_FOCUS_THRESHOLD = 0.6

# Thresholds for the safety valve that overrides a Groq "not an ask" verdict
# (see match_perfume): the message must be almost entirely the product name,
# and the customer must have typed almost all of that name's words.
_BARE_NAME_FOCUS = 0.9
_BARE_NAME_COVERAGE = 0.9

# How many fully-spelled product names make a message a shopping list rather
# than a remark that happens to name something.
_LIST_MIN_NAMES = 2


def _looks_like_explicit_request(
    text: str, focus: float, name_words: frozenset[str] = frozenset()
) -> bool:
    """
    Deterministic stand-in for Groq's intent judgment, used when Groq is
    unavailable. `focus` is how much of the message the matched name
    accounts for — see app.name_index.message_focus.

    Negation is checked first and overrides everything: erring toward
    silence is the safer failure mode for a coarse fallback.
    """
    all_tokens = name_index.tokenize_message(text)
    if not all_tokens:
        return False

    # A negation word that is part of a product's OWN name is not a
    # negation. "Chanel No 5" contains "no" and "Supremacy Not Only Intense"
    # contains "not" — both were reading as refusals, so a customer listing
    # either got total silence.
    #
    # Checked against DISPLAY NAMES only, never clone_of. Exempting anything
    # a match merely consumed was too loose: "the 9pm rebel is good but I
    # don't want it" pulled in Maison Asrar Love Key, whose clone_of is
    # "Kilian's Love, Don't Be Shy", and that stray "don't" switched the
    # decline guard off entirely.
    if any(t in _NEGATION_MARKERS and t not in name_words for t in all_tokens):
        return False
    if focus >= _NAME_FOCUS_THRESHOLD:
        return True

    words = set(normalize_message(text).split())
    # A bare size ("10ml") is a request cue on its own, and arrives as one
    # token rather than a word from the list above.
    if extract_requested_size_ml(normalize_message(text)) is not None:
        return True
    return bool(words & _REQUEST_CUES)


# --- Deterministic entry points ---------------------------------------------

def has_confident_keyword_match(message_text: str) -> bool:
    """
    True if the message names a product outright — every matched word spelled
    correctly, no typo tolerance involved.

    Used by app.main as an escape hatch on its catalog-request veto: a
    message that reads as "send me the catalogue" short-circuits to the
    catalog link UNLESS it also names a specific product this precisely, so
    "show me sauvage price" still returns the Sauvage card.

    Correct spelling is the bar rather than a score, and deliberately so: the
    veto exists because a *near* match on a catalog phrase is exactly what
    used to hijack it (the fuzzy matcher scored "please" against the catalog
    token "pleasure" at 85.7 and answered with one perfume's price card). A
    message that only resolves through typo tolerance is the case the veto
    must let through to the full pipeline, not the case it should override.
    """
    results = name_index.search(message_text, limit=1)
    return bool(results) and _clean_match(results[0])


def _clean_match(scored: name_index.Scored) -> bool:
    """True if every word this match rests on was spelled correctly — the
    difference between the "exact" and "fuzzy" layer labels the analytics
    dashboard reports on, and the bar has_confident_keyword_match uses."""
    return scored.similarity >= 0.999


# --- Groq refinement --------------------------------------------------------

# How many index candidates Groq is asked to choose between — everything the
# index found, since it already caps itself at name_index.MAX_RESULTS. A
# tighter limit here silently truncated multi-product messages: a customer's
# wishlist resolved to eight perfumes and Groq was shown six of them, so two
# of the products they asked for could not be answered whatever it replied.
_LLM_CANDIDATE_LIMIT = name_index.MAX_RESULTS

# How many distinct message spans make a message a LIST of products rather
# than one ambiguous name. Three keeps genuine two-way ambiguity in Groq's
# hands while protecting real multi-item orders.
_LIST_DISTINCT_SPANS = 3


async def _refine_with_llm(
    message_text: str,
    candidates: list[name_index.Scored],
    history: list[dict] | None,
) -> tuple[list[str] | None, bool, str | None, str | None]:
    """
    Ask Groq to judge intent and narrow the index's candidates.

    Returns (perfume_ids, llm_unavailable, opening, closing). perfume_ids is
    None when Groq could not be asked at all — the caller then falls back to
    its own deterministic intent gate. An empty list is a real answer: Groq
    looked and judged this not to be a request.

    Ids Groq returns are intersected with what the index actually found, so
    a hallucinated or misremembered id can never reach a price card.
    """
    from app.groq_client import classify_and_phrase

    shortlist = {
        s.perfume_id: PERFUMES[s.perfume_id]
        for s in candidates[:_LLM_CANDIDATE_LIMIT]
        if s.perfume_id in PERFUMES
    }
    if not shortlist:
        return None, True, None, None

    try:
        result = await classify_and_phrase(
            message_text, candidates=shortlist, history=history
        )
    except Exception:
        logger.exception("Groq classification failed")
        return None, True, None, None

    if result is None:
        return None, True, None, None

    valid = [pid for pid in result.perfume_ids if pid in shortlist]

    # Groq narrows; that is useful when one NAME fits several products (a
    # bare "sauvage"), and harmful when the customer listed several
    # different products. Asked to pick from a sixteen-item order it
    # quietly returned a subset, and the missing perfumes were simply not
    # answered.
    #
    # The two cases are told apart by what the matches rest on: a family
    # all rests on the same words, a list rests on different ones. When the
    # message is a list, Groq's verdict on INTENT still counts — an empty
    # answer still silences the reply — but it does not get to drop items.
    spans = {s.consumed for s in candidates}
    if valid and len(spans) >= _LIST_DISTINCT_SPANS:
        listed = [s.perfume_id for s in candidates]
        if len(listed) > len(valid):
            logger.info(
                "Groq returned %d of %d listed products — keeping the full list",
                len(valid), len(listed),
            )
        return listed, False, result.opening, result.closing

    return valid, False, result.opening, result.closing


# --- Pipeline ---------------------------------------------------------------

def _result_from(
    picked: list[str],
    scored_by_pid: dict[str, name_index.Scored],
    layer: str,
    opening: str | None = None,
    closing: str | None = None,
    llm_unavailable: bool = False,
    all_scores: list[name_index.Scored] | None = None,
    message_text: str = "",
) -> MatchResult:
    scores = [(s.perfume_id, round(s.score, 2)) for s in (all_scores or [])]

    if len(picked) == 1:
        s = scored_by_pid.get(picked[0])
        return MatchResult(
            perfume_id=picked[0],
            layer=layer,
            confidence=round(s.score, 2) if s else None,
            matched_keyword=" ".join(s.matched_tokens) if s else None,
            opening=opening,
            closing=closing,
            llm_unavailable=llm_unavailable,
            scores=scores,
        )

    top = scored_by_pid.get(picked[0]) if picked else None
    return MatchResult(
        ambiguous=True,
        matched_perfume_ids=picked,
        layer=layer,
        confidence=round(top.score, 2) if top else None,
        opening=opening,
        closing=closing,
        llm_unavailable=llm_unavailable,
        scores=scores,
    )


async def match_perfume(
    message_text: str, history: list[dict] | None = None
) -> MatchResult:
    """
    Resolve an inbound message to the perfume(s) it is asking about.

    `history` is the recent conversation for this sender (see
    app.conversation.recent_turns) — oldest first, each a dict with "role"
    ("customer"/"bot") and "text", plus "perfume_ids" on bot turns. It lets
    a follow-up like "and 5ml?" or "how much for the second one" resolve
    against what was just being discussed, and lets Groq read intent in
    context rather than from one isolated sentence.

    Returns an empty MatchResult when the message names no perfume, or names
    one only in passing — the caller stays silent rather than guessing.
    """
    if not message_text or not message_text.strip():
        return MatchResult()

    candidates = name_index.search(message_text)

    if not candidates:
        return await _resolve_from_context(message_text, history)

    scored_by_pid = {s.perfume_id: s for s in candidates}
    focus = name_index.message_focus(message_text, candidates)
    name_words = frozenset(
        w
        for c in candidates
        for w in name_index.tokenize(PERFUMES[c.perfume_id]["display_name"])
        if c.perfume_id in PERFUMES
    )

    picked, llm_unavailable, opening, closing = await _refine_with_llm(
        message_text, candidates, history
    )

    if picked is None:
        # Groq unreachable — the index result stands, gated by the coarse
        # deterministic intent check.
        if not _looks_like_explicit_request(message_text, focus, name_words):
            logger.info(
                "Index matched %s but message reads as a mention, not an ask: %s",
                [s.perfume_id for s in candidates],
                message_text[:100],
            )
            return MatchResult(llm_unavailable=True)

        ids = [s.perfume_id for s in candidates]
        layer = "exact" if all(_clean_match(scored_by_pid[p]) for p in ids) else "fuzzy"
        logger.info("Deterministic match (Groq unavailable): %s", ids)
        return _result_from(
            ids, scored_by_pid, layer, llm_unavailable=True, all_scores=candidates,
            message_text=message_text
        )

    if not picked:
        # Groq's "not an ask" is trusted — that judgment is the job it is
        # here to do, and it sees the whole sentence plus the conversation.
        #
        # With ONE safety valve. When the message is essentially nothing but
        # a product name, "the customer isn't asking" is not a judgment call
        # — it is the model being wrong, and the cost is silence on the
        # single most common message this bot receives ("sauvage", "kaaf",
        # "9pm rebel 3ml"). Confirmed repeatedly against the live model,
        # which fails this in two reproducible ways:
        #
        #   * "9pm rebel" is judged an ask; "9pm rebl" is not. The
        #     misspelling alone flips it — so coverage, not spelling, is the
        #     bar here.
        #   * A bare name matching several products is judged not an ask
        #     even while the model writes "We have Aventus variants from
        #     Armaf, Blanche and Anilla!" in the same breath. It plainly
        #     understood the question.
        #
        # So the valve asks only what it actually cares about: is this
        # message essentially nothing but a product name the customer typed
        # in full, with no negation anywhere? How many products that name
        # happens to fit says nothing about whether they were asking.
        #
        # The same verdict comes back for a pasted WISHLIST, which is the
        # other shape customers send constantly — "Al Haramain: Detour Noir,
        # Detour Eco... Armaf: Club de Nuit Untold..." was answered with
        # silence while Groq itself listed three of the products it had just
        # recognised. Somebody who writes out several complete product names
        # is ordering, not chatting.
        #
        # Two fully-spelled names is the discriminator, not the count of
        # candidates: a passing mention produces one ("the owner told me
        # sauvage is nice"), and "my friend uses sauvage and eros" produces
        # two but leaves most of the message unexplained, so the focus gate
        # inside _looks_like_explicit_request still turns it down.
        fully_named = [c for c in candidates if c.coverage >= _BARE_NAME_COVERAGE]
        looks_like_a_list = len(fully_named) >= _LIST_MIN_NAMES

        if _looks_like_explicit_request(message_text, focus, name_words) and (
            looks_like_a_list
            or (candidates[0].coverage >= _BARE_NAME_COVERAGE and focus >= _BARE_NAME_FOCUS)
        ):
            logger.info(
                "Groq said not-an-ask, but %r is a bare product name — replying anyway",
                message_text[:100],
            )
            ids = [s.perfume_id for s in candidates]
            layer = "exact" if all(_clean_match(s) for s in candidates) else "fuzzy"
            return _result_from(
                ids, scored_by_pid, layer, all_scores=candidates, message_text=message_text
            )

        logger.info(
            "Index matched %s but Groq judged this not an ask: %s",
            [s.perfume_id for s in candidates],
            message_text[:100],
        )
        return MatchResult(scores=[(s.perfume_id, round(s.score, 2)) for s in candidates])

    # Groq narrowed a multi-candidate list to one — that narrowing IS the
    # llm layer's contribution, and worth distinguishing in analytics from a
    # match the index resolved unambiguously on its own.
    if len(picked) == 1 and len(candidates) > 1:
        layer = "llm"
    elif all(_clean_match(scored_by_pid[p]) for p in picked if p in scored_by_pid):
        layer = "exact"
    else:
        layer = "fuzzy"

    logger.info("Match: %s (layer=%s)", picked, layer)
    return _result_from(
        picked, scored_by_pid, layer, opening, closing,
        all_scores=candidates, message_text=message_text,
    )


# --- Follow-ups that name no perfume at all ---------------------------------

# A message with no product name in it that is nonetheless clearly about the
# product just discussed: a bare size, a price question with no subject, or
# a reference word. Only these resolve from context — an unrelated message
# with no perfume in it must stay unrelated.
#
# Matched as whole words, never as substrings. "it" as a substring appears
# inside "with", "white" and "delivery estimate"; a bare "same" does not
# appear inside anything, but the rule has to hold for the whole list or it
# holds for none of it.
_FOLLOWUP_CUES: frozenset[str] = frozenset(
    {
        "price", "cost", "rate", "kitna", "kitne", "kitni", "keemat",
        "much", "iska", "uska", "isko", "usko", "that", "this", "it",
        "same", "available", "stock", "milega", "milegi", "decant",
    }
)


# A follow-up is short. Beyond this many words the customer has moved on to
# saying something new, and a message with no perfume name in it that long
# is not "and the 5ml?".
_FOLLOWUP_MAX_WORDS = 12


def _introduces_a_new_name(message_text: str) -> bool:
    """
    True if the message contains a word that is neither conversational
    filler, a follow-up cue, nor a size/concentration — i.e. the customer is
    naming something, and this is not a follow-up at all.

    Without this, a message naming a product we do not carry gets handed the
    PREVIOUS card's candidates and asked "which of these?". Observed live:
    after a Kaaf price card, "fathreneit" resolved through context and came
    back "Fahrenheit EDT is a great choice!" — the model confirming, in the
    shop's voice, a perfume that does not exist in the catalog.

    Concentration words are allowed through on purpose: "the EDP one" is a
    reference to what was already shown, not a new product.
    """
    allowed = _FOLLOWUP_CUES | name_index.NOISE_TOKENS
    return any(
        token not in name_index.MESSAGE_STOPWORDS and token not in allowed
        for token in name_index.tokenize_message(message_text)
    )


async def _resolve_from_context(
    message_text: str, history: list[dict] | None
) -> MatchResult:
    """
    Resolve a follow-up that names no perfume ("and 5ml?", "iska price kya
    hai", "the EDP one", "second one 10ml") against the cards the customer
    is replying to.

    The perfumes the last reply showed become the candidate list, and Groq
    picks from it exactly as it does for a normal match — which is what
    makes a reference like "the EDP one" or "the second one" resolvable at
    all, since neither names a product and both depend on what was shown
    and in what order. Groq's explicit_ask verdict is also what keeps
    "thanks bhai" and "order kab aayega" silent: they reach this code with
    the same candidates and are correctly turned down.

    When Groq is unavailable this falls back to a deterministic rule — a
    size or an explicit follow-up cue, and no negation — which handles the
    common "and 5ml?" shape but cannot resolve positional references.
    """
    if not history:
        return MatchResult()

    from app.conversation import last_discussed_perfumes

    recent = last_discussed_perfumes(history)
    if not recent:
        return MatchResult()

    if len(message_text.split()) > _FOLLOWUP_MAX_WORDS:
        return MatchResult()

    if _introduces_a_new_name(message_text):
        return MatchResult()

    candidates = [
        name_index.Scored(
            perfume_id=pid,
            score=0.0,
            coverage=1.0,
            similarity=1.0,
            kind="name",
            consumed=frozenset(),
            matched_tokens=(),
        )
        for pid in recent
        if pid in PERFUMES
    ]
    if not candidates:
        return MatchResult()

    picked, llm_unavailable, opening, closing = await _refine_with_llm(
        message_text, candidates, history
    )

    if picked is None:
        # No Groq — fall back to the deterministic follow-up rule.
        normalized = normalize_message(message_text)
        has_size = extract_requested_size_ml(normalized) is not None
        words = set(normalized.split())
        has_cue = bool(words & _FOLLOWUP_CUES)
        tokens = set(name_index.tokenize_message(message_text))

        if not (has_size or has_cue) or (tokens & _NEGATION_MARKERS):
            return MatchResult(llm_unavailable=True)
        picked = recent

    if not picked:
        return MatchResult(llm_unavailable=llm_unavailable)

    logger.info("Resolved follow-up %r from conversation context -> %s", message_text[:60], picked)

    if len(picked) == 1:
        return MatchResult(
            perfume_id=picked[0],
            layer="context",
            confidence=70.0,
            opening=opening,
            closing=closing,
            llm_unavailable=llm_unavailable,
        )
    return MatchResult(
        ambiguous=True,
        matched_perfume_ids=picked,
        layer="context",
        confidence=70.0,
        opening=opening,
        closing=closing,
        llm_unavailable=llm_unavailable,
    )
