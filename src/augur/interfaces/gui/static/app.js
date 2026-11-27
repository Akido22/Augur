// Augur GUI мониторинга (0.9.0). Без сборки и внешних библиотек.
// Данные вставляются только через textContent (никакого innerHTML с данными).
"use strict";

const Augur = (() => {
  const TOKEN_KEY = "augur.token";
  const $ = (id) => document.getElementById(id);

  // ---------- утилиты ----------
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "href") node.setAttribute("href", v);
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  const ts = (ms) => (ms === null || ms === undefined ? "—"
    : new Date(ms).toISOString().replace("T", " ").slice(0, 19));
  const dash = (v) => (v === null || v === undefined || v === "" ? "—" : v);
  const num = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
  const money = (v) => {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    return Number.isFinite(n) ? n.toLocaleString("ru-RU", { maximumFractionDigits: 2 }) : v;
  };
  const badge = (v) => el("span", { class: `s-${v}` }, dash(v));
  const chainLink = (id, label) => el("a", { href: `/chain/${encodeURIComponent(id)}` }, label || id);
  function fill(tbody, rows, emptyText, cols) {
    tbody.replaceChildren();
    if (!rows.length) {
      tbody.append(el("tr", {}, el("td", { colspan: String(cols), class: "muted" }, emptyText)));
      return;
    }
    for (const r of rows) tbody.append(r);
  }
  const td = (v, cls) => el("td", { class: cls }, v);

  // ---------- auth + API ----------
  const token = () => { try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; } };
  function setToken(t) { try { if (t) sessionStorage.setItem(TOKEN_KEY, t); else sessionStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } }

  class AuthError extends Error {}

  async function api(path, opts = {}) {
    const res = await fetch(`/api/v1${path}`, {
      ...opts,
      headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json", ...(opts.headers || {}) },
    });
    if (res.status === 401) throw new AuthError("unauthorized");
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
      throw new Error(`${res.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
    }
    return res.json();
  }

  function showLogin(message) {
    $("main").hidden = true;
    $("login").hidden = false;
    $("logout").hidden = true;
    const err = $("login-error");
    err.hidden = !message;
    err.textContent = message || "";
  }

  // ---------- шапка: статус и WebSocket ----------
  let lastStatus = null;
  function renderPills(st) {
    lastStatus = st;
    const mode = st.mode || `${st.configured_mode || "PAPER"}*`;
    const pm = $("pill-mode");
    pm.textContent = `mode: ${mode}`;
    pm.className = "pill " + (["MICRO_LIVE", "FULL_AUTO"].includes(st.mode) ? "warn" : "ok");
    pm.title = st.mode ? `изменён ${ts(st.mode_changed_ts)} (${st.mode_changed_by})` : "строки mode_state нет: эффективно не выше PAPER";
    const pc = $("pill-cb");
    pc.textContent = `CB: ${st.cb_state}`;
    pc.className = "pill " + (st.cb_state === "ARMED" ? "ok" : "bad");
    pc.title = st.cb_reason || "";
  }

  const listeners = [];
  const onEvent = (fn) => listeners.push(fn);
  let refreshTimer = null;
  function scheduleRefresh(fn, delay = 400) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(fn, delay);
  }

  function connectWs() {
    const pill = $("pill-ws");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: token() }));
    ws.onmessage = (m) => {
      let msg;
      try { msg = JSON.parse(m.data); } catch { return; }
      if (msg.type === "hello") { pill.className = "pill ok"; return; }
      for (const fn of listeners) fn(msg);
      if (["cb_state", "mode_changed", "order_update", "position_update"].includes(msg.type)) {
        scheduleRefresh(refreshStatus);
      }
    };
    ws.onclose = (e) => {
      pill.className = "pill off";
      if (e.code === 4401) { showLogin("Токен отклонён WebSocket-ом"); return; }
      setTimeout(connectWs, 5000);
    };
  }

  async function refreshStatus() {
    const st = await api("/status");
    renderPills(st);
    for (const fn of statusListeners) fn(st);
    return st;
  }
  const statusListeners = [];
  const onStatus = (fn) => statusListeners.push(fn);

  async function boot(pageInit) {
    $("login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      setToken($("token").value.trim());
      $("token").value = "";
      await start(pageInit);
    });
    $("logout").addEventListener("click", () => { setToken(null); location.reload(); });
    if (token()) await start(pageInit); else showLogin();
  }

  async function start(pageInit) {
    try {
      await refreshStatus();
    } catch (e) {
      setToken(null);
      showLogin(e instanceof AuthError ? "Неверный токен" : `Ошибка: ${e.message}`);
      return;
    }
    $("login").hidden = true;
    $("main").hidden = false;
    $("logout").hidden = false;
    connectWs();
    try { await pageInit(); } catch (e) { handleError(e); }
  }

  function handleError(e) {
    if (e instanceof AuthError) { setToken(null); showLogin("Сессия истекла: введите токен"); return; }
    console.error(e);
    const main = $("main");
    main.prepend(el("div", { class: "card error" }, `Ошибка: ${e.message}`));
  }

  // ---------- графики ----------
  function equityChart(container, snaps) {
    container.replaceChildren();
    if (snaps.length < 2) {
      container.append(el("p", { class: "muted" }, "Недостаточно снимков портфеля (portfolio_snapshots)."));
      return;
    }
    const NS = "http://www.w3.org/2000/svg";
    const W = 800, H = 220, P = { l: 70, r: 10, t: 10, b: 24 };
    const xs = snaps.map((s) => s.ts), ys = snaps.map((s) => Number(s.equity));
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    let y0 = Math.min(...ys), y1 = Math.max(...ys);
    if (y0 === y1) { y0 -= 1; y1 += 1; }
    const sx = (x) => P.l + ((x - x0) / Math.max(1, x1 - x0)) * (W - P.l - P.r);
    const sy = (y) => P.t + (1 - (y - y0) / (y1 - y0)) * (H - P.t - P.b);
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");
    const mk = (tag, attrs, text) => {
      const n = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
      if (text !== undefined) n.textContent = text;
      svg.append(n);
      return n;
    };
    const pts = snaps.map((s) => `${sx(s.ts).toFixed(1)},${sy(Number(s.equity)).toFixed(1)}`);
    mk("path", { class: "area", d: `M${sx(x0)},${H - P.b} L${pts.join(" L")} L${sx(x1)},${H - P.b} Z` });
    mk("path", { class: "line", d: `M${pts.join(" L")}` });
    mk("line", { class: "axis", x1: P.l, y1: H - P.b, x2: W - P.r, y2: H - P.b });
    mk("text", { x: P.l - 6, y: sy(y1) + 4, "text-anchor": "end" }, money(y1));
    mk("text", { x: P.l - 6, y: sy(y0) + 4, "text-anchor": "end" }, money(y0));
    mk("text", { x: P.l, y: H - 6 }, ts(x0).slice(0, 16));
    mk("text", { x: W - P.r, y: H - 6, "text-anchor": "end" }, ts(x1).slice(0, 16));
    container.append(svg);
  }

  // ---------- строки таблиц ----------
  const auditRow = (e) => el("tr", {},
    td(ts(e.ts)), td(String(e.id)), td(e.event), td(badge(e.severity)), td(e.actor),
    td(e.ref_type && e.ref_id
      ? (["signal", "order"].includes(e.ref_type) ? chainLink(e.ref_id, `${e.ref_type}:${e.ref_id}`) : `${e.ref_type}:${e.ref_id}`)
      : dash(e.ref_type)));

  const orderRow = (o) => el("tr", {},
    td(ts(o.created_ts)), td(chainLink(o.id)), td(badge(o.state)), td(o.mode), td(o.symbol),
    td(badge(o.side)), td(o.qty, "num"), td(o.filled_qty, "num"), td(dash(o.avg_fill_price), "num"),
    td(o.fee, "num"), td(o.reject_reason ? el("span", { class: "muted small", title: o.reject_reason }, "reject") : ""));

  const positionRow = (p) => el("tr", {},
    td(p.venue), td(p.symbol), td(p.mode), td(p.qty, "num"), td(p.avg_price, "num"),
    td(p.realized_pnl, "num"), td(p.unrealized_pnl, "num"), td(dash(p.stop_loss), "num"),
    td(dash(p.take_profit), "num"), td(ts(p.updated_ts)));

  const signalRow = (s) => el("tr", {},
    td(ts(s.created_ts)), td(chainLink(s.id)), td(s.kind), td(s.source), td(s.symbol),
    td(badge(s.action)), td(num(s.strength), "num"), td(s.status), td(dash(s.strategy)));

  // ---------- страницы ----------
  const pages = {};

  pages.dashboard = () => boot(async () => {
    onStatus((st) => {
      $("s-mode").replaceChildren(badge(st.mode || "PAPER"));
      $("s-mode-sub").textContent = `конфиг ${dash(st.configured_mode)}, максимум ${dash(st.max_allowed_mode)}`;
      $("s-cb").replaceChildren(badge(st.cb_state));
      $("s-cb-sub").textContent = st.cb_reason ? `${st.cb_reason} (${ts(st.cb_tripped_ts)})` : "—";
      $("s-orders").textContent = `${st.open_orders} / ${st.open_positions}`;
      $("s-orders-sub").textContent = `ждут подтверждения: ${st.awaiting_approval}; реакций новостей: ${st.active_news_reactions}`;
    });
    renderDashboardStatus();
    async function loadEquity() {
      const mode = $("eq-mode").value;
      const snaps = await api(`/portfolio/snapshots${mode ? `?mode=${encodeURIComponent(mode)}` : ""}`);
      equityChart($("equity-chart"), snaps);
      const last = snaps[snaps.length - 1];
      $("s-equity").textContent = last ? money(last.equity) : "—";
      $("s-equity-sub").textContent = last ? `${last.mode}: день ${money(last.day_pnl)}, DD ${num(Number(last.drawdown_pct), 2)}%` : "нет снимков";
    }
    $("eq-mode").addEventListener("change", () => loadEquity().catch(handleError));
    await loadEquity();
    const events = await api("/audit?limit=30");
    fill($("events"), events.map(auditRow), "Событий нет", 6);
    onEvent((msg) => {
      if (msg.data && typeof msg.data.event === "string" && typeof msg.data.id === "number") {
        const tb = $("events");
        if (tb.firstChild && tb.firstChild.querySelector("td[colspan]")) tb.replaceChildren();
        const row = auditRow(msg.data);
        row.classList.add("new");
        tb.prepend(row);
        while (tb.children.length > 100) tb.lastChild.remove();
      }
      if (msg.type === "position_update") scheduleRefresh(() => loadEquity().catch(handleError), 1000);
    });
  });
  function renderDashboardStatus() { if (lastStatus) for (const fn of statusListeners) fn(lastStatus); }

  pages.positions = () => boot(async () => {
    async function load() {
      const [positions, orders] = await Promise.all([
        api("/positions"),
        api(`/orders?limit=100${$("order-state").value ? `&state=${encodeURIComponent($("order-state").value)}` : ""}`),
      ]);
      fill($("positions"), positions.map(positionRow), "Открытых позиций нет", 10);
      fill($("orders"), orders.map(orderRow), "Ордеров нет", 11);
    }
    $("order-state").addEventListener("change", () => load().catch(handleError));
    onEvent((msg) => { if (["order_update", "position_update"].includes(msg.type)) scheduleRefresh(() => load().catch(handleError)); });
    await load();
  });

  pages.signals = () => boot(async () => {
    async function load() {
      const q = new URLSearchParams({ limit: "100" });
      if ($("sig-kind").value) q.set("kind", $("sig-kind").value);
      if ($("sig-symbol").value.trim()) q.set("symbol", $("sig-symbol").value.trim());
      fill($("signals"), (await api(`/signals?${q}`)).map(signalRow), "Сигналов нет", 9);
    }
    $("sig-kind").addEventListener("change", () => load().catch(handleError));
    $("sig-symbol").addEventListener("change", () => load().catch(handleError));
    $("chain-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const id = $("chain-id").value.trim();
      if (id) location.href = `/chain/${encodeURIComponent(id)}`;
    });
    onEvent((msg) => { if (msg.type === "signal_ready") scheduleRefresh(() => load().catch(handleError)); });
    await load();
  });

  pages.chain = () => boot(async () => {
    const ref = $("chain-ref").dataset.ref;
    const box = $("chain");
    let c;
    try {
      c = await api(`/signals/${encodeURIComponent(ref)}/chain`);
    } catch (e) {
      if (e instanceof AuthError) throw e;
      box.replaceChildren(el("p", { class: "error" }, e.message.startsWith("404") ? "Сигнал или ордер не найден" : e.message));
      return;
    }
    const kv = (pairs) => el("dl", { class: "kv" }, pairs.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v instanceof Node ? v : dash(v))]));
    const step = (title, body, missing) => el("div", { class: `chain-step${missing ? " missing" : ""}` }, el("h3", {}, title), body);
    const parts = [];
    parts.push(step(`Стратегические сигналы (${c.strategy_signals.length})`, c.strategy_signals.length
      ? el("table", { class: "tbl" }, el("tbody", {}, c.strategy_signals.map((s) => el("tr", {},
        td(dash(s.strategy)), td(badge(s.action)), td(num(s.strength), "num"), td(`бар ${ts(s.bar_ts)}`),
        td(`data_hash ${dash(s.data_hash && s.data_hash.slice(0, 12))}`), td(s.id)))))
      : el("p", { class: "muted" }, "нет"), !c.strategy_signals.length));
    const f = c.fused_signal;
    parts.push(step("Fusion", f ? kv([
      ["id", f.id], ["action", badge(f.action)], ["strength", num(f.strength)], ["valid", String(f.valid)],
      ["reason", f.reason], ["api_version", f.api_version], ["fusion_config", f.fusion_config_version],
      ["risk_flags", JSON.stringify(f.risk_flags)], ["source_breakdown", el("code", {}, JSON.stringify(f.source_breakdown))],
    ]) : el("p", { class: "muted" }, "fusion не участвовал (classic / classic_fallback)"), !f));
    const s = c.signal;
    parts.push(step(`Итоговый сигнал [${s.kind}]`, kv([
      ["id", s.id], ["symbol", s.symbol], ["action", badge(s.action)], ["strength", num(s.strength)],
      ["source", s.source], ["status", s.status], ["risk_flags", JSON.stringify(s.risk_flags)],
      ["создан", ts(s.created_ts)], ["истекает", ts(s.expires_ts)], ["code_version", s.code_version],
      ["config_hash", s.config_hash],
    ])));
    parts.push(step(`decision_audit (${c.decisions.length})`, c.decisions.length ? el("div", {}, c.decisions.map((d) => kv([
      ["id", d.id], ["verdict", badge(d.risk_verdict)], ["reason", d.risk_reason], ["mode", d.mode],
      ["app", d.app_version], ["strategy", d.strategy_version], ["ml_model", d.ml_model_version],
      ["llm_prompt", d.llm_prompt_version], ["llm_status", d.llm_status], ["config_hash", d.config_hash],
      ["execution_result", d.execution_result ? el("code", {}, JSON.stringify(d.execution_result)) : null],
      ["время", ts(d.created_at)],
    ]))) : el("p", { class: "muted" }, "решения Risk Gate нет"), !c.decisions.length));
    parts.push(step(`Ордера и fills (${c.orders.length})`, c.orders.length ? el("table", { class: "tbl" },
      el("tbody", {}, c.orders.flatMap((o) => [orderRow(o), ...o.fills.map((fl) => el("tr", { class: "muted" },
        td(""), td(`fill ${fl.venue_fill_id}`), td(""), td(""), td(""), td(""), td(fl.qty, "num"), td(""), td(fl.price, "num"), td(fl.fee, "num"), td(ts(fl.ts))))])))
      : el("p", { class: "muted" }, "ордеров нет"), !c.orders.length));
    parts.push(step(`Позиции (${c.positions.length})`, c.positions.length
      ? el("table", { class: "tbl" }, el("tbody", {}, c.positions.map(positionRow)))
      : el("p", { class: "muted" }, "открытой позиции нет"), !c.positions.length));
    parts.push(step(`События audit_log (${c.events.length})`, el("table", { class: "tbl" }, el("tbody", {}, c.events.map(auditRow)))));
    box.replaceChildren(...parts);
  });

  pages.news = () => boot(async () => {
    async function load() {
      const sym = $("news-symbol").value.trim();
      const [reactions, news] = await Promise.all([
        api("/news/reactions"),
        api(`/news?limit=100${sym ? `&symbol=${encodeURIComponent(sym)}` : ""}`),
      ]);
      fill($("reactions"), reactions.map((r) => el("tr", {},
        td(ts(r.created_ts)), td(r.id), td(r.rule), td(r.action), td(dash(r.symbol || r.strategy)),
        td(r.flag_code ? `${r.flag_code} (${r.flag_severity})` : "—"), td(ts(r.expires_ts)))), "Активных реакций нет", 7);
      fill($("news"), news.map((n) => el("tr", {},
        td(ts(n.published_ts)), td(dash(n.importance)), td(n.sentiment === null ? "—" : (n.sentiment >= 0 ? "+" : "") + num(n.sentiment), "num"),
        td(num(n.confidence), "num"), td(n.symbols.join(", ") || "—"), td(n.source),
        td(n.url && /^https?:\/\//i.test(n.url) ? el("a", { href: n.url, target: "_blank", rel: "noopener noreferrer" }, n.title) : n.title, "wrap"))),
      "Новостей нет", 7);
    }
    $("news-symbol").addEventListener("change", () => load().catch(handleError));
    onEvent((msg) => { if (msg.type === "news_event") scheduleRefresh(() => load().catch(handleError), 800); });
    await load();
  });

  pages.control = () => boot(async () => {
    onStatus((st) => {
      const pairs = [
        ["режим (mode_state)", badge(st.mode || "—")], ["режим в конфиге", st.configured_mode], ["максимум", st.max_allowed_mode],
        ["изменён", st.mode ? `${ts(st.mode_changed_ts)} — ${st.mode_changed_by}` : "—"],
        ["circuit breaker", badge(st.cb_state)], ["причина CB", st.cb_reason], ["CB с", ts(st.cb_tripped_ts)],
        ["активные ордера", String(st.open_orders)], ["ждут approve", String(st.awaiting_approval)],
        ["открытые позиции", String(st.open_positions)], ["fusion", st.fusion_enabled === null ? "—" : (st.fusion_enabled ? "включён" : "stub / выключен")],
        ["версии", `app ${st.app_version}, db_schema ${st.db_schema_version}, api ${st.api_version}`],
      ];
      $("ctl-status").replaceChildren(...pairs.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v instanceof Node ? v : dash(v))]));
    });
    if (lastStatus) for (const fn of statusListeners) fn(lastStatus);
    $("ks-button").addEventListener("click", async () => {
      const flatten = $("ks-flatten").checked;
      const msg = `Включить KILL SWITCH?\n\nKILLED, режим ANALYSIS_ONLY, отмена всех ордеров${flatten ? ", закрытие всех позиций" : ""}.\nСнятие — только augur cb reset --confirm в CLI.`;
      if (!window.confirm(msg)) return;
      const btn = $("ks-button");
      btn.disabled = true;
      const out = $("ks-result");
      try {
        const r = await api("/kill-switch", { method: "POST", body: JSON.stringify({ flatten }) });
        out.textContent = [`KILL SWITCH ENGAGED (was ${r.was_state}) -> ${r.state}, mode ${r.mode}`,
          `cancelled orders: ${r.cancelled}`, `flatten orders: ${r.flatten_orders}`,
          ...r.warnings.map((w) => `WARNING: ${w}`)].join("\n");
        out.className = r.warnings.length ? "small error" : "small";
      } catch (e) {
        if (e instanceof AuthError) { handleError(e); return; }
        out.textContent = `Ошибка: ${e.message}`;
        out.className = "small error";
      } finally {
        out.hidden = false;
        btn.disabled = false;
        refreshStatus().catch(handleError);
      }
    });
    $("verify-chain").addEventListener("click", async () => {
      const r = await api("/audit/verify-chain").catch((e) => { handleError(e); return null; });
      if (!r) return;
      const span = $("verify-result");
      span.textContent = r.ok ? ` OK: ${r.rows_checked} строк` : ` НАРУШЕНА на id ${r.first_bad_id}: ${r.problem}`;
      span.className = `small ${r.ok ? "s-ok" : "s-bad"}`;
    });
  });

  return { pages };
})();
