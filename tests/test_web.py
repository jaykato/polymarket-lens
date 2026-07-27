import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.request import urlopen

from polymarket_app import __version__
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
