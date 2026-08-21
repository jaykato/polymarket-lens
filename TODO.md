# 次回の作業リスト

最終更新: 2026-08-21 / 分岐元: `364c429` (v0.6.1) / 作業ブランチ: `l0-return-calculator`

## いまの状態

リターン計算機（L0）一式を `l0-return-calculator` ブランチに6コミットで積んだ。テストは117件すべて通過。
**`main` はまだ v0.6.1 のまま。** 次回いちばん最初にマージの可否を決める。

```
git switch main
git merge --ff-only l0-return-calculator
```

本日の最後の変更: 板が無い市場で「Cannot be priced」が大半だった件を、表示時のオンデマンド取得
（`search.ensure_order_book`）で解消した。DB 106市場中18市場しか板が無かったのが原因。

---

## 1. main へマージする（最優先）

作業ブランチに分けてあるので、`main` に取り込むかどうかをまず決める。

- [ ] `git merge --ff-only l0-return-calculator` で `main` へ取り込む
- [x] ~~コミットの切り方~~ → 6コミットに分割済み（板の読み取り → リターン計算 → 一覧のEntry cost → API/オンデマンド取得 → UI → 設計資料）
- [x] ~~`html/` の扱い~~ → `docs/design/` へ移してコミット済み
- [x] ~~`.claude/` の扱い~~ → `settings.json` のみコミット、`settings.local.json` は `.gitignore` 済み

コミット分割で1点だけ妥協した: `database.py` と `web.py` はそれぞれ2つの関心事
（一覧のEntry cost列 / 板の鮮度ヘルパ、`/api/returns/` / オンデマンド取得）を1コミットに
含んでいる。ファイル単位で切ったため。分けたい場合は `git rebase -i` で切り直す。

## 2. ドキュメントが機能に追いついていない

L0 の記述が README にも `docs/` にも一切ない。

- [ ] `README.md` にリターン計算機の説明を足す
- [ ] `docs/ARCHITECTURE.md` に `book.py` / `returns.py` と `/api/returns/` の口を足す
- [ ] `docs/how_to_start.html` に使い方（サイド切り替え・投資額スライダー・Entry cost 列）を足す
- [ ] バージョンを 0.7.0 へ上げるか決める。上げる場合の変更箇所は3つ: `polymarket_app/__init__.py`, `docs/how_to_start.html:336`, `docs/how_to_start.html:772`

## 3. オンデマンド取得の後始末

- [ ] `order_book_snapshots` にプルーニングが無い。閲覧のたびに最大60秒に1行増える（1行数KB）。トークンごとに直近N件だけ残す処理を入れるか判断
- [ ] `captured_at_utc` を API は返しているが画面に出していない。取得に失敗すると古い板で計算するため、いつ時点の板かを出したほうがよい（`app.js` の `renderReturnsResult`）
- [ ] `tests/test_search.py` に `ensure_order_book` の単体テストが無い（`test_web.py` の結合テストでは覆っている）

## 4. 検討課題

- [ ] NO側の板は YES のビッドの裏返し（価格 1−p）で導出している。実際の NO トークンの板と一致するか未検証。ズレるなら `collector` で両トークンぶん取得する必要がある
- [ ] `sync` は取得した市場ぶんしか板を保存しない。既存DBの全市場ぶんを更新するオプションを足すか、オンデマンド取得だけで足りるとするか
- [ ] `sync --markets 50` を流しても板が18件しか入っていなかった。途中で止まったのか、エラーで落ちたのか未確認（`SyncResult.errors` を出力で確認する）
