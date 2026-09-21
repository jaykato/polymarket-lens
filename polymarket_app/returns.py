"""建玉を持ったときの実効リターン。

表示されている確率と、実際に必要な確率の差を出す。板・手数料表・残存日数から
一意に決まる算術だけを扱い、結果がどちらになるかは推定しない。

二値市場では1株の払戻が1.00ドルなので、

    損益分岐確率 = 支払総額 ÷ 取得株数

がそのまま「勝たなければ損をする確率」になる。表示価格との差が、
スプレッドと手数料として実際に払っている隠れコストにあたる。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .book import Level, ask_levels, fee_per_share, walk

ONE = Decimal("1")
DAY_SECONDS = 24 * 60 * 60

# 表示用の既定額。UIのスライダー下限・上限と合わせる。
DEFAULT_STAKE = Decimal("100")
MIN_STAKE = Decimal("1")
MAX_STAKE = Decimal("1000000")


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def clamp_stake(value: Any) -> Decimal:
    """投資額を妥当な範囲へ収める。読めない値は既定額にする。

    推測して勝手な額を入れない。範囲外は境界へ寄せる。
    """
    amount = _decimal(value)
    if amount is None or not amount.is_finite():
        return DEFAULT_STAKE
    return max(MIN_STAKE, min(MAX_STAKE, amount))


def entry_cost(
    best_ask: Any, quoted: Any, fee_rate: Any
) -> Decimal | None:
    """最良気配での摩擦（トップオブブック）。

        entry_cost = best_ask + fee(best_ask) − quoted

    板を歩かないので、保存済みのbest_askだけで出せる。一覧の全市場に
    出す値はこれ。実際の約定はサイズ次第でこれより悪くなる。

    best_askが1.00以上のときはNoneを返す。1.00払って1.00受け取る建玉は
    利益が定義できず、差だけ見ると「安い」と誤解される。
    """
    ask = _decimal(best_ask)
    price = _decimal(quoted)
    rate = _decimal(fee_rate) or Decimal("0")
    if ask is None or price is None:
        return None
    if ask <= 0 or ask >= ONE:
        return None
    return ask + fee_per_share(ask, rate) - price


def days_to_resolution(end_date_utc: Any, now: datetime | None = None) -> int | None:
    """残存日数。終了日が読めない、または過ぎている場合はNone。

    終了日を過ぎた市場でcloseフラグが立っていないことがある。実測で100件中
    7件あり、最長で113日前だった。ここで弾かないと年率換算が
    「113日ぶんの損益を1日に圧縮した値」になる。
    """
    if not end_date_utc:
        return None
    try:
        parsed = datetime.fromisoformat(str(end_date_utc).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    seconds = (parsed - (now or datetime.now(timezone.utc))).total_seconds()
    if seconds <= 0:
        return None
    return max(1, round(seconds / DAY_SECONDS))


def _quoted_price(market: dict[str, Any], side: str) -> Decimal | None:
    """表示されている確率。outcomesの並びはGamma APIの順序に従う。"""
    outcomes = market.get("outcomes") or []
    index = 0 if side == "yes" else 1
    if index < len(outcomes):
        price = _decimal(outcomes[index].get("current_price"))
        if price is not None:
            return price
    # 片側しか無い場合は反対側から補う。YES+NO=1は実測100件すべてで成立した。
    other = 1 - index
    if other < len(outcomes):
        price = _decimal(outcomes[other].get("current_price"))
        if price is not None:
            return ONE - price
    return None


def _unavailable(reason: str, detail: str, side: str, stake: Decimal) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "detail": detail,
        "side": side,
        "stake": float(stake),
    }


def quote_position(
    market: dict[str, Any],
    book: dict[str, list[Level]],
    side: str = "yes",
    stake: Any = DEFAULT_STAKE,
    now: datetime | None = None,
    book_side: str | None = None,
) -> dict[str, Any]:
    """指定サイド・指定金額で買った場合の実効リターンを組み立てる。

    数値は表示用に丸めて返す。内部計算はDecimalのまま行い、丸めは最後だけ。
    """
    side = "no" if str(side).lower() == "no" else "yes"
    amount = clamp_stake(stake)
    rate = _decimal(market.get("fee_rate")) or Decimal("0")
    # NOトークン自身の板を渡された場合は、YESと同じくasksを食う。
    # 実板が無い旧データだけはbook_side="no"でYES板のbidsを反転する。
    levels = ask_levels(book, book_side or side)

    if not levels:
        return _unavailable(
            "no_book",
            "No resting offers are stored for this side, so nothing can be filled.",
            side,
            amount,
        )

    best_ask = levels[0].price
    if best_ask >= ONE:
        return _unavailable(
            "no_upside",
            "The cheapest offer is 1.00, so the position cannot return more than it costs.",
            side,
            amount,
        )

    fill = walk(levels, amount, rate)
    if fill.shares <= 0:
        return _unavailable(
            "no_fill",
            "The stake is too small to buy any shares at the current offers.",
            side,
            amount,
        )

    # 1株の払戻は1.00ドル。支払総額を株数で割ると、そのまま損益分岐確率になる。
    breakeven = fill.spent / fill.shares
    average_fill = fill.notional / fill.shares
    average_fee = fill.fees / fill.shares
    payout = fill.shares
    profit = payout - fill.spent
    win_ratio = profit / fill.spent if fill.spent > 0 else Decimal("0")

    quoted = _quoted_price(market, side)
    gap = breakeven - quoted if quoted is not None else None

    days = days_to_resolution(market.get("end_date_utc"), now)
    # 単利換算。全損しうる二値の賭けを複利で年率化すると桁が壊れる。
    annualised = win_ratio * Decimal(365) / Decimal(days) if days else None

    return {
        "available": True,
        "side": side,
        "stake": float(fill.spent),
        "requested_stake": float(amount),
        "unfilled": float(fill.unfilled),
        "book_exhausted": fill.unfilled > 0,
        "shares": float(fill.shares),
        "breakeven": float(breakeven),
        "quoted": float(quoted) if quoted is not None else None,
        "gap_points": float(gap * 100) if gap is not None else None,
        "fee_rate": float(rate),
        "fee_type": market.get("fee_type"),
        "breakdown": {
            "quoted": float(quoted) if quoted is not None else None,
            "best_ask": float(best_ask),
            "average_fill": float(average_fill),
            "average_fee": float(average_fee),
            "effective_cost": float(breakeven),
        },
        "win": {"payout": float(payout), "profit": float(profit), "percent": float(win_ratio * 100)},
        "lose": {"payout": 0.0, "profit": float(-fill.spent), "percent": -100.0},
        "days_to_resolution": days,
        "annualised_percent": float(annualised * 100) if annualised is not None else None,
        "levels": [
            {
                "price": float(level.price),
                "size": float(level.size),
                "taken": float(taken),
            }
            for level, taken in fill.levels
        ],
        "note": (
            "Arithmetic only, from the order book and the fee schedule. Assumes the "
            "position is held to resolution; selling earlier is a second taker trade "
            "and pays the fee again."
        ),
    }
