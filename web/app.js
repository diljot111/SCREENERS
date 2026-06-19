/* Stock Screener dashboard front-end.
   Talks to the stdlib web_server.py JSON API, renders candlestick charts with
   indicator overlays (TradingView lightweight-charts), filters by signal, and
   shows the notifications that have been sent. */

const COLORS = {
  up: "#26a69a", down: "#ef5350",
  ema: "#f5a623", bb: "#5b8def", bbFill: "rgba(91,141,239,.08)", vwap: "#c061ff",
};

const state = {
  filter: "all",
  search: "",
  show: { ema: true, bb: true, vwap: true },
  cards: [],          // raw payloads from /api/dashboard
  charts: new Map(),  // symbol -> { chart, series:{} }
};

/* ----------------------------------------------------------------- helpers */
const $ = (sel) => document.querySelector(sel);
const fmt = (v) => (v == null ? "–" : "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).error || detail; } catch (_) {}
    throw new Error(detail);
  }
  return r.json();
}

/* Try the live API first; fall back to a static JSON snapshot (Vercel/static
   hosting has no Python backend, so it serves pre-exported files in web/data/). */
async function apiOrStatic(apiUrl, staticUrl) {
  try {
    const r = await fetch(apiUrl, { cache: "no-store" });
    if (r.ok) return await r.json();
  } catch (_) { /* backend not present — fall through to static */ }
  return getJSON(staticUrl);
}

/* client-side mirror of web_server._passes_filter so one snapshot serves all filters */
function passesFilter(sig, flt) {
  if (!flt || flt === "all") return true;
  if (!sig) return false;
  if (flt === "ready") return !!sig.ready;
  if (flt === "ema") return !!sig.ema_crossed_bb_middle;
  if (flt === "vwap") return !!sig.vwap_crossed_bb_upper;
  return true;
}

/* line data with nulls (warm-up rows) stripped — charts need contiguous points */
function lineData(series, key) {
  const out = [];
  for (const row of series) {
    if (row[key] != null) out.push({ time: row.date, value: row[key] });
  }
  return out;
}
function candleData(series) {
  return series
    .filter((r) => r.open != null && r.close != null)
    .map((r) => ({ time: r.date, open: r.open, high: r.high, low: r.low, close: r.close }));
}

/* --------------------------------------------------------------- rendering */
function cardMatchesSearch(card) {
  if (!state.search) return true;
  const q = state.search.toLowerCase();
  return card.symbol.toLowerCase().includes(q) || (card.name || "").toLowerCase().includes(q);
}

function buildCard(card) {
  const sig = card.signal || {};
  const ready = !!sig.ready;
  const last = card.series[card.series.length - 1] || {};
  const prev = card.series[card.series.length - 2] || {};
  const chg = last.close != null && prev.close != null ? last.close - prev.close : null;
  const chgPct = chg != null && prev.close ? (chg / prev.close) * 100 : null;
  const dir = chg == null ? "" : chg >= 0 ? "up" : "down";

  const el = document.createElement("div");
  el.className = "card" + (ready ? " is-ready" : "");
  el.dataset.symbol = card.symbol;
  el.innerHTML = `
    <div class="card-head">
      <div class="card-title">
        <span class="card-symbol">${card.symbol}</span>
        <span class="card-name">${card.name || ""}</span>
      </div>
      <div class="card-right">
        <div class="card-price">${fmt(last.close)}</div>
        ${ready
          ? `<span class="ready-badge">✅ READY TO BUY</span>`
          : `<span class="watch-badge">${signalHint(sig)}</span>`}
      </div>
    </div>
    <div class="chart" id="chart-${card.symbol}"></div>
    <div class="card-foot">
      <div class="metric ${dir}"><span class="m-lbl">Change</span><span class="m-val">${
        chg == null ? "–" : (chg >= 0 ? "+" : "") + chgPct.toFixed(2) + "%"}</span></div>
      <div class="metric"><span class="m-lbl">9 EMA</span><span class="m-val">${fmt(last.ema9)}</span></div>
      <div class="metric"><span class="m-lbl">BB Up</span><span class="m-val">${fmt(last.bb_upper)}</span></div>
      <div class="metric"><span class="m-lbl">VWAP</span><span class="m-val">${fmt(last.vwap)}</span></div>
    </div>`;
  return el;
}

