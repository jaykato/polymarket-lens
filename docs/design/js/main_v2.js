/*
 * L0 Return Calculator — モックアップ v2 の操作部
 *
 * v1 からの変更（案B）:
 *   - 市場一覧を追加。検索・並べ替え・流動性フィルタは実アプリと同じ構成。
 *   - 一覧に Entry cost 列を追加し、並べ替えキーにも加えた。
 *
 * 実装時の対応:
 *   一覧            → GET /api/markets
 *   詳細と板         → GET /api/markets/<condition_id>
 *   Entry cost の並び → database.py の MARKET_SORTS に SQL 式を追加
 *   板を歩く計算      → returns.py / book.py へ移す
 *
 * 予測は含まない。すべて板・手数料表・残存日数から一意に決まる値。
 */
"use strict";

/*
 * 手数料の料率はカテゴリごとに違う（実測 0.03〜0.07）。100市場のうち15件は
 * feesEnabled=false で手数料ゼロだった（いずれも地政学系）。
 * 使うのは feeSchedule.rate。takerBaseFee / makerBaseFee は全市場 1000 固定の
 * レガシー項目で実際の料率と矛盾するため、参照してはいけない。
 */
const FEE_RATES = {
  crypto_fees_v2: 0.07,
  sports_fees_v3: 0.05,
  culture_fees: 0.05,
  economics_fees: 0.05,
  weather_fees: 0.05,
  politics_fees: 0.04,
  finance_prices_fees: 0.04,
  sports_fees_v2: 0.03,
  none: 0,                      // feesEnabled=false
};

const STAKE_MIN = 10;
const STAKE_MAX = 20000;
const DAY_MS = 24 * 60 * 60 * 1000;

/* 板の各段の厚み。最良気配の枚数に対する倍率で、奥ほど厚くなる。
   実際の CLOB は 70 段返すこともあるので、段数は表示側で絞る。 */
const DEPTH_PROFILE = [1, 2.1, 2.5, 3.75, 6.7, 8.4, 9.6, 10.8];

/* 板ラダーの既定表示段数。約定に関係ない奥の段まで並べると、
   幅 390px の詳細パネルが読めなくなる。 */
const LADDER_VISIBLE = 5;

/**
 * 気配から板を組む。
 * asks は best から上へ、bids は best から下へ 1 tick ずつ刻む。
 * 0.01 未満・0.99 超の段は現実に存在しないので落とす。
 */
function ladder(best, direction, depth, tick) {
  const levels = [];
  DEPTH_PROFILE.forEach((multiplier, index) => {
    const price = Math.round((best + direction * tick * index) * 1000) / 1000;
    if (price < tick || price > 1 - tick) return;
    levels.push([price, Math.round(depth * multiplier)]);
  });
  return levels;
}

/* ------------------------------------------------------------------ *
 * モックの市場データ
 * quoted は outcomePrices 相当。bid/ask から板を生成する。
 * ------------------------------------------------------------------ */
