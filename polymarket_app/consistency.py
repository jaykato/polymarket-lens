"""同一イベント内のYES取得コストを比較する。裁定機会の断定はしない。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .book import fee_per_share, load_book
from .database import Database


def event_cost_sums(database: Database) -> list[dict[str, Any]]:
    results = []
    for group in database.event_groups():
        members = []
        total = Decimal("0")
        for market in database.event_yes_markets(group["event_id"]):
            book = load_book(market.get("raw_json"))
            asks = book.get("asks") or []
            if not asks:
                continue
            try:
                fee_rate = Decimal(str(market.get("fee_rate") or 0))
            except Exception:
                continue
            # 取得コストは表示確率との差ではなく、ask + そのaskに掛かる手数料。
            effective = asks[0].price + fee_per_share(asks[0].price, fee_rate)
            total += effective
            members.append({"condition_id": market["condition_id"], "question": market["question"], "effective_cost": float(effective), "captured_at_utc": market.get("fetched_at_utc")})
        if len(members) >= 2:
            # Gammaのevent配下には、相互排他な候補群だけでなく、本戦と各ゲームの
            # ように同時に成立し得る市場も入る。質問・決済規則を検証するまでは
            # 合計乖離を裁定や優位性として扱わない。
            results.append(
                {
                    "event_id": group["event_id"],
                    "comparison_status": "unverified_event_membership",
                    "note": "Event membership alone does not prove mutual exclusivity; compare rules before using this sum.",
                    "markets": members,
                    "sum_effective_cost": float(total),
                    "deviation_points": float((total - 1) * 100),
                }
            )
    return results
