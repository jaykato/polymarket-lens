"""板の読み取りと消化。

`order_book_snapshots.raw_json` に保存済みの板を、価格順に並べ直してから
指定金額ぶん歩く。追加のAPI呼び出しは行わない。

CLOBの/bookは段を「最悪→最良」の順で返す。bidsは昇順、asksは降順で、
どちらも最良気配が配列の末尾に来る。受け取った順に約定を積むと最悪価格から
食うことになり、損益分岐が現実離れした値になる。ここでは必ず明示的に
並べ替えてから歩く。
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, NamedTuple

# 取引所が扱う最小・最大の気配。ここを外れた段は約定の対象にしない。
MIN_PRICE = Decimal("0")
MAX_PRICE = Decimal("1")

# 約定とみなさない下限。
#
# Decimalの除算は28桁で打ち切られるため、ある段を予算ちょうどで買い切っても
# 1e-26程度の端数が残る。これを残額として次の段へ持ち越すと、実際には1株も
# 買っていない段が「約定済み」として色付き、消化段数にも数えられてしまう。
# 最小注文数量は5株なので、ここまで小さい値に意味はない。
DUST = Decimal("1E-9")


class Level(NamedTuple):
    """板の一段。価格は0〜1、数量は株数。"""

    price: Decimal
    size: Decimal


class Fill(NamedTuple):
    """板を歩いた結果。"""

    shares: Decimal          # 取得株数
    notional: Decimal        # 手数料を除いた約定代金
    fees: Decimal            # 手数料の合計
    spent: Decimal           # 実際に使った金額（notional + fees）
    unfilled: Decimal        # 板が尽きて使えなかった金額
    levels: list[tuple[Level, Decimal]]  # 各段と、そこで取れた株数


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_levels(rows: Iterable[Any]) -> list[Level]:
    """APIの板配列をLevelへ直す。壊れた行は黙って落とす。

    上流の形は保証されないため、価格か数量が読めない段は無視する。
    範囲外の価格も落とす。0や1で約定しても利益が定義できない。
    """
    levels: list[Level] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        price = _decimal(row.get("price"))
        size = _decimal(row.get("size"))
        if price is None or size is None:
            continue
        if size <= 0 or price <= MIN_PRICE or price > MAX_PRICE:
            continue
        levels.append(Level(price, size))
    return levels


def load_book(raw_json: str | None) -> dict[str, list[Level]]:
    """保存済みのraw_jsonから、価格順に並べた板を取り出す。

    bidsは高い順（最良が先頭）、asksは安い順（最良が先頭）に揃える。
    """
    if not raw_json:
        return {"bids": [], "asks": []}
    try:
        payload = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return {"bids": [], "asks": []}
    if not isinstance(payload, dict):
        return {"bids": [], "asks": []}
    bids = parse_levels(payload.get("bids"))
    asks = parse_levels(payload.get("asks"))
    return {
        "bids": sorted(bids, key=lambda level: level.price, reverse=True),
        "asks": sorted(asks, key=lambda level: level.price),
    }


def ask_levels(book: dict[str, list[Level]], side: str) -> list[Level]:
    """買い注文が食う側の板を、安い順で返す。

    NOトークンの実板が無い場合だけ、YESのビッドを反転してNOアスクを導出する。
    """
    if side == "no":
        mirrored = [
            Level(MAX_PRICE - level.price, level.size)
            for level in book.get("bids", [])
            if MIN_PRICE < MAX_PRICE - level.price <= MAX_PRICE
        ]
        return sorted(mirrored, key=lambda level: level.price)
    return sorted(book.get("asks", []), key=lambda level: level.price)


def fee_per_share(price: Decimal, fee_rate: Decimal) -> Decimal:
    """Polymarketのテイカー手数料（1株あたり）。

        fee = rate × p × (1 − p)

    min(p, 1−p)ではない。p=0.5で最大、両端でゼロに近づく釣鐘型になる。
    100株・p=0.5なら、crypto（0.07）で1.75ドル、politics（0.04）で1.00ドル。
    メイカーは無料で、テイカーだけが払う。
    """
    if fee_rate <= 0:
        return Decimal("0")
    return fee_rate * price * (MAX_PRICE - price)


def walk(levels: list[Level], stake: Decimal, fee_rate: Decimal) -> Fill:
    """安い段から順に、予算が尽きるまで買う。

    手数料は株数に比例するため、株数を先に決めると循環参照になる。
    段ごとに「1株あたりの総額 = 価格 + 手数料」を出し、その単価で予算を
    割り当てることで循環を避ける。板が尽きた分はunfilledに残す。
    """
    budget = stake
    shares = Decimal("0")
    notional = Decimal("0")
    fees = Decimal("0")
    filled: list[tuple[Level, Decimal]] = []

    for level in sorted(levels, key=lambda item: item.price):
        take = Decimal("0")
        per_share = level.price + fee_per_share(level.price, fee_rate)
        # 価格0の段は総額0になり、無限株取れてしまう。約定の対象にしない。
        if budget > DUST and per_share > 0:
            candidate = min(level.size, budget / per_share)
            # 端数だけの約定は無かったことにする。残すと、買っていない段が
            # 消化済みとして表示される。
            if candidate > DUST:
                take = candidate
                shares += take
                notional += take * level.price
                fees += take * fee_per_share(level.price, fee_rate)
                budget -= take * per_share
        filled.append((level, take))

    # 端数の丸めで予算がわずかに残る、または負へ振れることがある。
    unfilled = budget if budget > DUST else Decimal("0")
    return Fill(
        shares=shares,
        notional=notional,
        fees=fees,
        spent=stake - unfilled,
        unfilled=unfilled,
        levels=filled,
    )