const RAW_MARKETS = [
  { id: "m1", question: "New Rihanna Album before GTA VI?",
    endDate: "2026-09-30", ask: 0.53, bid: 0.50, quoted: 0.515,
    volume: 871377, liquidity: 7423, depth: 1200,
    feeType: "culture_fees", tick: 0.01 },

  { id: "m2", question: "Fed cuts rates at the September meeting?",
    endDate: "2026-09-18", ask: 0.62, bid: 0.61, quoted: 0.615,
    volume: 12410880, liquidity: 243500, depth: 24000,
    feeType: "economics_fees", tick: 0.001 },

  { id: "m3", question: "Category 5 hurricane makes US landfall in 2026?",
    endDate: "2026-11-30", ask: 0.12, bid: 0.05, quoted: 0.085,
    volume: 145200, liquidity: 3100, depth: 900,
    feeType: "weather_fees", tick: 0.01 },

  { id: "m4", question: "Bitcoin above $150,000 on 31 December 2026?",
    endDate: "2026-12-31", ask: 0.34, bid: 0.31, quoted: 0.325,
    volume: 4820110, liquidity: 96400, depth: 11000,
    feeType: "crypto_fees_v2", tick: 0.01 },

  { id: "m5", question: "Incumbent party wins the 2026 midterms?",
    endDate: "2026-11-03", ask: 0.49, bid: 0.42, quoted: 0.455,
    volume: 388900, liquidity: 5900, depth: 1050,
    feeType: "politics_fees", tick: 0.01 },

  { id: "m6", question: "OpenAI releases GPT-6 before 2027?",
    endDate: "2026-12-31", ask: 0.67, bid: 0.55, quoted: 0.610,
    volume: 96700, liquidity: 2400, depth: 620,
    feeType: "culture_fees", tick: 0.01 },

  // 手数料ゼロの市場。実データでは地政学系15件が feesEnabled=false だった。
  { id: "m7", question: "NATO x Russia military clash by 31 December 2026?",
    endDate: "2026-12-31", ask: 0.25, bid: 0.20, quoted: 0.225,
    volume: 62300, liquidity: 1850, depth: 700,
    feeType: "none", tick: 0.01 },

  { id: "m8", question: "Bitcoin ends 2026 above $50,000?",
    endDate: "2026-12-31", ask: 0.91, bid: 0.90, quoted: 0.905,
    volume: 2140000, liquidity: 78000, depth: 9500,
    feeType: "crypto_fees_v2", tick: 0.001 },
];

/**
 * Polymarket の taker 手数料。
 *
 *   fee = shares × rate × p × (1 − p)
 *
 * min(p, 1−p) ではない。p=0.50 で最大、両端でゼロに近づく釣鐘型。
 * 100株・p=0.50 なら crypto(0.07) で $1.75、politics(0.04) で $1.00 になり、
 * これは公表値と一致する。メイカーは無料で、テイカーのみが払う。
 */
function feePerShare(price, rate) {
  return rate * price * (1 - price);
}

/**
 * Entry cost = best_ask + fee(best_ask) − quoted
 *
 * トップオブブックの摩擦。板を歩かないので保存済みの best_ask だけで出せる。
 * 一覧に出す値はこれ。実際の約定はサイズ次第でこれより悪くなる。
 */
function entryCost(market) {
  const ask = market.books.yes[0][0];
  return ask + feePerShare(ask, market.feeRate) - market.quoted.yes;
}

const MARKETS = RAW_MARKETS.map((raw) => {
  const asks = ladder(raw.ask, +1, raw.depth, raw.tick);
  const bids = ladder(raw.bid, -1, Math.round(raw.depth * 1.25), raw.tick);
  const market = {
    ...raw,
    feeRate: FEE_RATES[raw.feeType] ?? 0,
    bestAsk: raw.ask,
    bestBid: raw.bid,
    quoted: { yes: raw.quoted, no: Math.round((1 - raw.quoted) * 1000) / 1000 },
    // NO のアスクは YES のビッドの裏返し。価格は 1 − p で対応する。
    // collector は token_ids[0]（YES）の板しか取らないので、実装でもこの導出になる。
    books: {
      yes: asks,
      no: bids.map(([price, size]) => [Math.round((1 - price) * 1000) / 1000, size]),
    },
  };
  market.entryCost = entryCost(market);
  return market;
});

/* ------------------------------------------------------------------ *
 * 計算
 * ------------------------------------------------------------------ */

/**
 * 板を歩いて約定を積む。
 *
 * 手数料は株数に比例するため、株数を先に決めると循環参照になる。
 * 価格帯ごとに「1株あたりの総コスト = 価格 + 手数料」を出し、その単価で
 * 予算を割り当てて循環を避ける。板が尽きた分は unfilled に残す。
 */
function fillBook(book, stake, rate) {
  let budget = stake;
  let shares = 0;
  let notional = 0;
  let fees = 0;
  const levels = [];

  // 必ず明示的に安い順へ並べてから歩く。
  // CLOB の /book は asks を降順（最悪→最良）で返すため、受け取った順に
  // 積むと最悪価格から約定してしまい、損益分岐が99%付近になる。
  const sorted = [...book].sort((a, b) => a[0] - b[0]);

  for (const [price, size] of sorted) {
    let take = 0;
    if (budget > 1e-9) {
      const perShare = price + feePerShare(price, rate);
      take = Math.min(size, budget / perShare);
      shares += take;
      notional += take * price;
      fees += take * feePerShare(price, rate);
      budget -= take * perShare;
    }
    levels.push({ price, size, take });
  }

  return { shares, notional, fees, spent: stake - budget, unfilled: budget, levels };
}

