from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    gamma_id TEXT,
    event_id TEXT,
    question TEXT NOT NULL,
    slug TEXT,
    description TEXT,
    end_date_utc TEXT,
    active INTEGER NOT NULL,
    closed INTEGER NOT NULL,
    restricted INTEGER NOT NULL,
    accepting_orders INTEGER NOT NULL,
    neg_risk INTEGER NOT NULL,
    volume TEXT,
    liquidity TEXT,
    best_bid TEXT,
    best_ask TEXT,
    spread TEXT,
    last_trade_price TEXT,
    fees_enabled INTEGER NOT NULL DEFAULT 0,
    -- 手数料はカテゴリごとに違う（実測0.03〜0.07、無料の市場もある）。
    -- feeSchedule.rateを使う。takerBaseFee/makerBaseFeeは全市場1000固定の
    -- レガシー項目で実際の料率と矛盾するため、参照してはいけない。
    fee_rate TEXT,
    fee_type TEXT,
    updated_at_utc TEXT,
    fetched_at_utc TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    token_id TEXT PRIMARY KEY,
    condition_id TEXT NOT NULL REFERENCES markets(condition_id) ON DELETE CASCADE,
    outcome_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    current_price TEXT,
    UNIQUE(condition_id, outcome_index)
);

CREATE TABLE IF NOT EXISTS price_history (
    token_id TEXT NOT NULL REFERENCES outcomes(token_id) ON DELETE CASCADE,
    timestamp_utc INTEGER NOT NULL,
    price TEXT NOT NULL,
    fetched_at_utc TEXT NOT NULL,
    PRIMARY KEY(token_id, timestamp_utc)
);

CREATE TABLE IF NOT EXISTS order_book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL REFERENCES outcomes(token_id) ON DELETE CASCADE,
    exchange_timestamp_ms INTEGER,
    best_bid TEXT,
    best_ask TEXT,
    spread TEXT,
    fetched_at_utc TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history_fetches (
    token_id TEXT NOT NULL REFERENCES outcomes(token_id) ON DELETE CASCADE,
    range_key TEXT NOT NULL,
    point_count INTEGER NOT NULL DEFAULT 0,
    fetched_at_utc TEXT NOT NULL,
    PRIMARY KEY(token_id, range_key)
);

CREATE INDEX IF NOT EXISTS idx_markets_volume ON markets(volume);
CREATE INDEX IF NOT EXISTS idx_outcomes_condition ON outcomes(condition_id);
CREATE INDEX IF NOT EXISTS idx_price_history_token_time
    ON price_history(token_id, timestamp_utc);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def decimal_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return format(Decimal(str(value)), "f")
    except InvalidOperation:
        return None


def age_seconds(value: Any) -> float | None:
    """保存時刻のISO文字列から、いまnまでの経過秒を出す。読めなければNone。

    タイムゾーンの無い値はUTCとみなす。ローカル時刻として解釈すると、
    時差ぶんだけ「新しい」ことになり、取り直しが止まる。
    """
    if not value:
        return None
    try:
        fetched_at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - fetched_at).total_seconds()


def parse_json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    return []


# 最良気配での摩擦。板を歩かないので、保存済みのbest_askだけで出せる。
#
#     entry_cost = best_ask + rate × best_ask × (1 − best_ask) − quoted
#
# best_askが0以下または1以上のときはNULLにする。1.00払って1.00受け取る建玉は
# 利益が定義できず、差だけ見ると「最も安い」と誤解されて先頭に並んでしまう。
ENTRY_COST_SQL = """
        CASE
          WHEN m.best_ask IS NULL OR o.current_price IS NULL THEN NULL
          WHEN CAST(m.best_ask AS REAL) <= 0 OR CAST(m.best_ask AS REAL) >= 1 THEN NULL
          ELSE CAST(m.best_ask AS REAL)
               + COALESCE(CAST(m.fee_rate AS REAL), 0)
                 * CAST(m.best_ask AS REAL) * (1 - CAST(m.best_ask AS REAL))
               - CAST(o.current_price AS REAL)
        END"""

# 摩擦を表示価格に対する割合で見たもの。並べ替えはこちらを使う。
#
# ポイント差だけで並べると、極端な大穴が上位を独占する。ask 0.001 の市場は
# 摩擦が+0.05ptしかないが、表示価格に対しては100%の上乗せで、実際には
# 最も割高な部類にあたる。割合で見ればこれが正しく下位へ落ちる。
ENTRY_COST_RATIO_SQL = f"""
        CASE
          WHEN CAST(o.current_price AS REAL) IS NULL
            OR CAST(o.current_price AS REAL) <= 0 THEN NULL
          ELSE ({ENTRY_COST_SQL}) / CAST(o.current_price AS REAL)
        END"""

