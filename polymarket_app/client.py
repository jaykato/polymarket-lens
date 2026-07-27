from __future__ import annotations

import json
import logging
import random
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings

LOGGER = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """公開APIの取得に失敗した場合の例外。"""


class PolymarketClient:
    """認証不要・読み取り専用のPolymarket APIクライアント。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.settings.request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)

    def _get_json(
        self, base_url: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        query = urlencode(params or {})
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"

        error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            self._throttle()
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": self.settings.user_agent,
                },
                method="GET",
            )
            try:
                with urlopen(
                    request, timeout=self.settings.request_timeout_seconds
                ) as response:
                    payload = response.read().decode("utf-8")
                self._last_request_at = time.monotonic()
                return json.loads(payload)
            except HTTPError as exc:
                error = exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= self.settings.max_retries:
                    break
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                error = exc
                if attempt >= self.settings.max_retries:
                    break

            delay = min(8.0, (2**attempt) + random.uniform(0.0, 0.25))
            LOGGER.warning("API取得を再試行します: %s (%.2f秒後)", url, delay)
            time.sleep(delay)

        raise ApiError(f"API取得に失敗しました: {url}: {error}") from error

    def get_active_markets(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        payload = self._get_json(
            self.settings.gamma_base_url,
            "/markets",
            {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and "markets" in payload:
            return list(payload["markets"])
        if isinstance(payload, dict):
            return [payload]
        raise ApiError("Gamma APIのmarketsレスポンス形式が不明です")

    def search_public_markets(
        self, query: str, limit_per_type: int = 10
    ) -> list[dict[str, Any]]:
        """公開検索から取引中の市場を抽出する。

        public-searchはイベントを返し、その配下にmarketsが入る。検索結果には
        終了済みmarketが混ざる場合があるため、activeかつ未closedだけを返す。
        """
        payload = self._get_json(
            self.settings.gamma_base_url,
            "/public-search",
            {
                "q": query,
                "events_status": "active",
                "limit_per_type": limit_per_type,
                "page": 1,
                "keep_closed_markets": 0,
                "search_profiles": "false",
                "search_tags": "false",
            },
        )
        if not isinstance(payload, dict):
            raise ApiError("Gamma APIのpublic-searchレスポンス形式が不明です")

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                condition_id = market.get("conditionId")
                if (
                    not condition_id
                    or condition_id in seen
                    or not market.get("active")
                    or market.get("closed")
                ):
                    continue
                enriched = dict(market)
                enriched["events"] = [
                    {
                        "id": event.get("id"),
                        "title": event.get("title"),
                        "slug": event.get("slug"),
                    }
                ]
                seen.add(condition_id)
                results.append(enriched)
        return results

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        payload = self._get_json(
            self.settings.clob_base_url, "/book", {"token_id": token_id}
        )
        if not isinstance(payload, dict):
            raise ApiError("CLOB APIのbookレスポンス形式が不明です")
        return payload

    def get_price_history(
        self, token_id: str, interval: str = "1w", fidelity: int = 60
    ) -> list[dict[str, Any]]:
        payload = self._get_json(
            self.settings.clob_base_url,
            "/prices-history",
            {"market": token_id, "interval": interval, "fidelity": fidelity},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("history"), list):
            raise ApiError("CLOB APIのprices-historyレスポンス形式が不明です")
        return payload["history"]
