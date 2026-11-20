/**
 * Main Application Orchestrator for Gemina Quant Command Center (`app.js`).
 * Connects Topbar, Watchlist, Benchmarks, High-DPI Chart, Trace Waterfall,
 * System Health, Declarative Markdown Editor, Agent Chat, and Avanza Ledger Forms
 * over REST + ReadableStream SSE with strict sessionStorage Bearer auth.
 */
import { ApiError, SseStreamClient, api, clearToken, getToken, onAuthRequired, setToken } from './api.js';
import { QuantChartController } from './chart.js';
import { AgentChatController } from './components/agent_chat.js';
import { BenchmarksController } from './components/benchmarks.js';
import { DeclarativeController } from './components/declarative.js';
import { HealthController } from './components/health.js';
import { LedgerPanelController } from './components/ledger_panel.js';
import { TopbarController } from './components/topbar.js';
import { TracesController } from './components/traces.js';
import { WatchlistController } from './components/watchlist.js';
import { el } from './dom.js';

const state = {
  selectedTicker: '',
  chartRange: '5d',
  chartInterval: '15m',
  portfolio: null,
  risk: {},
  status: { is_red: false, color: '🟢', ticker_line: '', red_reasons: [], diagnostics: {} },
  latestByTicker: {},
  usdSek: 10.16,
  activeWsTab: 'traces',
  authLocked: false,
  hydrated: false,
};

let chartAbort = null;
let sseClient = null;

// --- Toast Notifications ---
const toastRoot = document.getElementById('toast-root');
function showToast(message, tone = 'info', durationMs = 3800) {
  if (!toastRoot) return;
  const node = el('div', { className: `toast is-${tone}`, text: message });
  toastRoot.appendChild(node);
  setTimeout(() => {
    node.remove();
  }, durationMs);
}

// --- Auth Unlock Overlay ---
const authOverlay = document.getElementById('auth-overlay');
const authForm = document.getElementById('auth-form');
const authInput = document.getElementById('auth-token-input');
const authError = document.getElementById('auth-error');
const authClearBtn = document.getElementById('auth-clear-btn');

function openAuthOverlay({ locked = true, errorText = '' } = {}) {
  state.authLocked = locked;
  if (!authOverlay) return;
  authOverlay.hidden = false;
  if (errorText) {
    authError.textContent = errorText;
    authError.hidden = false;
  } else {
    authError.hidden = true;
    authError.textContent = '';
  }
  authInput.value = getToken();
  setTimeout(() => authInput.focus(), 20);
}

function closeAuthOverlay() {
  state.authLocked = false;
  if (authOverlay) authOverlay.hidden = true;
  if (authError) authError.hidden = true;
}

onAuthRequired((payload) => {
  sseClient?.stop();
  topbar.update({ sseStatus: 'auth_required' });
  openAuthOverlay({
    locked: true,
    errorText: payload?.detail || payload?.message || 'Bearer token required to access Gemina Command Center.',
  });
});

// --- Workspace Tabs & Mobile Navigation ---
const commandGrid = document.getElementById('command-grid');
const wsTabButtons = Array.from(document.querySelectorAll('.ws-tab-btn'));
const wsTabPanes = {
  traces: document.getElementById('panel-traces'),
  health: document.getElementById('panel-health'),
  declarative: document.getElementById('panel-declarative'),
};

function switchWorkspaceTab(tabId) {
  if (!wsTabPanes[tabId]) return;
  state.activeWsTab = tabId;
  for (const btn of wsTabButtons) {
    const active = btn.dataset.wsTab === tabId;
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-selected', String(active));
  }
  for (const [id, pane] of Object.entries(wsTabPanes)) {
    const active = id === tabId;
    pane.classList.toggle('is-active', active);
    pane.hidden = !active;
  }
}

for (const btn of wsTabButtons) {
  btn.addEventListener('click', () => switchWorkspaceTab(btn.dataset.wsTab));
}

const mobileNavButtons = Array.from(document.querySelectorAll('.mobile-nav-btn'));
if (commandGrid) commandGrid.dataset.mobileView = 'center';
for (const btn of mobileNavButtons) {
  btn.addEventListener('click', () => {
    const sec = btn.dataset.section || 'center';
    if (commandGrid) commandGrid.dataset.mobileView = sec;
    for (const b of mobileNavButtons) {
      b.classList.toggle('is-active', b === btn);
    }
    if (sec === 'center') chartCtrl.draw();
  });
}

