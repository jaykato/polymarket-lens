from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryRange:
    """チャートの表示期間と、それを満たすための取得・抽出条件。

    interval と fidelity はCLOBの`prices-history`へ渡す。window_seconds は
    保存済みデータから表示分だけを切り出すために使う。異なる期間を取得すると
    解像度の違う点が同じテーブルに混ざるため、抽出側でも期間を絞る。
    """

    key: str
    label: str
    interval: str
    fidelity: int
    window_seconds: int | None


RANGES: dict[str, HistoryRange] = {
    "1d": HistoryRange("1d", "1D", "1d", 5, 24 * 60 * 60),
    "1w": HistoryRange("1w", "1W", "1w", 60, 7 * 24 * 60 * 60),
    "1m": HistoryRange("1m", "1M", "1m", 180, 30 * 24 * 60 * 60),
    "max": HistoryRange("max", "MAX", "max", 720, None),
}

DEFAULT_RANGE_KEY = "1w"


def resolve_range(key: str | None) -> HistoryRange:
    """未知の値は既定期間へ丸める。クエリ文字列を検証なしで受けても安全にする。"""
    return RANGES.get((key or "").strip().lower(), RANGES[DEFAULT_RANGE_KEY])


def range_options() -> list[dict[str, str]]:
    return [{"key": item.key, "label": item.label} for item in RANGES.values()]
