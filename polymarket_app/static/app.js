const DEFAULT_RANGES = [
  { key: "1d", label: "1D" },
  { key: "1w", label: "1W" },
  { key: "1m", label: "1M" },
  { key: "max", label: "MAX" }
];

// これ未満の流動性は、板が薄く価格が動かしやすいとみなして隠せるようにする。
const THIN_LIQUIDITY = 1000;

const DEFAULT_SORTS = [
  { key: "volume", label: "Volume" },
  { key: "liquidity", label: "Liquidity" },
  { key: "ending_soon", label: "Ending soon" },
  { key: "spread", label: "Tightest price gap" },
  { key: "entry_cost", label: "Lowest entry cost" }
];

// 投資額のスライダーは対数目盛り。$10〜$20,000 を1本のつまみで扱う。
const STAKE_MIN = 10;
const STAKE_MAX = 20000;
const STAKE_PRESETS = [100, 500, 1000, 5000];

// 板ラダーの既定表示段数。実際の板は70段返ることもあり、約定に関係ない
// 奥の段まで並べると詳細パネルが読めなくなる。
const LADDER_VISIBLE = 5;

const state = {
  markets: [],
  active: null,
  searchSequence: 0,
  detailSequence: 0,
  listing: { sort: "volume", minLiquidity: 0, sorts: DEFAULT_SORTS },
  // 市場を切り替えても持ち越す。同じ金額のまま一覧をたどると、
  // 損益分岐を市場間でそのまま比較できる。
  returns: { side: "yes", stake: 100, showAllLevels: false },
  chart: {
    tokenId: null,
    range: "1w",
    ranges: DEFAULT_RANGES,
    points: [],
    projection: null,
    hoverIndex: null
  }
};

const money = value => {
  const number = Number(value || 0);
  return new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1
  }).format(number);
};

const percent = value => `${(Number(value || 0) * 100).toFixed(1)}%`;

const clamp01 = value => Math.max(0, Math.min(1, Number(value) || 0));

const points = value =>
  `${value >= 0 ? "+" : "−"}${Math.abs(Number(value)).toFixed(1)}pt`;

const dollars = value => new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumFractionDigits: 2
}).format(Number(value) || 0);

const shareCount = value => new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2, maximumFractionDigits: 2
}).format(Number(value) || 0);

// 摩擦は単調なコスト。低い＝良いなので、走査しやすいよう2段階だけ色を振る。
// 判定は表示価格に対する割合で行う。ポイント差だけだと、大穴市場の
// 「+0.05pt だが実質2倍払っている」を安いと誤認する。
function entryTone(ratio) {
  if (ratio === null || ratio === undefined) return "";
  if (ratio < 0.03) return "low";
  if (ratio > 0.10) return "high";
  return "";
}

function renderEntryCost(market) {
  // ask が 1.00 以上の市場は摩擦が定義できない。差だけ見ると最安に見える。
  if (market.entry_cost === null || market.entry_cost === undefined) {
    return "<b>—</b><small>NO ENTRY</small>";
  }
  // 表示価格が0の市場では割合が出せない。ポイント差だけ示す。
  const ratio = market.entry_cost_ratio;
  const label = ratio === null || ratio === undefined
    ? "ENTRY COST"
    : `${percent(ratio)} OF PRICE`;
  return `<b>${points(market.entry_cost * 100)}</b><small>${label}</small>`;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return response.json();
}

function renderMarkets(markets) {
  const list = document.querySelector("#market-list");
  if (!markets.length) {
    list.innerHTML = '<p class="empty">No saved markets match your search.<br>Run a sync to refresh the database.</p>';
    return;
  }
  list.innerHTML = markets.map((market, index) => `
    <button class="market-row ${state.active === market.condition_id ? "active" : ""}"
            data-index="${index}">
      <div>
        <h3>${escapeHtml(market.question)}</h3>
        <div class="meta">ENDS ${formatDate(market.end_date_utc)}</div>
      </div>
      <div class="metric probability">
        <b>${percent(market.current_price)}</b><small>YES</small>
      </div>
      <div class="metric">
        <b>${money(market.volume)}</b><small>VOLUME</small>
      </div>
      <div class="metric entry ${entryTone(market.entry_cost_ratio)}">
        ${renderEntryCost(market)}
      </div>
    </button>
  `).join("");
  [...list.querySelectorAll(".market-row")].forEach(button => {
    button.addEventListener("click", () => selectMarket(markets[Number(button.dataset.index)]));
  });
}

