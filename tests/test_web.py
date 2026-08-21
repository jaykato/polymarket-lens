import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.request import urlopen

from polymarket_app import __version__
from polymarket_app.client import ApiError
from polymarket_app.database import Database
from polymarket_app.web import AppServer


class WebApiTest(unittest.TestCase):
    """ローカルHTTP層を実際に起動して確認する。外部通信はしない。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        sample = Path("docs/api_samples/gamma_market_sample.json")
        self.market = json.loads(sample.read_text(encoding="utf-8"))["response"]
        self.database.upsert_market(self.market)
        self.condition_id = self.market["conditionId"]
        self.token_id = json.loads(self.market["clobTokenIds"])[0]

        now = int(time.time())
        self.client = Mock()
        # 古い順に並び、価格はゆるやかに上昇する系列。
        self.client.get_price_history.return_value = [
            {"t": now - 3600 * hours_ago, "p": 0.40 + 0.01 * (12 - hours_ago)}
            for hours_ago in range(12, 0, -1)
        ]
        # 既定では板を取れない状態にしておく。取れる場合は各テストで差し替える。
        self.client.get_order_book.side_effect = ApiError("book unavailable")

        self.server = AppServer(
            ("127.0.0.1", 0),
            self.database,
            self.client,
            Path("polymarket_app/static"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temp_dir.cleanup()

    def get(self, path: str) -> object:
        with urlopen(f"{self.base}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_options_expose_sorts_ranges_and_version_to_the_ui(self) -> None:
        options = self.get("/api/options")
        self.assertIn("volume", [item["key"] for item in options["sorts"]])
        self.assertIn("max", [item["key"] for item in options["ranges"]])
        # 画面のバージョン表示はここから来る。HTMLへ焼き付けない。
        self.assertEqual(options["version"], __version__)

    def test_market_detail_omits_the_raw_response_but_keeps_the_analysis(self) -> None:
        detail = self.get(f"/api/markets/{self.condition_id}")
        self.assertNotIn("raw_json", detail)
        self.assertNotIn("internal_scores", detail["analysis"])
        self.assertIn("headline", detail["analysis"])

    def test_unknown_listing_parameters_do_not_break_the_request(self) -> None:
        for query in (
            "?sort=bogus",
            "?sort=volume;DROP+TABLE+markets",
            "?min_liquidity=abc",
            "?min_liquidity=-5",
        ):
            markets = self.get(f"/api/markets{query}")
            self.assertIsInstance(markets, list)
            self.assertEqual(len(markets), 1, f"{query} で市場が消えた")

    def test_the_liquidity_floor_filters_the_listing(self) -> None:
        self.assertEqual(len(self.get("/api/markets?min_liquidity=1")), 1)
        self.assertEqual(self.get("/api/markets?min_liquidity=999999999999"), [])

    def test_history_returns_points_and_a_movement_summary(self) -> None:
        payload = self.get(f"/api/history/{self.token_id}?range=1w")
        self.assertEqual(payload["range"], "1w")
        self.assertEqual(len(payload["points"]), 12)
        self.client.get_price_history.assert_called_once_with(
            self.token_id, interval="1w", fidelity=60
        )
        movement = payload["movement"]
        self.assertTrue(movement["available"])
        self.assertEqual(movement["observations"], 12)
        self.assertGreater(movement["change_points"], 0)

    def test_an_unknown_range_falls_back_instead_of_failing(self) -> None:
        payload = self.get(f"/api/history/{self.token_id}?range=1y")
        self.assertEqual(payload["range"], "1w")

    def test_missing_market_returns_not_found_as_json(self) -> None:
        try:
            self.get("/api/markets/does-not-exist")
        except Exception as exc:  # HTTPError
            self.assertEqual(getattr(exc, "code", None), 404)
        else:
            self.fail("404を返すべき")


if __name__ == "__main__":
    unittest.main()


class ReturnsApiTest(WebApiTest):
    """L0の口。板を保存した状態と、していない状態の両方を確かめる。"""

    BOOK = {
        "bids": [{"price": "0.50", "size": "1500"}],
        # CLOBは降順で返す。並べ直さずに歩くと最悪価格から食う。
        "asks": [{"price": "0.55", "size": "3000"}, {"price": "0.53", "size": "1200"}],
        "timestamp": "1700000000000",
    }

    def _store_book(self) -> None:
        """保存済みかつ新鮮な板を用意する。この状態では取り直しは起きない。"""
        self.database.save_order_book(self.token_id, self.BOOK)

    def test_returns_are_included_in_the_market_detail(self) -> None:
        self._store_book()
        detail = self.get(f"/api/markets/{self.condition_id}")
        self.assertTrue(detail["returns"]["available"])
        self.assertAlmostEqual(detail["returns"]["breakdown"]["best_ask"], 0.53, places=9)

    def test_returns_endpoint_walks_the_book_for_the_requested_size(self) -> None:
        self._store_book()
        small = self.get(f"/api/returns/{self.condition_id}?side=yes&stake=100")
        large = self.get(f"/api/returns/{self.condition_id}?side=yes&stake=1500")
        self.assertGreater(large["breakeven"], small["breakeven"])
        # 板の並び順に関わらず、最良の0.53から食い始める。
        self.assertAlmostEqual(small["breakdown"]["average_fill"], 0.53, places=9)

    def test_no_side_is_derived_from_the_stored_bids(self) -> None:
        self._store_book()
        quote = self.get(f"/api/returns/{self.condition_id}?side=no&stake=100")
        self.assertTrue(quote["available"])
        self.assertAlmostEqual(quote["breakdown"]["best_ask"], 0.50, places=9)

    def test_market_without_a_stored_book_is_fetched_on_demand(self) -> None:
        """同期は取得した市場ぶんの板しか保存しない。検索から入った市場では
        表示のたびに補わないと、ほとんどの市場が値付け不能になる。"""
        self.client.get_order_book.side_effect = None
        self.client.get_order_book.return_value = self.BOOK
        quote = self.get(f"/api/returns/{self.condition_id}?side=yes&stake=100")
        self.client.get_order_book.assert_called_once_with(self.token_id)
        self.assertTrue(quote["available"])
        self.assertAlmostEqual(quote["breakdown"]["best_ask"], 0.53, places=9)

    def test_a_fresh_book_is_not_refetched_while_the_stake_changes(self) -> None:
        """スライダーは動かすたびにこの口を叩く。そのたびに取り直さない。"""
        self._store_book()
        for stake in (100, 200, 300):
            self.get(f"/api/returns/{self.condition_id}?side=yes&stake={stake}")
        self.client.get_order_book.assert_not_called()

    def test_failed_fetch_is_reported_not_priced_and_not_retried(self) -> None:
        for _ in range(3):
            quote = self.get(f"/api/returns/{self.condition_id}?side=yes&stake=100")
        self.assertFalse(quote["available"])
        self.assertEqual(quote["reason"], "book_fetch_failed")
        # 失敗も覚えておく。失敗する呼び出しを毎回繰り返さない。
        self.client.get_order_book.assert_called_once_with(self.token_id)

    def test_a_stale_book_is_still_used_when_the_refetch_fails(self) -> None:
        """古い板でも、何も出せないよりは良い。取り直せなかったからといって
        保存済みの板を捨てると、通信が落ちた瞬間に画面から数字が消える。"""
        self.database.save_order_book(self.token_id, self.BOOK)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE order_book_snapshots SET fetched_at_utc = ?",
                ("2020-01-01T00:00:00+00:00",),
            )
        quote = self.get(f"/api/returns/{self.condition_id}?side=yes&stake=100")
        self.client.get_order_book.assert_called_once_with(self.token_id)
        self.assertTrue(quote["available"])
        self.assertEqual(quote["captured_at_utc"], "2020-01-01T00:00:00+00:00")

    def test_unreadable_stake_does_not_break_the_request(self) -> None:
        self._store_book()
        for query in ("stake=abc", "stake=", "stake=-40", "side=maybe", "stake=1e9999"):
            quote = self.get(f"/api/returns/{self.condition_id}?{query}")
            self.assertIn("side", quote)
            self.assertIn(quote["side"], ("yes", "no"))

    def test_unknown_market_is_not_found(self) -> None:
        from urllib.error import HTTPError

        with self.assertRaises(HTTPError) as raised:
            self.get("/api/returns/does-not-exist")
        self.assertEqual(raised.exception.code, 404)

    def test_entry_cost_is_exposed_to_the_listing(self) -> None:
        rows = self.get("/api/markets?sort=entry_cost")
        self.assertIn("entry_cost", rows[0])
        self.assertIn("entry_cost_ratio", rows[0])

    def test_entry_cost_sort_is_offered_to_the_ui(self) -> None:
        options = self.get("/api/options")
        self.assertIn("entry_cost", [item["key"] for item in options["sorts"]])
