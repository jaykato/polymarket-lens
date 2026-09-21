from __future__ import annotations

import logging
from dataclasses import dataclass

from .client import ApiError, PolymarketClient
from .database import Database, parse_json_array

LOGGER = logging.getLogger(__name__)


@dataclass
class SyncResult:
    markets: int = 0
    outcomes: int = 0
    histories: int = 0
    price_points: int = 0
    order_books: int = 0
    errors: int = 0


class Collector:
    def __init__(self, client: PolymarketClient, database: Database) -> None:
        self.client = client
        self.database = database

    def sync(self, market_limit: int = 25, history_limit: int = 0) -> SyncResult:
        """市場と履歴を同期する。

        history_limitが0以下の場合、取得した全市場の履歴を同期する。
        正の値を指定した場合だけ、API呼び出し数を抑えるために件数を制限する。
        """
        result = SyncResult()
        markets = self.client.get_active_markets(limit=market_limit)
        for market in markets:
            result.outcomes += self.database.upsert_market(market)
            result.markets += 1

        history_count = 0
        for market in markets:
            if history_limit > 0 and history_count >= history_limit:
                break
            token_ids = parse_json_array(market.get("clobTokenIds"))
            if not token_ids or not market.get("enableOrderBook"):
                continue
            token_id = str(token_ids[0])
            try:
                history = self.client.get_price_history(token_id)
                result.price_points += self.database.save_price_history(token_id, history)
                result.histories += 1
                book = self.client.get_order_book(token_id)
                self.database.save_order_book(token_id, book)
                result.order_books += 1
                # NOはYESのビッド反転で近似せず、実トークンの板も保存する。
                if len(token_ids) > 1:
                    no_book = self.client.get_order_book(str(token_ids[1]))
                    self.database.save_order_book(str(token_ids[1]), no_book)
                    result.order_books += 1
            except ApiError as exc:
                LOGGER.warning("市場データの一部を取得できませんでした: %s", exc)
                result.errors += 1
            history_count += 1
        return result

    def sync_resolved(self, market_limit: int = 100) -> SyncResult:
        """解決済み市場と全期間価格履歴を取得し、検証用の土台を作る。"""
        result = SyncResult()
        markets = self.client.get_closed_markets(limit=market_limit)
        for market in markets:
            result.outcomes += self.database.upsert_market(market)
            result.markets += 1
            token_ids = parse_json_array(market.get("clobTokenIds"))
            if not token_ids:
                continue
            try:
                history = self.client.get_price_history(str(token_ids[0]), interval="max", fidelity=720)
                result.price_points += self.database.save_price_history(str(token_ids[0]), history)
                result.histories += 1
            except ApiError as exc:
                LOGGER.warning("解決済み市場の履歴を取得できませんでした: %s", exc)
                result.errors += 1
        return result
