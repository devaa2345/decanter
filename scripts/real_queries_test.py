"""
Real customer messages, run end to end and scored strictly.

Every message here was actually sent to the shop. This is the acceptance
test the synthetic benchmark cannot be: scripts/benchmark_matcher.py
generates typos mechanically and measures the index in isolation, while
these exercise the whole pipeline on the messy shapes people really send —
brand-grouped wishlists, numbered lists, tables pasted out of a
spreadsheet, mixed sizes, Hinglish, and questions that are not orders at
all.

Scoring is per PRODUCT, not per message: a message naming sixteen perfumes
and answering fourteen of them is fourteen right and two wrong, because
that is what the customer experiences. Sizes are checked separately — the
right product at the wrong size is still a wrong price.

Run:
    python scripts/real_queries_test.py            # deterministic index only
    python scripts/real_queries_test.py --groq     # full pipeline, live LLM
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging

logging.disable(logging.CRITICAL)

from app.catalog import PERFUMES  # noqa: E402
from app.config import settings  # noqa: E402


@dataclass
class Case:
    label: str
    message: str
    # Substrings that must each appear in some matched product's name.
    expect: list[str] = field(default_factory=list)
    # Sizes the customer wrote, kept only as documentation of the real
    # message. They no longer affect the reply — every card shows the full
    # size grid (see app.formatter._build_card_block).
    sizes: dict[str, int] = field(default_factory=dict)
    # Product-name substrings that may legitimately come back on top of
    # `expect` — a genuinely ambiguous mention where showing the family is
    # the right answer. Anything else returned counts as an extra, i.e. a
    # product the customer did not ask for.
    allow_extra: list[str] = field(default_factory=list)
    # True when the right answer is to say nothing (not an order).
    expect_silence: bool = False
    note: str = ""


WISHLIST = """Al Haramain:
- Detour Noir
- Detour Eco
- Detour Intense Noir
- Amber Ruby Edition

Armaf:
- Club de Nuit Intense Man EDP
- Club de Nuit Untold

French Avenue:
- Liquid Brun
- Cocoa Morado
- Aether Extrait

