"use strict";

// ---------- state ----------
const S = {
  quotes: {},
  watchlist: [],
  indexes: [],
  market: {},
  account: { enabled: false },
  accounts: [],
  accountId: localStorage.getItem("cc.account"),
  botLogs: {},
  push: { supported: false, endpoint: null, accounts: null },
  signals: { loading: true, signals: [], scores: {} },
  alerts: [],
  bot: {},
  settings: {},
  config: {},
  selected: null,
  range: "1D",
  chart: null,
  alertFilter: "all",
  posTab: "positions",
  unread: 0,
  lastSeenAlertId: Number(localStorage.getItem("cc.lastSeenAlert") || 0),
  popups: localStorage.getItem("cc.popups") !== "off",
  sound: localStorage.getItem("cc.sound") !== "off",
  focus: null,
  history: {},
};

const $ = (sel) => document.querySelector(sel);
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------- formatting ----------
const money = (v, d = 2) => v == null || isNaN(v) ? "—" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const signedMoney = (v) => (v > 0 ? "+" : "") + money(v);
const am = (v, d = 2) => { const c = S.account.currency_symbol || "$"; return v == null || isNaN(v) ? "—" : (v < 0 ? "-" : "") + c + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }); };
const signedAm = (v) => (v > 0 ? "+" : "") + am(v);
const isT212 = () => S.config.broker === "trading212";
const brokerLabel = () => S.account.label || (isT212() ? "Trading 212 practice account" : "Alpaca paper account");
const curView = () => S.accounts.find((v) => v.id === S.accountId) || S.accounts[0];
const acctName = (id) => S.accounts.find((v) => v.id === id)?.name || "";
const ADVICE = { buy: ["BUY", "up"], sell: ["SELL", "down"], hold: ["HOLD", "hold"], watch: ["WATCH", "neutral"] };
function adviceBadge(adv) {
  if (!adv) return "";
  const [label, c] = ADVICE[adv.action];
  return `<span class="badge advice ${c}" title="${esc(adv.headline + "\n" + adv.reasons.join("\n"))}">${label}</span>`;
}
const pct = (v, d = 2) => v == null || isNaN(v) ? "—" : (v > 0 ? "+" : "") + v.toFixed(d) + "%";
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const compact = (v) => v == null ? "—" : Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(v);
function ago(ts) {
  const s = Math.max(0, (Date.now() - ts) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return new Date(ts).toLocaleDateString();
}
function dayAgo(iso) {
  const days = Math.round((Date.now() - new Date(iso + "T12:00:00").getTime()) / 86400000);
  return days <= 0 ? "today" : days === 1 ? "yesterday" : days + " days ago";
}

// ---------- API ----------
async function api(path, method = "GET", body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Command-Center": "1" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && S.config.auth !== false) { location.href = "/login"; throw new Error("Please sign in"); }
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(", ") : data.detail;
    throw new Error(detail || res.statusText);
  }
  return data;
}

// ---------- icons ----------
const ICONS = {
  move: '<svg viewBox="0 0 24 24"><path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/></svg>',
  sma: '<svg viewBox="0 0 24 24"><path d="M3 12c4 0 5-6 9-6s5 12 9 12"/><path d="M3 18c4 0 6-8 18-10"/></svg>',
  big_trader: '<svg viewBox="0 0 24 24"><path d="M12 2l3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1z"/></svg>',
  price: '<svg viewBox="0 0 24 24"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/></svg>',
  bot: '<svg viewBox="0 0 24 24"><rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 14h.01M15 14h.01"/></svg>',
  order: '<svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg>',
  system: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/></svg>',
  bell: '<svg viewBox="0 0 24 24"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
  expand: '<svg viewBox="0 0 24 24"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>',
  collapse: '<svg viewBox="0 0 24 24"><path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/></svg>',
  popout: '<svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>',
};

// ---------- sound + notifications ----------
let audioCtx;
function beep(level) {
  if (!S.sound) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const tones = { success: [660, 880], danger: [520, 330], warning: [600, 600], info: [700, 700] }[level] || [700, 700];
    tones.forEach((f, i) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = "sine"; o.frequency.value = f;
      const t = audioCtx.currentTime + i * 0.16;
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.18, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.14);
      o.connect(g).connect(audioCtx.destination); o.start(t); o.stop(t + 0.15);
    });
  } catch (_) { /* audio unavailable */ }
}

function desktopPopup(alert) {
  if (!S.popups || !("Notification" in window) || Notification.permission !== "granted") return;
  if (S.push.endpoint && pushWants(alert.account)) return; // this device gets it as a push notification
  const n = new Notification(alert.title, { body: alert.message, tag: "cc-" + alert.id, icon: "/static/icons/icon-192.png" });
  n.onclick = () => { window.focus(); if (alert.symbol && S.quotes[alert.symbol]) selectSymbol(alert.symbol); n.close(); };
}

function toast(alert, ms = 7000) {
  const t = el(`<div class="toast ${esc(alert.level || "info")}">
      <div><div class="t">${esc(alert.title)}</div><div class="m">${esc(alert.message || "")}</div></div>
      <button class="x" aria-label="Dismiss">&times;</button></div>`);
  const close = () => { t.classList.add("out"); setTimeout(() => t.remove(), 300); };
  t.querySelector(".x").onclick = (e) => { e.stopPropagation(); close(); };
  t.onclick = () => { if (alert.symbol && S.quotes[alert.symbol]) selectSymbol(alert.symbol); close(); };
  $("#toasts").prepend(t);
  while ($("#toasts").children.length > 4) $("#toasts").lastChild.remove();
  setTimeout(close, ms);
}

function notifyError(message) { toast({ level: "danger", title: "Something went wrong", message }, 6000); }

function updateNotifyUi() {
  const supported = "Notification" in window;
  const granted = supported && Notification.permission === "granted";
  $("#btn-notify").classList.toggle("active", granted && S.popups);
  $("#btn-notify").title = !supported ? "Pop-ups not supported in this browser" : granted ? (S.popups ? "Pop-up alerts on (click to turn off)" : "Pop-up alerts off (click to turn on)") : "Turn on pop-up alerts";
  const needsInstall = !supported && isIos() && !isStandalone();
  const showBanner = (needsInstall || (supported && Notification.permission === "default")) && localStorage.getItem("cc.bannerDismissed") !== "1";
  if (needsInstall) {
    $("#notify-banner strong").textContent = "Get alerts on this iPhone";
    $("#notify-banner .muted").textContent = "Add the command center to your home screen, then turn on notifications to get alerts even when it's closed.";
    $("#banner-enable").textContent = "Show me how";
  }
  $("#notify-banner").classList.toggle("hidden", !showBanner);
  $("#btn-sound").classList.toggle("active", S.sound);
  $("#btn-sound").classList.toggle("off", !S.sound);
}

const isIos = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