function signalHint(sig) {
  if (!sig || sig.ready === undefined) return "Insufficient data";
  if (sig.ema_crossed_bb_middle) return "9 EMA crossed ▲";
  if (sig.vwap_crossed_bb_upper) return "VWAP crossed ▲";
  if (sig.ema_above_bb_middle && sig.vwap_above_bb_upper) return "Both above bands";
  return "Watching";
}

function drawChart(card) {
  const container = document.getElementById(`chart-${card.symbol}`);
  if (!container || !window.LightweightCharts) return;

  const chart = LightweightCharts.createChart(container, {
    layout: { background: { color: "transparent" }, textColor: "#8a94a7" },
    grid: { vertLines: { color: "#1b2230" }, horzLines: { color: "#1b2230" } },
    rightPriceScale: { borderColor: "#232b3a" },
    timeScale: { borderColor: "#232b3a", timeVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    height: 240,
    autoSize: true,
  });

  const candle = chart.addCandlestickSeries({
    upColor: COLORS.up, downColor: COLORS.down,
    borderUpColor: COLORS.up, borderDownColor: COLORS.down,
    wickUpColor: COLORS.up, wickDownColor: COLORS.down,
  });
  candle.setData(candleData(card.series));

  const series = { candle };

  if (state.show.bb) {
    series.bbUpper = chart.addLineSeries({ color: COLORS.bb, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    series.bbMid = chart.addLineSeries({ color: COLORS.bb, lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false });
    series.bbLower = chart.addLineSeries({ color: COLORS.bb, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    series.bbUpper.setData(lineData(card.series, "bb_upper"));
    series.bbMid.setData(lineData(card.series, "bb_middle"));
    series.bbLower.setData(lineData(card.series, "bb_lower"));
  }
  if (state.show.ema) {
    series.ema = chart.addLineSeries({ color: COLORS.ema, lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    series.ema.setData(lineData(card.series, "ema9"));
  }
  if (state.show.vwap) {
    series.vwap = chart.addLineSeries({ color: COLORS.vwap, lineWidth: 2, lineStyle: 1, priceLineVisible: false, lastValueVisible: false });
    series.vwap.setData(lineData(card.series, "vwap"));
  }

  chart.timeScale().fitContent();
  state.charts.set(card.symbol, { chart, series });
}

function disposeCharts() {
  for (const { chart } of state.charts.values()) {
    try { chart.remove(); } catch (_) {}
  }
  state.charts.clear();
}

function render() {
  const grid = $("#grid");
  disposeCharts();
  grid.innerHTML = "";

  const visible = state.cards.filter(
    (c) => passesFilter(c.signal, state.filter) && cardMatchesSearch(c)
  );
  // stats reflect the full dataset / current view
  $("#statReady").textContent = state.cards.filter((c) => c.signal && c.signal.ready).length;
  $("#statShown").textContent = visible.length;
  if (!visible.length) {
    grid.innerHTML = `<div class="empty">No stocks match this filter.${
      state.filter !== "all" ? " Try “All”." : " Run the screener or seed demo data."}</div>`;
    return;
  }
  for (const card of visible) grid.appendChild(buildCard(card));
  // draw after the DOM nodes exist & have width
  requestAnimationFrame(() => visible.forEach(drawChart));
}

/* ------------------------------------------------------------------- data */
async function loadDashboard() {
  $("#grid").innerHTML = `<div class="empty">Loading charts…</div>`;
  try {
    // fetch the full set once (filter=all); filtering happens client-side so the
    // same payload / static snapshot serves every filter button.
    const data = await apiOrStatic(`/api/dashboard?filter=all&limit=10000`, `data/dashboard.json`);
    state.cards = data.cards || [];
    $("#statSymbols").textContent = data.total_symbols ?? state.cards.length;
    render();
  } catch (e) {
    $("#grid").innerHTML = `<div class="empty">⚠️ Could not load data: ${e.message}<br/>Run the server or export a static snapshot.</div>`;
  }
  loadStats();
}

async function loadStats() {
  try {
    const s = await apiOrStatic("/api/stats", "data/stats.json");
    $("#statAlerts").textContent = s.messages_sent ?? 0;
  } catch (_) {}
}

async function loadAlerts() {
  const list = $("#notifList");
  list.innerHTML = `<div class="notif-empty">Loading…</div>`;
  try {
    const { alerts } = await apiOrStatic("/api/alerts?limit=80", "data/alerts.json");
    $("#notifBadge").textContent = alerts.length;
    if (!alerts.length) {
      list.innerHTML = `<div class="notif-empty">No notifications yet.<br/>Alerts appear here when a stock signals.</div>`;
      return;
    }
    list.innerHTML = alerts.map(renderNotif).join("");
  } catch (e) {
    list.innerHTML = `<div class="notif-empty">⚠️ ${e.message}</div>`;
  }
}

function renderNotif(a) {
  const sent = a.message_sent ? `<span class="sent-pill sent">SENT</span>` : `<span class="sent-pill pending">PENDING</span>`;
  return `<div class="notif-item">
    <div class="ni-top"><span class="ni-sym">🟢 ${a.symbol}</span> ${sent}</div>
    <div class="ni-row">${a.name || ""}</div>
    <div class="ni-row">${fmt(a.price)} · 9EMA ${fmt(a.ema9)} · VWAP ${fmt(a.vwap)}</div>
    <div class="ni-top"><span class="ni-time">${a.date} ${a.time || ""}</span></div>
  </div>`;
}

/* ----------------------------------------------------------------- events */
function wire() {
  document.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.filter = btn.dataset.filter;
      render();
    });
  });

  $("#search").addEventListener("input", (e) => { state.search = e.target.value.trim(); render(); });

  const tg = (id, key) => $(id).addEventListener("change", (e) => { state.show[key] = e.target.checked; render(); });
  tg("#tgEma", "ema"); tg("#tgBb", "bb"); tg("#tgVwap", "vwap");

  $("#refreshBtn").addEventListener("click", loadDashboard);

  // drawer
  const openDrawer = () => { $("#drawer").classList.add("open"); $("#overlay").classList.add("open"); loadAlerts(); };
  const closeDrawer = () => { $("#drawer").classList.remove("open"); $("#overlay").classList.remove("open"); };
  $("#notifBtn").addEventListener("click", openDrawer);
  $("#drawerClose").addEventListener("click", closeDrawer);
  $("#overlay").addEventListener("click", closeDrawer);

  $("#sendTestBtn").addEventListener("click", async () => {
    const res = $("#testResult");
    res.textContent = "Sending…"; res.className = "test-result";
    try {
      const r = await getJSON("/api/send-test", { method: "POST" });
      res.textContent = r.ok ? "✅ Sent to WhatsApp." : `❌ ${r.detail}`;
      res.className = "test-result " + (r.ok ? "ok" : "err");
    } catch (e) {
      res.textContent = "❌ " + e.message; res.className = "test-result err";
    }
  });

  // keep charts sized on window resize (autoSize handles most, this is a nudge)
  window.addEventListener("resize", () => {
    for (const { chart } of state.charts.values()) chart.timeScale().fitContent();
  });
}

wire();
loadDashboard();
loadAlerts();
// light auto-refresh every 60s so live scans show up without a manual reload
setInterval(loadDashboard, 60000);