# UIの並べ替え。キーはSQLへ直接入れず、必ずこの表を通して解決する。
MARKET_SORTS: dict[str, tuple[str, str]] = {
    "volume": ("Volume", "CAST(m.volume AS REAL) DESC"),
    "liquidity": ("Liquidity", "CAST(m.liquidity AS REAL) DESC"),
    "ending_soon": ("Ending soon", "m.end_date_utc IS NULL, m.end_date_utc ASC"),
    "spread": ("Tightest price gap", "m.spread IS NULL, CAST(m.spread AS REAL) ASC"),
    # 算出できない市場は末尾へ送る。先頭に来ると「摩擦ゼロ」に見える。
    "entry_cost": (
        "Lowest entry cost",
        "entry_cost_ratio IS NULL, entry_cost_ratio ASC",
    ),
}

DEFAULT_SORT_KEY = "volume"


def resolve_sort(key: str | None) -> str:
    """未知の値は既定の並びへ丸める。クエリ文字列をSQLへ通さないための境界。"""
    entry = MARKET_SORTS.get((key or "").strip().lower())
    return (entry or MARKET_SORTS[DEFAULT_SORT_KEY])[1]


def sort_options() -> list[dict[str, str]]:
    return [{"key": key, "label": label} for key, (label, _) in MARKET_SORTS.items()]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """接続を開く。入れ子で呼ばれた場合は外側の接続と取引を共有する。

        ThreadingHTTPServerはリクエストごとにスレッドを作るため、接続を
        スレッドローカルへ貯め込むと解放されない。ここでは最も外側の
        `with`を抜けた時点で必ず閉じ、その内側でだけ再利用する。
        """
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            yield existing
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        self._local.connection = connection
        try:
            yield connection
            connection.commit()
        finally:
            self._local.connection = None
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """既存DBへ後から入った列を足す。

        スキーマは`CREATE TABLE IF NOT EXISTS`なので、テーブルが既にある
        データベースには新しい列が入らない。取りこぼすと手数料が常に0として
        扱われ、損益分岐が過小に出る。
        """
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(markets)")
        }
        for column in ("fee_rate", "fee_type"):
            if column not in existing:
                connection.execute(f"ALTER TABLE markets ADD COLUMN {column} TEXT")

    def upsert_market(self, market: dict[str, Any]) -> int:
        condition_id = market.get("conditionId") or market.get("condition_id")
        if not condition_id:
            return 0

        events = market.get("events") or []
        event_id = str(events[0].get("id")) if events and events[0].get("id") else None
        fetched_at = utc_now()
        # feesEnabledがfalseの市場にはfeeScheduleごと無い（実測100件中15件、
        # いずれも地政学系）。その場合の料率は0で、手数料項は消える。
        fee_schedule = market.get("feeSchedule")
        fee_rate = (
            decimal_text(fee_schedule.get("rate"))
            if isinstance(fee_schedule, dict) and market.get("feesEnabled")
            else decimal_text(0)
        )
        values = {
            "condition_id": condition_id,
            "gamma_id": str(market.get("id")) if market.get("id") is not None else None,
            "event_id": event_id,
            "question": market.get("question") or "(untitled)",
            "slug": market.get("slug"),
            "description": market.get("description"),
            "end_date_utc": market.get("endDate"),
            "active": int(bool(market.get("active"))),
            "closed": int(bool(market.get("closed"))),
            "restricted": int(bool(market.get("restricted"))),
            "accepting_orders": int(bool(market.get("acceptingOrders"))),
            "neg_risk": int(bool(market.get("negRisk"))),
            "volume": decimal_text(market.get("volume")),
            "liquidity": decimal_text(market.get("liquidity")),
            "best_bid": decimal_text(market.get("bestBid")),
            "best_ask": decimal_text(market.get("bestAsk")),
            "spread": decimal_text(market.get("spread")),
            "last_trade_price": decimal_text(market.get("lastTradePrice")),
            "fees_enabled": int(bool(market.get("feesEnabled"))),
            "fee_rate": fee_rate,
            "fee_type": market.get("feeType"),
            "updated_at_utc": market.get("updatedAt"),
            "fetched_at_utc": fetched_at,
            "raw_json": json.dumps(market, ensure_ascii=False, separators=(",", ":")),
        }
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        updates = ", ".join(
            f"{name}=excluded.{name}" for name in values if name != "condition_id"
        )

        outcomes = parse_json_array(market.get("outcomes"))
        prices = parse_json_array(market.get("outcomePrices"))
        token_ids = parse_json_array(market.get("clobTokenIds"))

        with self.connect() as connection:
            connection.execute(
                f"""INSERT INTO markets ({columns}) VALUES ({placeholders})
                    ON CONFLICT(condition_id) DO UPDATE SET {updates}""",
                values,
            )
            for index, token_id in enumerate(token_ids):
                name = str(outcomes[index]) if index < len(outcomes) else f"Outcome {index}"
                price = decimal_text(prices[index]) if index < len(prices) else None
                connection.execute(
                    """INSERT INTO outcomes
                       (token_id, condition_id, outcome_index, name, current_price)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(token_id) DO UPDATE SET
                         condition_id=excluded.condition_id,
                         outcome_index=excluded.outcome_index,
                         name=excluded.name,
                         current_price=excluded.current_price""",
                    (str(token_id), condition_id, index, name, price),
                )
        return len(token_ids)

    def save_price_history(self, token_id: str, history: list[dict[str, Any]]) -> int:
        fetched_at = utc_now()
        rows = []
        for point in history:
            timestamp = point.get("t")
            price = decimal_text(point.get("p"))
            if timestamp is not None and price is not None:
                rows.append((token_id, int(timestamp), price, fetched_at))
        with self.connect() as connection:
            connection.executemany(
                """INSERT INTO price_history
                   (token_id, timestamp_utc, price, fetched_at_utc)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(token_id, timestamp_utc) DO UPDATE SET
                     price=excluded.price,
                     fetched_at_utc=excluded.fetched_at_utc""",
                rows,
            )
        return len(rows)

    def save_order_book(self, token_id: str, book: dict[str, Any]) -> None:
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = max((Decimal(str(row["price"])) for row in bids), default=None)
        best_ask = min((Decimal(str(row["price"])) for row in asks), default=None)
        spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO order_book_snapshots
                   (token_id, exchange_timestamp_ms, best_bid, best_ask, spread,
                    fetched_at_utc, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    token_id,
                    int(book["timestamp"]) if book.get("timestamp") else None,
                    decimal_text(best_bid),
                    decimal_text(best_ask),
                    decimal_text(spread),
                    utc_now(),
                    json.dumps(book, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def list_markets(
        self,
        limit: int = 100,
        search: str = "",
        sort: str = DEFAULT_SORT_KEY,
        min_liquidity: float = 0.0,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if search:
            # 質問文だけだと、本文やURL断片にしか語が出ない市場を取り逃がす。
            conditions.append(
                "(m.question LIKE ? OR m.slug LIKE ? OR m.description LIKE ?)"
            )
            params.extend([f"%{search}%"] * 3)
        self._add_liquidity_floor(conditions, params, min_liquidity)
        return self._list_markets(conditions, params, sort, limit)

    def list_markets_by_ids(
        self,
        condition_ids: list[str],
        limit: int = 100,
        sort: str = DEFAULT_SORT_KEY,
        min_liquidity: float = 0.0,
    ) -> list[dict[str, Any]]:
        if not condition_ids:
            return []
        placeholders = ", ".join("?" for _ in condition_ids)
        conditions = [f"m.condition_id IN ({placeholders})"]
        params: list[Any] = list(condition_ids)
        self._add_liquidity_floor(conditions, params, min_liquidity)
        return self._list_markets(conditions, params, sort, limit)

    @staticmethod
    def _add_liquidity_floor(
        conditions: list[str], params: list[Any], min_liquidity: float
    ) -> None:
        """下限が0のときは条件を足さない。

        `NULL >= 0`はSQLではNULL（偽扱い）になるため、無条件で比較を足すと
        流動性が未取得の市場が黙って消える。
        """
        if min_liquidity <= 0:
            return
        conditions.append("CAST(m.liquidity AS REAL) >= ?")
        params.append(min_liquidity)

    def _list_markets(
        self,
        conditions: list[str],
        params: list[Any],
        sort: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        query = f"""
            SELECT m.condition_id, m.question, m.slug, m.end_date_utc, m.volume,
                   m.liquidity, m.best_bid, m.best_ask, m.spread,
                   m.last_trade_price, m.restricted, m.fees_enabled,
                   m.fee_rate, m.fee_type,
                   o.token_id, o.name AS outcome_name, o.current_price,
                   {ENTRY_COST_SQL} AS entry_cost,
                   {ENTRY_COST_RATIO_SQL} AS entry_cost_ratio
            FROM markets m
            LEFT JOIN outcomes o
              ON o.condition_id = m.condition_id AND o.outcome_index = 0
        """
        if conditions:
            query += f" WHERE {' AND '.join(conditions)}"
        query += f" ORDER BY {resolve_sort(sort)} LIMIT ?"
        with self.connect() as connection:
            rows = connection.execute(query, [*params, limit]).fetchall()
        return [dict(row) for row in rows]

    def market_detail(self, condition_id: str) -> dict[str, Any] | None:
        # raw_jsonは意図的に読まない。APIレスポンス原文は検証用にDBへ残すが、
        # ブラウザへ送っても使い道がなく、転送量だけが増える。
        with self.connect() as connection:
            market = connection.execute(
                """SELECT condition_id, gamma_id, event_id, question, slug,
                          description, end_date_utc, active, closed, restricted,
                          accepting_orders, neg_risk, volume, liquidity,
                          best_bid, best_ask, spread, last_trade_price,
                          fees_enabled, fee_rate, fee_type,
                          updated_at_utc, fetched_at_utc
                   FROM markets WHERE condition_id = ?""",
                (condition_id,),
            ).fetchone()
            if market is None:
                return None
            result = dict(market)
            result["outcomes"] = [
                dict(row)
                for row in connection.execute(
                    """SELECT token_id, outcome_index, name, current_price
                       FROM outcomes WHERE condition_id = ?
                       ORDER BY outcome_index""",
                    (condition_id,),
                ).fetchall()
            ]
            return result

    def latest_order_book(self, token_id: str) -> dict[str, Any] | None:
        """直近の板スナップショットを返す。

        raw_jsonをそのまま渡す。ここだけは原文が必要で、段ごとの価格と数量が
        入っているのはraw_jsonの中だけ。呼び出し側でbook.load_bookに通す。
        """
        with self.connect() as connection:
            row = connection.execute(
                """SELECT token_id, exchange_timestamp_ms, best_bid, best_ask,
                          spread, fetched_at_utc, raw_json
                   FROM order_book_snapshots
                   WHERE token_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (token_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def token_history(
        self,
        token_id: str,
        since_timestamp: int | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """表示期間内の価格系列を古い順で返す。

        上限に達した場合は古い側ではなく新しい側を残す。チャートの右端が
        欠けると現在値が読めなくなるため。
        """
        query = "SELECT timestamp_utc, price FROM price_history WHERE token_id = ?"
        params: list[Any] = [token_id]
        if since_timestamp is not None:
            query += " AND timestamp_utc >= ?"
            params.append(int(since_timestamp))
        query += " ORDER BY timestamp_utc DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def token_history_count(self, token_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM price_history WHERE token_id = ?", (token_id,)
            ).fetchone()
            return int(row[0])

    def record_history_fetch(
        self, token_id: str, range_key: str, point_count: int
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO history_fetches
                   (token_id, range_key, point_count, fetched_at_utc)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(token_id, range_key) DO UPDATE SET
                     point_count=excluded.point_count,
                     fetched_at_utc=excluded.fetched_at_utc""",
                (token_id, range_key, point_count, utc_now()),
            )

    def history_fetch_age_seconds(self, token_id: str, range_key: str) -> float | None:
        """この期間を最後に取得してからの経過秒。未取得ならNone。"""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT fetched_at_utc FROM history_fetches
                   WHERE token_id = ? AND range_key = ?""",
                (token_id, range_key),
            ).fetchone()
        return age_seconds(row["fetched_at_utc"]) if row is not None else None

    def order_book_age_seconds(self, token_id: str) -> float | None:
        """直近の板を取得してからの経過秒。一度も保存していなければNone。

        raw_jsonは大きい。鮮度の判定だけのために毎回読み出さない。
        """
        with self.connect() as connection:
            row = connection.execute(
                """SELECT fetched_at_utc FROM order_book_snapshots
                   WHERE token_id = ? ORDER BY id DESC LIMIT 1""",
                (token_id,),
            ).fetchone()
        return age_seconds(row["fetched_at_utc"]) if row is not None else None

    def stats(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM markets) AS markets,
                     (SELECT COUNT(*) FROM outcomes) AS outcomes,
                     (SELECT COUNT(*) FROM price_history) AS price_points,
                     (SELECT MAX(fetched_at_utc) FROM markets) AS last_sync"""
            ).fetchone()
            return dict(row)