// --- Instantiate Controllers ---
const topbar = new TopbarController(document.getElementById('topbar-root'), {
  onOpenAuth: () => openAuthOverlay({ locked: false }),
  onNavigateTab: (tabId) => {
    switchWorkspaceTab(tabId);
    if (commandGrid) commandGrid.dataset.mobileView = 'center';
  },
});

const watchlistCtrl = new WatchlistController(document.getElementById('watchlist-root'), {
  onSelectTicker: (sym) => selectTicker(sym),
  onToggleFocus: (sym) => toggleFocusTicker(sym),
});

const benchmarksCtrl = new BenchmarksController(document.getElementById('benchmarks-root'));

const tracesCtrl = new TracesController(wsTabPanes.traces, {
  onFetchTraceDetail: (traceId) => api.getTraceDetail(traceId),
  onRefreshTraces: () => refreshTraces(),
});

const healthCtrl = new HealthController(wsTabPanes.health, {
  onRefresh: () => refreshHealth(),
});

const declarativeCtrl = new DeclarativeController(wsTabPanes.declarative, {
  onRefresh: () => refreshDeclarative(),
  onReload: async () => {
    declarativeCtrl.setBusy(true);
    try {
      const res = await api.reloadDeclarative();
      if (res && (res.agents || res.skills)) {
        declarativeCtrl.setData(res);
      } else {
        await refreshDeclarative();
      }
      showToast('Hot-reloaded agents & skills from disk.', 'ok');
    } catch (err) {
      showToast(`Hot-reload failed: ${err.message}`, 'err');
    } finally {
      declarativeCtrl.setBusy(false);
    }
  },
  onSaveFile: async (kind, name, content) => {
    declarativeCtrl.setBusy(true);
    try {
      const res = await api.updateDeclarative(kind, name, content);
      if (res && (res.agents || res.skills)) {
        declarativeCtrl.setData(res);
      } else {
        await refreshDeclarative();
      }
      showToast(`Saved ${kind}/${name} and hot-reloaded personas.`, 'ok');
    } catch (err) {
      showToast(`Save failed: ${err.message}`, 'err');
    } finally {
      declarativeCtrl.setBusy(false);
    }
  },
});

function openTraceInWaterfall(traceId) {
  switchWorkspaceTab('traces');
  if (commandGrid) commandGrid.dataset.mobileView = 'center';
  tracesCtrl.selectTrace(traceId);
}

const chartCtrl = new QuantChartController(document.getElementById('chart-root'), {
  onInspectTrace: (traceId) => openTraceInWaterfall(traceId),
  onRangeChange: (range, interval) => {
    state.chartRange = range;
    state.chartInterval = interval;
    loadChart(state.selectedTicker);
  },
  onRefresh: () => loadChart(state.selectedTicker),
});

const agentChatCtrl = new AgentChatController(document.getElementById('agent-chat-root'), {
  onSendChat: (message, mode) => api.sendChat(message, mode),
  onClearChat: async () => {
    try {
      await api.clearChat();
      showToast('Chat memory cleared.', 'info');
    } catch (err) {
      showToast(`Could not clear chat: ${err.message}`, 'warn');
    }
  },
  onAnalyze: async (ticker, depth) => {
    const res = await api.analyzeTicker(ticker, depth);
    refreshTraces();
    return res;
  },
  onInspectTrace: (traceId) => openTraceInWaterfall(traceId),
  onPrefillOrder: (msg) => {
    ledgerCtrl.prefillFromSignal(msg);
    showToast(`Prefilled order form from ${msg.action} signal.`, 'info');
  },
});

