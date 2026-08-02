"""
Which row wins when the spreadsheet says the same thing twice.

The rule the shop settled on, and the reason it needs a test rather than a
comment: the sheet lists ~90 products more than once, 39 of them at
different prices, and the answer to "what does the bot quote?" is decided
entirely by the order scripts/import_catalog_xlsx.py happens to read them
in. That is far too much money to leave resting on the order of a list
literal nobody is watching.

    1. The men's/unisex sheet wins over every other sheet.
    2. Within a sheet, the row that comes first wins.
    3. Testers are not a price list and are never read.

The workbook itself is not needed here — read_decants takes any object
shaped like an openpyxl workbook, so these build one in memory. That keeps
the test about the precedence rule rather than about the current contents
of a spreadsheet that changes every week.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import import_catalog_xlsx as importer  # noqa: E402

HEADER = ["Brand", "Fragrance Name", "Clone of", "3ml", "5ml", "8ml", "10ml", "20ml", "30ml"]


class FakeSheet:
    """openpyxl's iter_rows, to the extent read_decants uses it: it scans
    from the top to find the header row, then re-reads from min_row."""

    def __init__(self, rows):
        self._rows = rows

    def iter_rows(self, min_row=1, values_only=True):
        return iter(self._rows[min_row - 1:])


class FakeWorkbook:
    """The two attributes read_decants actually uses."""

    def __init__(self, sheets: dict):
        self._sheets = sheets
        self.sheetnames = list(sheets)

    def __getitem__(self, name):
        return self._sheets[name]


def product(brand, name, *prices):
    """One row: brand, name, no clone, then the six size columns."""
    padded = list(prices) + [None] * (6 - len(prices))
    return [brand, name, None, *padded]


def book(**sheets) -> FakeWorkbook:
    """Sheets keyed by the real sheet names, each a header row plus rows."""
    named = {
        "Decant Sheet Men-UNISEX": sheets.get("men", []),
        "For Women": sheets.get("women", []),
        "New Decant Additions ": sheets.get("additions", []),
        "Testers ": sheets.get("testers", []),
    }
    return FakeWorkbook({name: FakeSheet([HEADER, *rows]) for name, rows in named.items()})


@pytest.fixture(autouse=True)
def clear_clashes():
    """CLASHES is module-level state the importer appends to."""
    importer.CLASHES.clear()
    yield
    importer.CLASHES.clear()


def read(wb):
    notes: list[str] = []
    return importer.read_decants(wb, notes), notes


class TestTheMensSheetWins:
    def test_the_mens_price_is_the_one_kept(self):
        wb = book(
            men=[product("Lattafa", "Khamrah", 160, 230, 330, 420)],
            women=[product("Lattafa", "Khamrah", 190, 300, 370, None)],
        )
        rows, _ = read(wb)
        assert rows["lattafa khamrah"].prices["3ml"] == 160

    def test_it_wins_even_when_the_womens_row_offers_more_sizes(self):
        """A real case (Lattafa Nebras Elixir): the women's sheet lists 20ml
        and 30ml the men's sheet does not, and men's-wins drops both. That
        is the cost of the rule, taken deliberately — one row deciding a
        product's whole price list beats two rows disagreeing about it."""
        wb = book(
            men=[product("Lattafa", "Nebras Elixir", 140, 190, 300, 370)],
            women=[product("Lattafa", "Nebras Elixir", None, 200, 300, 380, 660, 920)],
        )
        rows, _ = read(wb)
        prices = rows["lattafa nebras elixir"].prices
        assert prices["5ml"] == 190
        assert "20ml" not in prices and "30ml" not in prices

    def test_the_additions_sheet_loses_to_both(self):
        wb = book(
            men=[product("Chanel", "Bleu De Chanel Parfum", 300)],
            additions=[product("Chanel", "Bleu De Chanel Parfum", 999)],
        )
        rows, _ = read(wb)
        assert rows["chanel bleu de chanel parfum"].prices["3ml"] == 300

    def test_a_product_only_on_the_womens_sheet_is_still_imported(self):
        """Precedence is not exclusion — the women's sheet is most of the
        women's catalog and all of it must come through."""
        wb = book(
            men=[product("Lattafa", "Khamrah", 160)],
            women=[product("Lattafa", "Yara", 130)],
        )
        rows, _ = read(wb)
        assert set(rows) == {"lattafa khamrah", "lattafa yara"}


