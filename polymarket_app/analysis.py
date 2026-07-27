from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return default


def clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def log_score(value: float, low: float, high: float) -> float:
    """桁の異なる市場を比較するため、対数スケールで0〜100へ変換する。"""
    if value <= low:
        return 0.0
    if value >= high:
        return 100.0
    return clamp(
        (math.log10(value) - math.log10(low))
        / (math.log10(high) - math.log10(low))
        * 100
    )


def spread_score(spread: float) -> float:
    """狭いスプレッドほど高評価。10ポイント以上は強い注意対象。"""
    points = [
        (0.00, 100.0),
        (0.01, 100.0),
        (0.02, 85.0),
        (0.05, 60.0),
        (0.10, 30.0),
        (0.20, 0.0),
    ]
    if spread <= points[0][0]:
        return points[0][1]
    if spread >= points[-1][0]:
        return points[-1][1]
    for (left_x, left_y), (right_x, right_y) in zip(points, points[1:]):
        if left_x <= spread <= right_x:
            ratio = (spread - left_x) / (right_x - left_x)
            return left_y + ratio * (right_y - left_y)
    return 0.0


def tier(score: float, good: float = 72.0, caution: float = 45.0) -> str:
    if score >= good:
        return "good"
    if score >= caution:
        return "caution"
    return "risk"


def analyze_market(market: dict[str, Any]) -> dict[str, Any]:
    """内部スコアを、方向推奨ではない平易な市場状態へ変換する。"""
    spread = max(0.0, number(market.get("spread")))
    liquidity = max(0.0, number(market.get("liquidity")))
    volume = max(0.0, number(market.get("volume")))
    best_bid = clamp(number(market.get("best_bid")), 0.0, 1.0)
    best_ask = clamp(number(market.get("best_ask")), 0.0, 1.0)

    pricing = spread_score(spread)
    exit_flexibility = log_score(liquidity, 100.0, 100_000.0)
    participation = log_score(volume, 1_000.0, 1_000_000.0)
    execution = (
        pricing * 0.50 + exit_flexibility * 0.35 + participation * 0.15
    )

    execution_tier = tier(execution)
    if execution_tier == "good":
        headline = "Favorable trading conditions"
        summary = "Pricing is relatively tight and this market should be easier to enter or exit."
    elif execution_tier == "caution":
        headline = "Trade with care"
        summary = "Conditions are usable, but price or liquidity may worsen for larger amounts."
    else:
        headline = "High-friction market"
        summary = "Wide pricing or thin liquidity can materially change the effective price."

    pricing_tier = tier(pricing, good=78.0, caution=48.0)
    pricing_copy = {
        "good": (
            "Tight pricing",
            "Buyers and sellers are quoting relatively close prices.",
        ),
        "caution": (
            "Noticeable price gap",
            "The displayed probability may differ from an executable price.",
        ),
        "risk": (
            "Wide price gap",
            "Entering and exiting may carry a meaningful hidden cost.",
        ),
    }

    exit_tier = tier(exit_flexibility, good=68.0, caution=38.0)
    exit_copy = {
        "good": (
            "Flexible exits",
            "Available liquidity suggests positions may be easier to unwind.",
        ),
        "caution": (
            "Moderate liquidity",
            "Larger amounts may move through several order-book levels.",
        ),
        "risk": (
            "Limited exits",
            "Thin liquidity can make it difficult to leave at the expected price.",
        ),
    }

    participation_tier = tier(participation, good=68.0, caution=35.0)
    participation_copy = {
        "good": (
            "Broad participation",
            "The market has attracted substantial cumulative trading activity.",
        ),
        "caution": (
            "Developing market",
            "Participation is present but still limited relative to mature markets.",
        ),
        "risk": (
            "Light participation",
            "The quoted probability may rely on relatively little trading activity.",
        ),
    }

    if best_bid > 0 and best_ask > 0:
        executable_mid = (best_bid + best_ask) / 2
    else:
        executable_mid = number(market.get("last_trade_price"), 0.5)
    yes_probability = clamp(executable_mid, 0.0, 1.0)
    if yes_probability >= 0.62:
        crowd_status = "Crowd leans YES"
        crowd_detail = "Current executable prices favor the Yes outcome."
    elif yes_probability <= 0.38:
        crowd_status = "Crowd leans NO"
        crowd_detail = "Current executable prices favor the No outcome."
    else:
        crowd_status = "Crowd is divided"
        crowd_detail = "Neither outcome has a strong pricing advantage."

    def metric(
        key: str,
        label: str,
        score: float,
        metric_tier: str,
        copy: tuple[str, str],
    ) -> dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "status": copy[0],
            "explanation": copy[1],
            "tone": metric_tier,
            "bar_percent": round(clamp(score)),
        }

    return {
        "headline": headline,
        "summary": summary,
        "tone": execution_tier,
        "direction": {
            "status": crowd_status,
            "explanation": crowd_detail,
            "yes_percent": round(yes_probability * 100),
        },
        "metrics": [
            metric(
                "pricing",
                "Price gap",
                pricing,
                pricing_tier,
                pricing_copy[pricing_tier],
            ),
            metric(
                "liquidity",
                "Exit flexibility",
                exit_flexibility,
                exit_tier,
                exit_copy[exit_tier],
            ),
            metric(
                "participation",
                "Market participation",
                participation,
                participation_tier,
                participation_copy[participation_tier],
            ),
        ],
        # 画面には表示しない。判定の再現性と将来の検証用に保持する。
        "internal_scores": {
            "execution": round(execution, 2),
            "pricing": round(pricing, 2),
            "exit_flexibility": round(exit_flexibility, 2),
            "participation": round(participation, 2),
        },
        "disclaimer": "Market conditions describe execution quality, not whether Yes or No will be profitable.",
    }