async function selectMarket(market) {
  const sequence = ++state.detailSequence;
  state.active = market.condition_id;
  renderMarkets(state.markets);
  const panel = document.querySelector("#detail");
  panel.innerHTML = `
    <div class="placeholder">
      <span class="orb"></span>
      <h2>Loading market…</h2>
      <p>Reading saved market details.</p>
    </div>
  `;
  try {
    // 市場を跨いでも投資額とサイドを保つため、現在の指定を付けて取りに行く。
    const detail = await api(
      `/api/markets/${encodeURIComponent(market.condition_id)}`
      + `?side=${state.returns.side}&stake=${state.returns.stake}`
    );
    if (sequence !== state.detailSequence) return;
    renderDetail(detail);
    const yes = detail.outcomes[0];
    if (!yes) {
      renderHistoryState("No price token is available for this market.", "error");
      return;
    }
    state.chart.tokenId = yes.token_id;
    loadHistory(yes.token_id, sequence, state.chart.range);
  } catch (error) {
    if (sequence !== state.detailSequence) return;
    panel.innerHTML = `
      <div class="placeholder">
        <h2>Market unavailable</h2>
        <p>${escapeHtml(error.message)}</p>
      </div>
    `;
  }
}

function renderDetail(market) {
  const panel = document.querySelector("#detail");
  const analysis = market.analysis;
  const outcomes = market.outcomes.map(outcome => `
    <div class="outcome">
      <span>${escapeHtml(outcome.name)}</span>
      <strong>${percent(outcome.current_price)}</strong>
    </div>
  `).join("");
  panel.innerHTML = `
    <p class="eyebrow">MARKET DETAIL</p>
    <h2 class="detail-title">${escapeHtml(market.question)}</h2>
    <div class="chips">
      <span class="chip">${market.restricted ? "RESTRICTED MARKET" : "PUBLIC MARKET"}</span>
      <span class="chip">${market.fees_enabled ? "FEES ENABLED" : "NO FEE FLAG"}</span>
      <span class="chip">ENDS ${formatDate(market.end_date_utc)}</span>
    </div>
    <div class="outcomes">${outcomes}</div>
    <section class="readout">
      <p class="eyebrow">AT A GLANCE</p>
      <div class="condition-banner ${analysis.tone}">
        <span class="condition-icon">${toneIcon(analysis.tone)}</span>
        <div>
          <strong>${escapeHtml(analysis.headline)}</strong>
          <p>${escapeHtml(analysis.summary)}</p>
        </div>
      </div>
      <div class="crowd-view">
        <div>
          <span>WHAT THE MARKET SAYS</span>
          <strong>${escapeHtml(analysis.direction.status)}</strong>
          <small>${escapeHtml(analysis.direction.explanation)}</small>
        </div>
        <div class="crowd-gauge" aria-label="${analysis.direction.yes_percent}% Yes">
          <i style="width:${analysis.direction.yes_percent}%"></i>
        </div>
        <div class="gauge-labels"><span>NO</span><span>YES</span></div>
      </div>
    </section>
    ${renderReturnsShell(market)}
    <div class="chart-wrap">
      <div class="chart-head">
        <span>YES PROBABILITY</span>
        <span id="range-change" class="range-change"></span>
      </div>
      <div class="range-picker" id="range-picker" role="group" aria-label="Chart time range"></div>
      <div class="chart-canvas">
        <canvas id="price-chart"></canvas>
        <div id="chart-tooltip" class="chart-tooltip" hidden></div>
        <div id="history-state" class="history-state">
          <span class="spinner"></span> Loading history…
        </div>
      </div>
      <div class="chart-foot"><span id="history-count">LOADING…</span></div>
    </div>
    <section id="movement" class="movement"></section>
    <section class="market-readout">
      <p class="eyebrow">MARKET CONDITIONS</p>
      ${analysis.metrics.map(renderMetric).join("")}
      <p class="conditions-note">${escapeHtml(analysis.disclaimer)}</p>
    </section>
    <details class="market-facts">
      <summary>View market facts</summary>
      <div class="quality-row"><span>Volume</span><b>${money(market.volume)}</b></div>
      <div class="quality-row"><span>Liquidity</span><b>${money(market.liquidity)}</b></div>
      <div class="quality-row"><span>Bid / Ask</span><b>${percent(market.best_bid)} / ${percent(market.best_ask)}</b></div>
      <div class="quality-row"><span>Spread</span><b>${percent(market.spread)}</b></div>
    </details>
  `;
  state.chart.points = [];
  state.chart.projection = null;
  state.chart.hoverIndex = null;
  renderRangePicker();
  bindChartHover();
  drawChart();
  bindReturns();
  renderReturnsResult(market.returns);
}

/* ------------------------------------------------------------------ *
 * L0 リターン計算機
 *
 * 操作部と結果部を分けてある。結果だけを差し替えることで、入力中の
 * フォーカスが飛ばず、隣のcanvasチャートも作り直さずに済む。
 * 計算そのものはサーバの returns.py だけが持つ。式をここへ複製すると、
 * 食い違ったときに気づけない。
 * ------------------------------------------------------------------ */

