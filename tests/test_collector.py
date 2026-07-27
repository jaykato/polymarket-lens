import unittest
from unittest.mock import Mock

from polymarket_app.collector import Collector


def market(number: int) -> dict:
    return {
        "conditionId": f"condition-{number}",
        "clobTokenIds": f'["token-{number}-yes", "token-{number}-no"]',
        "enableOrderBook": True,
    }


class CollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.client.get_active_markets.return_value = [market(1), market(2), market(3)]
        self.client.get_price_history.return_value = [{"t": 100, "p": 0.5}]
        self.client.get_order_book.return_value = {"bids": [], "asks": []}
        self.database = Mock()
        self.database.upsert_market.return_value = 2
        self.database.save_price_history.return_value = 1

    def test_default_sync_fetches_history_for_every_market(self) -> None:
        result = Collector(self.client, self.database).sync(market_limit=3)
        self.assertEqual(self.client.get_price_history.call_count, 3)
        self.assertEqual(result.histories, 3)

    def test_positive_history_limit_is_respected(self) -> None:
        result = Collector(self.client, self.database).sync(
            market_limit=3, history_limit=1
        )
        self.assertEqual(self.client.get_price_history.call_count, 1)
        self.assertEqual(result.histories, 1)


if __name__ == "__main__":
    unittest.main()
