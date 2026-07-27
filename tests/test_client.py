import unittest
from unittest.mock import patch

from polymarket_app.client import PolymarketClient
from polymarket_app.config import DEFAULT_SETTINGS


class ClientTest(unittest.TestCase):
    def test_single_market_object_becomes_list(self) -> None:
        client = PolymarketClient(DEFAULT_SETTINGS)
        with patch.object(client, "_get_json", return_value={"conditionId": "abc"}):
            markets = client.get_active_markets(limit=1)
        self.assertEqual(markets, [{"conditionId": "abc"}])

    def test_market_wrapper_becomes_list(self) -> None:
        client = PolymarketClient(DEFAULT_SETTINGS)
        with patch.object(client, "_get_json", return_value={"markets": [{"id": "1"}]}):
            markets = client.get_active_markets()
        self.assertEqual(markets, [{"id": "1"}])

    def test_public_search_flattens_active_markets_and_deduplicates(self) -> None:
        client = PolymarketClient(DEFAULT_SETTINGS)
        active = {
            "conditionId": "active",
            "active": True,
            "closed": False,
            "question": "Active market",
        }
        closed = {
            "conditionId": "closed",
            "active": True,
            "closed": True,
            "question": "Closed market",
        }
        payload = {
            "events": [
                {"id": "event-1", "title": "Event", "markets": [active, closed]},
                {"id": "event-2", "title": "Duplicate", "markets": [active]},
            ]
        }
        with patch.object(client, "_get_json", return_value=payload) as get_json:
            markets = client.search_public_markets("market")
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0]["conditionId"], "active")
        self.assertEqual(markets[0]["events"][0]["id"], "event-1")
        _, _, params = get_json.call_args.args
        self.assertNotIn("optimized", params)


if __name__ == "__main__":
    unittest.main()
