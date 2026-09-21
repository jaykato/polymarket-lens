"""市場価格を基準とした確率予測の検証。外部通信やモデル推論は行わない。"""

from __future__ import annotations

from typing import Any


def summarize(observations: list[dict[str, Any]], bins: int = 10) -> dict[str, Any]:
    """YES価格と最終YES結果からBrier scoreと価格帯別の実績を出す。"""
    rows: list[tuple[float, int]] = []
    for item in observations:
        try:
            probability = float(item["price"])
            outcome = 1 if int(item["outcome_index"]) == 0 else 0
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= probability <= 1:
            rows.append((probability, outcome))
    buckets = [{"count": 0, "probability_sum": 0.0, "outcome_sum": 0} for _ in range(bins)]
    squared_error = 0.0
    for probability, outcome in rows:
        index = min(bins - 1, int(probability * bins))
        bucket = buckets[index]
        bucket["count"] += 1
        bucket["probability_sum"] += probability
        bucket["outcome_sum"] += outcome
        squared_error += (probability - outcome) ** 2
    calibration = []
    for index, bucket in enumerate(buckets):
        if not bucket["count"]:
            continue
        count = bucket["count"]
        calibration.append(
            {
                "from": index / bins,
                "to": (index + 1) / bins,
                "count": count,
                "mean_probability": bucket["probability_sum"] / count,
                "yes_rate": bucket["outcome_sum"] / count,
            }
        )
    return {
        "observations": len(rows),
        "brier_score": squared_error / len(rows) if rows else None,
        "calibration": calibration,
    }


def corrected_probability(
    observations: list[dict[str, Any]], market_probability: float, bins: int = 10
) -> dict[str, Any] | None:
    """同じ価格帯の過去実績をBeta(1, 1)で平滑化して確率へ直す。"""
    if not 0 <= market_probability <= 1:
        return None
    index = min(bins - 1, int(market_probability * bins))
    matched = []
    for item in observations:
        try:
            probability = float(item["price"])
            outcome = 1 if int(item["outcome_index"]) == 0 else 0
        except (KeyError, TypeError, ValueError):
            continue
        if min(bins - 1, int(probability * bins)) == index:
            matched.append(outcome)
    # 標本ゼロでは市場価格をそのまま返す。存在しない偏りを作らない。
    if not matched:
        return {"probability": market_probability, "samples": 0, "method": "market"}
    return {
        "probability": (sum(matched) + 1) / (len(matched) + 2),
        "samples": len(matched),
        "method": "smoothed_price_bin",
    }
