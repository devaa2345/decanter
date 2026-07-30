"""
Weighted-token name index — the deterministic core of perfume matching.

WHY THIS REPLACED KEYWORD MATCHING
----------------------------------
The original matcher walked every perfume's auto-generated keyword list and
took the first word-boundary hit, tie-breaking by keyword length. Benchmarked
strictly against all 1,207 catalog entries (scripts/benchmark_matcher.py),
that scored 53.9% — and, more tellingly, only 75% on perfectly-spelled full
product names. The failures were structural, not tuning:

  * "Chanel DKY" (one missing letter) returned "Albait Aldimashqi Chanel
    no 5", because the keyword "chanel" is an exact hit and exact beats
    fuzzy — the correct answer, "Chanel DKNY", never got to compete.
  * "Hugo Boss ElegantVetiver" returned "Hugo Boss Boss Bottled EDT OG"
    for the same reason ("boss" hits exactly).
  * A brand token shared by 100+ entries carried exactly as much weight as
    a token unique in the whole catalog.

The fix is to stop asking "does any keyword appear?" and start asking "how
much of THIS product's name did the customer actually type, and how
distinctive is the part they got right?" Every perfume is scored; the best
score wins; nothing short-circuits.

HOW SCORING WORKS
-----------------
Each perfume contributes several *variants* — alternative ways a real
customer refers to it (full name, name without the brand prefix, either of
those without concentration noise like "EDP", and the designer original it
clones). A perfume's score is the best score across its variants, so
"rebel" and "Afnan 9PM Rebel EDP" both resolve to the same entry.

Each token in a variant carries an IDF weight: log(N / how many perfumes
use that token). "edp" (144 entries) is worth ~2.1; a token unique to one
product is worth ~7.1. A variant's score is

    evidence = Σ weight(token) × similarity(token, message)
    coverage = evidence / Σ weight(token)
    score    = evidence × (COVERAGE_FLOOR + (1 - COVERAGE_FLOOR) × coverage)

Evidence is what makes a single distinctive token ("rebel") decisive even
though it is only part of the full name. Coverage is what stops a shared
brand token ("chanel", "boss", "french avenue") from ever being decisive on
its own — it can never supply enough weight relative to the rest of the
name it belongs to.

Similarity is token-level and typo-tolerant, matched against the catalog's
whole token vocabulary at once (rapidfuzz cdist over ~2.5K tokens — a
sub-millisecond matrix op, so no candidate pre-filtering is needed and
nothing is missed to a shortlist cutoff). Adjacent message tokens are also
joined and re-queried, which is what makes "sau vage" and "club denuit"
work; long message tokens are additionally scanned for embedded catalog
tokens, which is what makes "ElegantVetiver" work.

MULTIPLE PERFUMES IN ONE MESSAGE
--------------------------------
Handled by consumption rather than by a tie-break: the best match's message
tokens are removed and the remainder is re-scored, repeatedly. "sauvage and
eros price" resolves to two perfumes because "eros" still clears the bar
once "sauvage" is taken out of play — while "Chanel DKNY" resolves to one,
because nothing is left over. Genuine ambiguity (several products that fit
the SAME words, e.g. a bare "9pm") surfaces as a near-tie inside a single
round and returns the whole tied set.
"""

from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process
from rapidfuzz.distance import DamerauLevenshtein

from app.catalog import PERFUMES
from app.config import settings

# --- Tuning ----------------------------------------------------------------

# Two tokens are "the same word" at or above this rapidfuzz.ratio. 80 admits
# the transpositions and adjacent-key slips that dominate real phone typing
# ("msylf"/"myslf" = 80.0, "bimbhell"/"bombshell" = 82.4) without measurably
# increasing wrong answers — benchmarked across 76/78/80/82/84, wrong-perfume
# rate is flat at ~1.5% from 76 to 80 and rises above it, while recall falls
# off past 80.
#
# This being safe at all is a property of the scoring model, not of the
# number: a loose token match still has to survive IDF weighting and the
# coverage discount. The old matcher could not afford this, because there a
# single loose hit on any keyword decided the answer outright.
TOKEN_SIM_MIN = 80.0

# Tokens of 1-3 characters ("no", "og", "5", "fw") are too short for edit
# distance to mean anything — a single character changes ratio by 30+
# points — so they only ever match exactly.
_SHORT_TOKEN_MAX = 3

# Similarity required, by catalog-token length. A flat threshold means
# different things at different lengths: a four-letter token is easy to
# reach by accident from any longer word containing its letters, and both
# "sauvge"/"sage" and "tellme"/"elle" land on exactly 80 that way. 85 still
# admits every real four-letter typo — a dropped letter scores 85.7, an
# inserted one 88.9 — while excluding those collisions. Longer tokens keep
# the tolerant bar, where 80 genuinely means "a slip in a real word".
#
# Benchmarked: raising this further (88, or adding a 5-character rule) costs
# ~1 point of usable accuracy and buys no precision, so it stops here.
_SIM_MIN_BY_LEN: dict[int, float] = {4: 85.0}

# Short tokens also get a second, sharper chance: a single edit away counts
# as a match regardless of ratio. Ratio is a poor instrument at this length —
# ANY substitution in a four-letter word scores 75, so "kaff" for "kaaf"
# (one keystroke) and "sauvge" for "sage" (nothing alike) end up on the same
# side of any threshold. Edit distance separates them cleanly: 1 versus 3.
# Transpositions count as one edit, which is the single most common phone
# typo. Applied only up to _EDIT_DISTANCE_MAX_LEN — beyond that, ratio at 80
# already admits everything one edit could produce.
_EDIT_DISTANCE_MAX_LEN = 6
_MAX_EDITS = 1

# Finding a catalog token embedded inside a longer message token is how
# space-joined input ("elegantvetiver") is recovered. Held to a stricter bar
# than ordinary token matching because partial matching is inherently
# looser, and discounted slightly so a clean token match always outranks it.
_EMBED_SIM_MIN = 90.0
_EMBED_DISCOUNT = 0.95
_EMBED_MIN_TOKEN_LEN = 4
_EMBED_MIN_MESSAGE_TOKEN_LEN = 8

# How much a partially-typed name is discounted. At 0.5 a name the customer
# typed half of keeps half its evidence — enough for a unique token like
# "rebel" to still win outright, never enough for a brand token shared
# across 100+ products.
COVERAGE_FLOOR = 0.5

# How much a match is discounted for leaving parts of the message
# unexplained. Deliberately gentle: real messages are full of words that are
# not the product name, and filler is already excluded from this count. It
# exists to order near-identical candidates, not to reject matches.
MESSAGE_COVERAGE_FLOOR = 0.7

# How much a clone match is held back relative to a product stocked under
# the name itself. Mild — clones are legitimate answers, and for a designer
# original we do not carry they are the ONLY answer — but when both exist,
# the real thing wins.
_CLONE_PENALTY = 0.9

# Minimum score to be considered a match at all — roughly "one token unique
# in the catalog, typed correctly", or a couple of moderately distinctive
# ones. Below this the message did not identify a product and the bot stays
# silent rather than guessing. Read from settings on every search so it can
# be tuned by env var without a code change (see config.MATCH_MIN_SCORE).
MIN_SCORE_DEFAULT = 4.0