function installHelp() {
  openModal("Install the app on your phone", `
    <p style="margin-top:0">To get notifications on ${isIos() ? "iPhone" : "your phone"} even when the app is closed, add it to your home screen first:</p>
    ${isIos()
      ? `<ol><li>Open this page in <b>Safari</b></li><li>Tap the <b>Share</b> button (square with an arrow)</li><li>Tap <b>Add to Home Screen</b>, then <b>Add</b></li><li>Open <b>Command Center</b> from your home screen and tap the bell to turn on notifications</li></ol>`
      : `<ol><li>Open this page in <b>Chrome</b></li><li>Tap the <b>&#8942;</b> menu &rarr; <b>Add to Home screen</b> (or <b>Install app</b>)</li><li>Open <b>Command Center</b> from your home screen and tap the bell to turn on notifications</li></ol>`}
    <p class="muted small">Do this on each phone you want alerts on. Each phone can choose which accounts it hears about in Alert settings.</p>`,
    [["OK", "primary", () => {}]]);
}

async function enablePopups() {
  if (!("Notification" in window)) {
    if (isIos() && !isStandalone()) return installHelp();
    return notifyError("This browser doesn't support pop-ups. You'll still get in-page alerts.");
  }
  const result = await Notification.requestPermission();
  S.popups = result === "granted";
  localStorage.setItem("cc.popups", S.popups ? "on" : "off");
  updateNotifyUi();
  if (!S.popups) return notifyError("Notifications are blocked. Allow notifications for this site in your browser or phone settings.");
  const pushed = await subscribePush(S.push.accounts).catch((e) => { console.warn(e); return false; });
  toast({ level: "success", title: "Notifications are on", message: pushed
    ? "This device now gets alerts even when the command center is closed."
    : "You'll get a pop-up for every alert while the command center is open." });
}

// ---------- push notifications (work while the app is closed) ----------
const pushWants = (account) => !account || S.push.accounts == null || S.push.accounts.includes(account);
function b64ToBytes(b64) {
  const s = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}
async function initPush() {
  S.push.supported = "serviceWorker" in navigator && "PushManager" in window && window.isSecureContext;
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  const reg = await navigator.serviceWorker.register("/sw.js");
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data?.type !== "open") return;
    const sym = new URL(e.data.url).searchParams.get("symbol");
    if (sym && S.quotes[sym]) selectSymbol(sym);
  });
  if (!S.push.supported) return;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return;
  const dev = await api("/api/push/device", "POST", { endpoint: sub.endpoint });
  if (dev.subscribed) Object.assign(S.push, { endpoint: sub.endpoint, accounts: dev.accounts });
  else await subscribePush(null);
}
function serviceWorkerReady() {
  const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error("The app's background helper didn't start. Reload the page and try again.")), 10000));
  return Promise.race([navigator.serviceWorker.ready, timeout]);
}
async function subscribePush(accounts) {
  if (!S.push.supported || !S.config.push_key) return false;
  const reg = await serviceWorkerReady();
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(S.config.push_key) });
  const device = `${isIos() ? "iPhone/iPad" : /android/i.test(navigator.userAgent) ? "Android" : "Computer"} · ${new Date().toLocaleDateString()}`;
  const res = await api("/api/push/subscribe", "POST", { subscription: sub.toJSON(), accounts, device });
  Object.assign(S.push, { endpoint: sub.endpoint, accounts: res.accounts });
  updateNotifyUi();
  return true;
}
async function unsubscribePush() {
  const reg = await serviceWorkerReady();
  const sub = await reg.pushManager.getSubscription();
  if (sub) { await api("/api/push/unsubscribe", "POST", { endpoint: sub.endpoint }).catch(() => {}); await sub.unsubscribe(); }
  Object.assign(S.push, { endpoint: null, accounts: null });
  updateNotifyUi();
}

// ---------- rendering: top bar ----------
function renderMarket() {
  const m = S.market;
  const pill = $("#market-pill");
  const labels = { regular: ["Market open", "open"], pre: ["Pre-market", "ext"], post: ["After hours", "ext"], closed: ["Market closed", "closed"] };
  const [text, c] = labels[m.session] || ["—", ""];
  pill.textContent = text;
  pill.className = "pill " + c;
  const live = $("#live");
  const streaming = S.connected && m.stream_connected;
  live.className = "live " + (S.connected ? (streaming ? "on" : "") : "off");
  live.querySelector("span").textContent = !S.connected ? "Reconnecting…" : streaming ? "Live" : "Live (polling)";
  renderCountdown();
}

function renderCountdown() {
  const m = S.market;
  const target = m.is_open ? m.next_close : m.next_open;
  if (!target) { $("#market-countdown").textContent = ""; return; }
  let s = Math.max(0, (new Date(target) - Date.now()) / 1000);
  const h = Math.floor(s / 3600), mnt = Math.floor((s % 3600) / 60);
  const span = h >= 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : `${h}h ${String(mnt).padStart(2, "0")}m`;
  $("#market-countdown").textContent = (m.is_open ? "closes in " : "opens in ") + span;
}

function renderTape() {
  $("#tape").innerHTML = S.indexes.map((s) => {
    const q = S.quotes[s];
    if (!q) return "";
    return `<span class="tape-item" data-sym="${s}"><b>${s}</b><span class="num">${money(q.price)}</span><span class="num ${cls(q.change_pct)}">${pct(q.change_pct)}</span></span>`;
  }).join("");
}

// ---------- KPIs ----------
function renderKpis() {
  const a = S.account;
  const bot = S.bot;
  const botCard = `<div class="kpi"><div class="label">Trading bot</div><div class="value ${bot.running ? "up" : ""}">${bot.running ? "Running" : "Stopped"}</div>
      <div class="sub">${bot.running ? "since " + new Date(bot.started_at).toLocaleTimeString() : bot.can_start ? "Ready to start" : isT212() ? "Needs a Trading 212 practice account" : "Needs Alpaca paper keys"}</div></div>`;
  if (!a.enabled && isT212()) {
    $("#kpis").innerHTML = `<div class="kpi connect">
        <svg class="plug" viewBox="0 0 24 24"><path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4"/></svg>
        <div><div style="font-weight:700">Connect your Trading 212 practice account so the bot can trade and you can see your balance</div>
        <ol><li>In the Trading 212 app, switch to your <b>Practice</b> account (Invest or Stocks ISA)</li>
        <li>Settings &rarr; <b>API (Beta)</b> &rarr; Generate API key. Copy the key and secret.</li>
        <li>${S.accounts.length ? "Check the key in <code>stock-trading-bot/.env</code>, or add the account again here." : "Tap <b>Add practice account</b> and paste them in."}</li></ol>
        ${a.error ? `<div class="down small">${esc(a.error)}</div>` : ""}
        <button class="btn primary sm" data-add-account style="margin-top:6px">Add practice account</button></div></div>` + botCard;
    return;
  }
  if (!a.enabled) {
    $("#kpis").innerHTML = `<div class="kpi connect">
        <svg class="plug" viewBox="0 0 24 24"><path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4"/></svg>
        <div><div style="font-weight:700">Connect your Alpaca paper account to see your balance, positions and run the bot</div>
        <ol><li>Sign up free at <a href="https://app.alpaca.markets/signup" target="_blank" rel="noopener" style="color:#93c5fd">alpaca.markets</a> and switch to <b>Paper Trading</b></li>
        <li>Create API keys and paste them into <code>stock-trading-bot/.env</code> (<code>ALPACA_API_KEY</code>, <code>ALPACA_API_SECRET</code>)</li>
        <li>Restart <code>python run_dashboard.py</code>. Live prices and alerts already work without keys.</li></ol></div></div>` + botCard;
    return;
  }
  if (a.error) {
    $("#kpis").innerHTML = `<div class="kpi connect"><svg class="plug" viewBox="0 0 24 24" style="color:var(--amber)"><path d="M12 3 2 21h20L12 3zM12 10v5M12 18h.01"/></svg><div><div style="font-weight:700">Couldn't reach your ${esc(brokerLabel())}</div><div class="muted small">${esc(a.error)}</div></div></div>` + botCard;
    return;
  }
  if (a.equity == null) { $("#kpis").innerHTML = `<div class="kpi connect"><div>Loading your ${esc(brokerLabel())}…</div></div>` + botCard; return; }
  const invested = a.positions.reduce((t, p) => t + p.market_value, 0);
  $("#kpis").innerHTML = `
    <div class="kpi"><div class="label">Portfolio value</div><div class="value">${am(a.equity)}</div><div class="sub">${esc(a.label || brokerLabel())}</div></div>
    <div class="kpi"><div class="label">Today's P/L</div><div class="value ${cls(a.day_pl)}">${signedAm(a.day_pl)}</div><div class="sub ${cls(a.day_pl)}">${pct(a.day_pl_pct)} · limit -${(S.config.max_daily_loss_pct * 100).toFixed(1)}%</div></div>
    <div class="kpi"><div class="label">Cash</div><div class="value">${am(a.cash)}</div><div class="sub">${isT212() ? "Available to trade" : "Buying power " + am(a.buying_power, 0)}</div></div>
    <div class="kpi"><div class="label">Invested</div><div class="value">${am(invested)}</div><div class="sub">${a.positions.length} position${a.positions.length === 1 ? "" : "s"} · ${a.open_orders.length} open order${a.open_orders.length === 1 ? "" : "s"}</div></div>
    ${botCard}`;
}