function renderReturnsShell(market) {
  const yes = market.outcomes?.[0]?.name || "Yes";
  const no = market.outcomes?.[1]?.name || "No";
  return `
    <section class="returns">
      <p class="eyebrow">RETURN CALCULATOR</p>
      <div class="side-toggle" id="side-toggle">
        <button type="button" data-side="yes" aria-pressed="true">
          <strong>Buy ${escapeHtml(yes)}</strong>
        </button>
        <button type="button" data-side="no" aria-pressed="false">
          <strong>Buy ${escapeHtml(no)}</strong>
        </button>
      </div>
      <div class="stake-row">
        <label class="stake-field">
          <span>$</span>
          <input id="stake" type="text" inputmode="decimal"
                 value="${state.returns.stake}" aria-label="Stake in USDC">
        </label>
        <div class="presets" id="stake-presets">
          ${STAKE_PRESETS.map(value => `
            <button type="button" data-stake="${value}">
              ${value >= 1000 ? `${value / 1000}k` : value}
            </button>`).join("")}
        </div>
      </div>
      <input id="stake-range" type="range" min="0" max="1000"
             value="${stakeToSlider(state.returns.stake)}" aria-label="Stake slider">
      <div class="range-scale"><span>$${STAKE_MIN}</span><span>$${STAKE_MAX.toLocaleString("en-US")}</span></div>
      <div id="returns-result" aria-live="polite"></div>
    </section>
  `;
}

function sliderToStake(position) {
  const low = Math.log10(STAKE_MIN);
  const high = Math.log10(STAKE_MAX);
  const raw = Math.pow(10, low + (high - low) * (position / 1000));
  return raw < 100 ? Math.round(raw) : Math.round(raw / 10) * 10;
}

function stakeToSlider(value) {
  const low = Math.log10(STAKE_MIN);
  const high = Math.log10(STAKE_MAX);
  const bounded = Math.min(STAKE_MAX, Math.max(STAKE_MIN, Number(value) || STAKE_MIN));
  return Math.round(((Math.log10(bounded) - low) / (high - low)) * 1000);
}

function setStake(value, syncField = true) {
  // 数値にならない入力は下限へ寄せる。推測して勝手な額を入れない。
  const parsed = Number.parseFloat(String(value).replace(/[^0-9.]/g, ""));
  state.returns.stake = Number.isFinite(parsed)
    ? Math.min(STAKE_MAX, Math.max(STAKE_MIN, parsed))
    : STAKE_MIN;
  const field = document.querySelector("#stake");
  const slider = document.querySelector("#stake-range");
  if (field && syncField) field.value = String(state.returns.stake);
  if (slider) slider.value = String(stakeToSlider(state.returns.stake));
  loadReturns();
}

let returnsTimer;
function loadReturns() {
  clearTimeout(returnsTimer);
  const container = document.querySelector("#returns-result");
  if (container) container.classList.add("pending");
  // スライダーを掴んだまま動かすと入力が連続する。まとめてから1回だけ呼ぶ。
  returnsTimer = setTimeout(async () => {
    const sequence = state.detailSequence;
    const conditionId = state.active;
    if (!conditionId) return;
    try {
      const quote = await api(
        `/api/returns/${encodeURIComponent(conditionId)}`
        + `?side=${state.returns.side}&stake=${state.returns.stake}`
      );
      if (sequence !== state.detailSequence) return;
      renderReturnsResult(quote);
    } catch (error) {
      if (sequence !== state.detailSequence) return;
      renderReturnsResult({ available: false, reason: "error", detail: error.message });
    }
  }, 90);
}

function bindReturns() {
  const toggle = document.querySelector("#side-toggle");
  if (!toggle) return;
  toggle.addEventListener("click", event => {
    const button = event.target.closest("button[data-side]");
    if (!button) return;
    state.returns.side = button.dataset.side;
    [...toggle.querySelectorAll("button")].forEach(item => {
      item.setAttribute("aria-pressed", String(item.dataset.side === state.returns.side));
    });
    loadReturns();
  });
  document.querySelector("#stake-presets").addEventListener("click", event => {
    const button = event.target.closest("button[data-stake]");
    if (button) setStake(button.dataset.stake);
  });
  // 入力中は値を書き戻さない。書き戻すとカーソルが末尾へ飛ぶ。
  document.querySelector("#stake").addEventListener("input", event => {
    setStake(event.target.value, false);
  });
  document.querySelector("#stake").addEventListener("blur", () => {
    document.querySelector("#stake").value = String(state.returns.stake);
  });
  document.querySelector("#stake-range").addEventListener("input", event => {
    setStake(sliderToStake(Number(event.target.value)));
  });
}