# ...but a customer who types a product's WHOLE name, and nothing else, has
# identified it no matter what that score comes to. The floor measures how
# distinctive the matched words are, and popularity destroys distinctiveness:
# "sauvage" names 26 products in this catalog, so its IDF is 4.45 against a
# floor of 4.0 — a single typo multiplies that by 0.86 and "savuage" fell
# through to silence, while "rebel" (one product, IDF 7.71) sailed past. The
# bot was punishing its best-selling names for selling well, and got worse
# every time the catalog grew.
#
# Bypassing the floor is safe because it is only a backstop: the rules that
# actually prevent nonsense — no bare brand words, no lone ordinary English,
# no match without a real product word, no scattered words — all still apply
# above. This says only "they typed the entire name and nothing else", which
# no amount of IDF arithmetic should be allowed to overrule.
# How far the score floor relaxes when the match accounts for the entire
# message. Not to zero: see the residual bar in search().
_WHOLE_MESSAGE_FLOOR = 0.6

# A runner-up scoring at least this fraction of the winner is treated as a
# genuine co-candidate rather than a loser — the "which 9pm?" case, where
# showing every real candidate beats guessing one.
TIE_RATIO = 0.90

# Cap on perfumes returned from one message (multi-perfume asks plus ties).
MAX_RESULTS = 30

# How many consume-and-rescore rounds to run — how many distinct perfumes
# one message can name. Customers really do paste a whole wishlist ("Al
# Haramain: Detour Noir, Detour Eco... Armaf: Club de Nuit Untold..."), and
# a cap of four silently answered a quarter of such a message.
_MAX_ROUNDS = 30

# Cap on co-candidates from a single round, so one wide family cannot eat
# the whole result budget before the later rounds run. Without it, "sauvage
# and eros price" returned eight Eros variants and never got as far as
# noticing that Sauvage was also asked about.
_MAX_TIED_PER_ROUND = 5

# A perfume found in a LATER round has to be one the customer actually spelled
# out, not a coincidence scraped from the leftover words — "the owner told me
# 9pm rebel is really nice" found 9PM Rebel and then "Club de Nuit Untold",
# off the word "told".
#
# The test is coverage, not score. Scoring later finds against a fraction of
# the FIRST match's score seemed reasonable and was wrong: it made a list's
# later items depend on how strong its first item happened to be. In a real
# six-perfume order, "Bad Boy Cobalt Elixir" scored 17.4 (rare words) and set
# the bar at 8.7, which then discarded "Stronger With You Absolutely" at 7.4 —
# a product the customer had written out in full, scoring low only because
# "stronger with you" is shared across nine catalog entries. Coverage asks the
# right question: did they type this name, or did one of its words just happen
# to be lying around?
_EXTRA_ROUND_COVERAGE = 0.45


def _worth_an_extra_round(scored: "Scored") -> bool:
    """
    Whether a perfume found after the first is really a second thing the
    customer named.

    Two conditions, and the second one matters as much as the first. A match
    resting on a SINGLE word only counts when that word was spelled exactly:
    "sauvage and eros price" reaches Eros on a perfect one-word hit and is a
    genuine second product, whereas "the owner told me 9pm rebel is nice"
    reaches "Club de Nuit Untold" by fuzzily reading "told" as "untold" —
    one word, and not even the word that was written.
    """
    if scored.coverage < _EXTRA_ROUND_COVERAGE:
        return False
    return len(scored.matched_tokens) >= 2 or scored.similarity >= 0.999

# How many message words a product name is allowed to be spread across,
# beyond its own length. Customers interleave a little ("club de nuit
# INTENSE MAN edp"), so it cannot be zero — but a name whose words are
# scattered the width of a whole message was never really said.
_MAX_NAME_GAP = 5