// ---------- watchlist ----------
function sparkSvg(values, prevClose) {
  if (!values || values.length < 2) return '<svg class="spark"></svg>';
  const w = 96, h = 30;
  const all = prevClose ? values.concat([prevClose]) : values;
  const min = Math.min(...all), max = Math.max(...all), span = max - min || 1;
  const x = (i) => (i / (values.length - 1)) * w;
  const y = (v) => h - 2 - ((v - min) / span) * (h - 4);
  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const up = values[values.length - 1] >= (prevClose || values[0]);
  const color = up ? "#22c55e" : "#ef4444";
  const base = prevClose ? `<line x1="0" x2="${w}" y1="${y(prevClose).toFixed(1)}" y2="${y(prevClose).toFixed(1)}" stroke="#475569" stroke-dasharray="2 3" stroke-width="1"/>` : "";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${base}<polyline points="${pts}" stroke="${color}" fill="none"/></svg>`;
}

function signalBadges(q) {
  const score = (S.signals.scores || {})[q.symbol];
  let html = q.advice ? adviceBadge(q.advice) + " " : "";
  html += q.trend ? `<span class="badge ${q.trend}" title="${S.config.sma_short}-day average is ${q.trend === "up" ? "above" : "below"} the ${S.config.sma_long}-day average">${q.trend === "up" ? "▲ Uptrend" : "▼ Downtrend"}</span>` : '<span class="badge neutral">—</span>';
  if (score) html += ` <span class="badge star" title="Big traders: ${score > 0 ? score + " more buying than selling" : -score + " more selling than buying"}">★ ${score > 0 ? "+" : ""}${score}</span>`;
  return html;
}

function detailValues(q) {
  const vs = q.sma_long ? (q.price / q.sma_long - 1) * 100 : null;
  return [money(q.open), money(q.day_high), money(q.day_low), money(q.prev_close), compact(q.volume), money(q.sma_short), money(q.sma_long), `<span class="${cls(vs)}">${pct(vs, 1)}</span>`];
}
function detailCells(q) {
  return detailValues(q).map((v, i) => `<td class="dcol r" data-dc="${q.symbol}-${i}">${v}</td>`).join("");
}

function watchRow(sym) {
  const q = S.quotes[sym];
  if (!q) return "";
  return `<tr data-sym="${sym}" class="${S.selected === sym ? "selected" : ""}">
    <td class="sym">${sym}<small>${q.session === "regular" ? "" : esc(q.session)}</small></td>
    <td class="r"><span class="price-cell num" data-price="${sym}">${money(q.price)}</span></td>
    <td class="r"><span class="chg ${cls(q.change_pct)}" data-chg="${sym}">${pct(q.change_pct)}</span></td>
    <td data-spark="${sym}">${sparkSvg(q.spark, q.prev_close)}</td>
    <td data-sig="${sym}">${signalBadges(q)}</td>
    ${detailCells(q)}
    <td><div class="row-actions">
      <button data-act="alert" title="Set price alert">${ICONS.bell}</button>
      <button data-act="remove" title="Remove from watchlist">${ICONS.trash}</button></div></td></tr>`;
}

function renderWatchlist() {
  const body = $("#watchlist-body");
  body.innerHTML = S.watchlist.length ? S.watchlist.map(watchRow).join("") : `<tr><td colspan="14" class="empty">Add a ticker above to start watching it.</td></tr>`;
}

function updateWatchRow(q, oldPrice) {
  const sym = q.symbol;
  const priceEl = document.querySelector(`[data-price="${sym}"]`);
  if (!priceEl) return;
  priceEl.textContent = money(q.price);
  if (oldPrice && q.price !== oldPrice) {
    priceEl.classList.remove("flash-up", "flash-down");
    void priceEl.offsetWidth;
    priceEl.classList.add(q.price > oldPrice ? "flash-up" : "flash-down");
    setTimeout(() => priceEl.classList.remove("flash-up", "flash-down"), 450);
  }
  const chg = document.querySelector(`[data-chg="${sym}"]`);
  chg.textContent = pct(q.change_pct);
  chg.className = "chg " + cls(q.change_pct);
  document.querySelector(`[data-spark="${sym}"]`).innerHTML = sparkSvg(q.spark, q.prev_close);
  document.querySelector(`[data-sig="${sym}"]`).innerHTML = signalBadges(q);
  if (S.focus === "watchlist") detailValues(q).forEach((v, i) => { const c = document.querySelector(`[data-dc="${sym}-${i}"]`); if (c) c.innerHTML = v; });
}

// ---------- chart ----------
const canvas = $("#chart");
const ctx = canvas.getContext("2d");
let hoverIndex = null;
let chartReq = 0;

async function loadChart() {
  const sym = S.selected;
  if (!sym) return;
  const req = ++chartReq;
  $("#chart-loading").classList.remove("hidden");
  $("#chart-loading").textContent = "Loading chart…";
  try {
    const data = await api(`/api/chart/${sym}?range=${S.range}`);
    if (req !== chartReq) return;
    S.chart = data;
    $("#chart-loading").classList.toggle("hidden", data.c.length > 1);
    if (data.c.length <= 1) $("#chart-loading").textContent = "No data for this range yet.";
  } catch (e) {
    if (req === chartReq) $("#chart-loading").textContent = "Couldn't load chart: " + e.message;
  }
  drawChart();
}

function chartColors() {
  const d = S.chart;
  const q = S.quotes[S.selected];
  const first = d.intraday && S.range === "1D" && q?.prev_close ? q.prev_close : d.c.find((v) => v != null);
  const last = d.c[d.c.length - 1];
  return last >= first ? ["#22c55e", "rgba(34,197,94,0.22)"] : ["#ef4444", "rgba(239,68,68,0.2)"];
}

function drawChart() {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  const d = S.chart;
  renderLegend();
  if (!d || d.symbol !== S.selected || d.c.length < 2) return;
  const W = rect.width, H = rect.height, padR = 62, padT = 10, padB = 22;
  const q = S.quotes[S.selected];
  const series = [d.c, d.sma_short, d.sma_long].filter(Boolean);
  const vals = series.flat().filter((v) => v != null);
  if (d.intraday && S.range === "1D" && q?.prev_close) vals.push(q.prev_close);
  let min = Math.min(...vals), max = Math.max(...vals);
  const pad = (max - min) * 0.08 || 1; min -= pad; max += pad;
  const n = d.c.length;
  const x = (i) => (i / (n - 1)) * (W - padR);
  const y = (v) => padT + (1 - (v - min) / (max - min)) * (H - padT - padB);

  // grid + axis labels
  ctx.font = "11px Inter, system-ui, sans-serif";
  ctx.fillStyle = "#8b97ad"; ctx.strokeStyle = "rgba(139,151,173,0.1)"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const v = min + ((max - min) * i) / 4, yy = y(v);
    ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(W - padR, yy); ctx.stroke();
    ctx.fillText(money(v), W - padR + 6, yy + 4);
  }
  const labelEvery = Math.max(1, Math.floor(n / 5));
  for (let i = 0; i < n; i += labelEvery) {
    const dt = new Date(d.t[i]);
    const label = d.intraday && S.range === "1D" ? dt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : dt.toLocaleDateString([], { month: "short", day: "numeric" });
    ctx.fillText(label, Math.min(x(i), W - padR - 40), H - 6);
  }

  // prev close
  if (d.intraday && S.range === "1D" && q?.prev_close) {
    ctx.setLineDash([3, 4]); ctx.strokeStyle = "#475569";
    ctx.beginPath(); ctx.moveTo(0, y(q.prev_close)); ctx.lineTo(W - padR, y(q.prev_close)); ctx.stroke(); ctx.setLineDash([]);
  }

  // price area + line
  const [line, fill] = chartColors();
  const grad = ctx.createLinearGradient(0, padT, 0, H - padB);
  grad.addColorStop(0, fill); grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.beginPath();
  d.c.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.lineTo(x(n - 1), H - padB); ctx.lineTo(0, H - padB); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath(); ctx.strokeStyle = line; ctx.lineWidth = 2;
  d.c.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.stroke();

  // SMAs
  [[d.sma_short, "#60a5fa"], [d.sma_long, "#f59e0b"]].forEach(([s, color]) => {
    if (!s) return;
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 1.5;
    let started = false;
    s.forEach((v, i) => { if (v == null) return; started ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)); started = true; });
    ctx.stroke();
  });

  // last price tag
  const last = d.c[n - 1];
  ctx.fillStyle = line;
  ctx.fillRect(W - padR + 2, y(last) - 9, padR - 4, 18);
  ctx.fillStyle = "#04120a"; ctx.font = "bold 11px Inter, system-ui, sans-serif";
  ctx.fillText(money(last), W - padR + 6, y(last) + 4);
  ctx.beginPath(); ctx.arc(x(n - 1), y(last), 3.5, 0, Math.PI * 2); ctx.fillStyle = line; ctx.fill();

  // hover
  const tip = $("#chart-tip");
  if (hoverIndex != null && hoverIndex < n) {
    const i = hoverIndex, xx = x(i);
    ctx.strokeStyle = "rgba(230,235,245,0.35)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(xx, padT); ctx.lineTo(xx, H - padB); ctx.stroke();
    ctx.beginPath(); ctx.arc(xx, y(d.c[i]), 4, 0, Math.PI * 2); ctx.fillStyle = "#fff"; ctx.fill();
    const dt = new Date(d.t[i]);
    const when = d.intraday ? dt.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : dt.toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
    let html = `<div class="muted">${when}</div><div><b>${money(d.c[i])}</b></div>`;
    if (d.sma_short?.[i] != null) html += `<div style="color:#60a5fa">${d.short_window}-day avg ${money(d.sma_short[i])}</div>`;
    if (d.sma_long?.[i] != null) html += `<div style="color:#f59e0b">${d.long_window}-day avg ${money(d.sma_long[i])}</div>`;
    tip.innerHTML = html;
    tip.classList.remove("hidden");
    tip.style.left = Math.min(xx + 12, W - tip.offsetWidth - 4) + "px";
    tip.style.top = "8px";
  } else tip.classList.add("hidden");
}

function renderLegend() {
  const d = S.chart;
  if (!d || d.intraday) { $("#chart-legend").innerHTML = d && S.range === "1D" ? '<span><i style="background:#475569"></i>Yesterday\'s close</span>' : ""; return; }
  $("#chart-legend").innerHTML = `<span><i style="background:#60a5fa"></i>${d.short_window}-day average</span><span><i style="background:#f59e0b"></i>${d.long_window}-day average</span><span>Bot buys when blue crosses above orange, sells when it crosses below</span>`;
}

canvas.addEventListener("mousemove", (e) => {
  const d = S.chart;
  if (!d) return;
  const rect = canvas.getBoundingClientRect();
  const i = Math.round(((e.clientX - rect.left) / (rect.width - 62)) * (d.c.length - 1));
  hoverIndex = Math.max(0, Math.min(d.c.length - 1, i));
  drawChart();
});
canvas.addEventListener("mouseleave", () => { hoverIndex = null; drawChart(); });
window.addEventListener("resize", () => drawChart());
if ("ResizeObserver" in window) new ResizeObserver(() => drawChart()).observe(canvas);

function liveChartTick(q) {
  const d = S.chart;
  if (!d || d.symbol !== q.symbol || !d.intraday || S.range !== "1D" || q.session !== "regular") return;
  const minute = Math.floor(Date.now() / 60000) * 60000;
  const lastT = d.t[d.t.length - 1];
  if (minute > lastT) { d.t.push(minute); d.c.push(q.price); } else d.c[d.c.length - 1] = q.price;
  drawChart();
}

function renderChartHeader() {
  const sym = S.selected, q = S.quotes[sym];
  if (!q) return;
  $("#chart-symbol").textContent = sym;
  const trend = $("#chart-trend");
  if (q.advice) {
    trend.className = "badge advice " + ADVICE[q.advice.action][1];
    trend.textContent = q.advice.headline;
    trend.title = q.advice.headline + "\n" + q.advice.reasons.join("\n");
  } else {
    trend.className = "badge " + (q.trend || "neutral");
    trend.textContent = q.trend === "up" ? "▲ Uptrend (bot: hold/buy)" : q.trend === "down" ? "▼ Downtrend (bot: stay out)" : "";
    trend.title = "";
  }
  $("#chart-price").textContent = money(q.price);
  $("#chart-change").innerHTML = `<span class="${cls(q.change)}">${signedMoney(q.change)} (${pct(q.change_pct)})</span> <span class="muted small">today</span>`;
  const pos = (S.account.positions || []).find((p) => p.symbol === sym);
  const stats = [
    ["Open", money(q.open)], ["Day high", money(q.day_high)], ["Day low", money(q.day_low)], ["Prev close", money(q.prev_close)], ["Volume", compact(q.volume)],
    pos ? [pos.bot_qty != null ? "Held (bot / you)" : "You own", pos.bot_qty != null ? `${pos.bot_qty} / ${pos.your_qty}` : `${pos.qty} (${pct(pos.unrealized_plpc)})`] : [`${S.config.sma_short}/${S.config.sma_long}-day avg`, `${money(q.sma_short, 0)} / ${money(q.sma_long, 0)}`],
  ];
  $("#chart-stats").innerHTML = stats.map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  const canTrade = S.account.enabled && !S.account.error;
  ["#btn-buy", "#btn-sell"].forEach((b) => { $(b).disabled = !canTrade; $(b).title = canTrade ? `Your own trade on the ${brokerLabel()}` : `Connect your ${brokerLabel()} to trade`; });
}

function selectSymbol(sym) {
  if (!S.quotes[sym]) return;
  if (S.focus && S.focus !== "chart" && S.focus !== "bot") setFocus("chart");
  S.selected = sym;
  localStorage.setItem("cc.selected", sym);
  document.querySelectorAll(".watchlist tr").forEach((r) => r.classList.toggle("selected", r.dataset.sym === sym));
  S.chart = null; hoverIndex = null;
  renderChartHeader();
  loadChart();
  renderChartDetails(true);
}

// ---------- full-page panels ----------
const PANEL_NAMES = { watchlist: "Watchlist", chart: "Chart", alerts: "Alerts", traders: "Big-trader moves", positions: "Positions", bot: "Trading bot" };

function addPanelTools() {
  document.querySelectorAll(".panel[data-panel]").forEach((panel) => {
    const name = panel.dataset.panel;
    const tools = el(`<div class="panel-tools">
      <button class="tool-btn" data-popout="${name}" title="Open ${PANEL_NAMES[name]} in a new window">${ICONS.popout}</button>
      <button class="tool-btn" data-expand="${name}" title="Full page">${ICONS.expand}</button></div>`);
    panel.querySelector(".panel-head").appendChild(tools);
  });
  document.addEventListener("click", (e) => {
    const ex = e.target.closest("[data-expand]");
    if (ex) return setFocus(S.focus === ex.dataset.expand ? null : ex.dataset.expand);
    const po = e.target.closest("[data-popout]");
    if (po) {
      window.open(`${location.pathname}#full=${po.dataset.popout}`, `cc-${po.dataset.popout}`, "width=1280,height=860");
      if (S.focus === po.dataset.popout) setFocus(null);
    }
  });
  $("#focus-backdrop").onclick = () => setFocus(null);
  document.querySelectorAll(".panel-head h2, .chart-title").forEach((h) => {
    h.title = "Double-click for full page";
    h.addEventListener("dblclick", () => { const p = h.closest(".panel").dataset.panel; setFocus(S.focus === p ? null : p); });
  });
}