function renderReturnsResult(quote) {
  const container = document.querySelector("#returns-result");
  if (!container) return;
  container.classList.remove("pending");

  if (!quote || !quote.available) {
    container.innerHTML = `
      <div class="returns-unavailable">
        <strong>Cannot be priced</strong>
        <p>${escapeHtml(quote?.detail || "No order book is stored for this market.")}</p>
      </div>`;
    return;
  }

  const gap = quote.gap_points;
  const quoted = clamp01(quote.quoted) * 100;
  const extra = Math.max(0, Math.min(100 - quoted, gap === null ? 0 : gap));

  container.innerHTML = `
    <div class="breakeven">
      <span class="breakeven-label">BREAK-EVEN PROBABILITY</span>
      <b class="breakeven-value">${(quote.breakeven * 100).toFixed(1)}%</b>
      <p class="breakeven-caption">
        Below this the position loses money on average. This is what you must
        believe — not the quoted price.
      </p>
    </div>
    ${gap === null ? "" : `
      <div class="gap">
        <div class="gap-head"><span>QUOTED VS REQUIRED</span><b>${points(gap)}</b></div>
        <div class="gap-track">
          <i class="gap-shown" style="width:${quoted}%"></i>
          <i class="gap-extra" style="left:${quoted}%;width:${extra}%"></i>
        </div>
        <div class="gap-legend">
          <span>quoted ${(quote.quoted * 100).toFixed(1)}%</span>
          <span>required <b>${(quote.breakeven * 100).toFixed(1)}%</b></span>
        </div>
      </div>`}
    <div class="scenarios">
      <div class="scenario win">
        <span class="label">IF IT RESOLVES THIS WAY</span>
        <strong>+${quote.win.percent.toFixed(1)}%</strong>
        <span class="sub">${dollars(quote.stake)} → ${dollars(quote.win.payout)}</span>
      </div>
      <div class="scenario lose">
        <span class="label">IF IT DOES NOT</span>
        <strong>−100%</strong>
        <span class="sub">${dollars(quote.stake)} → $0.00</span>
      </div>
    </div>
    <table class="breakdown">
      <tbody>
        <tr><th>Quoted probability<small>what the market displays</small></th>
            <td>${quote.breakdown.quoted === null ? "—" : quote.breakdown.quoted.toFixed(4)}</td></tr>
        <tr><th>Best ask<small>the price you can actually hit</small></th>
            <td>${quote.breakdown.best_ask.toFixed(4)}</td></tr>
        <tr><th>Average fill<small>after walking the book for this size</small></th>
            <td>${quote.breakdown.average_fill.toFixed(4)}</td></tr>
        <tr><th>Taker fee<small>${quote.fee_rate > 0
              ? `${(quote.fee_rate * 100).toFixed(0)}% × p × (1−p) · ${escapeHtml(quote.fee_type || "")}`
              : "this market charges no taker fee"}</small></th>
            <td class="add">${quote.breakdown.average_fee.toFixed(4)}</td></tr>
        <tr class="total"><th>Effective cost</th>
            <td>${quote.breakdown.effective_cost.toFixed(4)}</td></tr>
      </tbody>
    </table>
    ${renderLadder(quote)}
    <div class="return-facts">
      <div class="fact"><span class="label">SHARES</span>
        <strong>${shareCount(quote.shares)}</strong>
        <span class="sub">pays $1.00 each on resolution</span></div>
      <div class="fact"><span class="label">CAPITAL LOCKED</span>
        <strong>${quote.days_to_resolution === null ? "—" : `${quote.days_to_resolution} days`}</strong>
        <span class="sub">${quote.days_to_resolution === null
          ? "end date has passed or is unknown" : "until resolution"}</span></div>
      ${quote.annualised_percent === null ? "" : `
        <div class="fact wide"><span class="label">ANNUALISED · SIMPLE</span>
          <strong>+${quote.annualised_percent.toFixed(0)}%</strong>
          <span class="sub">assumes the same edge is available again on redeployment,
            which is not guaranteed</span></div>`}
    </div>
    <p class="returns-note">${escapeHtml(quote.note)}</p>
  `;
  bindLadderToggle(quote);
}

