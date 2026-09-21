import json
import tempfile
import unittest
from pathlib import Path

from polymarket_app.database import Database


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        sample_path = Path("docs/api_samples/gamma_market_sample.json")
        self.market = json.loads(sample_path.read_text(encoding="utf-8"))["response"]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_upsert_market_and_outcomes(self) -> None:
        count = self.database.upsert_market(self.market)
        self.assertEqual(count, 2)
        stats = self.database.stats()
        self.assertEqual(stats["markets"], 1)
        self.assertEqual(stats["outcomes"], 2)
        market = self.database.market_detail(self.market["conditionId"])
        self.assertIsNotNone(market)
        self.assertEqual(market["outcomes"][0]["name"], "Yes")
        self.assertEqual(market["outcomes"][0]["current_price"], "0.515")

    def test_price_history_is_idempotent(self) -> None:
        self.database.upsert_market(self.market)
        token_id = json.loads(self.market["clobTokenIds"])[0]
        history = [{"t": 100, "p": 0.4}, {"t": 200, "p": 0.5}]
        self.database.save_price_history(token_id, history)
        self.database.save_price_history(token_id, history)
        self.assertEqual(len(self.database.token_history(token_id)), 2)

    def test_history_window_excludes_older_points(self) -> None:
        token_id = self._token_with_history(
            [{"t": 100, "p": 0.4}, {"t": 200, "p": 0.5}, {"t": 300, "p": 0.6}]
        )
        window = self.database.token_history(token_id, since_timestamp=200)
        self.assertEqual([row["timestamp_utc"] for row in window], [200, 300])

    def test_history_limit_keeps_the_newest_points_in_ascending_order(self) -> None:
        """右端が欠けると現在値が読めなくなるため、切り捨てるのは古い側。"""
        token_id = self._token_with_history(
            [{"t": index * 100, "p": 0.5} for index in range(1, 6)]
        )
        rows = self.database.token_history(token_id, limit=3)
        self.assertEqual([row["timestamp_utc"] for row in rows], [300, 400, 500])

    def test_history_fetch_age_is_none_until_recorded(self) -> None:
        self.database.upsert_market(self.market)
        token_id = json.loads(self.market["clobTokenIds"])[0]
        self.assertIsNone(self.database.history_fetch_age_seconds(token_id, "1d"))
        self.database.record_history_fetch(token_id, "1d", 12)
        age = self.database.history_fetch_age_seconds(token_id, "1d")
        self.assertIsNotNone(age)
        self.assertLess(age, 5.0)
        # 別の期間は独立して管理する。
        self.assertIsNone(self.database.history_fetch_age_seconds(token_id, "1w"))

    def test_market_detail_does_not_carry_the_raw_api_response(self) -> None:
        """原文は検証用にDBへ残すが、ブラウザへ送っても使い道がない。"""
        self.database.upsert_market(self.market)
        detail = self.database.market_detail(self.market["conditionId"])
        self.assertNotIn("raw_json", detail)
        self.assertIn("question", detail)

    def test_search_also_matches_description_and_slug(self) -> None:
        self._insert(
            "a", question="Totally unrelated", description="Mentions Iran inside"
        )
        self._insert("b", question="Unrelated too", slug="iran-strike-2026")
        self._insert("c", question="No match here")
        found = {row["condition_id"] for row in self.database.list_markets(search="iran")}
        self.assertEqual(found, {"a", "b"})

    def test_sorts_are_resolved_by_key_not_by_raw_sql(self) -> None:
        self._insert("low", volume="10", liquidity="900", end_date_utc="2030-01-01")
        self._insert("high", volume="900", liquidity="10", end_date_utc="2027-01-01")

        def order(sort: str) -> list[str]:
            return [row["condition_id"] for row in self.database.list_markets(sort=sort)]

        self.assertEqual(order("volume"), ["high", "low"])
        self.assertEqual(order("liquidity"), ["low", "high"])
        self.assertEqual(order("ending_soon"), ["high", "low"])
        # 未知の値と注入の試みは既定の並びへ丸める。
        self.assertEqual(order("volume; DROP TABLE markets"), ["high", "low"])
        self.assertEqual(order(""), ["high", "low"])
        self.assertEqual(self.database.stats()["markets"], 2)

    def test_markets_without_an_end_date_sort_last_when_ending_soon(self) -> None:
        self._insert("dated", end_date_utc="2027-01-01")
        self._insert("undated", end_date_utc=None)
        order = [
            row["condition_id"]
            for row in self.database.list_markets(sort="ending_soon")
        ]
        self.assertEqual(order, ["dated", "undated"])

    def test_thin_markets_can_be_excluded_by_a_liquidity_floor(self) -> None:
        self._insert("deep", liquidity="5000")
        self._insert("thin", liquidity="120")
        self.assertEqual(
            {row["condition_id"] for row in self.database.list_markets()},
            {"deep", "thin"},
        )
        self.assertEqual(
            [
                row["condition_id"]
                for row in self.database.list_markets(min_liquidity=1000)
            ],
            ["deep"],
        )

    def test_a_zero_floor_keeps_markets_whose_liquidity_is_unknown(self) -> None:
        """SQLでは`NULL >= 0`がNULLになるため、条件を足すと黙って消える。"""
        self._insert("known", liquidity="5000")
        self._insert("unknown", liquidity=None)
        found = {row["condition_id"] for row in self.database.list_markets()}
        self.assertEqual(found, {"known", "unknown"})
        self.assertEqual(
            [
                row["condition_id"]
                for row in self.database.list_markets(min_liquidity=1000)
            ],
            ["known"],
        )

    def test_lookup_by_ids_honours_the_same_sort_and_filter(self) -> None:
        self._insert("deep", volume="10", liquidity="5000")
        self._insert("thin", volume="900", liquidity="120")
        rows = self.database.list_markets_by_ids(["deep", "thin"], min_liquidity=1000)
        self.assertEqual([row["condition_id"] for row in rows], ["deep"])

    def test_nested_connect_shares_one_transaction(self) -> None:
        """入れ子の書き込みは、外側を抜けるまで確定しない。"""
        self.database.upsert_market(self.market)
        token_id = json.loads(self.market["clobTokenIds"])[0]
        with self.database.connect() as connection:
            self.database.save_price_history(token_id, [{"t": 10, "p": 0.5}])
            self.database.record_history_fetch(token_id, "1d", 1)
            self.assertEqual(len(self.database.token_history(token_id)), 1)
            self.assertTrue(connection.in_transaction)
        self.assertEqual(len(self.database.token_history(token_id)), 1)
        self.assertIsNotNone(self.database.history_fetch_age_seconds(token_id, "1d"))

    def test_a_failed_nested_write_leaves_nothing_behind(self) -> None:
        self.database.upsert_market(self.market)
        token_id = json.loads(self.market["clobTokenIds"])[0]
        with self.assertRaises(RuntimeError):
            with self.database.connect():
                self.database.save_price_history(token_id, [{"t": 10, "p": 0.5}])
                raise RuntimeError("取得の途中で失敗した")
        self.assertEqual(self.database.token_history_count(token_id), 0)

    def _insert(self, condition_id: str, **overrides: object) -> None:
        market = dict(self.market)
        market["conditionId"] = condition_id
        market["clobTokenIds"] = json.dumps([f"{condition_id}-yes", f"{condition_id}-no"])
        field_names = {
            "question": "question",
            "description": "description",
            "slug": "slug",
            "volume": "volume",
            "liquidity": "liquidity",
            "end_date_utc": "endDate",
            "restricted": "restricted",
        }
        for key, value in overrides.items():
            market[field_names[key]] = value
        self.database.upsert_market(market)

    def _token_with_history(self, history: list[dict[str, object]]) -> str:
        self.database.upsert_market(self.market)
        token_id = json.loads(self.market["clobTokenIds"])[0]
        self.database.save_price_history(token_id, history)
        return token_id


