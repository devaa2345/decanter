"""
Live Groq benchmark — the half of the pipeline the offline benchmark cannot
measure.

scripts/benchmark_matcher.py measures app.name_index: given a misspelled
name, does the right perfume come back, and does it come back alone. This
script measures what Groq adds on top of that, which is three things the
index deliberately does not attempt:

  1. Disambiguation. The index answers a bare "sauvage" with the whole
     family, because the family members have different prices and guessing
     one would be quoting the wrong number. Groq is supposed to pick when
     the message or the conversation actually says which.
  2. Intent. A perfume name in a message is not automatically a request for
     its price — "my friend wears sauvage" must not fire a price card.
  3. Context. "and the 5ml?" and "how much for the second one" only mean
     something against the reply before them.

Requires GROQ_API_KEY. Every case states what a correct answer looks like,
so failures name the behaviour that broke rather than just a diff.

Run:
    GROQ_API_KEY=... python scripts/benchmark_llm.py
    GROQ_API_KEY=... python scripts/benchmark_llm.py --repeat 3   # flakiness
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

logging.disable(logging.CRITICAL)

from app.catalog import PERFUMES  # noqa: E402
from app.config import settings  # noqa: E402
from app.matcher import match_perfume  # noqa: E402


@dataclass
class Case:
    """One message, plus what a correct reply looks like.

    expect_match=False means the bot must stay silent. Otherwise `expect`
    is a substring that must appear in the matched perfume's display name,
    and `expect_single` demands the bot commit to exactly one card.
    """

    message: str
    group: str
    expect: str | None = None
    expect_match: bool = True
    expect_single: bool = False
    history: list[dict] = field(default_factory=list)
    note: str = ""


def card(pid: str, text: str = "(price card)") -> dict:
    return {
        "role": "bot",
        "text": text,
        "perfume_ids": [pid],
        "perfume_names": [PERFUMES[pid]["display_name"]] if pid in PERFUMES else [],
    }


def cards(pids: list[str]) -> dict:
    return {
        "role": "bot",
        "text": "(price cards)",
        "perfume_ids": pids,
        "perfume_names": [PERFUMES[p]["display_name"] for p in pids if p in PERFUMES],
    }


def _find(fragment: str) -> str | None:
    fragment = fragment.lower()
    for pid, data in PERFUMES.items():
        if fragment in data["display_name"].lower():
            return pid
    return None


def build_cases() -> list[Case]:
    rebel = _find("9pm rebel")
    cases: list[Case] = [
        # --- Intent: asking vs. merely naming ---------------------------
        Case("9pm rebel", "intent", "9pm rebel", expect_single=True,
             note="a bare product name is the most common way customers ask"),
        Case("9pm rebel 5ml price", "intent", "9pm rebel", expect_single=True),
        Case("i want to confirm kaaf only", "intent", "kaaf", expect_single=True,
             note="confirming an order is asking, not mentioning"),
        Case("bhai 9pm rebel ka rate kya hai", "intent", "9pm rebel", expect_single=True),
        Case("the owner told me 9pm rebel is really nice apparently", "intent",
             expect_match=False, note="a name in passing is not a request"),
        Case("my friend uses 9pm rebel and loves it", "intent", expect_match=False),
        Case("the 9pm rebel is really good but i dont want it", "intent",
             expect_match=False, note="an explicit decline"),
        Case("thanks bhai", "intent", expect_match=False),
        Case("order kab aayega", "intent", expect_match=False),

        # --- Disambiguation: commit to one when the message says which ---
        Case("dior sauvage edt price", "disambiguation", "sauvage edt", expect_single=True,
             note="the concentration is stated, so one card is the right answer"),
        Case("armaf club de nuit intense man edp", "disambiguation",
             "club de nuit intense man edp", expect_single=True),

        # --- Misspellings the index recovers, Groq must not second-guess --
        Case("9pm rebl", "typos", "9pm rebel", expect_single=True),
        Case("9 pm rebel 3ml", "typos", "9pm rebel", expect_single=True),
        Case("kaff price", "typos", "kaaf", expect_single=True),

        # --- Absent products: silence, never a lookalike -----------------
        # Verified absent from this catalog. Fahrenheit used to live here
        # and was added to the sheet after a customer asked for it — the
        # right fix for "we get asked for X" is stocking X, not teaching
        # the matcher to guess.
        Case("penhaligons endymion price", "absent", expect_match=False),
        Case("byredo gypsy water", "absent", expect_match=False),
        Case("fahrenheit price", "typos", "fahrenheit", expect_single=True,
             note="added by hand after a customer asked — must behave like any other entry"),
        Case("fahreneit 3ml", "typos", "fahrenheit", expect_single=True),
    ]

    if rebel:
        cases += [
            Case("and 5ml?", "context", "9pm rebel", expect_single=True,
                 history=[{"role": "customer", "text": "9pm rebel price"}, card(rebel)],
                 note="a size-only follow-up to the card just shown"),
            Case("how much for that one", "context", "9pm rebel", expect_single=True,
                 history=[{"role": "customer", "text": "9pm rebel"}, card(rebel)]),
            Case("iska 10ml kitne ka hai", "context", "9pm rebel", expect_single=True,
                 history=[{"role": "customer", "text": "9pm rebel"}, card(rebel)]),
            Case("thanks bhai", "context", expect_match=False,
                 history=[{"role": "customer", "text": "9pm rebel"}, card(rebel)],
                 note="context must not turn every message into a price query"),
            Case("no i dont want it", "context", expect_match=False,
                 history=[{"role": "customer", "text": "9pm rebel"}, card(rebel)]),
        ]

    sauvages = [pid for pid, d in PERFUMES.items() if "sauvage" in d["display_name"].lower()]
    if len(sauvages) >= 2:
        edt = next((p for p in sauvages if "edt" in PERFUMES[p]["display_name"].lower()), None)
        edp = next((p for p in sauvages if "edp" in PERFUMES[p]["display_name"].lower()), None)
        if edt and edp:
            cases.append(
                Case(
                    "the edp one please",
                    "context",
                    PERFUMES[edp]["display_name"],
                    expect_single=True,
                    history=[{"role": "customer", "text": "sauvage"}, cards([edt, edp])],
                    note="resolving a reference against the cards just shown",
                )
            )
            cases.append(
                Case(
                    "second one 10ml",
                    "context",
                    PERFUMES[edp]["display_name"],
                    expect_single=True,
                    history=[{"role": "customer", "text": "sauvage"}, cards([edt, edp])],
                    note="positional reference — needs the ORDER cards were shown in",
                )
            )

    return [c for c in cases if c.expect is None or _find(c.expect) or not c.expect_match]


async def run_case(case: Case) -> tuple[bool, str]:
    result = await match_perfume(case.message, history=case.history or None)
    ids = result.matched_perfume_ids or ([result.perfume_id] if result.perfume_id else [])
    shown = [PERFUMES[p]["display_name"] for p in ids if p in PERFUMES]

    if not case.expect_match:
        return (not ids), ("silent" if not ids else f"replied with {shown}")

    if not ids:
        return False, "stayed silent"

    hit = any(case.expect.lower() in n.lower() for n in shown)
    if not hit:
        return False, f"wrong perfume: {shown}"
    if case.expect_single and len(ids) > 1:
        return False, f"right answer but did not commit — showed {len(ids)}: {shown}"
    return True, shown[0]


async def preflight() -> str | None:
    """
    Confirm the key actually works before measuring anything.

    Without this the script silently lies. app.matcher is built to survive a
    Groq outage by falling back to the deterministic index, so an invalid key
    does not raise — it just quietly routes every case down the fallback
    path, and the run reports a score for a layer it never touched. That
    happened: a rejected key produced a clean-looking 91% that had nothing to
    do with the model. Returns an error string, or None if the key is live.
    """
    from app.groq_client import GroqClassification, classify_and_phrase

    probe = next(iter(PERFUMES))
    try:
        result = await classify_and_phrase(
            PERFUMES[probe]["display_name"], candidates={probe: PERFUMES[probe]}
        )
    except Exception as exc:  # classify_and_phrase swallows these, but be safe
        return f"Groq call raised: {exc}"

    if result is None:
        return (
            "Groq could not be reached — classify_and_phrase returned None. "
            "Usually an invalid/expired API key (the server replies 401) or no "
            "network. Check the app log line 'Groq classify_and_phrase call "
            "failed' for the underlying error."
        )
    if not isinstance(result, GroqClassification):
        return f"Unexpected response type from Groq: {type(result)!r}"
    return None


async def main_async(repeat: int) -> int:
    problem = await preflight()
    if problem:
        print(f"\n  PREFLIGHT FAILED: {problem}\n", file=sys.stderr)
        print(
            "  Refusing to run: every case would silently fall back to the\n"
            "  deterministic matcher and report a score for a layer that was\n"
            "  never exercised.\n",
            file=sys.stderr,
        )
        return 2

    cases = build_cases()
    print(f"\n{'=' * 88}")
    print(f"  LIVE GROQ BENCHMARK — {len(cases)} cases x {repeat} run(s), model {settings.GROQ_MODEL}")
    print(f"{'=' * 88}\n")

    per_group: dict[str, Counter] = {}
    failures: list[str] = []

    for case in cases:
        outcomes = []
        for _ in range(repeat):
            ok, detail = await run_case(case)
            outcomes.append((ok, detail))

        passed = sum(1 for ok, _ in outcomes if ok)
        counter = per_group.setdefault(case.group, Counter())
        counter["pass"] += passed
        counter["fail"] += repeat - passed

        mark = "PASS" if passed == repeat else ("FLAKY" if passed else "FAIL")
        stability = "" if passed in (0, repeat) else f" ({passed}/{repeat} passed)"
        want = "silence" if not case.expect_match else case.expect
        # Report a FAILING run's detail, not the first run's. On a flaky case
        # the first run is often the one that passed, which made the report
        # read "FAIL ... got: <the correct answer>".
        detail = next((d for ok, d in outcomes if not ok), outcomes[0][1])

        print(f"  [{mark:5}]{stability} {case.message!r}")
        print(f"           want: {want}   got: {detail}")
        if case.note and passed != repeat:
            print(f"           why it matters: {case.note}")
        if passed != repeat:
            failures.append(f"{case.message!r} — want {want}, got {detail}")

    total_pass = sum(c["pass"] for c in per_group.values())
    total = sum(c["pass"] + c["fail"] for c in per_group.values())

    print(f"\n  {'-' * 84}")
    print(f"  {'group':<18} {'pass':>6} {'total':>6}   rate")
    for group, c in sorted(per_group.items()):
        n = c["pass"] + c["fail"]
        print(f"  {group:<18} {c['pass']:>6} {n:>6}   {c['pass'] / n * 100:5.1f}%")
    print(f"  {'-' * 84}")
    print(f"  {'OVERALL':<18} {total_pass:>6} {total:>6}   {total_pass / total * 100:5.1f}%\n")

    if failures:
        print("  Failures:")
        for f in failures:
            print(f"    - {f}")
        print()

    return 0 if total_pass == total else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1,
                        help="run each case N times to surface model flakiness")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if not settings.GROQ_API_KEY:
        print(
            "GROQ_API_KEY is not set — this script measures the live model.\n"
            "Set it in .env or the environment, then rerun.\n"
            "(The offline half of the pipeline is measured by "
            "scripts/benchmark_matcher.py, which needs no key.)",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(asyncio.run(main_async(args.repeat)))


if __name__ == "__main__":
    main()