# Words that must never anchor a match on their own. Two groups, both
# confirmed as real false-positive sources: ordinary English/Hinglish
# conversation ("please" scores 85.7 against the catalog token "pleasure";
# "great" is a literal token of "Zimaya Musk Is Great") and the request
# vocabulary customers wrap around a name ("price", "kitna", "available").
#
# These are excluded as STANDALONE queries only. They still participate in
# joined n-grams, so a product whose name genuinely contains one ("Musk Is
# Great", "Stronger With You") is still reachable by typing the phrase.
_MESSAGE_STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again all also am an and another any are as ask at
    available availability
    back be because been before best better between both bring but buy by
    call can cant come cost costs could
    day days deliver delivered delivery details detail did do does doesnt
    doing done dont down during
    each even ever every
    far few first for free from full
    get give go going good got great
    had has have having he hello help her here hey hi him his how however
    i if in info information into is it its
    just
    keep kind know
    last let like little long look lot love
    made make many may me mean might mine more most much must my
    morning afternoon evening
    name need never new next nice no not now
    of off ok okay old on once one only or order orders other our out over
    own
    part per place pls please price prices put
    quality quantity question quote
    rate rates really right
    said same say see send sent shall she ship shipping should show side
    since size sizes so some soon still stock such sure
    take tell than thank thanks that the their them then there these they
    thing think this those though thought through time to today too total
    two
    under until up us use used
    very
    want wanted was way we well were what when where which while who why
    will with within without would
    yes yet you your yours
    men women man woman mens womens ladies gents unisex boys girls kids
    perfume perfumes attar itra scent scents bottle bottles tester testers
    cod cash prepaid upi paytm courier parcel
    ka ki ko na ne to ye wo woh iska uska inka unka isme usme wala wali
    batao bolo kaha kahan raha rahi tha thi thik
    first second third fourth fifth previous next other another both either
    neither each
    aap acha achha aur bhai bhaiya bhej bhejo chahiye dedo de do ha haan
    hai hain hi ho hoga hona jaldi ji kab kaise kar karo katna keemat
    kitna kitne kitni kya kyun le lena lo mai main matlab me mera mere
    mil mujhe nahi nai par pe pls sab sahi se theek tum tumhara ye yeh
    """.split()
)

# Deliberately NOT in the set above: "told", "said", "owner", "friend",
# "bought", "wears" and their kin. They read like filler, but they are the
# exact signal that a message is REPORTING about a perfume rather than
# asking for one — and message_focus tells those apart by looking at which
# content words a match left unexplained. Silencing them gave "the owner
# told me sauvage is really nice apparently" a focus of 1.0, making it
# indistinguishable from someone typing the bare product name.

# Ordinary English words that also happen to be perfume-name words. These
# are real, matchable tokens — "Cool Water", "Lazy Sunday Morning", "Black
# Opium" all depend on them — but a match resting on ONE of them and nothing
# else is not a customer naming a product.
#
# IDF cannot catch this, and that is the whole point: "water" appears in
# fewer catalog names than "sauvage" does, so by catalog statistics it is
# the MORE distinctive of the two. What separates them is English, not the
# catalog — which is why this is a list rather than a threshold. Confirmed
# live in the benchmark: "byredo gypsy water" (a perfume we do not carry)
# returned five unrelated products, all on the strength of "water" alone.
_WEAK_ANCHOR_TOKENS: frozenset[str] = frozenset(
    """
    after afternoon air amber angel angels aqua best black blend blue bouquet
    cherry club coffee day dream dreams edition effect essence extreme fire
    flora forever fresh girl gold green home ice imperial king last latte
    lazy leather light long lost love male man men million most musk night
    note ocean old one open orient play power pure rain red rich rose roses
    royal rush share show silk silver sky sport star story summer sun sunday
    sweet swim touch tree valley vanilla velvet wanted warm water white wild
    wood word you young
    """.split()
)

# Concentration/packaging noise that appears in catalog names but that
# customers routinely omit. Not deleted — dropped in an ALTERNATIVE variant,
# so "Rasasi Hawas EDP" is reachable whether or not the customer types EDP.
_NOISE_TOKENS: frozenset[str] = frozenset(
    {
        "edp", "edt", "edc", "edv", "eau", "de", "du", "des", "la", "le",
        "les", "parfum", "parfums", "perfume", "fragrance", "fragrances",
        "og", "ml", "pour", "by", "and", "the", "a", "an", "of",
    }
)


# Every word this module considers too generic to identify a product on its
# own, in one set. Re-exported for app.catalog_upload, which filters the same
# words out of the keyword lists it writes into catalog_data.json. Those
# keyword lists are no longer what matching runs on — this index derives
# everything from display_name/clone_of — but they remain part of the
# catalog file's shape.
GENERIC_STOPWORDS = _MESSAGE_STOPWORDS | _NOISE_TOKENS | _WEAK_ANCHOR_TOKENS

# Public aliases for app.matcher, which needs to tell "words that carry no
# product identity" apart from "a name the customer just introduced".
MESSAGE_STOPWORDS = _MESSAGE_STOPWORDS
NOISE_TOKENS = _NOISE_TOKENS


_WORD_RE = re.compile(r"[a-z0-9]+")

# Decant sizes are a separate question from which perfume was named (see
# app.matcher.extract_requested_size_ml, which reads them independently), so
# they are removed before matching — "10ml" is not evidence about a product,
# and leaving it in only creates junk n-grams like "sauvage10ml".
_SIZE_RE = re.compile(r"\b\d{1,4}\s*ml\b")

# Community shorthand customers actually type. These are not in any catalog
# name, so no amount of fuzzy tolerance can reach them — the expansion has
# to be stated. Deliberately short and unambiguous: an abbreviation that
# could plausibly mean two different products does not belong here.
_ALIASES: dict[str, str] = {
    "cdni": "club de nuit intense",
    "cdnim": "club de nuit intense man",
    "cdnu": "club de nuit urban",
    "cdnum": "club de nuit urban man",
    "bdc": "bleu de chanel",
    "adg": "acqua di gio",
    "adgp": "acqua di gio profumo",
    "svg": "sauvage",
    "sce": "supremacy collectors edition",
    "snoi": "supremacy not only intense",
    "cdni": "club de nuit intense",
    "tfoud": "tom ford oud wood",
    "lidge": "l immensite",
    "mfk": "maison francis kurkdjian",
    "br540": "baccarat rouge 540",
    "br": "baccarat rouge",
    "ysl": "yves saint laurent",
    "ck": "calvin klein",
    "jpg": "jean paul gaultier",
    "ysllb": "yves saint laurent la nuit de l homme",
    "lidl": "la nuit de l homme",
    "gitm": "good girl",
    "1mil": "one million",
    "1million": "one million",
    "dhs": "dior homme sport",
    "vip": "212 vip",
}


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens. '&' becomes 'and' first so 'Oud &
    Roses' and 'Oud and Roses' tokenize identically."""
    return _WORD_RE.findall(text.lower().replace("&", " and "))


def tokenize_message(text: str) -> list[str]:
    """
    Tokenize an inbound customer message: sizes stripped, community
    abbreviations expanded. Catalog names go through plain tokenize()
    instead — neither transformation makes sense on the index side.

    An abbreviation is only expanded when the catalog does not already know
    the token. "YSL" is a real token in these display names, so expanding it
    to "yves saint laurent" actively hurt: "laurent" scores 92 against the
    unrelated catalog token "lauren", which was enough to answer "YSL Myslf
    Le Parfum" with a Ralph Lauren product. Aliases are a last resort for
    shorthand no amount of fuzzy tolerance could reach, not a rewrite of
    vocabulary the index already has.
    """
    return tokenize_message_with_sizes(text)[0]