if __name__ == "__main__":
    unittest.main()


class FeeAndEntryCostTest(unittest.TestCase):
    """手数料の取り込みと、一覧に出す摩擦の算出。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "test.db"
        self.database = Database(self.path)
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _market(self, condition_id: str, ask: str, quoted: str, **overrides):
        market = {
            "conditionId": condition_id,
            "question": f"Market {condition_id}",
            "active": True,
            "closed": False,
            "bestBid": "0.50",
            "bestAsk": ask,
            "liquidity": "5000",
            "volume": "10000",
            "feesEnabled": True,
            "feeType": "culture_fees",
            "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps([quoted, str(1 - float(quoted))]),
            "clobTokenIds": json.dumps([f"{condition_id}-yes", f"{condition_id}-no"]),
        }
        market.update(overrides)
        return market

    def test_fee_rate_and_type_are_stored(self) -> None:
        self.database.upsert_market(self._market("a", "0.53", "0.515"))
        detail = self.database.market_detail("a")
        self.assertEqual(detail["fee_rate"], "0.05")
        self.assertEqual(detail["fee_type"], "culture_fees")

    def test_market_without_a_fee_schedule_stores_a_zero_rate(self) -> None:
        """実測100件中15件はfeesEnabledがfalseでfeeSchedule自体が無い。"""
        self.database.upsert_market(
            self._market("b", "0.53", "0.515", feesEnabled=False, feeSchedule=None, feeType=None)
        )
        detail = self.database.market_detail("b")
        self.assertEqual(float(detail["fee_rate"]), 0.0)

    def test_entry_cost_includes_the_fee(self) -> None:
        self.database.upsert_market(self._market("c", "0.53", "0.515"))
        row = self.database.list_markets(limit=1)[0]
        self.assertAlmostEqual(row["entry_cost"], 0.027455, places=6)

    def test_untradable_markets_have_no_entry_cost(self) -> None:
        """ask=1.00は利益が定義できない。差だけ見ると最安に見えてしまう。"""
        self.database.upsert_market(self._market("d", "1", "0.999"))
        row = self.database.list_markets(limit=1)[0]
        self.assertIsNone(row["entry_cost"])
        self.assertIsNone(row["entry_cost_ratio"])

    def test_longshots_do_not_dominate_the_entry_cost_sort(self) -> None:
        """ポイント差で並べると ask=0.001 の大穴が上位を独占する。

        摩擦は+0.05ptしかないが、表示価格に対しては倍額の上乗せで、
        実際には最も割高な部類。割合で並べればこれが下位へ落ちる。
        """
        self.database.upsert_market(self._market("longshot", "0.001", "0.0005"))
        self.database.upsert_market(self._market("tight", "0.91", "0.905"))
        ordered = [row["condition_id"] for row in self.database.list_markets(sort="entry_cost")]
        self.assertEqual(ordered[0], "tight")
        self.assertEqual(ordered[-1], "longshot")

    def test_markets_without_an_entry_cost_sort_last(self) -> None:
        self.database.upsert_market(self._market("priced", "0.53", "0.515"))
        self.database.upsert_market(self._market("unpriced", "1", "0.999"))
        ordered = [row["condition_id"] for row in self.database.list_markets(sort="entry_cost")]
        self.assertEqual(ordered[-1], "unpriced")

    def test_order_book_snapshot_round_trip(self) -> None:
        self.database.upsert_market(self._market("e", "0.53", "0.515"))
        book = {
            "bids": [{"price": "0.50", "size": "10"}],
            "asks": [{"price": "0.53", "size": "20"}],
            "timestamp": "1700000000000",
        }
        self.database.save_order_book("e-yes", book)
        stored = self.database.latest_order_book("e-yes")
        self.assertEqual(stored["best_ask"], "0.53")
        self.assertEqual(json.loads(stored["raw_json"])["asks"][0]["size"], "20")

    def test_latest_snapshot_wins(self) -> None:
        self.database.upsert_market(self._market("f", "0.53", "0.515"))
        self.database.save_order_book("f-yes", {"bids": [], "asks": [{"price": "0.53", "size": "1"}]})
        self.database.save_order_book("f-yes", {"bids": [], "asks": [{"price": "0.61", "size": "1"}]})
        self.assertEqual(self.database.latest_order_book("f-yes")["best_ask"], "0.61")

    def test_order_book_snapshots_are_pruned_per_token(self) -> None:
        limited = Database(self.path, max_order_book_snapshots_per_token=2)
        limited.initialize()
        limited.upsert_market(self._market("prune", "0.53", "0.515"))
        for ask in ("0.51", "0.52", "0.53"):
            limited.save_order_book("prune-yes", {"bids": [], "asks": [{"price": ask, "size": "1"}]})
        with limited.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM order_book_snapshots WHERE token_id = ?", ("prune-yes",)
            ).fetchone()[0]
        self.assertEqual(count, 2)
        self.assertEqual(limited.latest_order_book("prune-yes")["best_ask"], "0.53")

    def test_closed_market_with_a_final_price_records_a_resolution(self) -> None:
        market = self._market("resolved", "0.99", "1.0", closed=True, active=False)
        self.database.upsert_market(market)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT outcome_index FROM market_resolutions WHERE condition_id = ?", ("resolved",)
            ).fetchone()
        self.assertEqual(row["outcome_index"], 0)

    def test_resolved_observation_uses_the_last_price_before_the_horizon(self) -> None:
        market = self._market(
            "timed", "0.99", "1.0", closed=True, active=False,
            endDate="2024-01-02T00:00:00Z",
        )
        self.database.upsert_market(market)
        self.database.save_price_history(
            "timed-yes",
            [
                {"t": 1703980800, "p": "0.40"},  # 48 hours before
                {"t": 1704067200, "p": "0.60"},  # exactly 24 hours before
                {"t": 1704110400, "p": "0.90"},  # 12 hours before: must not leak in
            ],
        )
        observations = self.database.resolved_observations(24 * 3600)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["price"], "0.60")

    def test_missing_snapshot_returns_none(self) -> None:
        self.assertIsNone(self.database.latest_order_book("nothing"))

    def test_migration_adds_fee_columns_to_an_existing_database(self) -> None:
        """スキーマはCREATE TABLE IF NOT EXISTSなので、既存DBには列が入らない。

        取りこぼすと手数料が常に0として扱われ、損益分岐が過小に出る。
        """
        import sqlite3

        with self.database.connect() as connection:
            connection.execute("ALTER TABLE markets DROP COLUMN fee_rate")
            connection.execute("ALTER TABLE markets DROP COLUMN fee_type")
        reopened = Database(self.path)
        reopened.initialize()
        with reopened.connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(markets)")}
        self.assertIn("fee_rate", columns)
        self.assertIn("fee_type", columns)
