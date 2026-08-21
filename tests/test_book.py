import json
import unittest
from decimal import Decimal

from polymarket_app.book import (
    Level,
    ask_levels,
    fee_per_share,
    load_book,
    parse_levels,
    walk,
)


class ParseBookTest(unittest.TestCase):
    def test_clob_ordering_is_normalised(self) -> None:
        """CLOBはasksを降順、bidsを昇順で返す。必ず並べ直す。

        受け取った順に約定を積むと最悪価格から食うことになり、損益分岐が
        現実離れした値になる。実データでは asks[0] が 0.99、実際の最良が
        末尾の 0.17 だった。
        """
        raw = json.dumps(
            {
                "bids": [{"price": "0.10", "size": "5"}, {"price": "0.16", "size": "9"}],
                "asks": [{"price": "0.99", "size": "3"}, {"price": "0.17", "size": "7"}],
            }
        )
        book = load_book(raw)
        self.assertEqual(book["asks"][0].price, Decimal("0.17"))
        self.assertEqual(book["bids"][0].price, Decimal("0.16"))

    def test_broken_rows_are_dropped(self) -> None:
        levels = parse_levels(
            [
                {"price": "0.30", "size": "10"},
                {"price": "bad", "size": "10"},
                {"price": "0.30"},
                {"price": "0.30", "size": "0"},
                {"price": "0", "size": "10"},
                {"price": "1.4", "size": "10"},
                "not a dict",
            ]
        )
        self.assertEqual(levels, [Level(Decimal("0.30"), Decimal("10"))])

    def test_unreadable_payload_yields_empty_book(self) -> None:
        for raw in (None, "", "not json", "[1,2,3]"):
            self.assertEqual(load_book(raw), {"bids": [], "asks": []})

    def test_no_side_is_mirrored_from_bids(self) -> None:
        """collectorはYESの板しか取らない。NOのアスクはYESのビッドの裏返し。"""
        book = load_book(
            json.dumps(
                {
                    "bids": [{"price": "0.40", "size": "10"}, {"price": "0.38", "size": "20"}],
                    "asks": [{"price": "0.45", "size": "30"}],
                }
            )
        )
        no_side = ask_levels(book, "no")
        self.assertEqual([level.price for level in no_side], [Decimal("0.60"), Decimal("0.62")])
        self.assertEqual(no_side[0].size, Decimal("10"))

    def test_no_side_is_empty_when_there_are_no_bids(self) -> None:
        book = load_book(json.dumps({"bids": [], "asks": [{"price": "0.5", "size": "1"}]}))
        self.assertEqual(ask_levels(book, "no"), [])


class FeeTest(unittest.TestCase):
    def test_formula_matches_published_amounts(self) -> None:
        """fee = shares × rate × p × (1−p)。min(p, 1−p) ではない。

        公表されている100株あたりの実額と一致することで裏を取る。
        """
        hundred = Decimal("100")
        crypto = hundred * fee_per_share(Decimal("0.5"), Decimal("0.07"))
        politics = hundred * fee_per_share(Decimal("0.5"), Decimal("0.04"))
        self.assertEqual(crypto, Decimal("1.75"))
        self.assertEqual(politics, Decimal("1.00"))

    def test_fee_peaks_at_the_middle_and_falls_to_the_edges(self) -> None:
        rate = Decimal("0.05")
        middle = fee_per_share(Decimal("0.50"), rate)
        self.assertGreater(middle, fee_per_share(Decimal("0.10"), rate))
        self.assertGreater(middle, fee_per_share(Decimal("0.90"), rate))

    def test_zero_rate_market_pays_nothing(self) -> None:
        self.assertEqual(fee_per_share(Decimal("0.5"), Decimal("0")), Decimal("0"))


class WalkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.levels = [
            Level(Decimal("0.53"), Decimal("1000")),
            Level(Decimal("0.55"), Decimal("2000")),
        ]

    def test_small_stake_fills_at_the_best_price(self) -> None:
        fill = walk(self.levels, Decimal("100"), Decimal("0.05"))
        # 1株あたり 0.53 + 0.05×0.53×0.47 = 0.5424...
        self.assertEqual(fill.unfilled, Decimal("0"))
        self.assertEqual(fill.levels[1][1], Decimal("0"))
        self.assertAlmostEqual(float(fill.spent / fill.shares), 0.542455, places=5)

    def test_larger_stake_walks_deeper_and_costs_more(self) -> None:
        small = walk(self.levels, Decimal("100"), Decimal("0.05"))
        large = walk(self.levels, Decimal("1000"), Decimal("0.05"))
        self.assertGreater(large.spent / large.shares, small.spent / small.shares)
        self.assertGreater(large.levels[1][1], Decimal("0"))

    def test_walk_is_independent_of_input_order(self) -> None:
        forward = walk(self.levels, Decimal("900"), Decimal("0.05"))
        reversed_input = walk(list(reversed(self.levels)), Decimal("900"), Decimal("0.05"))
        self.assertEqual(forward.shares, reversed_input.shares)

    def test_exhausted_book_reports_the_remainder(self) -> None:
        fill = walk(self.levels, Decimal("100000"), Decimal("0.05"))
        self.assertGreater(fill.unfilled, Decimal("0"))
        self.assertEqual(fill.shares, Decimal("3000"))
        self.assertEqual(fill.spent + fill.unfilled, Decimal("100000"))

    def test_spent_is_notional_plus_fees(self) -> None:
        fill = walk(self.levels, Decimal("500"), Decimal("0.05"))
        self.assertAlmostEqual(float(fill.spent), float(fill.notional + fill.fees), places=9)

    def test_empty_book_buys_nothing(self) -> None:
        fill = walk([], Decimal("100"), Decimal("0.05"))
        self.assertEqual(fill.shares, Decimal("0"))
        self.assertEqual(fill.unfilled, Decimal("100"))


class DustTest(unittest.TestCase):
    """端数が「幻の約定」を作らないこと。

    Decimalの除算は28桁で打ち切られるため、ある段を予算ちょうどで買い切っても
    1e-26程度の残額が出る。持ち越すと、1株も買っていない段が消化済みとして
    色付き、段数にも数えられる。
    """

    def test_exact_fill_does_not_touch_the_next_level(self) -> None:
        levels = [
            Level(Decimal("0.64"), Decimal("30707.73")),
            Level(Decimal("0.65"), Decimal("1803")),
        ]
        fill = walk(levels, Decimal("100"), Decimal("0.05"))
        self.assertEqual(fill.levels[1][1], Decimal("0"))
        self.assertEqual(fill.unfilled, Decimal("0"))
        self.assertEqual(len([1 for _, take in fill.levels if take > 0]), 1)

    def test_a_genuinely_exhausted_book_still_reports_the_remainder(self) -> None:
        levels = [Level(Decimal("0.50"), Decimal("10"))]
        fill = walk(levels, Decimal("1000"), Decimal("0"))
        self.assertEqual(fill.shares, Decimal("10"))
        self.assertEqual(fill.unfilled, Decimal("995"))


if __name__ == "__main__":
    unittest.main()