function setFocus(name) {
  if (name && !PANEL_NAMES[name]) name = null;
  S.focus = name;
  document.documentElement.style.setProperty("--topbar-h", $(".topbar").offsetHeight + "px");
  document.querySelectorAll(".panel[data-panel]").forEach((p) => {
    const on = p.dataset.panel === name;
    p.classList.toggle("expanded", on);
    const b = p.querySelector("[data-expand]");
    b.innerHTML = on ? ICONS.collapse : ICONS.expand;
    b.title = on ? "Back to dashboard (Esc)" : "Full page";
  });
  document.body.classList.toggle("focus-mode", !!name);
  $("#focus-backdrop").classList.toggle("hidden", !name);
  const hash = name ? `#full=${name}` : "";
  if (location.hash !== hash) history.replaceState(null, "", location.pathname + location.search + hash);
  if (name === "watchlist") renderWatchlist();
  if (name === "chart") renderChartDetails(true);
  requestAnimationFrame(drawChart);
}

function focusFromHash() {
  const m = location.hash.match(/full=(\w+)/);
  setFocus(m ? m[1] : null);
}
window.addEventListener("hashchange", focusFromHash);

async function loadHistory(sym) {
  const cached = S.history[sym];
  if (cached && Date.now() - cached.at < 30 * 60000) return cached.data;
  const data = await api(`/api/chart/${sym}?range=2Y`);
  S.history[sym] = { at: Date.now(), data };
  return data;
}

function lastCross(d) {
  const a = d.sma_short, b = d.sma_long;
  if (!a || !b) return null;
  for (let i = d.c.length - 1; i > 0; i--) {
    if ([a[i], b[i], a[i - 1], b[i - 1]].some((v) => v == null)) break;
    const now = a[i] > b[i], before = a[i - 1] > b[i - 1];
    if (now !== before) return { up: now, t: d.t[i], price: d.c[i] };
  }
  return null;
}