const ledgerCtrl = new LedgerPanelController(document.getElementById('ledger-panel-root'), {
  onCalculate: (params, signal) => api.getCalculator(params, signal),
  onSelectTicker: (sym) => selectTicker(sym),
  onSubmitAction: async (actionName, payload) => {
    const endpointMap = {
      'fill-mini': api.fillMini,
      'close-mini': api.closeMini,
      stop: api.setStop,
      'fill-shares': api.fillShares,
      'close-shares': api.closeShares,
      'mini-params': api.setMiniParams,
      'add-ticker': api.addTicker,
    };
    const fn = endpointMap[actionName];
    if (!fn) return;
    try {
      const res = await fn(payload);
      if (res && (res.portfolio || res.watchlist)) {
        applyPortfolioPayload(res);
      } else {
        await refreshPortfolio();
      }
      if (actionName === 'add-ticker' && payload.ticker) {
        await selectTicker(payload.ticker);
      } else if (actionName === 'stop' || actionName === 'mini-params') {
        await loadChart(state.selectedTicker);
      }
      showToast(res?.message || `Updated ledger (${actionName}).`, 'ok');
    } catch (err) {
      showToast(`Ledger error: ${err.message}`, 'err');
    }
  },
});

// --- State Synchronization Helpers ---
function resolveDefaultTicker(portfolio) {
  const watchlist = portfolio?.watchlist || {};
  const focusList = String(portfolio?.active_focus_ticker || '')
    .split(',')
    .map((s) => s.trim().toUpperCase())
    .filter((s) => s && watchlist[s]);
  if (focusList.length > 0) return focusList[0];
  const heldMini = Object.keys(watchlist).find((s) => Number(watchlist[s]?.mini_units_held || 0) > 0);
  if (heldMini) return heldMini;
  return Object.keys(watchlist)[0] || 'NVDA';
}

function normalizeLatestMap(raw) {
  if (!raw || typeof raw !== 'object') return state.latestByTicker;
  const out = { ...state.latestByTicker };
  for (const [k, v] of Object.entries(raw)) {
    if (k) out[k.toUpperCase()] = v;
  }
  return out;
}

function applyPortfolioPayload(data) {
  if (!data || typeof data !== 'object') return;
  const portfolio = data.portfolio && typeof data.portfolio === 'object' ? data.portfolio : data.watchlist ? data : state.portfolio;
  if (portfolio) state.portfolio = portfolio;
  if (data.risk || data.risk_profile) state.risk = data.risk || data.risk_profile;
  if (data.status) state.status = data.status;

  const latestRaw = data.latest_by_ticker || data.latest_audit_by_ticker || data.telemetry_by_ticker;
  if (latestRaw) {
    state.latestByTicker = normalizeLatestMap(latestRaw);
  } else if (data.positions && typeof data.positions === 'object') {
    const derived = {};
    for (const [sym, pos] of Object.entries(data.positions)) {
      if (pos && pos.spot) {
        derived[sym] = {
          ts: pos.telemetry_ts,
          ticker: sym,
          close: pos.spot,
          telemetry: { close: pos.spot, time: pos.telemetry_ts },
          decision: pos.latest_decision || null,
        };
      }
    }
    state.latestByTicker = normalizeLatestMap(derived);
  }

  const fxCand = Number(data.usd_sek ?? data.fx_multiplier ?? data.status?.usd_sek);
  if (Number.isFinite(fxCand) && fxCand > 1) {
    state.usdSek = fxCand;
  }

  if (!state.selectedTicker || !state.portfolio?.watchlist?.[state.selectedTicker]) {
    state.selectedTicker = resolveDefaultTicker(state.portfolio);
  }

  syncAllBoundViews();
}

function syncAllBoundViews() {
  const sym = state.selectedTicker || 'NVDA';
  const asset = state.portfolio?.watchlist?.[sym] || {};
  const latest = state.latestByTicker?.[sym.toUpperCase()];
  const spot = latest?.telemetry?.close ?? latest?.close ?? null;

  topbar.update({
    accountId: state.portfolio?.account_id || 'ISK 5872428',
    status: state.status,
    usdSek: state.usdSek,
    focusTicker: sym,
    spot,
    currency: asset.currency || 'USD',
    hardStop: asset.hard_stop_price ?? null,
    realizedPnlSek: state.portfolio?.daily_stats?.realized_pnl_today_sek ?? 0,
  });

  watchlistCtrl.update({
    portfolio: state.portfolio,
    risk: state.risk,
    latestByTicker: state.latestByTicker,
    selectedTicker: sym,
    usdSek: state.usdSek,
  });

  benchmarksCtrl.setSelectedTicker(sym);
  agentChatCtrl.setSelectedTicker(sym);
  ledgerCtrl.updateContext({
    portfolio: state.portfolio,
    risk: state.risk,
    latestByTicker: state.latestByTicker,
    selectedTicker: sym,
    usdSek: state.usdSek,
  });
}