function renderLadder(quote) {
  const levels = quote.levels || [];
  const touched = levels.filter(level => level.taken > 0).length;
  // 消化した段＋その先2段までを既定で見せる。判断に効くのは「いま食っている段」と
  // 「次にいくら悪くなるか」だけ。
  const cut = state.returns.showAllLevels
    ? levels.length
    : Math.min(levels.length, Math.max(LADDER_VISIBLE, touched + 2));
  const hidden = levels.slice(cut);
  // 気配の刻みは市場ごとに違う（実測 0.01 と 0.001）。0.53×100 が
  // 53.000000000000007 になるため、剰余では判定できない。丸めて往復させる。
  const digits = levels.some(
    level => Math.abs(level.price - Number(level.price.toFixed(2))) > 1e-9
  ) ? 3 : 2;

  const rows = levels.slice(0, cut).map(level => {
    const ratio = level.size > 0 ? level.taken / level.size : 0;
    const used = level.taken > 0;
    return `
      <div class="ladder-row ${used ? "used" : ""}">
        <i class="ladder-fill" style="width:${(ratio * 100).toFixed(2)}%"></i>
        <b>${level.price.toFixed(digits)}</b>
        <span class="depth">${shareCount(level.size)} available</span>
        <span class="taken">${used ? shareCount(level.taken) : "—"}</span>
      </div>`;
  }).join("");

  // 畳んだ段の存在は必ず示す。板が浅いのか、単に隠しているのかを取り違えさせない。
  let toggle = "";
  if (hidden.length) {
    const deeper = hidden.reduce((sum, level) => sum + level.size, 0);
    toggle = `<button class="ladder-toggle" id="ladder-toggle" type="button">
      ▾ ${hidden.length} deeper level${hidden.length === 1 ? "" : "s"} · ${shareCount(deeper)} more shares
    </button>`;
  } else if (state.returns.showAllLevels && levels.length > LADDER_VISIBLE) {
    toggle = `<button class="ladder-toggle" id="ladder-toggle" type="button">▴ Show fewer levels</button>`;
  }

  const note = quote.book_exhausted
    ? `<p class="ladder-note warn">Book exhausted. ${dollars(quote.unfilled)} of the stake could not be filled at any stored level.</p>`
    : `<p class="ladder-note">Filled across ${touched} price level${touched === 1 ? "" : "s"}. Larger sizes walk deeper and raise the break-even.</p>`;

  return `<div class="ladder-wrap"><span class="ladder-title">ORDER BOOK CONSUMED</span>
    <div class="ladder">${rows}</div>${toggle}${note}</div>`;
}

function bindLadderToggle(quote) {
  const toggle = document.querySelector("#ladder-toggle");
  if (!toggle) return;
  toggle.addEventListener("click", () => {
    state.returns.showAllLevels = !state.returns.showAllLevels;
    renderReturnsResult(quote);
  });
}

function toneIcon(tone) {
  if (tone === "good") return "✓";
  if (tone === "caution") return "!";
  return "×";
}

function renderMetric(metric) {
  return `
    <article class="condition-row">
      <div class="condition-copy">
        <span>${escapeHtml(metric.label)}</span>
        <strong class="${metric.tone}">${escapeHtml(metric.status)}</strong>
      </div>
      <div class="condition-track" aria-label="${escapeHtml(metric.status)}">
        <i class="${metric.tone}" style="width:${metric.bar_percent}%"></i>
      </div>
      <p>${escapeHtml(metric.explanation)}</p>
    </article>
  `;
}

function renderRangePicker() {
  const picker = document.querySelector("#range-picker");
  if (!picker) return;
  picker.innerHTML = state.chart.ranges.map(range => `
    <button type="button" class="range-button ${range.key === state.chart.range ? "active" : ""}"
            data-range="${escapeHtml(range.key)}"
            aria-pressed="${range.key === state.chart.range}">${escapeHtml(range.label)}</button>
  `).join("");
  [...picker.querySelectorAll(".range-button")].forEach(button => {
    button.addEventListener("click", () => {
      const key = button.dataset.range;
      if (key === state.chart.range || !state.chart.tokenId) return;
      state.chart.range = key;
      renderRangePicker();
      loadHistory(state.chart.tokenId, state.detailSequence, key);
    });
  });
}

async function loadHistory(tokenId, sequence, rangeKey) {
  state.chart.hoverIndex = null;
  hideTooltip();
  renderHistoryState("Loading history…");
  const countLabel = document.querySelector("#history-count");
  if (countLabel) countLabel.textContent = "LOADING…";
  try {
    const result = await api(
      `/api/history/${encodeURIComponent(tokenId)}?range=${encodeURIComponent(rangeKey)}`
    );
    if (sequence !== state.detailSequence) return;
    state.chart.points = result.points || [];
    state.chart.range = result.range || rangeKey;
    renderRangePicker();
    renderHistoryState(state.chart.points.length ? "" : "No price history for this range.");
    if (countLabel) {
      countLabel.textContent = `${state.chart.points.length} OBSERVATIONS`;
    }
    renderRangeChange();
    renderMovement(result.movement);
    drawChart();
  } catch (error) {
    if (sequence !== state.detailSequence) return;
    state.chart.points = [];
    if (countLabel) countLabel.textContent = "UNAVAILABLE";
    renderRangeChange();
    renderMovement(null);
    drawChart();
    renderHistoryState(`Could not load history: ${error.message}`, "error");
  }
}

const signedPoints = value => {
  const number = Number(value);
  const sign = number > 0 ? "+" : number < 0 ? "−" : "";
  return `${sign}${Math.abs(number).toFixed(1)} pts`;
};

