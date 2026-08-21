import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from polymarket_app.book import load_book
from polymarket_app.returns import (
    DEFAULT_STAKE,
    MIN_STAKE,
    clamp_stake,
    days_to_resolution,
    entry_cost,
    quote_position,
)

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)


def market(**overrides):
    base = {
        "question": "Test market",
        "end_date_utc": "2026-09-30T12:00:00Z",
        "fee_rate": "0.05",
        "fee_type": "culture_fees",
        "best_bid": "0.50",
        "best_ask": "0.53",
        "outcomes": [
            {"token_id": "yes", "outcome_index": 0, "name": "Yes", "current_price": "0.515"},
            {"token_id": "no", "outcome_index": 1, "name": "No", "current_price": "0.485"},
        ],
    }
    base.update(overrides)
    return base


def book(bids=(("0.50", "1500"),), asks=(("0.53", "1200"), ("0.55", "3000"))):
    return load_book(
        json.dumps(
            {
                "bids": [{"price": p, "size": s} for p, s in bids],
                "asks": [{"price": p, "size": s} for p, s in asks],
            }
        )
    )


class EntryCostTest(unittest.TestCase):
    def test_entry_cost_is_ask_plus_fee_above_quoted(self) -> None:
        cost = entry_cost("0.53", "0.515", "0.05")
        # 0.53 + 0.05×0.53×0.47 − 0.515
        self.assertAlmostEqual(float(cost), 0.027455, places=6)

    def test_fee_free_market_pays_only_the_spread(self) -> None:
        self.assertAlmostEqual(float(entry_cost("0.53", "0.515", "0")), 0.015, places=9)

    def test_untradable_ask_has_no_entry_cost(self) -> None:
        """1.00払って1.00受け取る建玉は利益が定義できない。

        差だけ見ると「最も安い」に見えてしまうため、Noneにして並びから外す。
        """
        self.assertIsNone(entry_cost("1.0", "0.999", "0.05"))
        self.assertIsNone(entry_cost("0", "0.001", "0.05"))

    def test_missing_inputs_yield_none(self) -> None:
        self.assertIsNone(entry_cost(None, "0.5", "0.05"))
        self.assertIsNone(entry_cost("0.5", None, "0.05"))


class StakeTest(unittest.TestCase):
    def test_unreadable_stake_falls_back_to_the_default(self) -> None:
        for value in ("", "abc", None, "NaN"):
            self.assertEqual(clamp_stake(value), DEFAULT_STAKE)

    def test_stake_is_bounded(self) -> None:
        self.assertEqual(clamp_stake("-5"), MIN_STAKE)
        self.assertEqual(clamp_stake("999999999"), Decimal("1000000"))


class DaysTest(unittest.TestCase):
    def test_future_date_returns_days(self) -> None:
        later = (NOW + timedelta(days=40)).isoformat()
        self.assertEqual(days_to_resolution(later, now=NOW), 40)

    def test_past_date_returns_none(self) -> None:
        """終了日を過ぎてもcloseフラグが立たない市場が実在する。

        弾かないと年率換算が「113日ぶんの損益を1日に圧縮した値」になる。
        """
        earlier = (NOW - timedelta(days=113)).isoformat()
        self.assertIsNone(days_to_resolution(earlier, now=NOW))

    def test_unreadable_date_returns_none(self) -> None:
        self.assertIsNone(days_to_resolution(None, now=NOW))
        self.assertIsNone(days_to_resolution("not a date", now=NOW))


class QuoteTest(unittest.TestCase):
    def test_breakeven_sits_above_the_quoted_price(self) -> None:
        quote = quote_position(market(), book(), side="yes", stake=100, now=NOW)
        self.assertTrue(quote["available"])
        self.assertAlmostEqual(quote["breakeven"], 0.542455, places=5)
        self.assertAlmostEqual(quote["quoted"], 0.515, places=6)
        self.assertAlmostEqual(quote["gap_points"], 2.7455, places=3)

    def test_breakeven_is_spent_over_shares(self) -> None:
        quote = quote_position(market(), book(), stake=250, now=NOW)
        self.assertAlmostEqual(quote["breakeven"], quote["stake"] / quote["shares"], places=9)

    def test_larger_size_walks_the_book_and_raises_breakeven(self) -> None:
        small = quote_position(market(), book(), stake=100, now=NOW)
        large = quote_position(market(), book(), stake=1500, now=NOW)
        self.assertGreater(large["breakeven"], small["breakeven"])
        self.assertGreater(large["breakdown"]["average_fill"], small["breakdown"]["average_fill"])

    def test_no_side_uses_the_mirrored_bid(self) -> None:
        quote = quote_position(market(), book(), side="no", stake=100, now=NOW)
        # NOの最良アスクは 1 − 0.50 = 0.50
        self.assertAlmostEqual(quote["breakdown"]["best_ask"], 0.50, places=9)
        self.assertAlmostEqual(quote["quoted"], 0.485, places=6)

    def test_missing_side_of_the_book_is_reported_not_priced(self) -> None:
        quote = quote_position(market(), book(bids=()), side="no", stake=100, now=NOW)
        self.assertFalse(quote["available"])
        self.assertEqual(quote["reason"], "no_book")

    def test_offer_at_one_cannot_return_anything(self) -> None:
        quote = quote_position(
            market(), book(asks=(("1.0", "500"),)), side="yes", stake=100, now=NOW
        )
        self.assertFalse(quote["available"])
        self.assertEqual(quote["reason"], "no_upside")

    def test_fee_free_market_costs_only_the_ask(self) -> None:
        quote = quote_position(market(fee_rate="0"), book(), stake=100, now=NOW)
        self.assertAlmostEqual(quote["breakdown"]["average_fee"], 0.0, places=9)
        self.assertAlmostEqual(quote["breakeven"], 0.53, places=9)

    def test_exhausted_book_reports_the_unfilled_remainder(self) -> None:
        quote = quote_position(market(), book(), stake=100000, now=NOW)
        self.assertTrue(quote["book_exhausted"])
        self.assertGreater(quote["unfilled"], 0)
        # 約定できなかった分は投じたことにしない。
        self.assertLess(quote["stake"], quote["requested_stake"])

    def test_annualised_is_omitted_once_the_end_date_has_passed(self) -> None:
        expired = market(end_date_utc=(NOW - timedelta(days=113)).isoformat())
        quote = quote_position(expired, book(), stake=100, now=NOW)
        self.assertIsNone(quote["days_to_resolution"])
        self.assertIsNone(quote["annualised_percent"])

    def test_losing_side_loses_everything(self) -> None:
        quote = quote_position(market(), book(), stake=100, now=NOW)
        self.assertEqual(quote["lose"]["percent"], -100.0)
        self.assertAlmostEqual(quote["lose"]["profit"], -quote["stake"], places=9)

    def test_quote_never_claims_the_outcome(self) -> None:
        quote = quote_position(market(), book(), stake=100, now=NOW)
        note = quote["note"].lower()
        self.assertIn("arithmetic only", note)
        for word in ("should", "recommend", "will win", "buy now"):
            self.assertNotIn(word, note)


if __name__ == "__main__":
    unittest.main()
