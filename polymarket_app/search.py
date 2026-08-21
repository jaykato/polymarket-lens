from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from .client import ApiError, PolymarketClient
from .database import DEFAULT_SORT_KEY, Database
from .history import HistoryRange

# 板を取り直すまでの猶予。実効リターンの入力そのものなので履歴より短くする。
# 投資額スライダーは動かすたびに/api/returnsを叩くため、0にはできない。
BOOK_MAX_AGE_SECONDS = 60.0


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
        # 板の取得に失敗したトークンと、その時刻（monotonic）・理由。
        self._book_failures: dict[str, tuple[float, str]] = {}

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

    def ensure_order_book(
        self, token_id: str, max_age_seconds: float = BOOK_MAX_AGE_SECONDS
    ) -> None:
        """リターン計算に使う板を、必要なだけ取り直す。

        同期は取得した市場ぶんの板しか保存しない。検索から入った市場や、
        `--markets`の上限より後ろの市場は板を持たないまま一覧に載るため、
        詳細を開いても「板が無い」としか出せない。履歴と同じく、表示のたびに
        その市場ぶんだけ補う。

        取得できなかった場合は理由を覚えたうえで例外を投げる。猶予のあいだは
        呼び出さずに同じ例外を投げ直す。板を持たない市場で投資額を動かされる
        たびに、失敗するAPI呼び出しを繰り返さないため。理由を捨てて黙って
        戻ると、呼び出し側からは「一度も試していない」場合と区別できず、
        画面の文言がスライダーを動かすたびに入れ替わる。
        """
        if self._book_is_fresh(token_id, max_age_seconds):
            return
        self._raise_recent_failure(token_id, max_age_seconds)
        with self._remote_lock:
            # ロック待ちの間に他のリクエストが取得済み、または失敗している。
            if self._book_is_fresh(token_id, max_age_seconds):
                return
            self._raise_recent_failure(token_id, max_age_seconds)
            try:
                book = self.client.get_order_book(token_id)
            except ApiError as exc:
                self._book_failures[token_id] = (time.monotonic(), str(exc))
                raise
            self._book_failures.pop(token_id, None)
            self.database.save_order_book(token_id, book)

    def _book_is_fresh(self, token_id: str, max_age_seconds: float) -> bool:
        age = self.database.order_book_age_seconds(token_id)
        return age is not None and age < max_age_seconds

    def _raise_recent_failure(self, token_id: str, max_age_seconds: float) -> None:
        failure = self._book_failures.get(token_id)
        if failure is None:
            return
        failed_at, reason = failure
        if time.monotonic() - failed_at < max_age_seconds:
            raise ApiError(reason)
        self._book_failures.pop(token_id, None)

    def _is_fresh(
        self, token_id: str, history_range: HistoryRange, max_age_seconds: float
    ) -> bool:
        age = self.database.history_fetch_age_seconds(token_id, history_range.key)
        return age is not None and age < max_age_seconds