let detailsTimer = 0;
async function renderChartDetails(force = false) {
  if (S.focus !== "chart" || !S.selected) return;
  if (!force && Date.now() - detailsTimer < 2000) return;
  detailsTimer = Date.now();
  const sym = S.selected, q = S.quotes[sym];
  const box = $("#chart-details");
  let d = S.history[sym]?.data;
  if (!d) {
    box.innerHTML = `<div class="empty">Loading details…</div>`;
    try { d = await loadHistory(sym); } catch (e) { box.innerHTML = `<div class="empty">Couldn't load history: ${esc(e.message)}</div>`; return; }
    if (sym !== S.selected) return;
  }
  const price = q.price, closes = d.c, n = closes.length;
  const back = (days) => { const cutoff = Date.now() - days * 86400000; const i = d.t.findIndex((t) => t >= cutoff); return i >= 0 ? closes[i] : null; };
  const perf = [["1 week", 7], ["1 month", 30], ["3 months", 91], ["6 months", 182], ["1 year", 365], ["2 years", 730]]
    .map(([k, days]) => { const p = back(days); const r = p ? (price / p - 1) * 100 : null; return `<div><div class="k">${k}</div><div class="v ${cls(r)}">${pct(r, 1)}</div></div>`; }).join("");
  const year = closes.slice(Math.max(0, n - 252));
  const hi = Math.max(...year, price), lo = Math.min(...year, price);
  const posPct = hi > lo ? ((price - lo) / (hi - lo)) * 100 : 50;
  const cross = lastCross(d);
  const gap = q.sma_long ? (q.sma_short / q.sma_long - 1) * 100 : null;
  const botText = q.trend === "up"
    ? `The ${d.short_window}-day average (${money(q.sma_short)}) is <b class="up">above</b> the ${d.long_window}-day (${money(q.sma_long)}), ${pct(gap, 1)} apart. The bot would <b>buy or hold</b> ${sym}.`
    : q.trend === "down"
      ? `The ${d.short_window}-day average (${money(q.sma_short)}) is <b class="down">below</b> the ${d.long_window}-day (${money(q.sma_long)}), ${pct(gap, 1)} apart. The bot would <b>stay out or sell</b> ${sym}.`
      : "Not enough history yet to compute the averages.";
  const crossText = cross ? `Last crossover: <b class="${cross.up ? "up" : "down"}">${cross.up ? "bullish (buy)" : "bearish (sell)"}</b> on ${new Date(cross.t).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })} at ${money(cross.price)} (${pct((price / cross.price - 1) * 100, 1)} since).` : "";
  const score = (S.signals.scores || {})[sym];
  const sigs = (S.signals.signals || []).filter((s) => s.symbol === sym);
  const alerts = S.alerts.filter((a) => a.symbol === sym).slice(0, 8);
  const pos = (S.account.positions || []).find((p) => p.symbol === sym);
  const rules = (S.settings.price_alerts || []).filter((r) => r.symbol === sym && !r.triggered);
  box.innerHTML = `
    <h4>Performance</h4><div class="kv">${perf}</div>
    <h4>52-week range</h4>
    <div class="range-bar"><i style="left:${posPct.toFixed(1)}%"></i></div>
    <div class="range-ends"><span>${money(lo)}</span><span>${pct((price / hi - 1) * 100, 1)} from high</span><span>${money(hi)}</span></div>
    ${q.advice ? `<h4>What should I do?</h4><div class="explain"><span class="badge advice ${ADVICE[q.advice.action][1]}">${ADVICE[q.advice.action][0]}</span> <b>${esc(q.advice.headline)}</b><ul class="reasons">${q.advice.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul><span class="muted small">Big-trader data comes from public filings, which are published days to weeks after the trade.</span></div>` : ""}
    <h4>What the bot sees</h4><div class="explain">${botText}${crossText ? "<br>" + crossText : ""}</div>
    ${pos ? `<h4>Your position</h4><div class="kv">
      <div><div class="k">Shares</div><div class="v">${pos.qty}</div></div>
      ${pos.bot_qty != null ? `<div><div class="k">Bot's / yours</div><div class="v">${pos.bot_qty} / ${pos.your_qty}</div></div>` : ""}
      <div><div class="k">Avg cost</div><div class="v">${money(pos.avg_entry_price)}</div></div>
      <div><div class="k">Value</div><div class="v">${am(pos.market_value)}</div></div>
      <div><div class="k">Total P/L</div><div class="v ${cls(pos.unrealized_pl)}">${signedAm(pos.unrealized_pl)}</div></div>
      <div><div class="k">Return</div><div class="v ${cls(pos.unrealized_plpc)}">${pct(pos.unrealized_plpc)}</div></div>
      <div><div class="k">Weight</div><div class="v">${pos.weight_pct.toFixed(1)}%</div></div></div>` : ""}
    <h4>Big traders${score ? ` <span class="badge star">★ ${score > 0 ? "+" : ""}${score}</span>` : ""}</h4>
    <div class="mini-list">${sigs.length ? sigs.slice(0, 8).map((s) => `<div class="mini"><span class="${s.action === "buy" ? "up" : "down"}"><b>${s.action.toUpperCase()}</b></span> · <b>${esc(s.trader)}</b><br>${esc(s.detail)}<br><span class="muted">${esc(s.source)} · disclosed ${dayAgo(s.disclosed_on)}</span></div>`).join("") : `<div class="mini muted">No recent filings from the people you follow mention ${sym}.</div>`}</div>
    <h4>Price alerts</h4>
    <div class="mini-list">${rules.length ? rules.map((r) => `<div class="mini">${r.op} <b>${money(r.price)}</b> <span class="muted">(${pct((r.price / price - 1) * 100, 1)} away)</span></div>`).join("") : `<div class="mini muted">None set. Use “Set price alert” to get a pop-up at your price.</div>`}</div>
    <h4>Recent alerts</h4>
    <div class="mini-list">${alerts.length ? alerts.map((a) => `<div class="mini"><b>${esc(a.title)}</b><br>${esc(a.message)}<br><span class="muted">${new Date(a.ts).toLocaleString()}</span></div>`).join("") : `<div class="mini muted">No alerts for ${sym} yet.</div>`}</div>`;
}

// ---------- alerts ----------
const ALERT_GROUPS = { move: ["move", "price"], sma: ["sma", "advice"], big_trader: ["big_trader"], trades: ["bot", "order"] };
function renderAlerts() {
  const f = S.alertFilter;
  const list = f === "all" ? S.alerts : S.alerts.filter((a) => ALERT_GROUPS[f].includes(a.category));
  $("#alerts-list").innerHTML = list.length ? list.slice(0, 150).map((a) => `
    <div class="alert ${esc(a.level)} ${a.id > S.lastSeenAlertId ? "unread" : ""}" data-sym="${esc(a.symbol || "")}">
      <div class="ico">${ICONS[a.category] || ICONS.system}</div>
      <div style="min-width:0"><div class="t">${esc(a.title)}</div><div class="m">${esc(a.message)}</div><div class="full-time">${new Date(a.ts).toLocaleString()}</div></div>
      <div class="time" data-ts="${a.ts}">${ago(a.ts)}</div></div>`).join("")
    : `<div class="empty">No alerts yet. You'll be alerted about big price moves, buy/sell signals, big-trader filings and trades.</div>`;
  S.unread = S.alerts.filter((a) => a.id > S.lastSeenAlertId).length;
  $("#unread").textContent = S.unread;
  $("#unread").classList.toggle("hidden", !S.unread);
  document.title = (S.unread ? `(${S.unread}) ` : "") + "Command Center";
}

function onAlert(a) {
  S.alerts.unshift(a);
  S.alerts = S.alerts.slice(0, 300);
  renderAlerts();
  if (a.symbol === S.selected) renderChartDetails(true);
  toast(a);
  beep(a.level);
  if (document.hidden || S.popups) desktopPopup(a);
}

// ---------- big traders ----------
function renderSignals() {
  const sig = S.signals;
  $("#follows").innerHTML = (S.config.follows || []).map((f) => `<span class="badge neutral">${esc(f)}</span>`).join("");
  const scores = Object.entries(sig.scores || {}).filter(([, v]) => v).sort((a, b) => b[1] - a[1]);
  $("#scores").innerHTML = scores.length ? `<span class="muted small" style="align-self:center">Net buys − sells, last ${sig.lookback_days || 45} days:</span>` + scores.map(([sym, v]) => `<span class="score-chip" data-sym="${esc(sym)}"><b>${esc(sym)}</b><span class="${cls(v)}">${v > 0 ? "+" : ""}${v}</span></span>`).join("") : "";
  const list = sig.signals || [];
  if (sig.loading && !list.length) { $("#signals-list").innerHTML = `<div class="empty">Checking SEC filings for new moves…</div>`; return; }
  if (sig.error) { $("#signals-list").innerHTML = `<div class="empty">Couldn't load filings: ${esc(sig.error)}</div>`; return; }
  $("#signals-list").innerHTML = list.length ? list.map((s) => `
    <div class="signal" data-sym="${esc(s.symbol)}" title="${esc(s.detail)}">
      <span class="act ${s.action}">${s.action.toUpperCase()}</span>
      <div style="min-width:0"><div class="who">${esc(s.trader)} · ${esc(s.symbol)}</div><div class="what">${esc(s.detail)}</div></div>
      <div class="when">${dayAgo(s.disclosed_on)}<br>${esc(s.source)}</div></div>`).join("")
    : `<div class="empty">No new filings from the people you follow in the last ${sig.display_days || 180} days.</div>`;
}

// ---------- positions ----------
function renderPositions() {
  const a = S.account;
  const body = $("#positions-body");
  if (!a.enabled || a.error || a.equity == null) { body.innerHTML = `<div class="empty">${a.error ? esc(a.error) : a.enabled ? `Waiting for your ${esc(brokerLabel())}…` : `Connect your ${esc(brokerLabel())} to see positions and orders.`}</div>`; return; }
  const split = isT212();
  const by = (o) => split ? `<td><span class="badge ${o.by === "bot" ? "star" : "neutral"}">${o.by === "bot" ? "Bot" : "You"}</span></td>` : "";
  if (S.posTab === "positions") {
    body.innerHTML = a.positions.length ? `<table><thead><tr><th>Symbol</th><th class="r">Shares</th>${split ? `<th class="r" title="Shares the bot bought (it only ever sells these)">Bot's</th><th class="r">Yours</th>` : ""}<th class="r">Avg cost</th><th class="r">Price</th><th class="r">Value</th><th class="r">Weight</th><th class="r">Total P/L</th>${split ? "" : `<th class="r">Today</th>`}<th></th></tr></thead><tbody>
      ${a.positions.map((p) => `<tr data-sym="${p.symbol}"><td class="sym">${p.symbol}</td><td class="r">${p.qty}</td>${split ? `<td class="r">${p.bot_qty}</td><td class="r">${p.your_qty}</td>` : ""}<td class="r">${money(p.avg_entry_price)}</td><td class="r">${money(p.current_price)}</td><td class="r">${am(p.market_value)}</td>
        <td class="r">${p.weight_pct.toFixed(1)}%</td><td class="r ${cls(p.unrealized_pl)}">${signedAm(p.unrealized_pl)}<br><span class="small">${pct(p.unrealized_plpc)}</span></td>${split ? "" : `<td class="r ${cls(p.intraday_pl)}">${signedAm(p.intraday_pl)}</td>`}
        <td><button class="btn ghost sm" data-close-pos="${p.symbol}" data-qty="${p.qty}">Close</button></td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">No open positions yet.</div>`;
  } else if (S.posTab === "orders") {
    body.innerHTML = a.open_orders.length ? `<table><thead><tr><th>Symbol</th><th>Side</th><th class="r">Qty</th><th>Type</th><th>Status</th><th>Sent</th>${split ? "<th>By</th>" : ""}<th></th></tr></thead><tbody>
      ${a.open_orders.map((o) => `<tr><td class="sym">${o.symbol}</td><td class="${o.side === "buy" ? "up" : "down"}">${o.side.toUpperCase()}</td><td class="r">${o.qty}</td><td>${esc(o.type)}</td><td>${esc(o.status)}</td><td class="small muted">${o.submitted_at ? new Date(o.submitted_at).toLocaleTimeString() : ""}</td>${by(o)}
        <td><button class="btn ghost sm" data-cancel="${o.id}">Cancel</button></td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">No open orders.</div>`;
  } else {
    const fills = a.recent_orders.filter((o) => o.status === "filled");
    body.innerHTML = fills.length ? `<table><thead><tr><th>Symbol</th><th>Side</th><th class="r">Qty</th><th class="r">Price</th><th>Filled</th>${split ? "<th>By</th>" : ""}</tr></thead><tbody>
      ${fills.map((o) => `<tr><td class="sym">${o.symbol}</td><td class="${o.side === "buy" ? "up" : "down"}">${o.side.toUpperCase()}</td><td class="r">${o.filled_qty}</td><td class="r">${money(o.filled_avg_price)}</td><td class="small muted">${o.filled_at ? new Date(o.filled_at).toLocaleString() : ""}</td>${by(o)}</tr>`).join("")}</tbody></table>`
      : `<div class="empty">No recent fills.</div>`;
  }
}

// ---------- bot ----------
function logClass(line) {
  if (/\bBUY \S+ [A-Z]/.test(line)) return "buy";
  if (/\bSELL \S+ [A-Z]/.test(line) || /Daily loss|Error|Traceback/.test(line)) return "sell";
  if (/Skip|WARNING|ALERT/.test(line)) return "warn";
  return "";
}
function appendLog(line) {
  const pre = $("#bot-log");
  const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  const span = document.createElement("span");
  span.className = logClass(line);
  span.textContent = line + "\n";
  pre.appendChild(span);
  while (pre.childNodes.length > 400) pre.firstChild.remove();
  if (stick) pre.scrollTop = pre.scrollHeight;
}
function renderBot() {
  const b = S.bot;
  $("#bot-pill").textContent = b.running ? "Running" : "Stopped";
  $("#bot-pill").className = "pill " + (b.running ? "running" : "");
  $("#btn-bot-start").disabled = b.running || !b.can_start;
  $("#btn-bot-stop").disabled = !b.running;
  $("#bot-strategy").disabled = b.running;
  $("#bot-info").innerHTML = !b.can_start
    ? (isT212() ? "Add a Trading 212 <b>practice</b> account (top bar &rarr; account menu) to let the bot trade. It never connects to real money." : "Add your Alpaca <b>paper</b> keys to <code>.env</code> to let the bot trade. It never uses real money.")
    : b.running ? `Trading your watchlist on your ${esc(brokerLabel())} (${b.strategy === "sma" ? "SMA crossover" : "smart money + SMA"}). Buys are capped at ${(S.config.max_position_pct * 100).toFixed(0)}% of the portfolio per stock.${isT212() ? " You get a pop-up for every bot trade, and it only sells shares it bought." : ""}`
    : `The bot checks your watchlist every 30 minutes and trades on your ${esc(brokerLabel())}.${isT212() ? " You can keep trading yourself; the bot never sells your shares." : ""}`;
  if (!$("#bot-log").childNodes.length) $("#bot-log").textContent = "";
}

// ---------- modals ----------
function openModal(title, bodyHtml, buttons) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHtml;
  const foot = $("#modal-foot");
  foot.innerHTML = "";
  buttons.forEach(([label, klass, fn]) => {
    const b = el(`<button class="btn ${klass}">${label}</button>`);
    b.onclick = async () => { b.disabled = true; try { if ((await fn()) !== false) closeModal(); } catch (e) { notifyError(e.message); } finally { b.disabled = false; } };
    foot.appendChild(b);
  });
  $("#modal").classList.remove("hidden");
  const first = $("#modal-body input");
  if (first) setTimeout(() => first.focus(), 30);
}
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal" || e.target.closest("[data-close]")) closeModal(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#modal").classList.contains("hidden")) closeModal();
  else if (S.focus) setFocus(null);
});

