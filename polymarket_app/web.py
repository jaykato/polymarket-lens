from __future__ import annotations

import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .analysis import analyze_market
from .book import load_book
from .client import ApiError, PolymarketClient
from .database import Database, sort_options
from .history import range_options, resolve_range
from .movement import summarize_movement
from .returns import DEFAULT_STAKE, quote_position
from .search import MarketSearch


class AppServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        database: Database,
        client: PolymarketClient,
        static_dir: Path,
    ):
        self.database = database
        self.search = MarketSearch(database, client)
        self.static_dir = static_dir.resolve()
        self.guide_path = self.static_dir.parent.parent / "docs" / "how_to_start.html"
        super().__init__(address, AppRequestHandler)


class AppRequestHandler(BaseHTTPRequestHandler):
    server: AppServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _listing(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """一覧の並べ替えと絞り込みをクエリ文字列から取り出す。

        `sort`はSQLへ直接入れず、`resolve_sort`が既知のキーだけを通す。
        `min_liquidity`は数値に変換できないものを0（絞り込みなし）にする。
        """
        try:
            min_liquidity = max(0.0, float(query.get("min_liquidity", ["0"])[0]))
        except ValueError:
            min_liquidity = 0.0
        return {"sort": query.get("sort", [""])[0], "min_liquidity": min_liquidity}

    def _returns(
        self, market: dict[str, Any], query: dict[str, list[str]]
    ) -> dict[str, Any]:
        """L0の実効リターンを組み立てる。

        YES/NOそれぞれの実トークン板を優先する。旧データなどでNO板が無い場合だけ、
        YESのビッド反転をフォールバックとして使う。

        同期が板を保存していない市場のほうが多い（検索から入った市場や、
        同期の件数上限より後ろの市場）。ここで表示のたびに補う。取得に失敗しても
        保存済みの板があればそれで計算する。古い板でも、何も出せないよりは良い。
        """
        outcomes = market.get("outcomes") or []
        side = "no" if query.get("side", ["yes"])[0].lower() == "no" else "yes"
        index = 1 if side == "no" else 0
        token_id = outcomes[index].get("token_id") if len(outcomes) > index else None
        snapshot = None
        fetch_failed = False
        if token_id:
            try:
                self.server.search.ensure_order_book(str(token_id))
            except ApiError:
                fetch_failed = True
            snapshot = self.server.database.latest_order_book(str(token_id))
        # 既存DBにはYES板だけがある。同期後に実NO板が入るまでの互換処理。
        book_side = "yes"
        if snapshot is None and side == "no" and outcomes:
            yes_token_id = outcomes[0].get("token_id")
            if yes_token_id:
                snapshot = self.server.database.latest_order_book(str(yes_token_id))
                if snapshot is not None:
                    token_id = yes_token_id
                    book_side = "no"
        if snapshot is None:
            return {
                "available": False,
                "reason": "book_fetch_failed" if fetch_failed else "no_snapshot",
                "detail": (
                    "Polymarket did not return an order book for this market just now, "
                    "and none is stored."
                    if fetch_failed
                    else "No order book has been captured for this market yet. Run a sync."
                ),
                "side": side,
                "stake": float(DEFAULT_STAKE),
            }
        quote = quote_position(
            market,
            load_book(snapshot.get("raw_json")),
            side=side,
            stake=query.get("stake", [str(DEFAULT_STAKE)])[0],
            book_side=book_side,
        )
        quote["captured_at_utc"] = snapshot.get("fetched_at_utc")
        quote["book_age_seconds"] = self.server.database.order_book_age_seconds(str(token_id))
        return quote

    def _static(self, relative_path: str) -> None:
        target = (self.server.static_dir / relative_path).resolve()
        if self.server.static_dir not in target.parents and target != self.server.static_dir:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _guide(self) -> None:
        """同梱の利用ガイドだけを、UIとは別の読み取り専用ページとして公開する。"""
        target = self.server.guide_path
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/how_to_start.html":
            self._guide()
            return

        if path == "/api/stats":
            self._json(self.server.database.stats())
            return
        if path == "/api/options":
            self._json(
                {
                    "version": __version__,
                    "sorts": sort_options(),
                    "ranges": range_options(),
                }
            )
            return
        if path == "/api/markets":
            self._json(
                self.server.database.list_markets(
                    search=query.get("q", [""])[0], **self._listing(query)
                )
            )
            return
        if path == "/api/search":
            result = self.server.search.search(
                query.get("q", [""])[0], **self._listing(query)
            )
            self._json(
                {
                    "markets": result.markets,
                    "source": result.source,
                    "external_error": result.external_error,
                }
            )
            return
        # 投資額を変えるたびに再計算するため、リターンだけを返す口を分けておく。
        # 計算式をブラウザ側へ複製すると、サーバと食い違ったときに気づけない。
        if path.startswith("/api/returns/"):
            condition_id = path.removeprefix("/api/returns/")
            market = self.server.database.market_detail(condition_id)
            if market is None:
                self._json({"error": "market not found"}, HTTPStatus.NOT_FOUND)
            else:
                self._json(self._returns(market, query))
            return
        if path.startswith("/api/markets/"):
            condition_id = path.removeprefix("/api/markets/")
            market = self.server.database.market_detail(condition_id)
            if market is None:
                self._json({"error": "market not found"}, HTTPStatus.NOT_FOUND)
            else:
                analysis = analyze_market(market)
                analysis.pop("internal_scores", None)
                market["analysis"] = analysis
                market["returns"] = self._returns(market, query)
                self._json(market)
            return
        if path.startswith("/api/history/"):
            token_id = path.removeprefix("/api/history/")
            history_range = resolve_range(query.get("range", [""])[0])
            try:
                self.server.search.ensure_history(token_id, history_range)
            except ApiError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
                return
            since = None
            if history_range.window_seconds is not None:
                since = int(time.time()) - history_range.window_seconds
            points = self.server.database.token_history(
                token_id, since_timestamp=since
            )
            self._json(
                {
                    "range": history_range.key,
                    "points": points,
                    "movement": summarize_movement(points),
                }
            )
            return

        static_path = "index.html" if path == "/" else path.lstrip("/")
        self._static(static_path)


def serve(
    database: Database,
    client: PolymarketClient,
    host: str,
    port: int,
    static_dir: Path,
) -> None:
    server = AppServer((host, port), database, client, static_dir)
    print(f"Polymarket Lens: http://{host}:{port}")
    print("終了するには Ctrl+C を押してください。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