def tokenize_message_with_sizes(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """
    Tokenize a message AND record where each decant size appeared.

    Returns (tokens, sizes) where sizes is [(position, ml), ...] and position
    is the number of real tokens that came before that size. Knowing WHERE a
    size was written is what lets "9pm rebel 3ml, khamrah 5ml, kaaf 10ml" be
    answered with three different sizes. Reading one size for the whole
    message quoted 3ml for all three — a customer ordering three decants got
    two wrong prices.
    """
    _ensure_index()
    tokens: list[str] = []
    sizes: list[tuple[int, int]] = []

    lowered = text.lower()
    cursor = 0
    for match in _SIZE_RE.finditer(lowered):
        for token in _expand(lowered[cursor : match.start()]):
            tokens.append(token)
        digits = "".join(c for c in match.group(0) if c.isdigit())
        if digits:
            sizes.append((len(tokens), int(digits)))
        cursor = match.end()

    tokens.extend(_expand(lowered[cursor:]))
    return tokens, sizes


# Acronyms shorter than this are too collision-prone to trust ("bo", "cn"),
# and a name needs at least this many words to have a meaningful one.
# Measured, not guessed. Generating an acronym for every product name
# produced 1,706 of them and made the bot WORSE across the full benchmark —
# strict accuracy 77.6% -> 76.4%, wrong answers 1.13% -> 1.50%. Three-letter
# acronyms are the problem: with 1,300 products they collide with each other
# and, worse, with real typos, so a misspelling gets rewritten into an
# unrelated product's name before it can be corrected.
#
# Four characters and up, and never one that is a plausible typo of a real
# catalog word, keeps the useful ones ("cdnim", "swya") without that cost.
_ACRONYM_MIN_LEN = 4
_ACRONYM_MIN_WORDS = 4


def _build_acronyms(by_pid_tokens: dict[str, list[tuple[str, ...]]]) -> dict[str, str]:
    """
    Derive short names customers actually type — "cdnim" for Club de Nuit
    Intense Man, "swya" for Stronger With You Absolutely — by taking the
    initials of every product name.

    An acronym is kept ONLY if it points at exactly one product. Perfume
    shorthand is worth supporting, but a short name resolving to the wrong
    bottle is worse than not recognising it at all, and with 1,300 products
    collisions are common — so ambiguous ones are dropped rather than
    guessed at. Anything the catalog already contains as a real word is left
    alone too, for the same reason "YSL" must not expand: the literal token
    is the better match.
    """
    owners: dict[str, set[str]] = {}
    expansion: dict[str, str] = {}

    for pid, variants in by_pid_tokens.items():
        for tokens in variants:
            # One-letter fragments are punctuation debris, not words:
            # "Collector's Edition" tokenizes to collector + s + edition, and
            # letting that "s" contribute an initial turned the customer's
            # real abbreviation "sce" into "sce"+junk and lost it.
            words = [t for t in tokens if t not in _NOISE_TOKENS and len(t) > 1]
            if len(words) < _ACRONYM_MIN_WORDS:
                continue
            acronym = "".join(w[0] for w in words)
            if len(acronym) < _ACRONYM_MIN_LEN:
                continue
            owners.setdefault(acronym, set()).add(pid)
            expansion.setdefault(acronym, " ".join(words))

    safe: dict[str, str] = {}
    for acronym, pids in owners.items():
        if len(pids) != 1 or acronym in _idf:
            continue
        # An acronym that is one edit from a real catalog word would capture
        # that word's typos and rewrite them into a different product.
        near = any(
            DamerauLevenshtein.distance(acronym, token, score_cutoff=1) <= 1
            for token in _short_vocab_by_key.get((acronym[0], len(acronym)), ())
        )
        if not near:
            safe[acronym] = expansion[acronym]
    return safe


def _expand(fragment: str) -> list[str]:
    """Tokenize a fragment, expanding community shorthand the catalog does
    not already know (see tokenize_message)."""
    out: list[str] = []
    for token in tokenize(fragment):
        curated = _ALIASES.get(token)
        if curated and token in _idf:
            # The catalog knows this word AND we know what customers mean by
            # it — "BDC" is a real clone_of token and also how people write
            # Bleu de Chanel. Offer both readings rather than choosing.
            out.append(token)
            out.extend(tokenize(curated))
            continue
        expansion = curated or _acronyms.get(token)
        if expansion and token not in _idf:
            out.extend(tokenize(expansion))
        else:
            out.append(token)
    return out


# --- Index -----------------------------------------------------------------

@dataclass(frozen=True)
class Variant:
    """One way of naming a perfume, with per-token IDF weights."""

    pid: str
    tokens: tuple[str, ...]
    weights: tuple[float, ...]
    total_weight: float
    kind: str  # "name" | "clone"
    # Whether the FULL name this variant was cut from contains any word that
    # identifies a product — as opposed to ordinary English and packaging
    # noise. Recorded per source name rather than per variant on purpose:
    # "Ralph Lauren Polo Blue EDT" has a suffix variant of ("blue", "edt"),
    # and judging that variant on its own would conclude the product simply
    # has no substantive words and wave the match through. See the check in
    # _score_variants.
    source_substantive: bool
    # True only for the variant that begins at the START of the full name —
    # the one whose leading token is the brand. Suffix variants begin later,
    # so their leading token is a product word. See the brand-only check in
    # _score_variants for why that difference matters.
    at_name_start: bool


@dataclass
class Scored:
    perfume_id: str
    score: float
    coverage: float               # how much of the perfume's name was typed
    similarity: float             # how cleanly the typed part was spelled (0-1)
    kind: str
    consumed: frozenset[int]      # message token indices this match used
    matched_tokens: tuple[str, ...]


_variants: list[Variant] = []
_by_token: dict[str, list[int]] = {}
_vocab: list[str] = []
_idf: dict[str, float] = {}
# (first letter, length) -> short vocabulary tokens, for the edit-distance
# pass. Both keys are things a single edit cannot change past the rules in
# _token_matches (first letter must agree, length may differ by at most
# _MAX_EDITS), so bucketing this way turns a full-vocabulary scan per query
# into a handful of comparisons. Without it the pass dominated runtime.
_short_vocab_by_key: dict[tuple[str, int], list[str]] = {}
# Auto-derived short names ("cdnim" -> "club de nuit intense man"), kept only
# where they identify exactly one product. See _build_acronyms.
_acronyms: dict[str, str] = {}
_built_for: int = -1


def _is_substantive(token: str) -> bool:
    """True if this word can identify a product on its own terms — i.e. it
    is neither ordinary English (_WEAK_ANCHOR_TOKENS) nor packaging noise
    (_NOISE_TOKENS)."""
    return token not in _WEAK_ANCHOR_TOKENS and token not in _NOISE_TOKENS


def _variant_token_sets(pid: str, data: dict) -> list[tuple[tuple[str, ...], str, bool]]:
    """
    Every distinct (tokens, kind) a customer might use for this perfume.

    Catalog display names are "Brand Product Name", and customers routinely
    type only the product part — "rebel", not "Afnan 9PM Rebel". Rather than
    relying on the `brand` column (present only on entries produced by a
    catalog upload; most of the live catalog has no brand field at all, which
    is why an earlier brand-prefix strip silently did nothing for ~all of
    it), every contiguous SUFFIX of the name is indexed as its own variant.
    That covers brand-stripping without needing to know what the brand is,
    and covers the catalog's duplicated-brand rows ("Arabiyat Prestige
    Arabiyat Prestige") for free.

    Suffixes are safe rather than permissive because each variant is scored
    against its OWN total weight: a suffix that is nothing but generic
    tokens ("edp", "man edp") can reach coverage 1.0 and still score far
    below MIN_SCORE, since generic tokens carry almost no IDF weight.
    """
    full = tokenize(data.get("display_name") or "")
    if not full:
        return []

    clone = tokenize(data.get("clone_of") or "")

    full_substantive = any(_is_substantive(t) for t in full)
    clone_substantive = any(_is_substantive(t) for t in clone)

    out: list[tuple[tuple[str, ...], str, bool, bool]] = []
    seen: set[tuple[str, ...]] = set()

    def add(tokens: list[str], kind: str, substantive: bool, at_start: bool) -> None:
        # Deduplicate repeated tokens while preserving order — some catalog
        # names literally repeat a word, which would otherwise double-count
        # its weight in both the evidence and the denominator.
        deduped: list[str] = []
        for t in tokens:
            if t not in deduped:
                deduped.append(t)
        key = tuple(deduped)
        if key and key not in seen:
            seen.add(key)
            out.append((key, kind, substantive, at_start))

    for start in range(len(full)):
        suffix = full[start:]
        add(suffix, "name", full_substantive, start == 0)
        add([t for t in suffix if t not in _NOISE_TOKENS], "name", full_substantive, start == 0)

    for start in range(len(clone)):
        suffix = clone[start:]
        add(suffix, "clone", clone_substantive, start == 0)
        add([t for t in suffix if t not in _NOISE_TOKENS], "clone", clone_substantive, start == 0)

    return out


def _token_weight(token: str, idf: float) -> float:
    """IDF, with 1-2 character tokens held back. Anything longer is left to
    IDF alone: a short-but-distinctive token like "9pm" (5 entries) or "bdc"
    (a clone abbreviation) is exactly the token a customer leans on, so
    penalising it by length would undo the thing that makes it useful."""
    return idf * 0.4 if len(token) <= 2 else idf


def build_index() -> None:
    """(Re)build the index from the live catalog. Called lazily and by
    app.catalog.reload_catalog, so a dashboard catalog publish takes effect
    without a redeploy — same hot-reload contract PERFUMES already has."""
    global _variants, _by_token, _vocab, _idf, _short_vocab_by_key, _acronyms, _built_for

    raw: list[tuple[str, tuple[str, ...], str, bool, bool]] = []
    doc_freq: dict[str, set[str]] = {}

    for pid, data in PERFUMES.items():
        for tokens, kind, substantive, at_start in _variant_token_sets(pid, data):
            raw.append((pid, tokens, kind, substantive, at_start))
            for t in tokens:
                doc_freq.setdefault(t, set()).add(pid)

    n_docs = max(len(PERFUMES), 1)
    idf = {t: math.log(n_docs / len(pids)) + 0.5 for t, pids in doc_freq.items()}

    variants: list[Variant] = []
    by_token: dict[str, list[int]] = {}

    for pid, tokens, kind, substantive, at_start in raw:
        weights = tuple(_token_weight(t, idf[t]) for t in tokens)
        total = sum(weights)
        if total <= 0:
            continue
        index = len(variants)
        variants.append(
            Variant(pid, tokens, weights, total, kind, substantive, at_start)
        )
        for t in set(tokens):
            by_token.setdefault(t, []).append(index)

    short_by_key: dict[tuple[str, int], list[str]] = {}
    for token in by_token:
        if _SHORT_TOKEN_MAX < len(token) <= _EDIT_DISTANCE_MAX_LEN:
            short_by_key.setdefault((token[0], len(token)), []).append(token)

    _variants = variants
    _by_token = by_token
    _vocab = list(by_token)
    _idf = idf
    _short_vocab_by_key = short_by_key
    _built_for = len(PERFUMES)
    _decompose.cache_clear()

    # Acronyms are taken from the full name AND from each brand-less
    # suffix of it, because customers drop the brand: "Armani Stronger With
    # You Absolutely" is typed "swya", not "aswya".
    by_pid_tokens: dict[str, list[tuple[str, ...]]] = {}
    for pid, data in PERFUMES.items():
        full = tokenize(data.get("display_name") or "")
        by_pid_tokens[pid] = [tuple(full[i:]) for i in range(min(len(full), 3))]
    _acronyms = _build_acronyms(by_pid_tokens)


def _ensure_index() -> None:
    if not _variants or _built_for != len(PERFUMES):
        build_index()


# --- Message -> catalog token similarity ------------------------------------

def _message_queries(tokens: list[str]) -> list[tuple[str, list[frozenset[int]]]]:
    """
    Query strings to look up in the catalog vocabulary, each paired with
    EVERY set of message token indices where it occurs.

    Single tokens, plus adjacent tokens joined WITHOUT a space — the joined
    forms are what recover a name the customer split across a space bar slip
    ("sau vage" -> "sauvage", "club denuit" -> "clubdenuit" is not needed but
    "de nuit" -> "denuit" is). Stopwords are skipped as standalone queries
    but kept inside joined forms, so "musk is great" can still reach a
    product actually named that.
    """
    # One entry per distinct query TEXT, carrying every place it occurs.
    # Deduplicating by text alone silently threw away repeat occurrences,
    # and a shopping list is full of them: in "maison margiela by the
    # fireplace, maison margiela jazz club" the second "maison margiela" was
    # never recorded, so Jazz Club bound to the FIRST one and By The
    # Fireplace was left without its brand. Same for "elixir" appearing in
    # two different products on one line.
    #
    # Still one vocabulary lookup per distinct text — the occurrences share
    # it — so nothing gets slower.
    queries: dict[str, list[frozenset[int]]] = {}

    def add(text: str, idx: frozenset[int]) -> None:
        # Single characters included on purpose: the digit in "TAJ 2" or
        # "SHK 1" is the ONLY thing separating two real catalog entries, and
        # dropping it made those pairs indistinguishable. Safe because short
        # catalog tokens are required to match exactly (see _token_matches),
        # so a one-character query can only ever hit a one-character token.
        if not text:
            return
        spots = queries.setdefault(text, [])
        if idx not in spots:
            spots.append(idx)

    for i, tok in enumerate(tokens):
        if tok not in _MESSAGE_STOPWORDS:
            add(tok, frozenset({i}))

    for n in (2, 3):
        for i in range(len(tokens) - n + 1):
            window = tokens[i : i + n]
            # A window made entirely of filler is filler — joining it just
            # manufactures a string that can collide with a real product
            # token by accident. Confirmed: "tell me" joins to "tellme",
            # which scores 80 against the catalog token "elle" and answered
            # "please tell me more" with a price card for ARABIYAT NYLA
            # VANI-ELLE. A window containing even one real word is kept, so
            # "musk is great" and "club de nuit" are unaffected.
            if all(w in _MESSAGE_STOPWORDS for w in window):
                continue
            add("".join(window), frozenset(range(i, i + n)))

    return list(queries.items())


@functools.lru_cache(maxsize=4096)
def _decompose(word: str) -> tuple[str, ...]:
    """
    Split a space-joined word into catalog tokens, or return nothing.

    The split must account for the WHOLE word. That requirement is the
    entire point, and it replaced a substring search that had a nasty
    failure mode: "5ml for the Fahrenheit EDT, and how much difference will
    bluedart air have?" answered with a price card for Ralph Lauren Polo
    Blue EDT. "bluedart" — the courier — contains "blue", and "blue" plus
    the "edt" the customer typed about Fahrenheit happen to be the complete
    name of a suffix variant of that product. Nothing the customer said had
    anything to do with it.

    Demanding a full decomposition kills that: "bluedart" is "blue" + "dart",
    and "dart" is not a catalog word, so the split fails and "blue" is never
    offered. "elegantvetiver" and "clubdenuit" split cleanly and still work.

    Segments must match a catalog token exactly. A customer who runs two
    words together is normally spelling them right — the missing space is
    the mistake — and allowing fuzzy segments here would re-open the same
    hole from a different angle.
    """
    n = len(word)
    # reachable[i] = the token sequence spelling word[:i], if any.
    reachable: list[tuple[str, ...] | None] = [()] + [None] * n
    for end in range(_EMBED_MIN_TOKEN_LEN, n + 1):
        for start in range(0, end - _EMBED_MIN_TOKEN_LEN + 1):
            if reachable[start] is None:
                continue
            segment = word[start:end]
            if segment in _by_token:
                reachable[end] = reachable[start] + (segment,)
                break
    parts = reachable[n]
    # A single "split" that is just the whole word is not a split; the
    # ordinary token pass already handles that case.
    return parts if parts and len(parts) > 1 else ()


def _token_matches(tokens: list[str]) -> dict[str, tuple[float, frozenset[int]]]:
    """
    Best similarity (0-1) of every catalog vocabulary token against this
    message, plus which message token indices produced it.

    Every query is scored against the ENTIRE vocabulary (~1.4K tokens, a
    handful of queries per message — a few milliseconds in rapidfuzz's C
    core), so nothing is lost to a shortlist cutoff the way it was when the
    LLM was handed a pre-narrowed top-25 built from noisy n-gram scores.
    """
    _ensure_index()
    queries = _message_queries(tokens)
    if not queries or not _vocab:
        return {}

    # Every PLACE a catalog word occurs, not just the best one. Keeping only
    # one position quietly fabricated products out of a customer's wishlist:
    # given a list naming sixteen perfumes, "al haramain" (word 0), "amber"
    # (9), "oud" (36) and "black" (45) were stitched into "Al Haramain Amber
    # Oud Black Edition" — a product nobody had mentioned, assembled from
    # four different lines. It also made repeats unmatchable, so "Club de
    # Nuit Intense Man" and "Club de Nuit Untold" in one message could only
    # ever resolve to whichever came first.
    matches: dict[str, list[tuple[float, frozenset[int]]]] = {}

    def offer(vocab_token: str, sim: float, idx: frozenset[int]) -> None:
        spots = matches.setdefault(vocab_token, [])
        for i, (prev_sim, prev_idx) in enumerate(spots):
            if prev_idx == idx:
                if sim > prev_sim:
                    spots[i] = (sim, idx)
                return
        spots.append((sim, idx))

    for query, spots in queries:
        # A word the catalog already knows is taken at face value — no fuzzy
        # alternatives are offered for it. Someone who typed a real catalog
        # word meant that word, and fuzzy expansion actively hurt here
        # because rarer tokens carry more IDF weight: "aventus" (typed
        # exactly, and a real token) scored 93 against "aventure", which is
        # unique in the catalog, so "Al Haramain L'Aventure Intense" beat
        # every actual Aventus. Same shape as the alias rule in
        # tokenize_message: do not rewrite vocabulary the index has.
        if query in _by_token:
            for idx in spots:
                offer(query, 1.0, idx)
            continue

        for vocab_token, score, _pos in process.extract(
            query,
            _vocab,
            scorer=fuzz.ratio,
            score_cutoff=TOKEN_SIM_MIN,
            limit=None,
        ):
            # Short catalog tokens only ever match exactly — edit distance
            # on 1-3 characters is noise, not tolerance.
            if len(vocab_token) <= _SHORT_TOKEN_MAX and score < 100:
                continue
            if score < _SIM_MIN_BY_LEN.get(len(vocab_token), TOKEN_SIM_MIN):
                continue
            for idx in spots:
                offer(vocab_token, float(score) / 100.0, idx)

        # Short-token second chance, by edit distance rather than ratio —
        # see _MAX_EDITS. Scanned directly against the vocabulary because
        # these are exactly the pairs the ratio cutoff above filters out
        # before they are ever seen.
        # The buckets encode the two hard constraints, so only genuinely
        # possible candidates are ever compared:
        #
        #   * First letter must agree. At four characters "one edit apart"
        #     is a wide net — "rose"/"dose", "eros"/"oros", "vage"/"sage"
        #     are each one edit apart and none is a typo of the other.
        #     People mistype a word's middle and end constantly and its
        #     first letter almost never, so anchoring there keeps the real
        #     recoveries ("kaff" for "kaaf", "rebl" for "rebel") and drops
        #     the coincidences.
        #   * Length can differ by at most the edit budget.
        if _SHORT_TOKEN_MAX < len(query) <= _EDIT_DISTANCE_MAX_LEN + _MAX_EDITS:
            for length in range(len(query) - _MAX_EDITS, len(query) + _MAX_EDITS + 1):
                for vocab_token in _short_vocab_by_key.get((query[0], length), ()):
                    if (
                        DamerauLevenshtein.distance(
                            query, vocab_token, score_cutoff=_MAX_EDITS
                        )
                        <= _MAX_EDITS
                    ):
                        # Scored as a near-match rather than a perfect one,
                        # so a cleanly-typed token always outranks a
                        # recovered typo.
                        for idx in spots:
                            offer(vocab_token, 1.0 - 0.1 * _MAX_EDITS, idx)

    # Space-joined input: recover the words inside a long message token
    # ("elegantvetiver" -> "elegant" + "vetiver"). See _decompose for why
    # this demands a complete split rather than hunting for substrings.
    for i, tok in enumerate(tokens):
        if len(tok) < _EMBED_MIN_MESSAGE_TOKEN_LEN or tok in _MESSAGE_STOPWORDS:
            continue
        # Same principle as above: if the word is itself a catalog word,
        # it is not two words run together.
        if tok in _by_token:
            continue
        idx = frozenset({i})
        for vocab_token in _decompose(tok):
            offer(vocab_token, _EMBED_DISCOUNT, idx)

    return matches


# --- Scoring ---------------------------------------------------------------

def _score_variants(
    matches: dict[str, list[tuple[float, frozenset[int]]]],
    available: frozenset[int],
    content: frozenset[int] = frozenset(),
    weak: frozenset[int] = frozenset(),
    literal: dict[str, list[frozenset[int]]] | None = None,
) -> dict[str, Scored]:
    """Best-scoring variant per perfume, considering only matches whose
    message tokens are still unconsumed. `content` is the message's
    non-filler token indices (used for the message-coverage factor) and
    `weak` its ordinary-English ones (see _WEAK_ANCHOR_TOKENS), and
    `literal` maps every message word — filler included — to where it
    occurs.

    Filler words are excluded from matching so they cannot anchor a match,
    but they are still words the customer typed, and pretending they are
    absent understates how much of a name was written. "Stronger With You
    Absolutely", typed out in full, reached only 0.55 coverage because
    "with" and "you" were invisible to the scorer — enough to get it
    discarded from a six-perfume order. They now count toward coverage while
    still being barred from anchoring anything."""
    usable = {
        token: [(sim, idx) for sim, idx in spots if idx <= available]
        for token, spots in matches.items()
    }
    usable = {token: spots for token, spots in usable.items() if spots}
    if not usable:
        return {}

    candidate_variants: set[int] = set()
    for token in usable:
        candidate_variants.update(_by_token.get(token, ()))

    best: dict[str, Scored] = {}

    for vi in candidate_variants:
        variant = _variants[vi]

        # A product name is words standing TOGETHER. Anchor on the
        # highest-weight word this variant matched, then take each other
        # word at whichever of its occurrences sits nearest that anchor —
        # so "Club de Nuit Untold" binds to the "club de nuit" next to
        # "untold", not to an earlier one belonging to a different product
        # on the same list.
        anchor_token = max(
            (t for t in variant.tokens if t in usable),
            key=lambda t: variant.weights[variant.tokens.index(t)],
            default=None,
        )
        if anchor_token is None:
            continue
        anchor_sim, anchor_idx = max(usable[anchor_token])
        anchor_at = min(anchor_idx)

        evidence = 0.0
        matched_weight = 0.0
        consumed: set[int] = set()
        matched: list[str] = []

        for token, weight in zip(variant.tokens, variant.weights):
            spots = usable.get(token)
            if not spots:
                # Not matchable, but did they type it anyway? Filler words
                # count toward coverage; they never become `matched`, so
                # every anchoring rule below still sees only real hits.
                here = [i for i in (literal or {}).get(token, ()) if i <= available]
                if here:
                    idx = min(here, key=lambda s: abs(min(s) - anchor_at))
                    evidence += weight
                    matched_weight += weight
                    consumed.update(idx)
                continue
            sim, idx = min(spots, key=lambda s: (abs(min(s[1]) - anchor_at), -s[0]))
            evidence += weight * sim
            matched_weight += weight
            consumed.update(idx)
            matched.append(token)

        if evidence <= 0:
            continue

        # Reject a match whose words are strewn across the message. This is
        # what stopped a sixteen-perfume wishlist from producing "Al Haramain
        # Amber Oud Black Edition" out of words at positions 0, 1, 9, 36 and
        # 45 — a product the customer never named, assembled from four
        # different lines of their list.
        span = max(consumed) - min(consumed) + 1
        if span > len(variant.tokens) + _MAX_NAME_GAP:
            continue

        # Matching nothing but the first word of the FULL name is matching
        # the brand, not the product. Names read brand-first, so "byredo
        # gypsy water" hitting only "byredo" inside the clone name "Byredo
        # Rose Noir" is not a customer asking for Rose Noir — and with 1,200
        # entries a brand word points at dozens of products at once.
        #
        # at_name_start is what keeps this from over-firing, and it is not a
        # detail: without it the rule also blocked the leading word of every
        # SUFFIX variant, which is a product word rather than a brand.
        # "asrar" — spelled correctly — returned nothing at all, because the
        # only variant it could carry alone was ("asrar", "al", "lail") and
        # it happened to be that variant's first token.
        if (
            len(matched) == 1
            and variant.at_name_start
            and len(variant.tokens) > 1
            and matched[0] == variant.tokens[0]
        ):
            continue

        # Nor may a match rest on a single ordinary English word — see
        # _WEAK_ANCHOR_TOKENS. In combination those words are fine; alone
        # they are not someone naming a product.
        #
        # Checked on BOTH sides, because either alone leaks. The catalog
        # side catches "water" hitting the token "water"; the message side
        # catches "rose" hitting the token "rosa", one edit away and not an
        # English word itself — the customer still only typed an ordinary
        # noun, which is the thing that makes it too little to go on.
        if len(matched) == 1 and (
            matched[0] in _WEAK_ANCHOR_TOKENS or (weak and consumed <= weak)
        ):
            continue

        # More generally: a match must rest on at least one word that
        # actually identifies a product. Ordinary English ("blue", "water")
        # and packaging noise ("edt", "edp") are real parts of real names,
        # but a match assembled ONLY out of them names nothing.
        #
        # This is what let "Fahrenheit EDT ... bluedart air" resolve to a
        # product: "blue" and "edt" together are the complete name of a
        # suffix variant, so coverage was 1.0 and the score looked strong,
        # while neither word came from anything the customer meant. The
        # exemption is for the rare catalog entry whose name is nothing but
        # such words — without it those become unreachable — and it is
        # judged on the full name, not this variant (see Variant.
        # source_substantive).
        if variant.source_substantive and not any(_is_substantive(t) for t in matched):
            continue

        # A match must rest on at least one word that is not conversational
        # filler. This matters in the later consume-and-rescore rounds: once
        # the real product name has been taken out of "what is the price of
        # X", all that is left is filler, and without this a stray n-gram
        # built from those leftovers ("theprice", "priceof") could add a
        # second, entirely imaginary perfume to the reply.
        if content and not (consumed & content):
            continue

        # Coverage answers "which words of the name did the customer type",
        # deliberately NOT "how well did they spell them" — spelling quality
        # is already priced into `evidence`. Folding it in twice punished a
        # single typo in a short name by ~20%, which was enough to push
        # "savuage" below the match threshold entirely while leaving
        # coincidental short-word collisions above it.
        coverage = matched_weight / variant.total_weight
        score = evidence * (COVERAGE_FLOOR + (1.0 - COVERAGE_FLOOR) * coverage)

        # How much of what the customer actually typed this match accounts
        # for. Mild, but it is what separates near-identical catalog
        # neighbours: "FW/FRENCH AVENUE TAJ 2 EDP" and "…TAJ 1 EDP" score
        # identically on everything else, because the digit that tells them
        # apart is one character and carries almost no IDF weight. The entry
        # holding the digit the customer typed explains one more word of the
        # message than its sibling, and that is enough to order them right.
        if content:
            msg_cov = len(consumed & content) / len(content)
            score *= MESSAGE_COVERAGE_FLOOR + (1.0 - MESSAGE_COVERAGE_FLOOR) * msg_cov

        # A product we stock under the name the customer typed beats one
        # that merely clones something by that name. Applied to the score
        # rather than left to the tie-break, because clone names are often
        # SHORTER than the stocking product's own name and so reach a higher
        # coverage on the same words: "ombre leather" scored Maison Alhambra
        # Opulence Leather (clone_of "Ombr Leather", two tokens, coverage
        # 1.0) above Tom Ford Ombre Leather EDP, which is the actual thing
        # asked for.
        if variant.kind == "clone":
            score *= _CLONE_PENALTY

        current = best.get(variant.pid)
        if current is None or score > current.score:
            best[variant.pid] = Scored(
                perfume_id=variant.pid,
                score=score,
                coverage=coverage,
                similarity=evidence / matched_weight if matched_weight else 0.0,
                kind=variant.kind,
                consumed=frozenset(consumed),
                matched_tokens=tuple(matched),
            )

    return best


def _is_the_whole_message(scored: "Scored", content: frozenset[int]) -> bool:
    """
    True when this match accounts for every real word the customer typed.

    Deliberately about the MESSAGE, not the name. Requiring the product's
    whole name as well was too strict and lost the commonest shape of all:
    "yraa" is a customer asking for Yara, but Yara's full names are "Yara
    Candy", "Yara Elixir" and so on, so name coverage is only 0.46 and the
    match was thrown away. What matters is that they typed nothing else —
    there is no other subject in the message for this to be wrong about.
    """
    return bool(content) and content <= scored.consumed


def _rank_key(s: Scored) -> tuple:
    """Best first. Score decides; equal scores prefer the perfume whose own
    NAME the customer typed over one that merely clones something by that
    name, then the one whose name is most completely accounted for — so a
    bare "sauvage" leads with Dior Sauvage rather than an arbitrary clone."""
    return (-s.score, s.kind != "name", -s.coverage, s.perfume_id)


def _collapse_duplicates(ranked: list[Scored]) -> list[Scored]:
    """Catalog entries whose display names are identical render an identical
    card — keep only the best-scoring one so a duplicate row never shows up
    as a second 'candidate' the customer has to choose between."""
    seen: set[str] = set()
    out: list[Scored] = []
    for s in ranked:
        name = (PERFUMES.get(s.perfume_id, {}).get("display_name") or "").strip().lower()
        key = name or s.perfume_id
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def search(text: str, limit: int = MAX_RESULTS) -> list[Scored]:
    """
    Every perfume this message plausibly names, best first.

    Returns [] when nothing clears MIN_SCORE — the message did not identify
    a product, and the caller should stay silent rather than show its best
    guess. A single-element result is an unambiguous match; several elements
    mean either the customer named several perfumes or one name genuinely
    fits several products (see the module docstring).
    """
    _ensure_index()
    tokens = tokenize_message(text)
    if not tokens:
        return []

    matches = _token_matches(tokens)
    if not matches:
        return []

    min_score = getattr(settings, "MATCH_MIN_SCORE", MIN_SCORE_DEFAULT)
    available = frozenset(range(len(tokens)))
    content = frozenset(
        i for i, t in enumerate(tokens) if t not in _MESSAGE_STOPWORDS
    )
    weak = frozenset(i for i, t in enumerate(tokens) if t in _WEAK_ANCHOR_TOKENS)
    literal: dict[str, list[frozenset[int]]] = {}
    for i, t in enumerate(tokens):
        literal.setdefault(t, []).append(frozenset({i}))
    results: list[Scored] = []

    for _ in range(_MAX_ROUNDS):
        scored = _score_variants(matches, available, content & available, weak, literal)
        if not scored:
            break

        # A candidate resting on ONE fuzzily-read word, inside a message
        # that says other things too, is a coincidence rather than a
        # product. "the owner told me sauvage is really nice" read "told" as
        # "untold" and returned Club de Nuit Untold — beating the Sauvage
        # actually named, because "untold" is rare in the catalog and
        # "sauvage" (26 products) is not. Rarity is not evidence when the
        # word was not even the one written. One word IS enough when it is
        # the whole message ("kaff", "yraa"), which is how people usually
        # ask.
        #
        # Discarded candidate by candidate, never by abandoning the round:
        # breaking out here also threw away every later product in the same
        # message and cost a full point of recall across the catalog.
        message_content = content & available
        ranked = [
            s
            for s in _collapse_duplicates(sorted(scored.values(), key=_rank_key))
            if len(s.matched_tokens) > 1
            or s.similarity >= 0.999
            or _is_the_whole_message(s, message_content)
        ]
        # Later rounds additionally require the customer to have written a
        # real share of the name. Applied as a FILTER, never as a reason to
        # abandon the round: a real order listed seven products, the third
        # of which the customer had abbreviated ("North Stag III" for "North
        # Stag Expressions III Trois", coverage 0.58), and breaking there
        # silently discarded the four products listed after it.
        if results:
            ranked = [s for s in ranked if _worth_an_extra_round(s)]
        if not ranked:
            break

        top = ranked[0]
        whole = _is_the_whole_message(top, message_content)

        # The score floor measures how distinctive the matched words are, and
        # popularity destroys distinctiveness. When the customer typed
        # nothing but this product, that floor is the wrong question — but a
        # residual bar remains so a genuinely threadbare match still cannot
        # pass on being the only thing in a one-word message.
        if top.score < (min_score * _WHOLE_MESSAGE_FLOOR if whole else min_score):
            break


        # Co-candidates are decided by WHICH WORDS a match rests on, not by
        # how close its score is. Two different situations both need to be
        # handled and neither is a score question:
        #
        #   * Coincidence, which must be excluded. "club denuit intense"
        #     scores "Club de Nuit Intense Man" at 6.1 off four words and
        #     "Bella Rouge Intenso" at 6.0 off the single word "intense" —
        #     numerically a tie, but only one of them is a thing the
        #     customer said.
        #   * A product family, which must be included. A bare "9pm" fits
        #     "Afnan 9PM" exactly and "Afnan 9PM Rebel", "9PM Night Out"
        #     and "9PM Elixir" partially, so the exact one scores highest —
        #     but the customer's word matches all four equally well, and the
        #     four have DIFFERENT prices. Answering with one card would be
        #     quoting a price for a product they may not have meant; four
        #     cards in one message cannot be wrong.
        #
        # Matching on the same message words AND the same name words is what
        # separates them. "9pm rebel" still resolves to exactly one product,
        # because the base "Afnan 9PM" cannot account for "rebel".
        top_tokens = frozenset(top.matched_tokens)
        tied = [
            s
            for s in ranked
            if s.consumed == top.consumed
            and (
                frozenset(s.matched_tokens) == top_tokens
                or s.score >= top.score * TIE_RATIO
            )
        ]
        # If the customer named something we stock under that very name, the
        # entries that merely CLONE it are alternatives, not co-candidates —
        # "dior sauvage edt price" must answer with Dior Sauvage EDT, not
        # with it plus four unrelated-looking products that happen to clone
        # it. When nothing in the catalog carries the name (the customer
        # asked for a designer original we only stock clones of), the clones
        # are the answer and are kept.
        if top.kind == "name":
            tied = [s for s in tied if s.kind == "name"]

        tied = tied[:_MAX_TIED_PER_ROUND]
        results.extend(tied)

        consumed = frozenset().union(*(s.consumed for s in tied))
        if not consumed:
            break
        available = available - consumed
        if not available or len(results) >= limit:
            break

    # A perfume can win one round and tie into another; keep the first
    # (highest-scoring) appearance only.
    deduped: list[Scored] = []
    seen: set[str] = set()
    for s in results:
        if s.perfume_id not in seen:
            seen.add(s.perfume_id)
            deduped.append(s)

    return deduped[:limit]


# A leftover word this similar to some token of the matched perfume's name
# is counted as part of the name the customer was trying to type, not as
# evidence the message is about something else. Deliberately far looser than
# TOKEN_SIM_MIN: the word already FAILED to match, so the only question here
# is whether it was aiming at the name — "cahnel" scores 83 against "chanel"
# and "oslar" 80 against "solar", while "owner" and "friend" score nowhere
# near any of them.
_NEAR_NAME_SIM = 60.0


def message_focus(text: str, results: "list[Scored]") -> float:
    """
    How much of the message is the perfume name itself, ignoring filler.

    1.0 means the customer typed nothing but a product name; near 0 means
    the name is one incidental word inside a sentence about something else.
    app.matcher uses this instead of a message-length rule to tell a bare
    product name from a passing mention — a length rule cannot, since real
    catalog names run to eight or more words on their own.

    Two things are excluded from the denominator. Filler ("price", "how
    much", "bhai") — so wrapping a name in a normal question does not dilute
    it. And words that *failed* to match but are near-misses of the matched
    name — otherwise every typo would be counted as off-topic content, which
    is exactly backwards: "Cahnel DKNY" scored 0.5 and was rejected as a
    passing mention when it is nothing but a misspelled product name.
    """
    tokens = tokenize_message(text)
    if not tokens or not results:
        return 0.0

    # Bare numbers are prices, quantities or line numbers — never the thing
    # being named. Counting them as unexplained content dragged focus down
    # on every message where a customer quotes prices back at us.
    content = {
        i for i, t in enumerate(tokens)
        if t not in _MESSAGE_STOPWORDS and not t.isdigit()
    }
    if not content:
        return 0.0

    consumed = frozenset().union(*(r.consumed for r in results))

    name_tokens: set[str] = set()
    for r in results:
        data = PERFUMES.get(r.perfume_id) or {}
        name_tokens.update(tokenize(data.get("display_name") or ""))
        name_tokens.update(tokenize(data.get("clone_of") or ""))

    near_miss = {
        i
        for i in content - consumed
        if len(tokens[i]) >= 3
        and any(
            fuzz.ratio(tokens[i], nt, score_cutoff=_NEAR_NAME_SIM) for nt in name_tokens
        )
    }

    counted = content - near_miss
    if not counted:
        return 1.0

    return len(counted & consumed) / len(counted)
