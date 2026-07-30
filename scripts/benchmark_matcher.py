"""
Large-scale, strict accuracy benchmark for the perfume matcher.

Unlike tests/test_typo_stress.py (32 hand-written cases, and a scoring rule
that counts "matched *something*" as a pass), this generates thousands of
queries mechanically from EVERY entry in the live catalog and scores them
strictly: the pipeline must return the exact perfume the query was derived
from. Anything else — a different perfume, or nothing at all — is a failure.

Two things make the strict score meaningful rather than pedantic:

  * Duplicate display names. The catalog genuinely contains entries whose
    display names normalize identically (different sheet rows, same product
    text). Returning any of those is counted correct — the reply the
    customer sees is the same name either way. See _equivalence_groups.
  * Ambiguous returns. When the pipeline returns matched_perfume_ids (2+
    candidates, one card each), the target being in that list is scored
    separately as "ambiguous-hit", not silently as a win — a customer who
    typed one specific name and got five cards was not served perfectly.

Negative controls (greetings, delivery questions, perfumes we don't carry,
plain chit-chat) are scored in the opposite direction: any match at all is
a failure. Precision matters as much as recall here — a wrong price card is
worse than no reply.

Run:
    python scripts/benchmark_matcher.py                # full catalog
    python scripts/benchmark_matcher.py --sample 200   # quick pass
    python scripts/benchmark_matcher.py --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

logging.disable(logging.CRITICAL)

from app.config import settings  # noqa: E402

# This benchmark measures the DETERMINISTIC half of the pipeline, so Groq is
# switched off for the whole run — before any query executes. Not a
# convenience: with a key configured, twenty thousand cases become twenty
# thousand live API calls, which is slow, expensive, rate-limited, and
# turns a reproducible offline measurement into one that drifts with the
# model. scripts/benchmark_llm.py is where the model itself gets measured.
settings.GROQ_API_KEY = ""

from app.catalog import PERFUMES  # noqa: E402
from app.matcher import match_perfume, normalize_message  # noqa: E402

SEED = 20240730


# --- Query generation -------------------------------------------------------

# Keys physically adjacent on a QWERTY phone keyboard — a "wrong letter"
# typo is overwhelmingly a neighbouring key, not a random one, so random
# substitution would test a distribution customers don't actually produce.
_ADJACENT = {
    "a": "qwsz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr",
    "f": "drtgvc", "g": "ftyhbv", "h": "gyujnb", "i": "ujko", "j": "huikmn",
    "k": "jiolm", "l": "kop", "m": "njk", "n": "bhjm", "o": "iklp",
    "p": "ol", "q": "wa", "r": "edft", "s": "awedxz", "t": "rfgy",
    "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc", "y": "tghu",
    "z": "asx",
}

# How customers actually mangle perfume names phonetically. Ordered longest
# first so "ph" is tried before "p".
_PHONETIC = [
    ("ph", "f"), ("que", "k"), ("ch", "sh"), ("ck", "k"), ("qu", "kw"),
    ("x", "ks"), ("z", "s"), ("c", "k"), ("y", "i"), ("ge", "j"),
    ("ea", "ee"), ("ou", "u"), ("au", "o"), ("oo", "u"), ("ie", "ee"),
]

_FILLERS = [
    "{q} price",
    "price of {q}",
    "what is the price of {q}",
    "how much is {q}",
    "{q} kitna ka hai",
    "bhai {q} ka rate batao",
    "i want {q}",
    "do you have {q}",
    "{q} 10ml price",
    "{q} 5ml",
    "hi {q} price please",
    "need {q} decant",
    "{q} available hai kya",
    "send me {q} rate",
]

_NEGATIVES = [
    # Greetings / chit-chat
    "hello bro", "hi", "good morning", "thanks bhai", "thank you so much",
    "ok", "okay done", "hmm", "great", "nice one", "haha", "bye",
    # Logistics — nothing to do with naming a perfume
    "order kab aayega", "when will my order arrive", "how much is shipping",
    "do you deliver to mumbai", "what is your address", "cod available",
    "is cash on delivery available", "my parcel is not delivered yet",
    "can i return this", "how long does delivery take",
    "shipping charges kitne hai", "tracking id do",
    # Perfumes genuinely absent from this catalog — no entry, and no entry
    # cloning them either (verified against clone_of, which IS matchable by
    # design; the originals that DO have clones here live in _CLONE_PROBES
    # below as positive cases instead).
    "penhaligons endymion price", "kilian love dont be shy",
    "byredo gypsy water", "serge lutens chergui price",
    "zoologist squid 5ml", "nasomatto baraonda",
    # Generic sentences that happen to contain catalog-ish words
    "please tell me more", "i will let you know", "let me check and tell",
    "have a good day", "what all do you have in stock for men",
    "the owner said your decants are very good",
    "my friend uses your perfumes and loves them",
    "i dont want anything right now",
    "just browsing for now thanks",
]


def _rng_for(text: str) -> random.Random:
    """Per-query deterministic RNG so a rerun produces the identical corpus
    even when only a subset of perfumes is sampled."""
    return random.Random(f"{SEED}:{text}")


def _drop_char(text: str, rng: random.Random) -> str:
    idx = [i for i, c in enumerate(text) if c.isalpha()]
    if not idx:
        return text
    i = rng.choice(idx)
    return text[:i] + text[i + 1 :]


def _transpose(text: str, rng: random.Random) -> str:
    idx = [i for i in range(len(text) - 1) if text[i].isalpha() and text[i + 1].isalpha()]
    if not idx:
        return text
    i = rng.choice(idx)
    return text[:i] + text[i + 1] + text[i] + text[i + 2 :]


def _substitute(text: str, rng: random.Random) -> str:
    idx = [i for i, c in enumerate(text) if c.lower() in _ADJACENT]
    if not idx:
        return text
    i = rng.choice(idx)
    return text[:i] + rng.choice(_ADJACENT[text[i].lower()]) + text[i + 1 :]


def _double_letter(text: str, rng: random.Random) -> str:
    idx = [i for i, c in enumerate(text) if c.isalpha()]
    if not idx:
        return text
    i = rng.choice(idx)
    return text[: i + 1] + text[i] + text[i + 1 :]


def _phonetic(text: str, rng: random.Random) -> str:
    lowered = text.lower()
    applicable = [(a, b) for a, b in _PHONETIC if a in lowered]
    if not applicable:
        return _substitute(text, rng)
    a, b = rng.choice(applicable)
    return lowered.replace(a, b, 1)


def _drop_vowel(text: str, rng: random.Random) -> str:
    """Vowel-dropping — how people type fast on a phone ("sauvge", "clb de nuit")."""
    idx = [i for i, c in enumerate(text) if c.lower() in "aeiou" and i > 0]
    if not idx:
        return text
    i = rng.choice(idx)
    return text[:i] + text[i + 1 :]


def _split_word(text: str, rng: random.Random) -> str:
    words = text.split()
    long_words = [i for i, w in enumerate(words) if len(w) >= 5]
    if not long_words:
        return text
    i = rng.choice(long_words)
    w = words[i]
    cut = rng.randint(2, len(w) - 2)
    words[i] = w[:cut] + " " + w[cut:]
    return " ".join(words)


def _join_words(text: str, rng: random.Random) -> str:
    words = text.split()
    if len(words) < 2:
        return text
    i = rng.randrange(len(words) - 1)
    return " ".join(words[:i] + [words[i] + words[i + 1]] + words[i + 2 :])


def _random_case(text: str, rng: random.Random) -> str:
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in text)


def _distinctive_part(display_name: str, brand: str | None) -> str:
    """The name minus its brand prefix — customers very often type only this
    ("rebel", "club de nuit intense") rather than the full catalog string."""
    if not brand:
        return display_name
    lowered = display_name.lower()
    b = brand.lower().strip()
    if b and lowered.startswith(b):
        rest = display_name[len(brand) :].strip()
        if len(rest) >= 3:
            return rest
    return display_name


@dataclass
class Case:
    query: str
    target: str          # perfume_id the query was derived from ("" for negatives)
    category: str
    # Extra perfume_ids that count as correct for this specific query, on top
    # of the target's duplicate-display-name group. Used for clone probes:
    # several catalog entries can legitimately clone the same original, so
    # asking for that original has more than one right answer.
    also_accept: frozenset[str] = frozenset()


def _clone_groups() -> dict[str, frozenset[str]]:
    """
    perfume_id -> every id that is a correct answer when a customer asks for
    the original this entry clones.

    That is every OTHER entry cloning the same original (they are equally
    valid substitutes and the shop stocks several), plus any entry actually
    named that original — the catalog carries some designer originals
    outright, and answering "JPG Ultramale" with the real Jean Paul Gaultier
    entry is a better answer than the clone, not a wrong one.
    """
    by_clone: dict[str, set[str]] = defaultdict(set)
    named: dict[str, set[str]] = defaultdict(set)
    for pid, data in PERFUMES.items():
        clone = normalize_message(data.get("clone_of") or "")
        if clone:
            by_clone[clone].add(pid)
        named[normalize_message(data.get("display_name") or "")].add(pid)

    groups: dict[str, frozenset[str]] = {}
    for clone, pids in by_clone.items():
        accepted = set(pids)
        for name, name_pids in named.items():
            # A catalog entry whose own name contains the cloned original
            # ("Jean Paul Gaultier Ultra Male" for clone_of "JPG Ultramale"
            # once both are normalized to shared words) answers the question.
            if clone and (clone in name or name in clone):
                accepted |= name_pids
        for pid in pids:
            groups[pid] = frozenset(accepted)
    return groups


def build_positive_cases(pids: list[str]) -> list[Case]:
    cases: list[Case] = []
    clone_groups = _clone_groups()

    for pid in pids:
        data = PERFUMES[pid]
        full = data["display_name"]
        part = _distinctive_part(full, data.get("brand"))
        rng = _rng_for(pid)

        def add(q: str, cat: str, also: frozenset[str] = frozenset()) -> None:
            q = " ".join(q.split())
            if q:
                cases.append(Case(q, pid, cat, also))

        # Baselines — these must be perfect; anything less is a plain bug.
        add(full, "clean-full")
        add(part, "clean-distinctive")

        # Single character-level error on the name the customer is most
        # likely to type (the distinctive part).
        add(_drop_char(part, rng), "typo-missing-letter")
        add(_transpose(part, rng), "typo-transposed")
        add(_substitute(part, rng), "typo-wrong-key")
        add(_double_letter(part, rng), "typo-doubled-letter")
        add(_drop_vowel(part, rng), "typo-dropped-vowel")
        add(_phonetic(part, rng), "typo-phonetic")

        # Two independent errors in one query — common on a phone keyboard,
        # and where a per-word threshold of 80 starts to genuinely hurt.
        add(_substitute(_drop_char(part, rng), rng), "typo-double-error")

        # Spacing errors.
        add(_split_word(part, rng), "space-split")
        add(_join_words(part, rng), "space-joined")

        # Casing noise.
        add(part.upper(), "case-upper")
        add(_random_case(part, rng), "case-random")

        # Realistic sentence wrappers around a lightly misspelled name —
        # this is what an actual inbound WhatsApp message looks like.
        add(rng.choice(_FILLERS).format(q=part), "filler-clean")
        add(rng.choice(_FILLERS).format(q=_drop_char(part, rng)), "filler-typo")
        add(rng.choice(_FILLERS).format(q=_transpose(full, rng)), "filler-typo-full")

        # Customers routinely ask for the designer ORIGINAL and expect the
        # clone we actually stock — clone_of is in the keyword set for
        # exactly this reason, so it belongs in the benchmark.
        clone = (data.get("clone_of") or "").strip()
        if len(clone) >= 4:
            siblings = clone_groups.get(pid, frozenset({pid}))
            add(clone, "clone-clean", siblings)
            add(_drop_char(clone, rng), "clone-typo", siblings)

    return cases


def build_negative_cases() -> list[Case]:
    return [Case(q, "", "negative") for q in _NEGATIVES]


# --- Scoring ----------------------------------------------------------------

def _equivalence_groups() -> dict[str, frozenset[str]]:
    """
    Map each perfume_id to the set of ids whose display name normalizes to
    the same string. The catalog has real duplicates (the same product
    appearing on more than one sheet row); returning a duplicate of the
    target renders an identical card, so it is not a matching error.
    """
    by_name: dict[str, set[str]] = defaultdict(set)
    for pid, data in PERFUMES.items():
        by_name[normalize_message(data["display_name"])].add(pid)
    return {pid: frozenset(group) for group in by_name.values() for pid in group}


@dataclass
class Outcome:
    case: Case
    verdict: str          # hit | ambiguous-hit | wrong | miss | false-positive | correct-silence
    got: str | None
    got_name: str | None
    layer: str | None
    n_candidates: int = 0
    # 1-based position of the target within the returned candidates, or 0 if
    # it is absent. Rank 1 with several candidates is the case the LLM layer
    # exists to close: the deterministic index already put the right answer
    # on top and only needs someone to commit to it.
    rank: int = 0


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)
    elapsed: float = 0.0

    def by_category(self) -> dict[str, Counter]:
        agg: dict[str, Counter] = defaultdict(Counter)
        for o in self.outcomes:
            agg[o.case.category][o.verdict] += 1
        return agg


async def _run_case(case: Case, equiv: dict[str, frozenset[str]]) -> Outcome:
    result = await match_perfume(case.query)
    ids = result.matched_perfume_ids or ([result.perfume_id] if result.perfume_id else [])
    got = ids[0] if ids else None
    got_name = PERFUMES[got]["display_name"] if got in PERFUMES else None

    if not case.target:
        verdict = "false-positive" if ids else "correct-silence"
        return Outcome(case, verdict, got, got_name, result.layer, len(ids))

    accepted = equiv.get(case.target, frozenset({case.target})) | case.also_accept
    rank = next((i for i, pid in enumerate(ids, 1) if pid in accepted), 0)

    if len(ids) == 1 and rank == 1:
        verdict = "hit"
    elif rank:
        verdict = "ambiguous-hit"
    elif ids:
        verdict = "wrong"
    else:
        verdict = "miss"

    return Outcome(case, verdict, got, got_name, result.layer, len(ids), rank)


async def run(cases: list[Case], progress_every: int = 250) -> Report:
    equiv = _equivalence_groups()
    report = Report()
    start = time.time()

    for i, case in enumerate(cases, 1):
        report.outcomes.append(await _run_case(case, equiv))
        if progress_every and i % progress_every == 0:
            done = time.time() - start
            rate = i / done if done else 0
            eta = (len(cases) - i) / rate if rate else 0
            print(
                f"  {i}/{len(cases)}  ({rate:.0f} q/s, ETA {eta / 60:.1f} min)",
                file=sys.stderr,
                flush=True,
            )

    report.elapsed = time.time() - start
    return report


# --- Presentation -----------------------------------------------------------

def print_report(report: Report, show_failures: int = 40) -> None:
    positives = [o for o in report.outcomes if o.case.target]
    negatives = [o for o in report.outcomes if not o.case.target]

    print("\n" + "=" * 92)
    print("  MATCHER ACCURACY BENCHMARK")
    print("=" * 92)

    n = len(positives)
    if n:
        hits = sum(1 for o in positives if o.verdict == "hit")
        amb = sum(1 for o in positives if o.verdict == "ambiguous-hit")
        wrong = sum(1 for o in positives if o.verdict == "wrong")
        miss = sum(1 for o in positives if o.verdict == "miss")

        print(f"\n  Positives: {n} queries over {len({o.case.target for o in positives})} perfumes")
        print(f"    exact hit (one right card)   {hits:>6}  {hits / n * 100:>6.2f}%")
        print(f"    ambiguous hit (right + more) {amb:>6}  {amb / n * 100:>6.2f}%")
        print(f"    WRONG perfume                {wrong:>6}  {wrong / n * 100:>6.2f}%")
        print(f"    no match at all              {miss:>6}  {miss / n * 100:>6.2f}%")
        lead = sum(1 for o in positives if o.rank == 1)
        print(f"    -> strict accuracy           {hits / n * 100:>6.2f}%")
        print(f"    -> usable accuracy           {(hits + amb) / n * 100:>6.2f}%")
        # The ceiling a perfect disambiguator (the LLM layer, or a tighter
        # tie rule) could reach without the index itself getting better.
        print(f"    -> target ranked #1          {lead / n * 100:>6.2f}%")

    if negatives:
        fp = sum(1 for o in negatives if o.verdict == "false-positive")
        print(f"\n  Negative controls: {len(negatives)}")
        print(f"    correctly silent             {len(negatives) - fp:>6}  {(len(negatives) - fp) / len(negatives) * 100:>6.2f}%")
        print(f"    FALSE POSITIVE               {fp:>6}  {fp / len(negatives) * 100:>6.2f}%")

    print("\n  Per-category strict accuracy")
    print(f"    {'category':<24} {'n':>6} {'hit':>8} {'amb':>8} {'wrong':>8} {'miss':>8}")
    print(f"    {'-' * 24} {'-' * 6} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}")
    for cat, c in sorted(report.by_category().items()):
        total = sum(c.values())
        if cat == "negative":
            print(
                f"    {cat:<24} {total:>6} {'':>8} {'':>8} "
                f"{c['false-positive'] / total * 100:>7.1f}% {'':>8}"
            )
            continue
        print(
            f"    {cat:<24} {total:>6} {c['hit'] / total * 100:>7.1f}% "
            f"{c['ambiguous-hit'] / total * 100:>7.1f}% "
            f"{c['wrong'] / total * 100:>7.1f}% {c['miss'] / total * 100:>7.1f}%"
        )

    layers = Counter(o.layer or "(none)" for o in report.outcomes)
    print("\n  Layer usage: " + "  ".join(f"{k}={v}" for k, v in layers.most_common()))
    print(f"  Elapsed: {report.elapsed:.1f}s  ({len(report.outcomes) / report.elapsed:.0f} q/s)")

    bad = [o for o in report.outcomes if o.verdict in ("wrong", "false-positive")]
    if bad and show_failures:
        print(f"\n  Worst failures (wrong perfume / false positive) — showing {min(len(bad), show_failures)} of {len(bad)}:")
        for o in bad[:show_failures]:
            want = PERFUMES[o.case.target]["display_name"] if o.case.target else "(silence)"
            print(f"    [{o.case.category}] {o.case.query!r}")
            print(f"        want: {want}")
            print(f"        got : {o.got_name} ({o.layer}, {o.n_candidates} candidate(s))")

    misses = [o for o in report.outcomes if o.verdict == "miss"]
    if misses and show_failures:
        print(f"\n  Sample misses (no match at all) — showing {min(len(misses), show_failures)} of {len(misses)}:")
        for o in misses[:show_failures]:
            print(f"    [{o.case.category}] {o.case.query!r}  ->  want {PERFUMES[o.case.target]['display_name']}")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=0, help="benchmark only N randomly chosen perfumes (0 = all)")
    parser.add_argument("--json", type=str, default="", help="write full per-case results to this JSON file")
    parser.add_argument("--show", type=int, default=40, help="how many failing cases to print")
    parser.add_argument("--categories", type=str, default="", help="comma-separated category filter")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    pids = sorted(PERFUMES)
    if args.sample and args.sample < len(pids):
        pids = random.Random(SEED).sample(pids, args.sample)

    cases = build_positive_cases(pids) + build_negative_cases()
    if args.categories:
        wanted = {c.strip() for c in args.categories.split(",")}
        cases = [c for c in cases if c.category in wanted]

    print(f"Running {len(cases)} queries against {len(PERFUMES)} catalog entries...", file=sys.stderr)
    report = asyncio.run(run(cases))
    print_report(report, show_failures=args.show)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "query": o.case.query,
                        "category": o.case.category,
                        "target": o.case.target,
                        "verdict": o.verdict,
                        "got": o.got,
                        "got_name": o.got_name,
                        "layer": o.layer,
                        "n_candidates": o.n_candidates,
                        "rank": o.rank,
                    }
                    for o in report.outcomes
                ],
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"Wrote per-case results to {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