async function selectTicker(sym) {
  const clean = String(sym || '').trim();
  if (!clean || clean === state.selectedTicker) return;
  state.selectedTicker = clean;
  syncAllBoundViews();
  await loadChart(clean);
}

async function toggleFocusTicker(sym) {
  if (!state.portfolio) return;
  const target = String(sym || '').toUpperCase();
  const current = String(state.portfolio.active_focus_ticker || '')
    .split(',')
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);

  let next;
  if (current.includes(target)) {
    next = current.filter((s) => s !== target);
    if (next.length === 0) {
      showToast('At least one focus ticker must remain active.', 'warn');
      return;
    }
  } else {
    next = [...current, target];
  }

  watchlistCtrl.update({ focusPending: true });
  try {
    const res = await api.setFocus(next.join(','));
    if (res && (res.portfolio || res.watchlist)) {
      applyPortfolioPayload(res);
    } else {
      state.portfolio.active_focus_ticker = next.join(',');
      syncAllBoundViews();
    }
    showToast(`Focus updated: ${next.join(', ')}`, 'ok');
  } catch (err) {
    showToast(`Focus update failed: ${err.message}`, 'err');
  } finally {
    watchlistCtrl.update({ focusPending: false });
  }
}

// --- Data Loaders ---
async function refreshPortfolio(opts) {
  const data = await api.getPortfolio(opts);
  applyPortfolioPayload(data);
  return data;
}

async function loadChart(ticker) {
  if (!ticker) return;
  if (chartAbort) chartAbort.abort();
  const controller = new AbortController();
  chartAbort = controller;

  chartCtrl.setLoading(true);
  try {
    const payload = await api.getChart(ticker, state.chartRange, state.chartInterval, controller.signal);
    if (controller.signal.aborted) return;
    if (payload?.usd_sek && Number(payload.usd_sek) > 1) {
      state.usdSek = Number(payload.usd_sek);
      topbar.update({ usdSek: state.usdSek });
    }
    const bars = payload?.bars || payload?.candles || payload?.ohlcv || [];
    if (bars.length > 0 && !state.latestByTicker[ticker.toUpperCase()]) {
      const last = bars[bars.length - 1];
      state.latestByTicker[ticker.toUpperCase()] = {
        ts: last.ts ?? last.time ?? last.t,
        ticker: ticker.toUpperCase(),
        telemetry: last,
      };
      syncAllBoundViews();
    }
    const asset = state.portfolio?.watchlist?.[ticker] || null;
    const latestAudit = state.latestByTicker?.[ticker.toUpperCase()] || null;
    chartCtrl.setData({ ticker, asset, chartPayload: payload, latestAudit });
  } catch (err) {
    if (err?.name === 'AbortError') return;
    chartCtrl.setData({
      ticker,
      asset: state.portfolio?.watchlist?.[ticker] || null,
      chartPayload: { bars: [], error: err?.message || 'Failed to load chart data' },
      latestAudit: state.latestByTicker?.[ticker.toUpperCase()] || null,
    });
  }
}

async function refreshBenchmarks() {
  const data = await api.getBenchmarks();
  benchmarksCtrl.setBenchmarks(data);
}

async function refreshTraces() {
  const data = await api.getTraces(30);
  if (Array.isArray(data)) {
    tracesCtrl.setTraces({ traces: data, activeTraces: [] });
  } else {
    tracesCtrl.setTraces({
      traces: data?.traces || data?.items || [],
      activeTraces: data?.active_traces || [],
    });
  }
}

async function refreshHealth() {
  const data = await api.getHealth();
  healthCtrl.setHealth(data);
  if (data?.status) {
    state.status = data.status;
    topbar.update({ status: data.status });
  }
}

async function refreshDeclarative() {
  const data = await api.getDeclarative();
  declarativeCtrl.setData(data);
}

