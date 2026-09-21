from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .client import PolymarketClient
from .calibration import summarize
from .consistency import event_cost_sums
from .collector import Collector
from .config import DEFAULT_SETTINGS, Settings
from .database import Database
from .paper import scan as scan_paper
from .model_forecast import (
    ModelDependencyError, forecast as forecast_model, infer_interval_seconds,
)
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
        max_order_book_snapshots_per_token=base.max_order_book_snapshots_per_token,
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

    resolved = subparsers.add_parser("sync-resolved", help="解決済み市場と全期間履歴を同期")
    resolved.add_argument("--markets", type=int, default=100, help="取得する解決済み市場数")

    calibrate = subparsers.add_parser("calibrate", help="市場価格のキャリブレーションを集計")
    calibrate.add_argument("--hours-before", type=int, default=24, help="決済の何時間前を評価するか")

    paper = subparsers.add_parser("paper-scan", help="保存済み板から仮想ポジションを記録")
    paper.add_argument("--stake", type=float, default=100, help="1件あたりの仮想投資額")
    paper.add_argument("--min-edge", type=float, default=0.03, help="最低コスト込み優位性")
    paper.add_argument("--hours-before", type=int, default=24, help="補正モデルの評価時点")
    paper.add_argument("--min-samples", type=int, default=30, help="補正に必要な最小標本数")

    subparsers.add_parser("settle-paper", help="解決済み仮想ポジションを精算")
    subparsers.add_parser("consistency", help="同一イベント内のYES取得コスト合計を検査")

    forecast = subparsers.add_parser("forecast", help="Chronos-2で保存済み価格を予測")
    forecast.add_argument("token_id", help="予測するYESまたはNOトークンID")
    forecast.add_argument("--interval-minutes", type=int, default=0, help="入力の固定間隔（0で自動）")
    forecast.add_argument("--steps", type=int, default=24, help="予測する時間足数")
    forecast.add_argument("--context", type=int, default=512, help="最大入力点数")
    forecast.add_argument("--device", default="cpu", choices=("cpu", "cuda"), help="推論デバイス")

    subparsers.add_parser("stats", help="保存件数を表示")

    web = subparsers.add_parser("serve", help="ローカルWeb UIを起動")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
    elif args.command == "sync-resolved":
        collector = Collector(PolymarketClient(settings), database)
        result = collector.sync_resolved(args.markets)
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    elif args.command == "calibrate":
        hours = max(0, args.hours_before)
        report = summarize(database.resolved_observations(hours * 3600))
        report["hours_before_resolution"] = hours
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "paper-scan":
        report = scan_paper(
            database, args.stake, args.min_edge,
            max(0, args.hours_before), max(1, args.min_samples),
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "settle-paper":
        settled = database.settle_paper_positions()
        print(json.dumps({"settled": settled, **database.paper_position_summary()}, ensure_ascii=False, indent=2))
    elif args.command == "consistency":
        print(json.dumps(event_cost_sums(database), ensure_ascii=False, indent=2))
    elif args.command == "forecast":
        points = database.token_history(args.token_id, limit=max(8, args.context * 4))
        interval = args.interval_minutes * 60 if args.interval_minutes > 0 else infer_interval_seconds(points)
        try:
            report = forecast_model(
                points, interval, max(1, args.steps), max(8, args.context), args.device
            )
        except (ModelDependencyError, ValueError) as exc:
            parser.error(str(exc))
        database.save_model_forecast(args.token_id, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
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