function priceAlertModal(sym) {
  const q = S.quotes[sym] || {};
  const rules = (S.settings.price_alerts || []).filter((r) => r.symbol === sym);
  openModal(`Price alert for ${sym}`, `
    <p class="muted" style="margin-top:0">Currently ${money(q.price)}. You'll get a pop-up the moment the price gets there.</p>
    <div class="field-row">
      <div class="field"><label>Alert me when price is</label><select id="pa-op"><option value="above">Above</option><option value="below">Below</option></select></div>
      <div class="field"><label>Price ($)</label><input id="pa-price" type="number" step="0.01" min="0" value="${q.price ? (q.price * 1.05).toFixed(2) : ""}" /></div>
    </div>
    <div class="muted small" style="margin-bottom:10px">Quick picks: ${[-10, -5, 5, 10].map((p) => `<button class="btn ghost sm" data-quick="${p}">${p > 0 ? "+" : ""}${p}%</button>`).join(" ")}</div>
    ${rules.length ? `<div class="field"><label>Existing alerts</label>${rules.map((r) => `<div class="rule ${r.triggered ? "done" : ""}">${r.op} ${money(r.price)} ${r.triggered ? "(triggered)" : ""}<button class="btn ghost sm" data-del-rule="${r.id}">Remove</button></div>`).join("")}</div>` : ""}`,
    [["Cancel", "ghost", () => {}], ["Create alert", "primary", async () => {
      const price = parseFloat($("#pa-price").value);
      if (!(price > 0)) { notifyError("Enter a price above 0"); return false; }
      await api("/api/price-alerts", "POST", { symbol: sym, op: $("#pa-op").value, price });
      toast({ level: "success", title: "Price alert created", message: `${sym} ${$("#pa-op").value} ${money(price)}` }, 4000);
    }]]);
  $("#modal-body").querySelectorAll("[data-quick]").forEach((b) => b.onclick = () => {
    const p = Number(b.dataset.quick);
    $("#pa-op").value = p > 0 ? "above" : "below";
    $("#pa-price").value = (q.price * (1 + p / 100)).toFixed(2);
  });
  $("#modal-body").querySelectorAll("[data-del-rule]").forEach((b) => b.onclick = async () => {
    await api(`/api/price-alerts/${b.dataset.delRule}`, "DELETE"); b.closest(".rule").remove();
  });
}