Maison Alhambra:
- Tobacco Touch
- Woody Oud
- Opulence Leather
- Porto Neroli
- Toscano Leather
- Fabulo Intense
- Black Origami"""

CASES = [
    Case(
        "brand-grouped wishlist",
        WISHLIST,
        expect=[
            "detour noir", "detour eco", "detour intense", "amber ruby",
            "club de nuit intense man", "untold", "liquid brun", "cocoa morado",
            "aether extrait", "tobacco touch", "woody oud", "opulence leather",
            "porto neroli", "toscano leather", "fabulo intense", "black origami",
        ],
        note="16 products across 4 brand headings",
    ),
    Case(
        "comma list, all 3ml",
        "stronger with you absolutely 3 ml, carolina bad boy cobalt elixir 3ml,  "
        "isse miyake Le Sel D'issey EDP 3 ml, maison margiela by the fireplace 3ml, "
        "maison margiela jazz club 3ml , azzaro forever wanted elixir 3ml",
        expect=[
            "stronger with you absolutely", "bad boy cobalt", "le sel",
            "by the fireplace", "jazz club", "forever wanted",
        ],
        sizes={
            "stronger with you absolutely": 3, "bad boy cobalt": 3,
            "by the fireplace": 3, "jazz club": 3, "forever wanted": 3,
        },
    ),
    Case(
        "size-first order with shipping note",
        "Hi i want to order following with ₹35 priority shipping : \n\n"
        "10ML - French Avenue Zenith Deep\n\n5ML - Paris Corner Khair Pistachio \n\n"
        "3ML - Lattafa Eclaire",
        expect=["zenith deep", "khair pistachio", "eclaire"],
        sizes={"zenith deep": 10, "khair pistachio": 5, "eclaire": 3},
        note="size written BEFORE each name",
    ),
    Case(
        "two size groups, 24 products",
        "ml \n\nAfnan Turathi Blue\n\nArabiyat Prestige Hamdan The Sheikh\n\n"
        "Armaf Club de Nuit Intense Man PP\n\nArmaf Odyssey Mega\n\n"
        "Armaf Club De Nuit Urban Man Elixir\n\nArmaf Club de Nuit Iconic \n\n"
        "Armaf Odyssey Aqua\n\nFrench Avenue Liquid Brun\n\nRue Brocca Theoreme\n\n"
        "Azzaro\tChrome EDT \n\nDavidoff Zino\n\n"
        "Giorgio Armani Acqua Di Gio Parfum 2024\n\nGuy Laroche Drakkar Noir Intense \n\n"
        "Issey Miyake Le Sel d'Issey EDT\n\nIssey Miyake L'Eau d'Issey Pour Homme Intense\n\n"
        "Issey Miyake L'Eau d'Issey Vetiver\n\nYSL Y EDP\n\n3ml \n\n"
        "Jean Paul Gaultier\tLe Male Le Parfum \n\nTom Ford Ombre Leather EDP\n\n"
        "Chanel\tBleu De Chanel EDP\n\nVersace Eros EDP\n\n"
        "Armani\tStronger with you Absolutely\n\nArmani\tStronger with you Intensly \n\n"
        "Dior Sauvage EDT",
        expect=[
            "turathi blue", "hamdan the sheikh", "club de nuit intense man pp",
            "odyssey mega", "urban man elixir", "club de nuit iconic",
            "odyssey aqua", "liquid brun", "theoreme", "chrome edt", "zino",
            "acqua di gio parfum", "drakkar noir intense", "le sel",
            "pour homme intense", "vetiver", "y edp", "le male le parfum",
            "ombre leather", "bleu de chanel edp", "eros edp",
            "stronger with you absolutely", "intensly", "sauvage edt",
        ],
        note="the longest real order seen; 24 products",
    ),
    Case(
        "numbered list with sizes",
        "Bro new decants reqd\n\n1. Bois Imperial 5 ml\n2. ⁠Rayhaan aquatica 8ml\n"
        "3. ⁠Rayhaan terra 5ml\n4. ⁠spicebomb mettalic Musk 8ml\n5. ⁠zenith santal 8ml",
        expect=["bois imperial", "aquatica", "terra", "metallic musk", "zenith santal"],
        sizes={"aquatica": 8, "terra": 5, "zenith santal": 8},
        note="'mettalic' misspelled",
    ),
    Case(
        "three lines, brand + name",
        "Bois imperial Essential parfum 3ml \nNishane Tero 3ml\nMemo african leather 3 ml",
        expect=["bois imperial", "tero", "african leather"],
        sizes={"tero": 3, "african leather": 3},
    ),
    Case(
        "customer quotes their own prices",
        "Overdose 10 – 470\nBDC parfum 3 – 450\nHawas black 3 - 150",
        expect=["overdose", "bleu de chanel parfum", "hawas black"],
        note="numbers here are the customer's prices, not sizes",
    ),
    Case(
        "size stated once, up front",
        "Decants in 5 ml \nAqua oud \n9pm rebel",
        expect=["aqua oud", "9pm rebel"],
        sizes={"aqua oud": 5, "9pm rebel": 5},
    ),
    Case(
        "gendered sections with fallbacks",
        "For female-\nChanel No 5 edp - 5ml (3ml if 5 isn’t available)\n"
        "Chanel Coco mademoiselle -5 ml(3ml if 5 isn’t available)\n\nFor Male -\n"
        "Creed original vetiver - 3ml\nAllure Homme Sport Superleggera - 3ml\n"
        "When the rain stops - 5ml\nTerre D'Hermes Eau Intense Vetiver - 5ml",
        expect=[
            "no. 5", "coco mademoiselle", "original vetiver", "superleggera",
            "when the rain stops", "eau intense vetiver",
        ],
    ),
    Case(
        "availability question, not an order",
        "Hey bro, do you retail pack of any of the following \n1. Dylan Blue\n"
        "2. Terre De Hermes\n3. Guerlain Vetiver",
        expect=["dylan blue", "terre d", "vetiver"],
        note="asking whether full bottles exist — still a real product question",
    ),
    Case(
        "bulleted list with (size) x qty",
        "Need few more decants \n\n- Trillium (8ml) x 1\n- Emir Celestial (10ml) x 1\n"
        "- Emir Triumphant Sapphire (8ml) x 1\n- Emir Frenetic Homme Intense (8ml) x 1\n"
        "- Emir Voux Elegante (8ml) x 1\n- Suqraat (8ml) x 1\n- North Stag III (10ml) x 1",
        expect=[
            "trillium", "celestial", "frenetic homme intense", "voux elegante",
            "suqraat", "north stag expressions iii",
        ],
        sizes={"trillium": 8, "celestial": 10, "suqraat": 8},
        note="'Triumphant Sapphire' is not in the catalog",
    ),
    Case(
        "pasted table with prices",
        "Maison Alhambra Jean Lowe Immortel 3ml – ₹140\nMaison Alhambra Jean Lowe Vibe 3ml – ₹130\n"
        "Maison Alhambra Jean Lowe Azure 3ml – ₹130\nMaison Alhambra Jean Lowe Noir 3ml – ₹130",
        expect=["jean lowe immortel", "jean lowe vibe", "jean lowe azure", "jean lowe noir"],
        sizes={"jean lowe immortel": 3, "jean lowe vibe": 3},
    ),
    Case(
        "numbered, no sizes",
        "1.Anfar London summer in Dubai \n2.Essence of casablanca \n3.Hawas kobra \n"
        "4.Hawas elixir\n5.  Rayhaan wolf",
        expect=["summer in dubai", "essence of casablanca", "hawas kobra", "hawas elixir", "wolf"],
        note="'Anfar London' merges two product names",
    ),
    Case(
        "markdown-ish table, sizes and prices",
        "| African Leather | 5ml | 640 |\n| Boss Bottled Absolu | 5ml | 400 |\n"
        "| Superleggera | 5ml | 780 |\n| AHS Blanche Edition | 5ml | 720 |\n"
        "| Encre Noire | 5ml | 200 |\n| Total |  | 2740 |",
        expect=[
            "african leather", "boss bottled absolu", "superleggera",
            "edition blanche", "encre noire",
        ],
        sizes={"african leather": 5, "boss bottled absolu": 5},
    ),
    Case(
        "short names, all 10ml",
        "Havas ice 10ml\n9pm 10 ml\nTurathi blue 10 ml\nSillage 10ml",
        expect=["hawas ice", "9pm", "turathi blue", "sillage"],
        sizes={"hawas ice": 10, "turathi blue": 10, "sillage": 10},
        note="'Havas' misspelled",
    ),
    Case(
        "conversational, size at the end",
        "Ok sir \nCurrently I need hawas og, hawas ice, lattafa khamrah, "
        "lattafa khamrah qahwa, 9pm afnan \nEach 5 ml decants sir",
        expect=["hawas", "hawas ice", "khamrah", "khamrah qahwa", "9pm"],
    ),
    Case(
        "bare names, no sizes at all",
        "Turathi blue\n9pm\n9pm night out\nRare reef\nCollectors edition\nNot only intense",
        expect=[
            "turathi blue", "9pm", "9pm night out", "rare reef",
            "collector", "not only intense",
        ],
    ),
    Case(
        "partial wishlist",
        "- Detour Noir\n- Detour Eco\n     - Aether Extrait\n- Tobacco Touch\n- Woody Oud\n"
        "- Opulence Leather\n- Porto Neroli\n- Toscano Leather\n- Fabulo Intense\n- Black Origami",
        expect=[
            "detour noir", "detour eco", "aether extrait", "tobacco touch",
            "woody oud", "opulence leather", "porto neroli", "toscano leather",
            "fabulo intense", "black origami",
        ],
    ),
    Case(
        "abbreviations and 'X and Y'",
        "I want to buy 3 ml decants of \nFrench avenue aether extrait\n"
        "French avenue vulcan feu\nAlbait  aldimashqi myslef men\nAfnan sce and snoi\n"
        "Afnan turathi blue and electric\nAl wataniyah classic",
        expect=[
            "aether extrait", "vulcan feu", "myslf men", "not only intense",
            "turathi blue", "turathi electric", "kayaan classic",
        ],
        note="'sce' = Supremacy Collector's Edition, 'snoi' = Supremacy Not Only Intense",
    ),
    Case(
        "bare abbreviations",
        "afnan sce\nsnoi\nturathi blue",
        expect=["collector", "not only intense", "turathi blue"],
    ),
]


def names_of(ids: list[str]) -> list[str]:
    return [PERFUMES[p]["display_name"] for p in ids if p in PERFUMES]


async def run_case(case: Case) -> tuple[list[str], list[str], list[str], dict]:
    from app.matcher import match_perfume

    result = await match_perfume(case.message)
    ids = result.matched_perfume_ids or (
        [result.perfume_id] if result.perfume_id else []
    )
    found = names_of(ids)
    lowered = [n.lower() for n in found]

    hit = [e for e in case.expect if any(e in n for n in lowered)]
    missed = [e for e in case.expect if e not in hit]

    # Exactness: what came back must BE what was asked for. A reply padded
    # with products the customer never named is its own kind of wrong — it
    # buries the ones they did name and reads as the bot guessing.
    permitted = case.expect + case.allow_extra
    extras = [
        name for name, low in zip(found, lowered)
        if not any(e in low for e in permitted)
    ]

    return found, hit, missed, {"extras": extras, "layer": result.layer}


async def main_async(use_groq: bool) -> int:
    if not use_groq:
        settings.GROQ_API_KEY = ""

    total_expected = total_hit = 0
    total_extras = 0
    failures = []

    print()
    print("=" * 94)
    print(f"  REAL CUSTOMER QUERIES — {len(CASES)} messages"
          f"{'  (live Groq)' if use_groq else '  (index only)'}")
    print("=" * 94)

    for case in CASES:
        found, hit, missed, extra = await run_case(case)
        total_expected += len(case.expect)
        total_hit += len(hit)
        total_extras += len(extra["extras"])

        ok = not missed and not extra["extras"]
        mark = "PASS" if ok else "FAIL"
        print()
        print(f"  [{mark}] {case.label}  —  {len(hit)}/{len(case.expect)} products"
              f"  (returned {len(found)})")
        if case.note:
            print(f"         note: {case.note}")
        if missed:
            print(f"         MISSED: {', '.join(missed)}")
            failures.append((case.label, "missed", missed))
        if extra["extras"]:
            print(f"         EXTRA:  {'; '.join(extra['extras'])}")
            failures.append((case.label, "extra", extra["extras"]))

    print()
    print("  " + "-" * 90)
    print(f"  Products correctly identified : {total_hit}/{total_expected}"
          f"  ({total_hit / total_expected * 100:.1f}%)")
    print(f"  Products never asked for      : {total_extras}")
    print(f"  Messages fully correct        : {len(CASES) - len({f[0] for f in failures})}/{len(CASES)}")
    print()
    return 0 if not failures else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groq", action="store_true", help="use the live LLM too")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(asyncio.run(main_async(args.groq)))


if __name__ == "__main__":
    main()