function renderMovement(movement) {
  const host = document.querySelector("#movement");
  if (!host) return;
  if (!movement || !movement.available) {
    host.innerHTML = "";
    return;
  }
  const rows = [
    ["Change over range", signedPoints(movement.change_points)],
    movement.change_24h_points == null
      ? null
      : ["Change, last 24h", signedPoints(movement.change_24h_points)],
    ["Range high", `${movement.high.percent.toFixed(1)}%`, formatTooltipTime(movement.high.timestamp_utc)],
    ["Range low", `${movement.low.percent.toFixed(1)}%`, formatTooltipTime(movement.low.timestamp_utc)],
    ["High to low", `${movement.range_points.toFixed(1)} pts`],
    movement.daily_swing_points == null
      ? null
      : ["Typical daily swing", `${movement.daily_swing_points.toFixed(1)} pts`],
    movement.largest_move == null
      ? null
      : [
          "Largest single move",
          signedPoints(movement.largest_move.points),
          `over ${movement.largest_move.hours}h`
        ]
  ].filter(Boolean);

  host.innerHTML = `
    <p class="eyebrow">PRICE MOVEMENT</p>
    <div class="movement-headline ${escapeHtml(movement.stability.level)}">
      <strong>${escapeHtml(movement.stability.status)}</strong>
      <p>${escapeHtml(movement.stability.explanation)}</p>
    </div>
    ${rows.map(([label, value, note]) => `
      <div class="quality-row">
        <span>${escapeHtml(label)}</span>
        <b>${escapeHtml(value)}${note ? `<small>${escapeHtml(note)}</small>` : ""}</b>
      </div>
    `).join("")}
    <p class="conditions-note">Observed from stored price history for the selected range. Past movement is not a forecast.</p>
  `;
}

function renderRangeChange() {
  const element = document.querySelector("#range-change");
  if (!element) return;
  const points = state.chart.points;
  if (points.length < 2) {
    element.textContent = "";
    element.className = "range-change";
    return;
  }
  const first = Number(points[0].price);
  const last = Number(points[points.length - 1].price);
  const delta = (last - first) * 100;
  const sign = delta > 0 ? "+" : delta < 0 ? "−" : "";
  element.textContent = `${sign}${Math.abs(delta).toFixed(1)} PTS THIS RANGE`;
  element.className = `range-change ${delta > 0 ? "up" : delta < 0 ? "down" : ""}`.trim();
}

function renderHistoryState(message, className = "") {
  const element = document.querySelector("#history-state");
  if (!element) return;
  element.className = `history-state ${className}`.trim();
  element.innerHTML = message
    ? `${className === "error" ? "" : '<span class="spinner"></span>'}${escapeHtml(message)}`
    : "";
}

const CHART_PADDING = { top: 14, right: 14, bottom: 26, left: 46 };
const CHART_INK = {
  grid: "#2b3748",
  axis: "#8a97a8",
  line: "#d9ff66",
  fillTop: "rgba(217, 255, 102, .22)",
  fillBottom: "rgba(217, 255, 102, 0)",
  crosshair: "#78e7dc"
};

function drawChart() {
  const canvas = document.querySelector("#price-chart");
  if (!canvas) return;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;

  const scale = window.devicePixelRatio || 1;
  canvas.width = width * scale;
  canvas.height = height * scale;
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  ctx.clearRect(0, 0, width, height);

  const plot = {
    left: CHART_PADDING.left,
    right: width - CHART_PADDING.right,
    top: CHART_PADDING.top,
    bottom: height - CHART_PADDING.bottom
  };
  // 確率は常に0〜100%で描く。データ範囲へ自動ズームすると、わずかな変動が
  // 大きな動きに見えてしまうため。
  drawProbabilityAxis(ctx, plot);

  const points = state.chart.points;
  if (!points.length) {
    state.chart.projection = null;
    return;
  }

  const firstTime = Number(points[0].timestamp_utc);
  const lastTime = Number(points[points.length - 1].timestamp_utc);
  const span = lastTime - firstTime || 1;
  const toX = time => plot.left + ((time - firstTime) / span) * (plot.right - plot.left);
  const toY = price => plot.bottom - clamp01(price) * (plot.bottom - plot.top);
  state.chart.projection = { plot, firstTime, lastTime, span, toX, toY };

  drawTimeAxis(ctx, plot, firstTime, lastTime);

  if (points.length === 1) {
    drawPointMarker(ctx, toX(firstTime), toY(points[0].price));
    return;
  }

  const path = new Path2D();
  points.forEach((point, index) => {
    const x = toX(Number(point.timestamp_utc));
    const y = toY(point.price);
    if (index) path.lineTo(x, y); else path.moveTo(x, y);
  });

  const area = new Path2D(path);
  area.lineTo(toX(lastTime), plot.bottom);
  area.lineTo(toX(firstTime), plot.bottom);
  area.closePath();
  const gradient = ctx.createLinearGradient(0, plot.top, 0, plot.bottom);
  gradient.addColorStop(0, CHART_INK.fillTop);
  gradient.addColorStop(1, CHART_INK.fillBottom);
  ctx.fillStyle = gradient;
  ctx.fill(area);

  ctx.strokeStyle = CHART_INK.line;
  ctx.lineWidth = 2.5;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke(path);

  drawPointMarker(ctx, toX(lastTime), toY(points[points.length - 1].price));

  if (state.chart.hoverIndex !== null) {
    const hovered = points[state.chart.hoverIndex];
    if (hovered) {
      const x = toX(Number(hovered.timestamp_utc));
      ctx.strokeStyle = CHART_INK.crosshair;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, plot.top);
      ctx.lineTo(x, plot.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      drawPointMarker(ctx, x, toY(hovered.price), CHART_INK.crosshair);
    }
  }
}

