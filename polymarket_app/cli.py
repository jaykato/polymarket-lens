from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .client import PolymarketClient
from .collector import Collector
from .config import DEFAULT_SETTINGS, Settings
from .database import Database
from .web import serve


def settings_for(database_path: str) -> Settings:
    base = DEFAULT_SETTINGS
    return Settings(
        gamma_base_url=base.gamma_base_url,
        clob_base_url=base.clob_base_url,
        data_base_url=base.data_base_url,
        database_path=Path(database_path),
        request_interval_seconds=base.request_interval_seconds,
        request_timeout_seconds=base.request_timeout_seconds,
        max_retries=base.max_retries,
        user_agent=base.user_agent,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="polymarket-lens",
        description="Polymarket公開データの読み取り専用分析アプリ",
    )
    parser.add_argument("--db", default="data/polymarket.db", help="SQLite DBのパス")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="データベースを初期化")

    sync = subparsers.add_parser("sync", help="公開APIからデータを同期")
    sync.add_argument("--markets", type=int, default=25, help="取得する市場数")
    sync.add_argument(
        "--histories",
        type=int,
        default=0,
        help="履歴を取得する市場数（0または省略で全市場）",
    )

    subparsers.add_parser("stats", help="保存件数を表示")

    web = subparsers.add_parser("serve", help="ローカルWeb UIを起動")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    settings = settings_for(args.db)
    database = Database(settings.database_path)
    database.initialize()

    if args.command == "init":
        print(f"初期化しました: {settings.database_path}")
    elif args.command == "sync":
        collector = Collector(PolymarketClient(settings), database)
        result = collector.sync(args.markets, args.histories)
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    elif args.command == "stats":
        print(json.dumps(database.stats(), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        static_dir = Path(__file__).parent / "static"
        serve(
            database,
            PolymarketClient(settings),
            args.host,
            args.port,
            static_dir,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
