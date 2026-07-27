import unittest
from unittest.mock import MagicMock, Mock

from polymarket_app.client import ApiError
from polymarket_app.history import RANGES
from polymarket_app.search import MarketSearch


class MarketSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        # connect()を`with`で使うため、MagicMockでなければならない。
        self.database = MagicMock()
        self.client = Mock()
        self.search = MarketSearch(self.database, self.client)

    def test_local_results_prevent_external_request(self) -> None:
        local = [{"condition_id": "local"}]
        self.database.list_markets.return_value = local
        result = self.search.search("Iran")
        self.assertEqual(result.markets, local)
        self.assertEqual(result.source, "local")
        self.client.search_public_markets.assert_not_called()

    def test_empty_local_results_fall_back_and_persist_remote_markets(self) -> None:
        remote = [{"conditionId": "remote", "question": "Remote"}]
        saved = [{"condition_id": "remote", "question": "Remote"}]
        self.database.list_markets.return_value = []
        self.client.search_public_markets.return_value = remote
        self.database.list_markets_by_ids.return_value = saved
        result = self.search.search("Taylor Swift")
        self.database.upsert_market.assert_called_once_with(remote[0])
        self.database.list_markets_by_ids.assert_called_once_with(
            ["remote"], limit=100, sort="volume", min_liquidity=0.0
        )
        self.assertEqual(result.markets, saved)
        self.assertEqual(result.source, "polymarket")

    def test_listing_options_reach_every_query_path(self) -> None:
        """並べ替えと絞り込みは、ローカル一覧・ローカル検索・外部検索の
        いずれの経路でも同じように効かなければならない。"""
        options = {"sort": "ending_soon", "min_liquidity": 1000.0}

        self.database.list_markets.return_value = [{"condition_id": "x"}]
        self.search.search("", **options)
        self.database.list_markets.assert_called_once_with(
            limit=100, sort="ending_soon", min_liquidity=1000.0
        )

        self.database.list_markets.reset_mock()
        self.search.search("Iran", **options)
        self.database.list_markets.assert_called_once_with(
            search="Iran", limit=100, sort="ending_soon", min_liquidity=1000.0
        )

        self.database.list_markets.return_value = []
        self.client.search_public_markets.return_value = [{"conditionId": "remote"}]
        self.search.search("Taylor Swift", **options)
        self.database.list_markets_by_ids.assert_called_once_with(
            ["remote"], limit=100, sort="ending_soon", min_liquidity=1000.0
        )

    def test_history_write_and_fetch_record_share_one_transaction(self) -> None:
        """記録だけが残ると、空の期間を15分間「取得済み」にしてしまう。"""
        self.database.history_fetch_age_seconds.return_value = None
        self.client.get_price_history.return_value = [{"t": 1, "p": 0.5}]
        self.database.save_price_history.return_value = 1
        self.search.ensure_history("token", RANGES["1w"])
        self.database.connect.assert_called_once()

    def test_external_failure_is_returned_without_crashing_server(self) -> None:
        self.database.list_markets.return_value = []
        self.client.search_public_markets.side_effect = ApiError("offline")
        result = self.search.search("unknown")
        self.assertEqual(result.markets, [])
        self.assertEqual(result.source, "local")
        self.assertIn("offline", result.external_error)

    def test_unfetched_range_is_requested_with_its_own_resolution(self) -> None:
        self.database.history_fetch_age_seconds.return_value = None
        self.client.get_price_history.return_value = [{"t": 1, "p": 0.5}]
        self.database.save_price_history.return_value = 1
        self.search.ensure_history("token", RANGES["1d"])
        self.client.get_price_history.assert_called_once_with(
            "token", interval="1d", fidelity=5
        )
        self.database.record_history_fetch.assert_called_once_with("token", "1d", 1)
        self.client.get_order_book.assert_not_called()
        self.database.save_order_book.assert_not_called()

    def test_recently_fetched_range_is_served_from_the_database(self) -> None:
        self.database.history_fetch_age_seconds.return_value = 60.0
        self.search.ensure_history("token", RANGES["1w"], max_age_seconds=900.0)
        self.client.get_price_history.assert_not_called()

    def test_stale_range_is_fetched_again(self) -> None:
        self.database.history_fetch_age_seconds.return_value = 5000.0
        self.client.get_price_history.return_value = [{"t": 1, "p": 0.5}]
        self.database.save_price_history.return_value = 1
        self.search.ensure_history("token", RANGES["1w"], max_age_seconds=900.0)
        self.client.get_price_history.assert_called_once_with(
            "token", interval="1w", fidelity=60
        )

    def test_one_range_being_fresh_does_not_satisfy_another(self) -> None:
        """1週間分を取得済みでも、1日分の高解像度データは別途取得する。"""
        self.database.history_fetch_age_seconds.side_effect = (
            lambda token_id, range_key: 10.0 if range_key == "1w" else None
        )
        self.client.get_price_history.return_value = [{"t": 1, "p": 0.5}]
        self.database.save_price_history.return_value = 1
        self.search.ensure_history("token", RANGES["1d"])
        self.client.get_price_history.assert_called_once_with(
            "token", interval="1d", fidelity=5
        )


if __name__ == "__main__":
    unittest.main()
