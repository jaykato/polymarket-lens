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

CREATE TABLE IF NOT EXISTS market_resolutions (
    condition_id TEXT PRIMARY KEY REFERENCES markets(condition_id) ON DELETE CASCADE,
    outcome_index INTEGER NOT NULL,
    resolved_at_utc TEXT,
    recorded_at_utc TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL REFERENCES markets(condition_id) ON DELETE CASCADE,
    token_id TEXT NOT NULL REFERENCES outcomes(token_id) ON DELETE CASCADE,
    side_index INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    predicted_probability TEXT NOT NULL,
    entry_cost TEXT NOT NULL,
    shares TEXT NOT NULL,
    stake TEXT NOT NULL,
    edge TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    settled_at_utc TEXT,
    payout TEXT,
    profit TEXT,
    UNIQUE(condition_id, token_id, model_name, observed_at_utc)
);

CREATE TABLE IF NOT EXISTS model_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL REFERENCES outcomes(token_id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    observed_at_utc TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    context_points INTEGER NOT NULL,
    prediction_length INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_markets_volume ON markets(volume);
CREATE INDEX IF NOT EXISTS idx_outcomes_condition ON outcomes(condition_id);
CREATE INDEX IF NOT EXISTS idx_price_history_token_time
    ON price_history(token_id, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_books_token_id ON order_book_snapshots(token_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_paper_positions_status ON paper_positions(status, condition_id);
CREATE INDEX IF NOT EXISTS idx_model_forecasts_token ON model_forecasts(token_id, id DESC);
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
    def __init__(self, path: Path, max_order_book_snapshots_per_token: int = 120) -> None:
        self.path = path
        self.max_order_book_snapshots_per_token = max(1, max_order_book_snapshots_per_token)
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
            resolution_index = self._resolved_outcome_index(market, prices)
            if resolution_index is not None:
                connection.execute(
                    """INSERT INTO market_resolutions
                       (condition_id, outcome_index, resolved_at_utc, recorded_at_utc, raw_json)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(condition_id) DO UPDATE SET
                         outcome_index=excluded.outcome_index,
                         resolved_at_utc=excluded.resolved_at_utc,
                         recorded_at_utc=excluded.recorded_at_utc,
                         raw_json=excluded.raw_json""",
                    (
                        condition_id,
                        resolution_index,
                        market.get("endDate"),
                        fetched_at,
                        json.dumps(market, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
        return len(token_ids)

    @staticmethod
    def _resolved_outcome_index(market: dict[str, Any], prices: list[Any]) -> int | None:
        """確定済みの二値市場だけを評価ラベルにする。

        closedだけではvoidや決済待ちも混ざる。結果価格が1/0に十分近い場合にだけ
        ラベルを作り、曖昧な市場をキャリブレーションへ混ぜない。
        """
        if not market.get("closed"):
            return None
        parsed = [decimal_text(price) for price in prices]
        for index, price in enumerate(parsed):
            if price is not None and Decimal(price) >= Decimal("0.999"):
                return index
        return None

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
            connection.execute(
                """DELETE FROM order_book_snapshots
                   WHERE token_id = ? AND id NOT IN (
                       SELECT id FROM order_book_snapshots
                       WHERE token_id = ? ORDER BY id DESC LIMIT ?
                   )""",
                (token_id, token_id, self.max_order_book_snapshots_per_token),
            )

    def resolved_observations(self, horizon_seconds: int) -> list[dict[str, Any]]:
        """決済の指定時間前に観測できたYES価格と正解を返す。"""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT m.condition_id, m.question, m.end_date_utc,
                          o.token_id, r.outcome_index,
                          h.timestamp_utc, h.price
                   FROM market_resolutions r
                   JOIN markets m ON m.condition_id = r.condition_id
                   JOIN outcomes o ON o.condition_id = m.condition_id
                                      AND o.outcome_index = 0
                   JOIN price_history h ON h.token_id = o.token_id
                   WHERE m.end_date_utc IS NOT NULL
                     AND h.timestamp_utc <= unixepoch(m.end_date_utc) - ?
                     AND h.timestamp_utc = (
                       SELECT MAX(h2.timestamp_utc)
                       FROM price_history h2
                       WHERE h2.token_id = o.token_id
                         AND h2.timestamp_utc <= unixepoch(m.end_date_utc) - ?
                     )
                   ORDER BY h.timestamp_utc""",
                (horizon_seconds, horizon_seconds),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_market_details(self) -> list[dict[str, Any]]:
        """仮想運用スキャン用のアクティブ市場。raw_jsonは返さない。"""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT condition_id FROM markets WHERE active = 1 AND closed = 0"
            ).fetchall()
        return [detail for row in rows if (detail := self.market_detail(row["condition_id"]))]

    def save_paper_position(self, position: dict[str, Any]) -> bool:
        """同時刻に同じ市場を二重記録しない仮想ポジションを保存する。"""
        values = {
            "condition_id": position["condition_id"],
            "token_id": position["token_id"],
            "side_index": int(position["side_index"]),
            "model_name": position["model_name"],
            "observed_at_utc": position.get("observed_at_utc", utc_now()),
            "predicted_probability": decimal_text(position["predicted_probability"]),
            "entry_cost": decimal_text(position["entry_cost"]),
            "shares": decimal_text(position["shares"]),
            "stake": decimal_text(position["stake"]),
            "edge": decimal_text(position["edge"]),
        }
        if any(value is None for value in values.values()):
            return False
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        with self.connect() as connection:
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO paper_positions ({columns}) VALUES ({placeholders})",
                values,
            )
        return cursor.rowcount == 1

    def settle_paper_positions(self) -> int:
        """保存済みの最終アウトカムで、openの仮想ポジションだけを精算する。"""
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE paper_positions
                   SET status = 'resolved', settled_at_utc = ?,
                       payout = CASE WHEN side_index = (
                           SELECT outcome_index FROM market_resolutions r
                           WHERE r.condition_id = paper_positions.condition_id
                       ) THEN shares ELSE '0' END,
                       profit = CASE WHEN side_index = (
                           SELECT outcome_index FROM market_resolutions r
                           WHERE r.condition_id = paper_positions.condition_id
                       ) THEN CAST(shares AS REAL) - CAST(stake AS REAL)
                            ELSE -CAST(stake AS REAL) END
                   WHERE status = 'open' AND EXISTS (
                       SELECT 1 FROM market_resolutions r
                       WHERE r.condition_id = paper_positions.condition_id
                   )""",
                (utc_now(),),
            )
        return cursor.rowcount

    def paper_position_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS positions,
                          SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open_positions,
                          SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) AS resolved_positions,
                          SUM(CASE WHEN status = 'resolved' THEN CAST(profit AS REAL) ELSE 0 END) AS profit
                   FROM paper_positions"""
            ).fetchone()
        return dict(row)

    def save_model_forecast(self, token_id: str, forecast: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO model_forecasts
                   (token_id, model_id, observed_at_utc, interval_seconds,
                    context_points, prediction_length, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    token_id,
                    str(forecast["model_id"]),
                    utc_now(),
                    int(forecast["interval_seconds"]),
                    int(forecast["context_points"]),
                    int(forecast["prediction_length"]),
                    json.dumps(forecast, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def event_groups(self) -> list[dict[str, Any]]:
        """同一event_idに2つ以上の市場があるグループを返す。"""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT event_id, COUNT(*) AS market_count
                   FROM markets WHERE event_id IS NOT NULL AND active = 1 AND closed = 0
                   GROUP BY event_id HAVING COUNT(*) >= 2"""
            ).fetchall()
        return [dict(row) for row in rows]

    def event_yes_markets(self, event_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT m.condition_id, m.question, m.fee_rate, o.token_id,
                          o.current_price, b.raw_json, b.fetched_at_utc
                   FROM markets m JOIN outcomes o
                     ON o.condition_id = m.condition_id AND o.outcome_index = 0
                   LEFT JOIN order_book_snapshots b ON b.id = (
                     SELECT id FROM order_book_snapshots b2
                     WHERE b2.token_id = o.token_id ORDER BY id DESC LIMIT 1
                   )
                   WHERE m.event_id = ? AND m.active = 1 AND m.closed = 0
                   ORDER BY m.question""",
                (event_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
                     (SELECT COUNT(*) FROM market_resolutions) AS resolutions,
                     (SELECT COUNT(*) FROM paper_positions) AS paper_positions,
                     (SELECT COUNT(*) FROM model_forecasts) AS model_forecasts,
                     (SELECT MAX(fetched_at_utc) FROM markets) AS last_sync"""
            ).fetchone()
            return dict(row)