function computeLine(market, side, stake) {
  const fill = fillBook(market.books[side], stake, market.feeRate);

  // 損益分岐確率 = 支払総額 / 取得株数。1株の払戻が $1.00 なので、
  // これがそのまま「勝たなければならない確率」になる。
  const breakeven = fill.shares > 0 ? fill.spent / fill.shares : 0;
  const avgFill = fill.shares > 0 ? fill.notional / fill.shares : 0;
  const avgFee = fill.shares > 0 ? fill.fees / fill.shares : 0;

  const payout = fill.shares;                 // $1.00 × 株数
  const winPct = fill.spent > 0 ? (payout - fill.spent) / fill.spent : 0;

  const days = Math.max(
    1,
    Math.round((new Date(`${market.endDate}T12:00:00Z`).getTime() - Date.now()) / DAY_MS)
  );
  // 単利換算。全損しうる二値の賭けを複利で年率化すると桁が壊れるため。
  const annual = winPct * (365 / days);

  const quoted = market.quoted[side];
  return { fill, breakeven, avgFill, avgFee, payout, winPct, days, annual, quoted,
           gap: breakeven - quoted };
}

/* ------------------------------------------------------------------ *
 * 表示整形
 * ------------------------------------------------------------------ */
const el = (id) => document.getElementById(id);
const pct = (v, d = 1) => `${(v * 100).toFixed(d)}%`;
const price4 = (v) => v.toFixed(4);
/* 気配の刻みは市場ごとに違う（実測 0.01 と 0.001）。刻みに合わせて桁を変える。 */
const priceText = (v, tick) => v.toFixed(tick < 0.01 ? 3 : 2);
const points = (v) => `${v >= 0 ? "+" : "−"}${Math.abs(v * 100).toFixed(1)}pt`;
const money = (v) =>
  `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const compact = (v) => {
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `$${Math.round(v / 1e3)}k`;
  return `$${Math.round(v)}`;
};
const sharesText = (v) =>
  v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const dateLabel = (iso) =>
  new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-GB",
    { day: "numeric", month: "short", year: "numeric" });
const escapeHtml = (text) =>
  text.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------------ *
 * 状態
 * ------------------------------------------------------------------ */
const state = {
  query: "",
  sort: "volume",
  liquidOnly: false,
  selected: MARKETS[0].id,
  side: "yes",
  stake: 100,
  showAllLevels: false,
};

const SORTS = {
  volume: (a, b) => b.volume - a.volume,
  liquidity: (a, b) => b.liquidity - a.liquidity,
  ending_soon: (a, b) => a.endDate.localeCompare(b.endDate),
  entry_cost: (a, b) => a.entryCost - b.entryCost,
};

function visibleMarkets() {
  const query = state.query.trim().toLowerCase();
  const rows = MARKETS.filter((market) => {
    if (state.liquidOnly && market.liquidity < 5000) return false;
    if (query && !market.question.toLowerCase().includes(query)) return false;
    return true;
  });
  // 未知のキーは既定の並びへ丸める。実装側の resolve_sort と同じ考え方。
  return rows.sort(SORTS[state.sort] || SORTS.volume);
}

/** 対数スケールのスライダー位置 (0-1000) と金額の相互変換。 */
function sliderToStake(position) {
  const lo = Math.log10(STAKE_MIN);
  const hi = Math.log10(STAKE_MAX);
  const raw = Math.pow(10, lo + (hi - lo) * (position / 1000));
  return raw < 100 ? Math.round(raw) : Math.round(raw / 10) * 10;
}

function stakeToSlider(value) {
  const lo = Math.log10(STAKE_MIN);
  const hi = Math.log10(STAKE_MAX);
  const clamped = Math.min(STAKE_MAX, Math.max(STAKE_MIN, value));
  return Math.round(((Math.log10(clamped) - lo) / (hi - lo)) * 1000);
}

/* ------------------------------------------------------------------ *
 * 描画 — 一覧
 * ------------------------------------------------------------------ */
function entryTone(cost) {
  if (cost < 0.025) return "low";
  if (cost > 0.05) return "high";
  return "";
}

function renderList() {
  const rows = visibleMarkets();
  const list = el("market-list");

  el("list-status").textContent = state.query
    ? `${rows.length} of ${MARKETS.length} markets match "${state.query}"`
    : `${rows.length} markets · entry cost is top-of-book friction, before size`;

  // 並べ替え中の列を凡例で示す
  el("list-legend").querySelectorAll("span").forEach((span) => span.classList.remove("sorted"));
  const sortedColumn = { volume: ".col-volume", entry_cost: ".col-entry" }[state.sort];
  if (sortedColumn) el("list-legend").querySelector(sortedColumn).classList.add("sorted");

  if (!rows.length) {
    list.innerHTML = `<p class="list-status">No market matches that search.</p>`;
    return;
  }

  list.innerHTML = rows.map((market) => `
    <button class="market-row ${state.selected === market.id ? "active" : ""}"
            data-id="${market.id}" type="button">
      <div>
        <h3>${escapeHtml(market.question)}</h3>
        <div class="meta">ENDS ${dateLabel(market.endDate).toUpperCase()}</div>
      </div>
      <div class="metric probability"><b>${Math.round(market.quoted.yes * 100)}%</b><small>YES</small></div>
      <div class="metric volume"><b>${compact(market.volume)}</b><small>VOLUME</small></div>
      <div class="metric entry ${entryTone(market.entryCost)}">
        <b>${points(market.entryCost)}</b><small>ENTRY</small>
      </div>
    </button>
  `).join("");

  list.querySelectorAll(".market-row").forEach((button) => {
    button.addEventListener("click", () => {
      state.selected = button.dataset.id;
      render();
    });
  });
}

/* ------------------------------------------------------------------ *
 * 描画 — 詳細（L0）
 * ------------------------------------------------------------------ */
function renderLadder(result, tick) {
  const all = result.fill.levels;
  const touched = all.filter((level) => level.take > 1e-6).length;

  // 消化した段＋その先2段までを既定で見せる。残りは畳む。
  // 判断に効くのは「いま食っている段」と「次にいくら悪くなるか」だけ。
  const cut = state.showAllLevels
    ? all.length
    : Math.min(all.length, Math.max(LADDER_VISIBLE, touched + 2));
  const hidden = all.slice(cut);

  el("ladder").innerHTML = all.slice(0, cut).map((level) => {
    const ratio = level.size > 0 ? level.take / level.size : 0;
    const used = level.take > 1e-6;
    return `
      <div class="ladder-row ${used ? "used" : ""}">
        <div class="fill" style="width:${(ratio * 100).toFixed(2)}%"></div>
        <b>${priceText(level.price, tick)}</b>
        <span class="depth">${level.size.toLocaleString("en-US")} available</span>
        <span class="taken">${used ? sharesText(level.take) : "—"}</span>
      </div>`;
  }).join("");

  // 畳んだ段の存在は必ず示す。板が浅いのか、単に隠しているのかを取り違えさせない。
  const toggle = el("ladder-toggle");
  if (hidden.length) {
    const deeper = hidden.reduce((sum, level) => sum + level.size, 0);
    toggle.hidden = false;
    toggle.textContent =
      `▾ ${hidden.length} deeper level${hidden.length === 1 ? "" : "s"} · ${deeper.toLocaleString("en-US")} more shares`;
  } else if (state.showAllLevels && all.length > LADDER_VISIBLE) {
    toggle.hidden = false;
    toggle.textContent = "▴ Show fewer levels";
  } else {
    toggle.hidden = true;
  }

  const note = el("ladder-note");
  if (result.fill.unfilled > 0.01) {
    note.className = "ladder-note warn";
    note.textContent =
      `Book exhausted. ${money(result.fill.unfilled)} of the stake could not be filled at any displayed level.`;
  } else {
    note.className = "ladder-note";
    note.textContent =
      `Filled across ${touched} price level${touched === 1 ? "" : "s"}. Larger sizes walk deeper and raise the break-even.`;
  }
}

function renderDetail() {
  const market = MARKETS.find((entry) => entry.id === state.selected) || MARKETS[0];
  const result = computeLine(market, state.side, state.stake);

  el("d-question").textContent = market.question;
  el("d-end").textContent = dateLabel(market.endDate);
  el("d-liq").textContent = compact(market.liquidity);
  el("d-quote").textContent =
    `${priceText(market.bestBid, market.tick)} / ${priceText(market.bestAsk, market.tick)}`;
  el("d-fee").textContent = market.feeRate > 0 ? pct(market.feeRate, 0) : "none";
  el("b-fee-formula").textContent = market.feeRate > 0
    ? `${pct(market.feeRate, 0)} × p × (1−p) · ${market.feeType}`
    : "this market charges no taker fee";

  el("side-yes-ask").textContent = `ask ${priceText(market.books.yes[0][0], market.tick)}`;
  el("side-no-ask").textContent = `ask ${priceText(market.books.no[0][0], market.tick)}`;

  el("b-quoted").textContent = price4(result.quoted);
  el("b-ask").textContent = price4(market.books[state.side][0][0]);
  el("b-fill").textContent = price4(result.avgFill);
  el("b-fee").textContent = price4(result.avgFee);
  el("b-total").textContent = price4(result.breakeven);

  el("r-breakeven").textContent = pct(result.breakeven);
  el("r-gap").textContent = points(result.gap);
  el("r-quoted").textContent = pct(result.quoted);
  el("r-required").textContent = pct(result.breakeven);

  const shownWidth = Math.min(100, Math.max(0, result.quoted * 100));
  const extraWidth = Math.max(0, Math.min(100 - shownWidth, result.gap * 100));
  el("gap-shown").style.width = `${shownWidth}%`;
  el("gap-extra").style.left = `${shownWidth}%`;
  el("gap-extra").style.width = `${extraWidth}%`;

  el("r-win-pct").textContent = `+${pct(result.winPct)}`;
  el("r-win-abs").textContent = `${money(result.fill.spent)} → ${money(result.payout)}`;
  el("r-lose-abs").textContent = `${money(result.fill.spent)} → $0.00`;

  el("r-shares").textContent = sharesText(result.fill.shares);
  el("r-days").textContent = `${result.days} days`;
  el("r-enddate").textContent = `until ${dateLabel(market.endDate)}`;
  el("r-annual").textContent = `+${pct(result.annual, 0)}`;

  renderLadder(result, market.tick);

  const stakeInput = el("stake");
  if (document.activeElement !== stakeInput) stakeInput.value = String(state.stake);
  el("stake-range").value = String(stakeToSlider(state.stake));

  document.querySelectorAll("#side-toggle button").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.side === state.side));
  });
}

function render() {
  renderList();
  renderDetail();
}

/* ------------------------------------------------------------------ *
 * 入力
 * ------------------------------------------------------------------ */
function setStake(value) {
  // 数値にならない入力は下限へ寄せる。推測して勝手な額を入れない。
  const parsed = Number.parseFloat(String(value).replace(/[^0-9.]/g, ""));
  state.stake = Number.isFinite(parsed)
    ? Math.min(STAKE_MAX, Math.max(STAKE_MIN, parsed))
    : STAKE_MIN;
  renderDetail();
}

el("search").addEventListener("input", (event) => {
  state.query = event.target.value;
  renderList();
});

el("sort").addEventListener("change", (event) => {
  state.sort = event.target.value;
  renderList();
});

el("liquid-only").addEventListener("change", (event) => {
  state.liquidOnly = event.target.checked;
  renderList();
});

el("ladder-toggle").addEventListener("click", () => {
  state.showAllLevels = !state.showAllLevels;
  renderDetail();
});

el("side-toggle").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-side]");
  if (!button) return;
  state.side = button.dataset.side;
  renderDetail();
});

el("presets").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-stake]");
  if (!button) return;
  setStake(button.dataset.stake);
});

el("stake").addEventListener("input", (event) => setStake(event.target.value));
el("stake").addEventListener("blur", () => { el("stake").value = String(state.stake); });
el("stake-range").addEventListener("input", (event) => {
  state.stake = sliderToStake(Number(event.target.value));
  renderDetail();
});

render();