async function hydrateAll() {
  try {
    await refreshPortfolio();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      return;
    }
    showToast(`Portfolio load error: ${err.message}`, 'warn');
  }

  await Promise.allSettled([
    loadChart(state.selectedTicker || 'NVDA'),
    refreshBenchmarks(),
    refreshTraces(),
    refreshHealth(),
    refreshDeclarative(),
  ]);

  state.hydrated = true;
  sseClient.start();
}

// --- SSE Stream Handler ---
sseClient = new SseStreamClient({
  onStatusChange: (statusName) => {
    topbar.update({ sseStatus: statusName });
  },
  onAuthError: (payload) => {
    topbar.update({ sseStatus: 'auth_required' });
    openAuthOverlay({
      locked: true,
      errorText: payload?.detail || payload?.message || 'Session locked — enter Bearer token.',
    });
  },
  onEvent: (eventName, payload) => {
    if (!payload || typeof payload !== 'object') return;

    if (eventName === 'snapshot.ready') {
      applyPortfolioPayload(payload);
      if (payload.health) healthCtrl.setHealth(payload.health);
      if (payload.traces || payload.active_traces) {
        tracesCtrl.setTraces({
          traces: payload.traces || [],
          activeTraces: payload.active_traces || [],
        });
      }
      return;
    }

    if (eventName === 'trace.stage') {
      tracesCtrl.handleTraceStage(payload);
      return;
    }

    if (eventName === 'trace.completed') {
      tracesCtrl.handleTraceCompleted(payload);
      return;
    }

    if (eventName === 'bar.processed') {
      const sym = String(payload.ticker || payload.telemetry?.ticker || '').toUpperCase();
      if (sym) {
        state.latestByTicker[sym] = {
          ts: payload.ts || payload.telemetry?.time,
          ticker: sym,
          telemetry: payload.telemetry || payload,
          decision: payload.decision || null,
          trace_id: payload.trace_id,
        };
      }
      if (payload.status) state.status = payload.status;
      if (payload.metrics?.fx_multiplier && Number(payload.metrics.fx_multiplier) > 1) {
        state.usdSek = Number(payload.metrics.fx_multiplier);
      }
      syncAllBoundViews();
      chartCtrl.appendLiveBar(payload);
      return;
    }

    if (eventName === 'portfolio.updated') {
      if (payload.watchlist || payload.portfolio) {
        applyPortfolioPayload(payload);
      } else {
        refreshPortfolio({ suppressAuthPrompt: true }).catch(() => {});
      }
      return;
    }

    if (eventName === 'health.tick') {
      healthCtrl.setHealth(payload);
      const tickStatus = payload.status || (payload.is_red !== undefined ? payload : null);
      if (tickStatus) {
        state.status = tickStatus;
        topbar.update({ status: tickStatus });
      }
      return;
    }

    if (eventName === 'personas.reloaded' || eventName === 'declarative.reloaded' || eventName === 'declarative.updated') {
      if (payload.agents || payload.skills) {
        declarativeCtrl.setData(payload);
      } else {
        refreshDeclarative().catch(() => {});
      }
      showToast('Declarative agents & skills updated.', 'info');
    }
  },
});

// --- Auth Form Events ---
if (authForm) {
  authForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const candidate = authInput.value.trim();
    if (!candidate) return;
    setToken(candidate);
    authError.hidden = true;

    try {
      await api.getPortfolio({ suppressAuthPrompt: true });
      closeAuthOverlay();
      showToast('Session unlocked.', 'ok');
      await hydrateAll();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        clearToken();
        authError.textContent = 'Invalid Bearer token. Please check GEMINA_UI_TOKEN and try again.';
        authError.hidden = false;
        authInput.focus();
      } else {
        closeAuthOverlay();
        await hydrateAll();
      }
    }
  });
}

if (authClearBtn) {
  authClearBtn.addEventListener('click', () => {
    clearToken();
    authInput.value = '';
    authError.textContent = 'Cleared sessionStorage token.';
    authError.hidden = false;
  });
}

window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && authOverlay && !authOverlay.hidden && !state.authLocked) {
    closeAuthOverlay();
  }
});

// Boot Application
hydrateAll();
