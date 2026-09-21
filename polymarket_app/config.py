from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """アプリケーション設定。秘密情報は扱わない。"""

    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    data_base_url: str = "https://data-api.polymarket.com"
    database_path: Path = Path("data/polymarket.db")
    request_interval_seconds: float = 0.25
    request_timeout_seconds: float = 20.0
    max_retries: int = 3
    # 閲覧のたびに板を保存してもDBが無制限に増えないよう、各トークンに残す件数。
    max_order_book_snapshots_per_token: int = 120
    user_agent: str = "polymarket-readonly-analysis/0.1"


DEFAULT_SETTINGS = Settings()