function drawProbabilityAxis(ctx, plot) {
  ctx.font = "10px monospace";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  [0, 0.25, 0.5, 0.75, 1].forEach(value => {
    const y = plot.bottom - value * (plot.bottom - plot.top);
    ctx.strokeStyle = CHART_INK.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(plot.left, y);
    ctx.lineTo(plot.right, y);
    ctx.stroke();
    ctx.fillStyle = CHART_INK.axis;
    ctx.fillText(`${value * 100}%`, plot.left - 8, y);
  });
}

// 目盛り候補。5分〜1年まで、人が読みやすい区切りだけを並べている。
const AXIS_STEPS = [
  5 * 60, 15 * 60, 30 * 60, 3600, 3 * 3600, 6 * 3600, 12 * 3600,
  86400, 2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400, 60 * 86400,
  90 * 86400, 365 * 86400
];

function axisTicks(firstTime, lastTime, target = 4) {
  const span = lastTime - firstTime;
  if (span <= 0) return [firstTime];
  // 目標本数に最も近い刻みを選ぶ。本数の比は対数で比べる。差で比べると
  // 「多すぎる」側だけが強く罰せられ、目盛りが2本まで減ってしまう。
  const distance = candidate => Math.abs(Math.log(span / candidate / target));
  const step = AXIS_STEPS.reduce((best, candidate) =>
    distance(candidate) < distance(best) ? candidate : best);

  // 等間隔に置くと「7月19日 / 7月21日 / 7月22日」のような半端な日付になる。
  // 日付や時刻の境界へ合わせてから刻む。
  const anchor = new Date(firstTime * 1000);
  if (step >= 86400) anchor.setHours(0, 0, 0, 0); else anchor.setMinutes(0, 0, 0);

  const ticks = [];
  let time = Math.floor(anchor.getTime() / 1000);
  while (time < firstTime) time += step;
  for (; time <= lastTime; time += step) ticks.push(time);
  return ticks.length ? ticks : [firstTime, lastTime];
}

function drawTimeAxis(ctx, plot, firstTime, lastTime) {
  ctx.font = "10px monospace";
  ctx.textBaseline = "top";
  ctx.fillStyle = CHART_INK.axis;
  const span = lastTime - firstTime || 1;
  axisTicks(firstTime, lastTime).forEach(time => {
    const x = plot.left + ((time - firstTime) / span) * (plot.right - plot.left);
    // 端のラベルがプロット領域からはみ出さないように寄せる。
    ctx.textAlign = x < plot.left + 26 ? "left"
      : x > plot.right - 26 ? "right" : "center";
    ctx.fillText(formatAxisTime(time), x, plot.bottom + 8);
  });
}

function drawPointMarker(ctx, x, y, color = CHART_INK.line) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(x, y, 3.5, 0, Math.PI * 2);
  ctx.fill();
}

function bindChartHover() {
  const canvas = document.querySelector("#price-chart");
  if (!canvas) return;
  canvas.addEventListener("mousemove", event => {
    const index = nearestPointIndex(canvas, event.clientX);
    if (index === state.chart.hoverIndex) return;
    state.chart.hoverIndex = index;
    drawChart();
    showTooltip(index);
  });
  canvas.addEventListener("mouseleave", () => {
    if (state.chart.hoverIndex === null) return;
    state.chart.hoverIndex = null;
    drawChart();
    hideTooltip();
  });
}

function nearestPointIndex(canvas, clientX) {
  const projection = state.chart.projection;
  const points = state.chart.points;
  if (!projection || !points.length) return null;
  const bounds = canvas.getBoundingClientRect();
  const x = clientX - bounds.left;
  if (x < projection.plot.left - 4 || x > projection.plot.right + 4) return null;
  let best = 0;
  let bestDistance = Infinity;
  points.forEach((point, index) => {
    const distance = Math.abs(projection.toX(Number(point.timestamp_utc)) - x);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = index;
    }
  });
  return best;
}

