from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from .client import ApiError, PolymarketClient
from .database import DEFAULT_SORT_KEY, Database
from .history import HistoryRange


@dataclass
class SearchResult:
    markets: list[dict[str, Any]]
    source: str
    external_error: str | None = None


class MarketSearch:
    """ローカルDBを優先し、0件の場合だけ公開検索へフォールバックする。"""

    def __init__(self, database: Database, client: PolymarketClient) -> None:
        self.database = database
        self.client = client
        self._remote_lock = threading.Lock()

    def search(
        self,
        query: str,
        limit: int = 100,
        sort: str = DEFAULT_SORT_KEY,
        min_liquidity: float = 0.0,
    ) -> SearchResult:
        listing = {
            "limit": limit,
            "sort": sort,
            "min_liquidity": min_liquidity,
        }
        normalized = query.strip()
        if not normalized:
            return SearchResult(self.database.list_markets(**listing), "local")

        local = self.database.list_markets(search=normalized, **listing)
        if local:
            return SearchResult(local, "local")

        try:
            # ThreadingHTTPServerで同時入力が来ても外部検索を直列化する。
            with self._remote_lock:
                remote = self.client.search_public_markets(normalized)
                condition_ids = []
                for market in remote:
                    self.database.upsert_market(market)
                    condition_ids.append(market["conditionId"])
            return SearchResult(
                self.database.list_markets_by_ids(condition_ids, **listing),
                "polymarket",
            )
        except ApiError as exc:
            return SearchResult([], "local", str(exc))

    def ensure_history(
        self,
        token_id: str,
        history_range: HistoryRange,
        max_age_seconds: float = 900.0,
    ) -> None:
        """表示しようとしている期間の履歴を必要なだけ補完する。

        期間ごとに解像度が異なるため、取得済みかどうかも期間ごとに判定する。
        取得から`max_age_seconds`が過ぎていれば取り直し、そうでなければ
        保存済みデータをそのまま使う。チャート応答を遅らせないため、注文板は
        ここでは取得しない。
        """
        if self._is_fresh(token_id, history_range, max_age_seconds):
            return
        with self._remote_lock:
            # ロック待ちの間に他のリクエストが取得済みの場合がある。
            if self._is_fresh(token_id, history_range, max_age_seconds):
                return
            history = self.client.get_price_history(
                token_id,
                interval=history_range.interval,
                fidelity=history_range.fidelity,
            )
            # 保存と取得記録を同じ取引にまとめる。記録だけが残ると、実際には
            # 空のままの期間を15分間「取得済み」として扱ってしまう。
            with self.database.connect():
                saved = self.database.save_price_history(token_id, history)
                self.database.record_history_fetch(token_id, history_range.key, saved)

    def _is_fresh(
        self, token_id: str, history_range: HistoryRange, max_age_seconds: float
    ) -> bool:
        age = self.database.history_fetch_age_seconds(token_id, history_range.key)
        return age is not None and age < max_age_seconds
