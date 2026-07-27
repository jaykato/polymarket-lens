from __future__ import annotations

import math
from statistics import pstdev
from typing import Any

from .analysis import number

DAY_SECONDS = 24 * 60 * 60

# 1日あたりの典型的な変動幅（ポイント）の境目。良し悪しではなく、
# 動きの大きさだけを表す。
STEADY_LIMIT = 2.0
ACTIVE_LIMIT = 6.0


def _points(value: float) -> float:
    """確率(0〜1)の差を、読みやすい「ポイント」へ直す。"""
    return round(value * 100, 1)


def _series(history: list[dict[str, Any]]) -> list[tuple[int, float]]:
    series: list[tuple[int, float]] = []
    for point in history:
        timestamp = point.get("timestamp_utc")
        if timestamp is None:
            continue
        series.append((int(timestamp), number(point.get("price"))))
    series.sort(key=lambda item: item[0])
    return series


def summarize_movement(history: list[dict[str, Any]]) -> dict[str, Any]:
    """保存済みの価格系列から、観測された事実だけを要約する。

    予測も推奨も行わない。実際に記録された価格がどう動いたかだけを返す。
    追加のAPI呼び出しは必要としない。
    """
    series = _series(history)
    if len(series) < 2:
        return {"available": False, "observations": len(series)}

    first_timestamp, first_price = series[0]
    last_timestamp, last_price = series[-1]
    high_timestamp, high_price = max(series, key=lambda item: item[1])
    low_timestamp, low_price = min(series, key=lambda item: item[1])

    increments: list[float] = []
    largest: tuple[float, int, int] | None = None
    for (previous_at, previous_price), (current_at, current_price) in zip(
        series, series[1:]
    ):
        elapsed = current_at - previous_at
        if elapsed <= 0:
            continue
        delta = current_price - previous_price
        # 観測間隔が一定とは限らないため、ランダムウォークと同じ√時間で
        # 割って1日あたりへ揃えてから散らばりを見る。
        increments.append(delta / math.sqrt(elapsed / DAY_SECONDS))
        if largest is None or abs(delta) > abs(largest[0]):
            largest = (delta, previous_at, current_at)

    daily_swing = _points(pstdev(increments)) if len(increments) >= 3 else None

    return {
        "available": True,
        "observations": len(series),
        "start_utc": first_timestamp,
        "end_utc": last_timestamp,
        "first_percent": _points(first_price),
        "last_percent": _points(last_price),
        "change_points": _points(last_price - first_price),
        "change_24h_points": _change_since(series, DAY_SECONDS),
        "high": {"percent": _points(high_price), "timestamp_utc": high_timestamp},
        "low": {"percent": _points(low_price), "timestamp_utc": low_timestamp},
        "range_points": _points(high_price - low_price),
        "daily_swing_points": daily_swing,
        "largest_move": _describe_move(largest),
        "stability": _describe_stability(daily_swing),
    }


def _change_since(series: list[tuple[int, float]], seconds: int) -> float | None:
    """指定時間前の観測値との差。そこまで遡れない場合はNone。"""
    cutoff = series[-1][0] - seconds
    earlier = [price for timestamp, price in series if timestamp <= cutoff]
    if not earlier:
        return None
    return _points(series[-1][1] - earlier[-1])


def _describe_move(largest: tuple[float, int, int] | None) -> dict[str, Any] | None:
    if largest is None:
        return None
    delta, started_at, ended_at = largest
    return {
        "points": _points(delta),
        "from_utc": started_at,
        "to_utc": ended_at,
        "hours": round((ended_at - started_at) / 3600, 1),
    }


def _describe_stability(daily_swing: float | None) -> dict[str, str]:
    """動きの大きさを平易な言葉にする。値動きの大小に良し悪しはない。"""
    if daily_swing is None:
        return {
            "level": "unknown",
            "status": "Not enough history",
            "explanation": "This range holds too few observations to describe how the price moves.",
        }
    if daily_swing < STEADY_LIMIT:
        return {
            "level": "steady",
            "status": "Steady",
            "explanation": "Day-to-day moves have been small over this range.",
        }
    if daily_swing < ACTIVE_LIMIT:
        return {
            "level": "active",
            "status": "Active",
            "explanation": "The quoted probability has moved noticeably from day to day.",
        }
    return {
        "level": "volatile",
        "status": "Volatile",
        "explanation": "Large day-to-day swings; the quoted probability has been unstable.",
    }
