"""注文を出さない仮想ポジションの作成と精算。"""

from __future__ import annotations

from typing import Any

from .book import load_book
from .calibration import corrected_probability
from .database import Database
from .returns import quote_position


MODEL_NAME = "smoothed_price_bin_v1"


def scan(
    database: Database, stake: float, min_edge: float, horizon_hours: int = 24,
    min_samples: int = 30,
) -> dict[str, Any]:
    """保存済み板で候補を仮想記録する。外部通信や実注文は行わない。"""
    training = database.resolved_observations(max(0, horizon_hours) * 3600)
    created = 0
    considered = 0
    for market in database.active_market_details():
        outcomes = market.get("outcomes") or []
        if not outcomes:
            continue
        token_id = outcomes[0].get("token_id")
        snapshot = database.latest_order_book(str(token_id)) if token_id else None
        if snapshot is None:
            continue
        quote = quote_position(market, load_book(snapshot["raw_json"]), stake=stake)
        if not quote.get("available") or quote.get("quoted") is None:
            continue
        considered += 1
        estimate = corrected_probability(training, float(quote["quoted"]))
        if estimate is None or estimate["samples"] < min_samples:
            continue
        edge = float(estimate["probability"]) - float(quote["breakeven"])
        if edge < min_edge:
            continue
        if database.save_paper_position(
            {
                "condition_id": market["condition_id"],
                "token_id": token_id,
                "side_index": 0,
                "model_name": MODEL_NAME,
                "predicted_probability": estimate["probability"],
                "entry_cost": quote["breakeven"],
                "shares": quote["shares"],
                "stake": quote["stake"],
                "edge": edge,
            }
        ):
            created += 1
    return {"considered": considered, "created": created, "training_observations": len(training), **database.paper_position_summary()}
