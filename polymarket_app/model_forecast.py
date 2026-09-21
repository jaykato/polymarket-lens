"""Chronos-2を使う任意の時系列予測機能。

通常のビューアはこのモジュールの依存を必要としない。モデルは初回実行時に
Hugging Faceのローカルキャッシュへ取得され、SQLiteには予測結果だけを残す。
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any


MODEL_ID = "amazon/chronos-2"


class ModelDependencyError(RuntimeError):
    pass


def infer_interval_seconds(points: list[dict[str, Any]]) -> int:
    """保存済みの実測間隔から、前方補完の粒度を決める。"""
    timestamps = sorted(
        {
            int(item["timestamp_utc"])
            for item in points
            if str(item.get("timestamp_utc", "")).lstrip("-").isdigit()
        }
    )
    gaps = [right - left for left, right in zip(timestamps, timestamps[1:]) if right > left]
    if not gaps:
        return 300
    # 分単位に丸め、極端に細かいAPI上の時刻ずれを入力頻度にしない。
    return max(60, round(median(gaps) / 60) * 60)


def regularize(
    points: list[dict[str, Any]], interval_seconds: int, max_points: int
) -> list[dict[str, Any]]:
    """保存済み確率を固定間隔にし、過去値だけで欠測を前方補完する。"""
    cleaned: list[tuple[int, float]] = []
    for item in points:
        try:
            timestamp = int(item["timestamp_utc"])
            value = float(item["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= value <= 1:
            cleaned.append((timestamp, value))
    if not cleaned or interval_seconds <= 0:
        return []
    cleaned.sort()
    last_timestamp = cleaned[-1][0] // interval_seconds * interval_seconds
    first_timestamp = max(
        cleaned[0][0] // interval_seconds * interval_seconds,
        last_timestamp - interval_seconds * (max(2, max_points) - 1),
    )
    result = []
    index = 0
    last_value: float | None = None
    for timestamp in range(first_timestamp, last_timestamp + 1, interval_seconds):
        while index < len(cleaned) and cleaned[index][0] <= timestamp:
            last_value = cleaned[index][1]
            index += 1
        if last_value is not None:
            result.append({"timestamp_utc": timestamp, "target": last_value})
    return result[-max_points:]


def forecast(
    points: list[dict[str, Any]], interval_seconds: int, prediction_length: int,
    context_points: int = 512, device_map: str = "cpu",
) -> dict[str, Any]:
    """Chronos-2の分位点予測を返す。入力不足や依存不足は明示的に失敗する。"""
    series = regularize(points, interval_seconds, context_points)
    if len(series) < 8:
        raise ValueError("At least eight regularly sampled price points are required.")
    try:
        import pandas as pd
        from chronos import Chronos2Pipeline
    except ImportError as exc:
        raise ModelDependencyError(
            "Install optional model dependencies with: pip install -r requirements-model.txt"
        ) from exc
    context = pd.DataFrame(
        {
            "item_id": "market",
            "timestamp": [datetime.fromtimestamp(item["timestamp_utc"], timezone.utc) for item in series],
            "target": [item["target"] for item in series],
        }
    )
    pipeline = Chronos2Pipeline.from_pretrained(MODEL_ID, device_map=device_map)
    predicted = pipeline.predict_df(
        context,
        prediction_length=prediction_length,
        quantile_levels=[0.1, 0.5, 0.9],
        id_column="item_id",
        timestamp_column="timestamp",
        target="target",
    )
    rows = []
    for row in predicted.to_dict(orient="records"):
        rows.append({str(key): _json_value(value) for key, value in row.items()})
    return {
        "model_id": MODEL_ID,
        "device_map": device_map,
        "interval_seconds": interval_seconds,
        "context_points": len(series),
        "prediction_length": prediction_length,
        "source_last_timestamp_utc": series[-1]["timestamp_utc"],
        "forecast": rows,
    }


def _json_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        return float(value)
    except (TypeError, ValueError):
        return value