function orderModal(side, sym, qty) {
  const q = S.quotes[sym];
  const est = q ? q.price * qty : 0;
  const eq = S.account.equity || 0;
  const weight = eq ? (est / eq) * 100 : 0;
  const over = side === "buy" && weight > S.config.max_position_pct * 100;
  openModal(`${side === "buy" ? "Buy" : "Sell"} ${sym}`, `
    <div class="summary">${side === "buy" ? "Buy" : "Sell"} <b>${qty}</b> share${qty === 1 ? "" : "s"} of <b>${sym}</b> at market<br>
    <span class="muted">≈ ${money(est)} at ${money(q?.price)}${eq ? ` · ${weight.toFixed(1)}% of your portfolio` : ""}</span></div>
    ${over ? `<p class="down small">Heads up: that's more than the bot's ${(S.config.max_position_pct * 100).toFixed(0)}% per-stock limit.</p>` : ""}
    <p class="muted small">This goes to ${isT212() ? `<b>${esc(S.account.name || "your Trading 212 practice account")}</b> (Trading 212 <b>practice</b>) as <b>your own</b> trade (the bot won't count these shares as its own)` : "your Alpaca <b>paper</b> account"}. No real money is used.${S.market.is_open ? "" : " The market is closed, so it will fill when it next opens."}</p>`,
    [["Cancel", "ghost", () => {}], [`Confirm ${side}`, side, async () => { await api("/api/orders", "POST", { symbol: sym, qty, side, account: S.accountId }); }]]);
}

function settingsModal() {
  const s = S.settings;
  openModal("Alert settings", `
    <div class="field"><label>Daily move alerts (%)</label><input id="st-moves" value="${(s.move_alert_pcts || []).join(", ")}" /><span class="hint">Alert when a watchlist stock is up or down this much since yesterday's close. Separate levels with commas.</span></div>
    <div class="field-row">
      <div class="field"><label>Sudden move (%)</label><input id="st-fast" type="number" step="0.1" min="0.1" value="${s.fast_move_pct}" /></div>
      <div class="field"><label>within (minutes)</label><input id="st-fast-min" type="number" min="1" max="60" value="${s.fast_move_minutes}" /></div>
    </div>
    <label class="check"><input type="checkbox" id="st-advice" ${s.advice_alerts ? "checked" : ""}/> Buy/sell calls (when a stock turns BUY or SELL, with the reasons)</label>
    <label class="check"><input type="checkbox" id="st-sma" ${s.sma_alerts ? "checked" : ""}/> Buy/sell signals (${S.config.sma_short}/${S.config.sma_long}-day average crossovers)</label>
    <label class="check"><input type="checkbox" id="st-big" ${s.big_trader_alerts ? "checked" : ""}/> New big-trader filings (funds, insiders, CEOs, Congress)</label>
    <label class="check"><input type="checkbox" id="st-bot" ${s.bot_alerts ? "checked" : ""}/> Bot trades and daily-loss limit</label>
    <div class="field" style="margin-top:10px"><label>Check for new filings every (minutes)</label><input id="st-refresh" type="number" min="5" value="${s.signal_refresh_minutes}" /></div>
    <label class="check"><input type="checkbox" id="st-popups" ${S.popups ? "checked" : ""}/> Pop-up notifications</label>
    ${pushSettingsHtml()}
    <label class="check"><input type="checkbox" id="st-sound" ${S.sound ? "checked" : ""}/> Alert sounds</label>
    ${(s.price_alerts || []).length ? `<div class="field"><label>Price alerts</label>${s.price_alerts.map((r) => `<div class="rule ${r.triggered ? "done" : ""}"><span><b>${r.symbol}</b> ${r.op} ${money(r.price)} ${r.triggered ? "(triggered)" : ""}</span><button class="btn ghost sm" data-del-rule="${r.id}">Remove</button></div>`).join("")}</div>` : ""}`,
    [["Cancel", "ghost", () => {}], ["Save", "primary", async () => {
      const moves = $("#st-moves").value.split(",").map((v) => parseFloat(v)).filter((v) => v > 0);
      await api("/api/settings", "PUT", {
        move_alert_pcts: moves,
        fast_move_pct: parseFloat($("#st-fast").value),
        fast_move_minutes: parseInt($("#st-fast-min").value, 10),
        sma_alerts: $("#st-sma").checked,
        advice_alerts: $("#st-advice").checked,
        big_trader_alerts: $("#st-big").checked,
        bot_alerts: $("#st-bot").checked,
        signal_refresh_minutes: parseInt($("#st-refresh").value, 10),
      });
      S.sound = $("#st-sound").checked; localStorage.setItem("cc.sound", S.sound ? "on" : "off");
      const wantPopups = $("#st-popups").checked;
      if (wantPopups && "Notification" in window && Notification.permission !== "granted") await enablePopups();
      else { S.popups = wantPopups; localStorage.setItem("cc.popups", S.popups ? "on" : "off"); }
      if (S.push.endpoint && S.accounts.length > 1) {
        const picked = [...document.querySelectorAll("[data-push-acct]")].filter((c) => c.checked).map((c) => c.dataset.pushAcct);
        await subscribePush(picked.length === S.accounts.length ? null : picked);
      }
      updateNotifyUi();
      toast({ level: "success", title: "Settings saved", message: "Your alert preferences are updated." }, 3000);
    }]]);
  $("#modal-body").querySelectorAll("[data-del-rule]").forEach((b) => b.onclick = async () => {
    await api(`/api/price-alerts/${b.dataset.delRule}`, "DELETE"); b.closest(".rule").remove();
  });
  bindPushSettings();
}

function pushSettingsHtml() {
  const on = Boolean(S.push.endpoint);
  let html = `<div class="field" style="margin-top:10px"><label>This device</label>`;
  if (!S.push.supported) {
    html += `<span class="hint">${isIos() && !isStandalone() ? `Add the command center to your home screen to get notifications when it's closed. <button class="btn ghost sm" data-install-help>Show me how</button>` : window.isSecureContext ? "This browser can't receive notifications while the command center is closed." : "Notifications while closed need the https:// address of your cloud install."}</span>`;
  } else {
    html += `<div class="rule"><span>${on ? "Gets notifications even when the app is closed" : "Notifications only while the app is open"}</span>
      ${on ? `<span><button class="btn ghost sm" data-push-test>Send test</button> <button class="btn ghost sm" data-push-off>Turn off</button></span>` : `<button class="btn primary sm" data-push-on>Turn on</button>`}</div>`;
    if (on && S.accounts.length > 1) {
      html += `<span class="hint">Accounts this device gets bot and order alerts for:</span>` + S.accounts.map((v) =>
        `<label class="check"><input type="checkbox" data-push-acct="${esc(v.id)}" ${pushWants(v.id) ? "checked" : ""}/> ${esc(v.name)}</label>`).join("");
    }
  }
  html += `</div>`;
  const sys = [];
  if (S.config.self_update) sys.push(`<button class="btn ghost sm" data-update-app>Update app</button>`);
  if (S.config.auth) sys.push(`<button class="btn ghost sm" data-logout>Sign out</button>`);
  if (sys.length) html += `<div class="field"><label>App</label><div>${sys.join(" ")}</div></div>`;
  return html;
}
function bindPushSettings() {
  const body = $("#modal-body");
  body.querySelector("[data-install-help]")?.addEventListener("click", installHelp);
  body.querySelector("[data-push-on]")?.addEventListener("click", async () => {
    if (Notification.permission !== "granted") await enablePopups(); else await subscribePush(null).catch((e) => notifyError(e.message));
    settingsModal();
  });
  body.querySelector("[data-push-off]")?.addEventListener("click", async () => { await unsubscribePush().catch((e) => notifyError(e.message)); settingsModal(); });
  body.querySelector("[data-push-test]")?.addEventListener("click", () => api("/api/push/test", "POST", { endpoint: S.push.endpoint })
    .then(() => toast({ level: "info", title: "Test sent", message: "It should arrive in a few seconds." }, 3000)).catch((e) => notifyError(e.message)));
  body.querySelector("[data-logout]")?.addEventListener("click", async () => { await api("/api/logout", "POST"); location.href = "/login"; });
  body.querySelector("[data-update-app]")?.addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Updating… (can take a few minutes)";
    try {
      const res = await api("/api/system/update", "POST");
      toast({ level: "success", title: "Updated, restarting", message: `${res.summary}. The page reloads in a moment.` });
      setTimeout(() => location.reload(), 12000);
    } catch (err) { notifyError(err.message); e.target.disabled = false; e.target.textContent = "Update app"; }
  });
}

// ---------- accounts ----------
function renderAccounts() {
  const sel = $("#account-select");
  sel.classList.toggle("hidden", !isT212());
  sel.innerHTML = (S.accounts.length ? "" : `<option value="" disabled>No account yet</option>`) + S.accounts.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}${v.bot.running ? " · bot on" : ""}</option>`).join("")
    + `<option value="__add">+ Add practice account…</option>` + (S.accounts.some((v) => !v.from_env) ? `<option value="__manage">Manage accounts…</option>` : "");
  sel.value = S.accountId || "";
}
function applyAccount() {
  const v = curView();
  S.accountId = v ? v.id : null;
  if (v) localStorage.setItem("cc.account", v.id);
  S.account = v ? v.state : { enabled: false };
  S.bot = v ? v.bot : {};
  const pre = $("#bot-log");
  pre.textContent = "";
  (v ? S.botLogs[v.id] || [] : []).forEach(appendLog);
  if (v?.bot.strategy) $("#bot-strategy").value = v.bot.strategy;
  renderAccounts(); renderKpis(); renderPositions(); renderBot(); renderChartHeader();
}
function addAccountModal() {
  openModal("Add a Trading 212 practice account", `
    <ol class="small" style="margin-top:0;padding-left:18px"><li>In the Trading 212 app, switch to the <b>Practice</b> account you want the bot to trade</li>
    <li>Settings &rarr; <b>API (Beta)</b> &rarr; <b>Generate API key</b>. Allow account data, history, orders and portfolio access.</li>
    <li>Copy the key and the secret here. Each account gets its own bot and its own record of which shares the bot bought.</li></ol>
    <div class="field"><label>Name</label><input id="ac-name" maxlength="40" placeholder="e.g. My practice ISA" value="${S.accounts.length ? "" : "Practice account"}" /></div>
    <div class="field"><label>API key</label><input id="ac-key" autocomplete="off" autocapitalize="off" spellcheck="false" /></div>
    <div class="field"><label>API secret</label><input id="ac-secret" type="password" autocomplete="off" /></div>
    <p class="muted small">Practice accounts only: real-money keys are refused. Keys stay on your server and are never shown again.</p>`,
    [["Cancel", "ghost", () => {}], ["Add account", "primary", async () => {
      const view = await api("/api/accounts", "POST", { name: $("#ac-name").value, api_key: $("#ac-key").value, api_secret: $("#ac-secret").value });
      upsertView(view); S.accountId = view.id; applyAccount();
      toast({ level: "success", title: `Added ${view.name}`, message: "Its balance and positions will load in a few seconds." }, 4000);
    }]]);
}
function manageAccountsModal() {
  openModal("Your accounts", `<p class="muted small" style="margin-top:0">Removing an account stops its bot and deletes its keys from the server. Nothing is sold.</p>
    ${S.accounts.map((v) => `<div class="rule"><span><b>${esc(v.name)}</b>${v.from_env ? ' <span class="muted small">(from .env)</span>' : ""}</span>${v.from_env ? "" : `<button class="btn ghost sm" data-remove-acct="${esc(v.id)}">Remove</button>`}</div>`).join("")}`,
    [["Done", "primary", () => {}]]);
  $("#modal-body").querySelectorAll("[data-remove-acct]").forEach((b) => b.onclick = async () => {
    if (!confirm(`Remove ${acctName(b.dataset.removeAcct)}? Its bot will stop.`)) return;
    try { await api(`/api/accounts/${b.dataset.removeAcct}`, "DELETE"); b.closest(".rule").remove(); } catch (err) { notifyError(err.message); }
  });
}
function upsertView(view) {
  const i = S.accounts.findIndex((v) => v.id === view.id);
  if (i >= 0) S.accounts[i] = view; else S.accounts.push(view);
  if (view.bot_log && !S.botLogs[view.id]) S.botLogs[view.id] = view.bot_log.slice();
}
$("#account-select").addEventListener("change", (e) => {
  const v = e.target.value;
  if (v === "__add") { e.target.value = S.accountId || ""; return addAccountModal(); }
  if (v === "__manage") { e.target.value = S.accountId || ""; return manageAccountsModal(); }
  S.accountId = v; applyAccount();
});
$("#kpis").addEventListener("click", (e) => { if (e.target.closest("[data-add-account]")) addAccountModal(); });

// ---------- events ----------
$("#watchlist-body").addEventListener("click", async (e) => {
  const row = e.target.closest("tr[data-sym]");
  if (!row) return;
  const sym = row.dataset.sym;
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "alert") return priceAlertModal(sym);
  if (act === "remove") {
    try {
      await api(`/api/watchlist/${sym}`, "DELETE");
      S.watchlist = S.watchlist.filter((s) => s !== sym);
      renderWatchlist();
      if (S.selected === sym && S.watchlist.length) selectSymbol(S.watchlist[0]);
    } catch (err) { notifyError(err.message); }
    return;
  }
  selectSymbol(sym);
});

$("#add-symbol").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#add-symbol-input");
  const sym = input.value.trim().toUpperCase();
  if (!sym) return;
  const btn = e.target.querySelector("button");
  btn.disabled = true; btn.textContent = "Adding…";
  try {
    const res = await api("/api/watchlist", "POST", { symbol: sym });
    S.quotes[sym] = res.quote;
    S.watchlist = res.watchlist;
    renderWatchlist();
    selectSymbol(sym);
    input.value = "";
  } catch (err) { notifyError(err.message); }
  btn.disabled = false; btn.textContent = "Add";
});

$("#ranges").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-range]");
  if (!b) return;
  S.range = b.dataset.range;
  document.querySelectorAll("#ranges button").forEach((x) => x.classList.toggle("active", x === b));
  loadChart();
});

$("#tape").addEventListener("click", (e) => { const t = e.target.closest("[data-sym]"); if (t) selectSymbol(t.dataset.sym); });
$("#alerts-list").addEventListener("click", (e) => { const a = e.target.closest("[data-sym]"); if (a?.dataset.sym && S.quotes[a.dataset.sym]) selectSymbol(a.dataset.sym); });
$("#scores").addEventListener("click", (e) => { const c = e.target.closest("[data-sym]"); if (c) $(`#signals-list [data-sym="${c.dataset.sym}"]`)?.click(); });
$("#signals-list").addEventListener("click", async (e) => {
  const s = e.target.closest("[data-sym]");
  if (!s) return;
  const sym = s.dataset.sym;
  if (!S.quotes[sym]) {
    try { const res = await api("/api/watchlist", "POST", { symbol: sym }); S.quotes[sym] = res.quote; S.watchlist = res.watchlist; renderWatchlist(); }
    catch (err) { return notifyError(err.message); }
  }
  selectSymbol(sym);
});