function showTooltip(index) {
  const tooltip = document.querySelector("#chart-tooltip");
  const projection = state.chart.projection;
  const point = index === null ? null : state.chart.points[index];
  if (!tooltip || !projection || !point) {
    hideTooltip();
    return;
  }
  tooltip.hidden = false;
  tooltip.innerHTML = `
    <strong>${percent(point.price)}</strong>
    <span>${escapeHtml(formatTooltipTime(Number(point.timestamp_utc)))}</span>
  `;
  const x = projection.toX(Number(point.timestamp_utc));
  const y = projection.toY(point.price);
  // 右端では左側へ寄せて、ツールチップがチャートからはみ出さないようにする。
  const flip = x > (projection.plot.left + projection.plot.right) / 2;
  tooltip.style.left = `${x + (flip ? -12 : 12)}px`;
  tooltip.style.top = `${y}px`;
  tooltip.style.transform = `translate(${flip ? "-100%" : "0"}, -50%)`;
}

function hideTooltip() {
  const tooltip = document.querySelector("#chart-tooltip");
  if (tooltip) tooltip.hidden = true;
}

function formatAxisTime(seconds) {
  const date = new Date(seconds * 1000);
  if (state.chart.range === "1d") {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  if (state.chart.range === "max") {
    return date.toLocaleDateString([], { month: "short", year: "2-digit" });
  }
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function formatTooltipTime(seconds) {
  return new Date(seconds * 1000).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
  });
}

function formatDate(value) {
  if (!value) return "UNKNOWN";
  return new Date(value).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }).toUpperCase();
}

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value || "";
  return node.innerHTML;
}

function renderSortOptions() {
  const select = document.querySelector("#sort");
  select.innerHTML = state.listing.sorts.map(sort => `
    <option value="${escapeHtml(sort.key)}"${sort.key === state.listing.sort ? " selected" : ""}>
      ${escapeHtml(sort.label)}
    </option>
  `).join("");
}

function bindListingControls() {
  document.querySelector("#sort").addEventListener("change", event => {
    state.listing.sort = event.target.value;
    searchMarkets();
  });
  document.querySelector("#hide-thin").addEventListener("change", event => {
    state.listing.minLiquidity = event.target.checked ? THIN_LIQUIDITY : 0;
    searchMarkets();
  });
}

async function loadOverview() {
  // 統計や選択肢が取れなくても市場一覧は表示する。既定値で動作する。
  try {
    const [stats, options] = await Promise.all([
      api("/api/stats"), api("/api/options")
    ]);
    document.querySelector("#market-count").textContent = Number(stats.markets).toLocaleString();
    document.querySelector("#point-count").textContent = Number(stats.price_points).toLocaleString();
    document.querySelector("#last-sync").textContent = stats.last_sync
      ? new Date(stats.last_sync).toLocaleString() : "Not synced";
    if (options.version) {
      document.querySelector("#app-version").textContent = `ver.${options.version}`;
    }
    if (Array.isArray(options.sorts) && options.sorts.length) {
      state.listing.sorts = options.sorts;
      renderSortOptions();
    }
    if (Array.isArray(options.ranges) && options.ranges.length) {
      state.chart.ranges = options.ranges;
    }
  } catch (error) {
    document.querySelector("#last-sync").textContent = "Unavailable";
  }
}

function boot() {
  renderSortOptions();
  bindListingControls();
  loadOverview();
  searchMarkets();
}

function setSearchStatus(message, className = "") {
  const status = document.querySelector("#search-status");
  status.textContent = message;
  status.className = `search-status ${className}`.trim();
}

async function searchMarkets() {
  const query = document.querySelector("#search").value.trim();
  const sequence = ++state.searchSequence;
  setSearchStatus(query ? "SEARCHING LOCAL DATABASE…" : "LOADING SAVED MARKETS…");
  const params = new URLSearchParams({
    q: query,
    sort: state.listing.sort,
    min_liquidity: String(state.listing.minLiquidity)
  });
  const filtered = state.listing.minLiquidity > 0 ? " — THIN MARKETS HIDDEN" : "";
  try {
    const result = await api(`/api/search?${params}`);
    if (sequence !== state.searchSequence) return;
    state.markets = result.markets;
    renderMarkets(state.markets);
    if (result.external_error) {
      setSearchStatus("POLYMARKET SEARCH IS TEMPORARILY UNAVAILABLE", "error");
    } else if (result.source === "polymarket") {
      setSearchStatus(`${state.markets.length} RESULTS FROM POLYMARKET — SAVED LOCALLY`, "remote");
    } else if (query) {
      setSearchStatus(`${state.markets.length} RESULTS FROM LOCAL DATABASE${filtered}`);
    } else {
      setSearchStatus(`${state.markets.length} SAVED MARKETS${filtered}`);
    }
  } catch (error) {
    if (sequence !== state.searchSequence) return;
    state.markets = [];
    renderMarkets([]);
    setSearchStatus(error.message, "error");
  }
}

let searchTimer;
document.querySelector("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  setSearchStatus("WAITING FOR INPUT…");
  searchTimer = setTimeout(searchMarkets, 350);
});

// 再描画だけを行う。以前は市場詳細と履歴をAPIから取り直していた。
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    state.chart.hoverIndex = null;
    hideTooltip();
    drawChart();
  }, 120);
});

boot();