class TestWithinASheetTheFirstRowWins:
    def test_the_earlier_row_is_kept(self):
        """Six products are listed twice on one sheet — Bvlgari Glacial
        Essence on rows 826 and 830, Pure XS on 1080 and 1088. Row order
        decides, so row order is the rule."""
        wb = book(
            men=[
                product("Bvlgari", "Glacial Essence", 250, 390, 610, 740),
                product("Bvlgari", "Glacial Essence", 290, 430, 670, 820),
            ]
        )
        rows, _ = read(wb)
        assert rows["bvlgari glacial essence"].prices["3ml"] == 250

    def test_the_later_row_does_not_add_its_extra_sizes(self):
        """First-wins means the whole row, not a merge of the two."""
        wb = book(
            men=[
                product("Paco Rabanne", "Pure XS", 160, 240, 380, 480),
                product("Paco Rabanne", "Pure XS", 200, 290, 450, 550, 940, 1330),
            ]
        )
        rows, _ = read(wb)
        assert "20ml" not in rows["paco rabanne pure xs"].prices


class TestNothingIsResolvedSilently:
    def test_a_price_clash_is_recorded_for_the_owner(self):
        wb = book(
            men=[product("Lattafa", "Khamrah", 160)],
            women=[product("Lattafa", "Khamrah", 190)],
        )
        read(wb)
        assert len(importer.CLASHES) == 1
        name, sheet, kept, dropped = importer.CLASHES[0]
        assert name == "Lattafa Khamrah"
        assert sheet == "For Women"
        assert kept["3ml"] == 160
        assert dropped["3ml"] == 190

    def test_an_identical_second_copy_is_not_reported_as_a_clash(self):
        """Half the duplicates agree on every price. Reporting those would
        bury the ones that do not."""
        wb = book(
            men=[product("Lattafa", "Khamrah", 160)],
            women=[product("Lattafa", "Khamrah", 160)],
        )
        read(wb)
        assert importer.CLASHES == []


class TestTestersAreNotAPriceList:
    def test_the_testers_sheet_is_never_read(self):
        """It is stock-on-hand for the owner. A tester price is not what a
        customer pays, and it must never win — or be imported at all."""
        wb = book(
            men=[product("Lattafa", "Khamrah", 160)],
            testers=[product("Lattafa", "Khamrah", 1), product("Lattafa", "Tester Only", 1)],
        )
        rows, _ = read(wb)
        assert rows["lattafa khamrah"].prices["3ml"] == 160
        assert "lattafa tester only" not in rows

    def test_the_sheet_order_is_the_rule_itself(self):
        """If this list is ever reordered, every duplicate in the catalog
        changes price. That is the whole reason this file exists."""
        assert importer.DECANT_SHEETS[0] == "Decant Sheet Men-UNISEX"
        assert "Testers " not in importer.DECANT_SHEETS


class TestRowsWithoutPrices:
    def test_a_heading_row_is_not_a_product(self):
        wb = book(men=[product("", "MEN'S SECTION"), product("Lattafa", "Khamrah", 160)])
        rows, _ = read(wb)
        assert set(rows) == {"lattafa khamrah"}

    def test_a_priceless_row_does_not_block_a_later_priced_one(self):
        """Otherwise a blank row early in the sheet would claim the name and
        the real prices below it would be discarded as a duplicate."""
        wb = book(
            men=[product("Lattafa", "Khamrah"), product("Lattafa", "Khamrah", 160)]
        )
        rows, _ = read(wb)
        assert rows["lattafa khamrah"].prices["3ml"] == 160