$("#alert-filters").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-f]");
  if (!b) return;
  S.alertFilter = b.dataset.f;
  document.querySelectorAll("#alert-filters button").forEach((x) => x.classList.toggle("active", x === b));
  renderAlerts();
});
$("#btn-clear-alerts").onclick = () => {
  S.lastSeenAlertId = S.alerts.reduce((m, a) => Math.max(m, a.id), S.lastSeenAlertId);
  localStorage.setItem("cc.lastSeenAlert", S.lastSeenAlertId);
  renderAlerts();
};
$("#btn-test-alert").onclick = async () => {
  if ("Notification" in window && Notification.permission === "default") await enablePopups();
  api("/api/alerts/test", "POST").catch((e) => notifyError(e.message));
};
$("#btn-refresh-signals").onclick = () => api("/api/signals/refresh", "POST").catch((e) => notifyError(e.message));

$("#pos-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  S.posTab = b.dataset.tab;
  document.querySelectorAll("#pos-tabs button").forEach((x) => x.classList.toggle("active", x === b));
  renderPositions();
});
$("#positions-body").addEventListener("click", async (e) => {
  const cancel = e.target.closest("[data-cancel]");
  if (cancel) { try { await api(`/api/orders/${cancel.dataset.cancel}?account=${encodeURIComponent(S.accountId || "")}`, "DELETE"); toast({ level: "info", title: "Order cancelled", message: "" }, 3000); } catch (err) { notifyError(err.message); } return; }
  const close = e.target.closest("[data-close-pos]");
  if (close) return orderModal("sell", close.dataset.closePos, Number(close.dataset.qty));
  const row = e.target.closest("tr[data-sym]");
  if (row && S.quotes[row.dataset.sym]) selectSymbol(row.dataset.sym);
});

$("#btn-buy").onclick = () => { const qty = Number($("#trade-qty").value); if (qty > 0 && S.selected) orderModal("buy", S.selected, qty); };
$("#btn-sell").onclick = () => { const qty = Number($("#trade-qty").value); if (qty > 0 && S.selected) orderModal("sell", S.selected, qty); };
$("#btn-price-alert").onclick = () => S.selected && priceAlertModal(S.selected);

$("#btn-bot-start").onclick = () => openModal("Start the trading bot?", `
  <p>The bot will trade your watchlist (<b>${S.watchlist.join(", ")}</b>) on ${isT212() ? `<b>${esc(S.account.name || "your account")}</b> (Trading 212 <b>practice</b>)` : "your Alpaca <b>paper</b> account"} using the <b>${$("#bot-strategy").selectedOptions[0].text}</b> strategy.</p>
  <p class="muted small">Each stock is capped at ${(S.config.max_position_pct * 100).toFixed(0)}% of the portfolio and new buys stop if the account is down ${(S.config.max_daily_loss_pct * 100).toFixed(1)}% on the day. It keeps running (and restarts after a server restart) until you stop it. No real money is used.</p>`,
  [["Cancel", "ghost", () => {}], ["Start bot", "primary", async () => { await api("/api/bot/start", "POST", { strategy: $("#bot-strategy").value, account: S.accountId }); }]]);
$("#btn-bot-stop").onclick = () => api("/api/bot/stop", "POST", { account: S.accountId }).catch((e) => notifyError(e.message));

$("#btn-notify").onclick = async () => {
  if (!("Notification" in window)) return notifyError("This browser doesn't support desktop pop-ups.");
  if (Notification.permission !== "granted") return enablePopups();
  S.popups = !S.popups; localStorage.setItem("cc.popups", S.popups ? "on" : "off"); updateNotifyUi();
  toast({ level: "info", title: `Pop-up alerts ${S.popups ? "on" : "off"}`, message: "" }, 2500);
};
$("#btn-sound").onclick = () => { S.sound = !S.sound; localStorage.setItem("cc.sound", S.sound ? "on" : "off"); updateNotifyUi(); if (S.sound) beep("success"); };
$("#btn-settings").onclick = settingsModal;
$("#banner-enable").onclick = () => (!("Notification" in window) && isIos() && !isStandalone() ? installHelp() : enablePopups());
$("#banner-dismiss").onclick = () => { localStorage.setItem("cc.bannerDismissed", "1"); updateNotifyUi(); };

// ---------- live stream ----------
function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => { S.connected = true; renderMarket(); };
  es.onerror = () => { S.connected = false; renderMarket(); };
  es.addEventListener("quotes", (e) => {
    for (const q of JSON.parse(e.data)) {
      const old = S.quotes[q.symbol];
      S.quotes[q.symbol] = q;
      if (S.watchlist.includes(q.symbol)) updateWatchRow(q, old?.price);
      if (q.symbol === S.selected) { renderChartHeader(); liveChartTick(q); renderChartDetails(); }
    }
    renderTape();
  });
  es.addEventListener("alert", (e) => onAlert(JSON.parse(e.data)));
  es.addEventListener("market", (e) => { S.market = JSON.parse(e.data); renderMarket(); });
  es.addEventListener("account", (e) => {
    const { id, state } = JSON.parse(e.data);
    const v = S.accounts.find((x) => x.id === id);
    if (!v) return;
    v.state = state;
    if (id === S.accountId) { S.account = state; renderKpis(); renderPositions(); renderChartHeader(); }
  });
  es.addEventListener("accounts", (e) => {
    S.accounts = [];
    JSON.parse(e.data).forEach(upsertView);
    applyAccount();
  });
  es.addEventListener("signals", (e) => { S.signals = JSON.parse(e.data); renderSignals(); renderWatchlist(); renderChartDetails(true); });
  es.addEventListener("bot", (e) => {
    const status = JSON.parse(e.data);
    const v = S.accounts.find((x) => x.id === status.account);
    if (!v) return;
    v.bot = status;
    renderAccounts();
    if (v.id === S.accountId) { S.bot = status; renderBot(); renderKpis(); }
  });
  es.addEventListener("bot_log", (e) => {
    const { account, line } = JSON.parse(e.data);
    const log = (S.botLogs[account] = S.botLogs[account] || []);
    log.push(line);
    if (log.length > 400) log.shift();
    if (account === S.accountId) appendLog(line);
  });
  es.addEventListener("settings", (e) => { S.settings = JSON.parse(e.data); });
  es.addEventListener("watchlist", (e) => { S.watchlist = JSON.parse(e.data).filter((s) => S.quotes[s]); renderWatchlist(); });
}

async function init() {
  const st = await api("/api/state");
  Object.assign(S, {
    quotes: st.quotes, watchlist: st.watchlist, indexes: st.indexes, market: st.market,
    signals: st.signals, alerts: st.alerts, settings: st.settings, config: st.config,
  });
  $("#bot-strategy").value = st.config.strategy;
  $("#brand-sub").innerHTML = `${isT212() ? "Trading 212 practice" : "Paper trading"} &middot; live data`;
  st.accounts.forEach(upsertView);
  applyAccount();
  updateNotifyUi(); renderMarket(); renderTape(); renderWatchlist(); renderAlerts(); renderSignals();
  const saved = new URLSearchParams(location.search).get("symbol") || localStorage.getItem("cc.selected");
  selectSymbol(S.quotes[saved] ? saved : S.watchlist[0] || S.indexes[0]);
  initPush().catch((e) => console.warn("push setup failed", e)).finally(updateNotifyUi);
  addPanelTools();
  focusFromHash();
  connect();
  setInterval(() => { renderCountdown(); document.querySelectorAll(".alert .time").forEach((t) => (t.textContent = ago(Number(t.dataset.ts)))); }, 15000);
}

init().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<div class="banner" style="border-color:#ef4444">Couldn't start: ${esc(e.message)}. Is run_dashboard.py running?</div>`); });
