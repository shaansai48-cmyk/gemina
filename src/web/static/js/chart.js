/**
 * High-DPI Canvas Financial Chart Component (`chart.js`).
 * Renders 15m candlesticks, volume bars, EMA4/EMA50/VWAP/OR/PD/Stop/Target/Barrier overlays,
 * Audit Signal markers, interactive hover crosshair OHLCV readout, zoom/pan, and signal rationale popover.
 */
import { clearEl, el, formatCetTime, formatNumber, formatPrice, renderSafeMarkdown } from './dom.js';

const OVERLAY_DEFS = [
  { key: 'ema4', label: 'EMA4', swatch: 'swatch-ema4', defaultOn: true },
  { key: 'ema50', label: 'EMA50', swatch: 'swatch-ema50', defaultOn: true },
  { key: 'vwap', label: 'VWAP', swatch: 'swatch-vwap', defaultOn: true },
  { key: 'orLevels', label: 'OR High/Low', swatch: 'swatch-or', defaultOn: true },
  { key: 'pdLevels', label: 'PD High/Low', swatch: 'swatch-pd', defaultOn: true },
  { key: 'stopTarget', label: 'Hard Stop / Target', swatch: 'swatch-stop', defaultOn: true },
  { key: 'miniBarrier', label: 'Mini Barrier', swatch: 'swatch-barrier', defaultOn: false },
  { key: 'auditSignals', label: 'Audit Signals', swatch: 'swatch-sig', defaultOn: true },
];

function toEpochMs(tsVal) {
  if (typeof tsVal === 'number') {
    return tsVal > 1e12 ? tsVal : tsVal * 1000;
  }
  const parsed = Date.parse(String(tsVal || ''));
  return Number.isFinite(parsed) ? parsed : 0;
}

function normalizeBar(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const close = Number(raw.close ?? raw.c);
  if (!Number.isFinite(close)) return null;
  const open = Number(raw.open ?? raw.o ?? close);
  const high = Number(raw.high ?? raw.h ?? Math.max(open, close));
  const low = Number(raw.low ?? raw.l ?? Math.min(open, close));
  const volume = Number(raw.volume ?? raw.v ?? 0);
  const timeRaw = raw.ts ?? raw.time ?? raw.t ?? raw.timestamp;
  const epochMs = toEpochMs(timeRaw);

  return {
    epochMs,
    timeRaw,
    open: Number.isFinite(open) ? open : close,
    high: Number.isFinite(high) ? high : close,
    low: Number.isFinite(low) ? low : close,
    close,
    volume: Number.isFinite(volume) ? volume : 0,
    ema4: Number.isFinite(Number(raw.ema4)) ? Number(raw.ema4) : null,
    ema50: Number.isFinite(Number(raw.ema50)) ? Number(raw.ema50) : null,
    vwap: Number.isFinite(Number(raw.vwap)) ? Number(raw.vwap) : null,
    or_high: Number.isFinite(Number(raw.or_high)) ? Number(raw.or_high) : null,
    or_low: Number.isFinite(Number(raw.or_low)) ? Number(raw.or_low) : null,
    pd_high: Number.isFinite(Number(raw.pd_high)) ? Number(raw.pd_high) : null,
    pd_low: Number.isFinite(Number(raw.pd_low)) ? Number(raw.pd_low) : null,
    rsi14: Number.isFinite(Number(raw.rsi14)) ? Number(raw.rsi14) : null,
    atr14: Number.isFinite(Number(raw.atr14)) ? Number(raw.atr14) : null,
  };
}

function fillMissingEma(bars, period, fieldName) {
  const k = 2 / (period + 1);
  let ema = null;
  for (const bar of bars) {
    if (bar[fieldName] !== null) {
      ema = bar[fieldName];
      continue;
    }
    ema = ema === null ? bar.close : bar.close * k + ema * (1 - k);
    bar[fieldName] = Number(ema.toFixed(4));
  }
}

function normalizeSignal(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const dec = raw.decision && typeof raw.decision === 'object' ? raw.decision : raw;
  const tel = raw.telemetry && typeof raw.telemetry === 'object' ? raw.telemetry : raw;
  const timeRaw = raw.ts ?? raw.time ?? tel.time ?? tel.ts;
  const epochMs = toEpochMs(timeRaw);
  const price = Number(raw.price ?? raw.close ?? tel.close ?? dec.limit_price);
  const action = String(dec.action ?? raw.action ?? 'HOLD').toUpperCase();
  return {
    epochMs,
    timeRaw,
    price: Number.isFinite(price) ? price : null,
    action,
    instrument: dec.instrument ?? raw.instrument ?? '',
    quantity: Number(dec.quantity ?? raw.quantity ?? 0),
    confidence: dec.confidence_score ?? raw.confidence_score ?? null,
    criticVerdict: dec.critic_verdict ?? raw.critic_verdict ?? '',
    criticNotes: dec.critic_notes ?? raw.critic_notes ?? '',
    netRr: dec.net_risk_reward_ratio ?? raw.net_risk_reward_ratio ?? null,
    rationale: dec.rationale ?? raw.rationale ?? '',
    avanzaGuide: dec.avanza_execution_guide ?? raw.avanza_execution_guide ?? '',
    traceId: raw.trace_id ?? dec.trace_id ?? '',
    regime: dec.market_regime ?? raw.market_regime ?? '',
  };
}

function formatAxisTime(epochMs) {
  if (!epochMs) return '';
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Europe/Stockholm',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(epochMs));
}

function formatCompactVol(vol) {
  const v = Number(vol);
  if (!Number.isFinite(v) || v <= 0) return '0';
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return String(Math.round(v));
}

export class QuantChartController {
  constructor(rootEl, { onInspectTrace, onRangeChange, onRefresh } = {}) {
    this.root = rootEl;
    this.onInspectTrace = onInspectTrace;
    this.onRangeChange = onRangeChange;
    this.onRefresh = onRefresh;

    this.ticker = 'NVDA';
    this.currency = 'USD';
    this.exchange = 'NASDAQ';
    this.range = '5d';
    this.interval = '15m';

    this.bars = [];
    this.levels = {};
    this.signals = [];
    this.asset = null;
    this.loading = false;
    this.error = '';

    this.overlays = Object.fromEntries(OVERLAY_DEFS.map((d) => [d.key, d.defaultOn]));
    this.hoverIdx = -1;
    this.viewStart = 0;
    this.viewEnd = 0;
    this.dragging = false;
    this.dragStartX = 0;
    this.dragStartViewStart = 0;
    this.dragMoved = false;

    this.signalHitboxes = [];
    this.selectedSignal = null;

    this._canvas = null;
    this._ohlcvNode = null;
    this._emptyNode = null;
    this._popoverNode = null;
    this._headerMetaNode = null;
    this._resizeObserver = null;

    this._buildShell();
  }

  _buildShell() {
    clearEl(this.root);

    // Header
    this._titleEl = el('span', { className: 'chart-ticker-name', text: this.ticker });
    this._exchBadge = el('span', { className: 'badge', text: 'NASDAQ · USD' });
    this._spotBadge = el('span', { className: 'badge badge-cyan mono', text: '-' });
    this._chgBadge = el('span', { className: 'badge mono', text: '-' });
    this._rsiPill = el('span', { className: 'pill mono', text: 'RSI -' });
    this._atrPill = el('span', { className: 'pill mono', text: 'ATR -' });

    const rangeSelect = el(
      'select',
      {
        className: 'select btn-sm',
        ariaLabel: 'Chart range and interval',
        onChange: (e) => {
          const [r, iv] = e.target.value.split('|');
          this.range = r;
          this.interval = iv;
          this.onRangeChange?.(r, iv);
        },
      },
      el('option', { value: '5d|15m', text: '5D · 15m', selected: true }),
      el('option', { value: '1d|5m', text: '1D · 5m' }),
      el('option', { value: '1mo|15m', text: '1M · 15m' }),
      el('option', { value: '60d|1h', text: '60D · 1h' }),
    );

    const resetZoomBtn = el('button', {
      type: 'button',
      className: 'btn btn-xs btn-ghost',
      text: 'Reset Zoom',
      onClick: () => {
        this.viewStart = 0;
        this.viewEnd = this.bars.length;
        this.draw();
      },
    });

    const refreshBtn = el('button', {
      type: 'button',
      className: 'btn btn-xs',
      text: '↻ Refresh',
      onClick: () => this.onRefresh?.(),
    });

    const header = el(
      'div',
      { className: 'chart-header' },
      el(
        'div',
        { className: 'chart-title-group' },
        this._titleEl,
        this._exchBadge,
        this._spotBadge,
        this._chgBadge,
        this._rsiPill,
        this._atrPill,
      ),
      el('div', { className: 'chart-controls' }, rangeSelect, resetZoomBtn, refreshBtn),
    );

    // Overlay Toggles
    const checksWrap = el('div', { className: 'overlay-checks' });
    for (const def of OVERLAY_DEFS) {
      const cb = el('input', {
        type: 'checkbox',
        checked: this.overlays[def.key],
        onChange: (e) => {
          this.overlays[def.key] = e.target.checked;
          if (!this.overlays.auditSignals) {
            this._hidePopover();
          }
          this.draw();
        },
      });
      checksWrap.appendChild(
        el(
          'label',
          { className: 'overlay-check' },
          cb,
          el('span', { className: `swatch-dot ${def.swatch}` }),
          def.label,
        ),
      );
    }

    this._barrierEdgeNote = el('span', { className: 'pill mono', hidden: true });
    const overlayBar = el('div', { className: 'chart-overlay-bar' }, checksWrap, this._barrierEdgeNote);

    // OHLCV Readout Strip
    this._ohlcvNode = el('div', { className: 'ohlcv-bar mono' });

    // Canvas Container
    this._canvas = el('canvas', { className: 'chart-canvas' });
    this._emptyNode = el('div', { className: 'chart-empty-state', hidden: true });
    this._popoverNode = el('div', { className: 'chart-signal-popover', hidden: true });

    const canvasWrap = el(
      'div',
      { className: 'chart-canvas-wrap' },
      this._canvas,
      this._emptyNode,
      this._popoverNode,
    );

    this.root.append(header, overlayBar, this._ohlcvNode, canvasWrap);

    this._bindCanvasEvents();
    this._resizeObserver = new ResizeObserver(() => this.draw());
    this._resizeObserver.observe(canvasWrap);
    this._renderOhlcvBar(null);
  }

  _bindCanvasEvents() {
    const c = this._canvas;

    c.addEventListener('mousemove', (e) => {
      const rect = c.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;

      if (this.dragging && this.bars.length > 0) {
        const dx = mx - this.dragStartX;
        if (Math.abs(dx) > 4) this.dragMoved = true;
        const span = this.viewEnd - this.viewStart;
        const chartW = Math.max(1, rect.width - 78);
        const shiftBars = Math.round((-dx / chartW) * span);
        let nextStart = this.dragStartViewStart + shiftBars;
        nextStart = Math.max(0, Math.min(this.bars.length - span, nextStart));
        this.viewStart = nextStart;
        this.viewEnd = nextStart + span;
        this.draw();
        return;
      }

      const hit = this._findSignalAt(mx, my);
      c.style.cursor = hit ? 'pointer' : 'crosshair';

      const idx = this._xToBarIndex(mx, rect.width);
      if (idx !== this.hoverIdx) {
        this.hoverIdx = idx;
        this._renderOhlcvBar(idx >= 0 ? this.bars[idx] : this.bars[this.bars.length - 1]);
        this.draw();
      }
    });

    c.addEventListener('mouseleave', () => {
      this.dragging = false;
      this.hoverIdx = -1;
      this._renderOhlcvBar(this.bars[this.bars.length - 1] || null);
      this.draw();
    });

    c.addEventListener('mousedown', (e) => {
      this.dragging = true;
      this.dragMoved = false;
      const rect = c.getBoundingClientRect();
      this.dragStartX = e.clientX - rect.left;
      this.dragStartViewStart = this.viewStart;
    });

    window.addEventListener('mouseup', () => {
      this.dragging = false;
    });

    c.addEventListener('click', (e) => {
      if (this.dragMoved) return;
      const rect = c.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const hit = this._findSignalAt(mx, my);
      if (hit) {
        this._showSignalPopover(hit.signal);
      } else {
        this._hidePopover();
      }
    });

    c.addEventListener(
      'wheel',
      (e) => {
        if (this.bars.length <= 15) return;
        e.preventDefault();
        const span = this.viewEnd - this.viewStart;
        const delta = e.deltaY > 0 ? Math.max(4, Math.round(span * 0.14)) : -Math.max(4, Math.round(span * 0.14));
        const nextSpan = Math.max(15, Math.min(this.bars.length, span + delta));
        const anchorEnd = this.viewEnd;
        let nextStart = Math.max(0, anchorEnd - nextSpan);
        let nextEnd = Math.min(this.bars.length, nextStart + nextSpan);
        if (nextEnd - nextStart < nextSpan) {
          nextStart = Math.max(0, nextEnd - nextSpan);
        }
        this.viewStart = nextStart;
        this.viewEnd = nextEnd;
        this.draw();
      },
      { passive: false },
    );
  }

  _findSignalAt(mx, my) {
    for (const hb of this.signalHitboxes) {
      const dist = Math.hypot(mx - hb.x, my - hb.y);
      if (dist <= 13) return hb;
    }
    return null;
  }

  _xToBarIndex(mx, cssWidth) {
    const padLeft = 10;
    const padRight = 70;
    const plotW = Math.max(1, cssWidth - padLeft - padRight);
    const sliceLen = Math.max(1, this.viewEnd - this.viewStart);
    if (mx < padLeft || mx > padLeft + plotW) return -1;
    const rel = (mx - padLeft) / plotW;
    const localIdx = Math.min(sliceLen - 1, Math.max(0, Math.floor(rel * sliceLen)));
    return this.viewStart + localIdx;
  }

  setLoading(isLoading) {
    this.loading = isLoading;
    if (isLoading && this.bars.length === 0) {
      this._showEmptyState(`Loading ${this.ticker} 15m chart telemetry...`);
    }
  }

  setData({ ticker, asset, chartPayload, latestAudit }) {
    this.ticker = ticker || this.ticker;
    this.asset = asset || this.asset;
    this.currency = chartPayload?.currency || asset?.currency || 'USD';
    this.exchange = chartPayload?.exchange || asset?.exchange || (this.currency === 'SEK' ? 'OMXSTO' : 'NASDAQ');

    this.error = chartPayload?.error || '';

    const rawBars = chartPayload?.bars || chartPayload?.candles || chartPayload?.ohlcv || [];
    const parsed = rawBars.map(normalizeBar).filter(Boolean);
    parsed.sort((a, b) => a.epochMs - b.epochMs);

    if (parsed.length > 0) {
      fillMissingEma(parsed, 4, 'ema4');
      fillMissingEma(parsed, 50, 'ema50');
    }

    this.bars = parsed;
    this.viewStart = Math.max(0, parsed.length - 110);
    this.viewEnd = parsed.length;
    this.hoverIdx = -1;

    const rawLevels = chartPayload?.levels || {};
    const lastBar = parsed[parsed.length - 1] || {};
    const tel = latestAudit?.telemetry || {};

    this.levels = {
      or_high: Number(rawLevels.or_high ?? lastBar.or_high ?? tel.or_high ?? 0) || null,
      or_low: Number(rawLevels.or_low ?? lastBar.or_low ?? tel.or_low ?? 0) || null,
      pd_high: Number(rawLevels.pd_high ?? lastBar.pd_high ?? tel.pd_high ?? 0) || null,
      pd_low: Number(rawLevels.pd_low ?? lastBar.pd_low ?? tel.pd_low ?? 0) || null,
      hard_stop_price: Number(rawLevels.hard_stop_price ?? asset?.hard_stop_price ?? 0) || null,
      take_profit_price: Number(rawLevels.take_profit_price ?? asset?.take_profit_price ?? 0) || null,
      mini_barrier: Number(rawLevels.mini_barrier ?? asset?.mini_barrier ?? 0) || null,
      mini_short_barrier: Number(rawLevels.mini_short_barrier ?? asset?.mini_short_barrier ?? 0) || null,
    };

    const rawSignals = chartPayload?.signals || chartPayload?.audit_markers || chartPayload?.markers || [];
    this.signals = rawSignals.map(normalizeSignal).filter(Boolean);

    this._updateHeaderMeta();
    this._renderOhlcvBar(lastBar || null);

    if (this.bars.length === 0) {
      this._showEmptyState(this.error || `No 15m chart candles returned yet for ${this.ticker}.`);
    } else {
      this._emptyNode.hidden = true;
    }

    this.draw();
  }

  appendLiveBar(payload) {
    if (!payload || String(payload.ticker || '').toUpperCase() !== this.ticker.toUpperCase()) return;
    const bar = normalizeBar(payload.telemetry || payload);
    if (bar && bar.epochMs > 0) {
      const existingIdx = this.bars.findIndex((b) => b.epochMs === bar.epochMs);
      if (existingIdx >= 0) {
        this.bars[existingIdx] = { ...this.bars[existingIdx], ...bar };
      } else {
        this.bars.push(bar);
        this.bars.sort((a, b) => a.epochMs - b.epochMs);
        this.viewEnd = this.bars.length;
        this.viewStart = Math.max(0, this.viewEnd - 110);
      }
      fillMissingEma(this.bars, 4, 'ema4');
      fillMissingEma(this.bars, 50, 'ema50');
    }
    if (payload.decision) {
      const sig = normalizeSignal({
        ts: payload.ts || payload.telemetry?.time,
        price: payload.telemetry?.close,
        decision: payload.decision,
        trace_id: payload.trace_id,
      });
      if (sig) {
        this.signals.push(sig);
      }
    }
    this._emptyNode.hidden = this.bars.length > 0;
    this._updateHeaderMeta();
    this._renderOhlcvBar(this.bars[this.bars.length - 1] || null);
    this.draw();
  }

  _updateHeaderMeta() {
    this._titleEl.textContent = this.ticker;
    this._exchBadge.textContent = `${this.exchange} · ${this.currency}`;

    const last = this.bars[this.bars.length - 1];
    const first = this.bars[0];
    if (last) {
      this._spotBadge.textContent = formatPrice(last.close, this.currency);
      const refClose = first ? first.open : last.open;
      const pct = refClose > 0 ? ((last.close - refClose) / refClose) * 100 : 0;
      this._chgBadge.textContent = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
      this._chgBadge.className = `badge mono ${pct >= 0 ? 'badge-green' : 'badge-red'}`;
      this._rsiPill.textContent = last.rsi14 !== null ? `RSI ${last.rsi14.toFixed(1)}` : 'RSI -';
      this._atrPill.textContent = last.atr14 !== null ? `ATR ${last.atr14.toFixed(2)}` : 'ATR -';
    } else {
      this._spotBadge.textContent = '-';
      this._chgBadge.textContent = '-';
      this._chgBadge.className = 'badge mono';
      this._rsiPill.textContent = 'RSI -';
      this._atrPill.textContent = 'ATR -';
    }
  }

  _renderOhlcvBar(bar) {
    clearEl(this._ohlcvNode);
    if (!bar) {
      this._ohlcvNode.appendChild(el('span', { className: 'txt-muted', text: 'Hover chart for OHLCV & indicator telemetry' }));
      return;
    }
    const items = [
      ['CET', formatCetTime(bar.epochMs || bar.timeRaw, true)],
      ['O', formatNumber(bar.open)],
      ['H', formatNumber(bar.high)],
      ['L', formatNumber(bar.low)],
      ['C', formatNumber(bar.close)],
      ['Vol', formatCompactVol(bar.volume)],
      ['EMA4', bar.ema4 !== null ? formatNumber(bar.ema4) : '-'],
      ['EMA50', bar.ema50 !== null ? formatNumber(bar.ema50) : '-'],
      ['VWAP', bar.vwap !== null ? formatNumber(bar.vwap) : '-'],
    ];
    for (const [k, v] of items) {
      this._ohlcvNode.appendChild(
        el(
          'span',
          { className: 'ohlcv-kv' },
          el('span', { className: 'ohlcv-k', text: k }),
          el('span', { text: v }),
        ),
      );
    }
  }

  _showEmptyState(msg) {
    clearEl(this._emptyNode);
    this._emptyNode.hidden = false;
    this._emptyNode.append(
      el('div', { className: 'txt-secondary', text: msg }),
      el('button', {
        type: 'button',
        className: 'btn btn-sm btn-cyan',
        text: 'Retry Chart Fetch',
        onClick: () => this.onRefresh?.(),
      }),
    );
  }

  _showSignalPopover(sig) {
    this.selectedSignal = sig;
    clearEl(this._popoverNode);
    this._popoverNode.hidden = false;

    const isBuy = sig.action.startsWith('BUY');
    const isSell = sig.action.startsWith('SELL');
    const badgeClass = isBuy ? 'badge-green' : isSell ? 'badge-red' : 'badge-cyan';

    const rationaleBox = el('div', { className: 'chart-signal-rationale' });
    renderSafeMarkdown(rationaleBox, sig.rationale || sig.criticNotes || 'No rationale recorded.');

    const footerActions = el('div', { className: 'auth-actions' });
    if (sig.traceId && this.onInspectTrace) {
      footerActions.appendChild(
        el('button', {
          type: 'button',
          className: 'btn btn-xs btn-cyan',
          text: `Inspect Trace ${sig.traceId} →`,
          onClick: () => {
            this._hidePopover();
            this.onInspectTrace(sig.traceId);
          },
        }),
      );
    }

    this._popoverNode.append(
      el(
        'div',
        { className: 'chart-signal-head' },
        el('span', { className: `badge ${badgeClass} mono`, text: sig.action }),
        el('span', { className: 'mono txt-muted', text: formatCetTime(sig.epochMs || sig.timeRaw, true) }),
        el('button', {
          type: 'button',
          className: 'btn btn-xs btn-ghost',
          text: '✕',
          ariaLabel: 'Close signal popup',
          onClick: () => this._hidePopover(),
        }),
      ),
      el(
        'div',
        { className: 'panel-subtitle mono' },
        `${sig.instrument || this.ticker} | Price: ${sig.price ?? '-'} | Conf: ${sig.confidence ?? '-'}% | Net R:R: ${sig.netRr ?? '-'}`,
      ),
      rationaleBox,
      footerActions,
    );
  }

  _hidePopover() {
    this.selectedSignal = null;
    this._popoverNode.hidden = true;
  }

  draw() {
    const canvas = this._canvas;
    const parent = canvas.parentElement;
    if (!parent) return;

    const cssW = parent.clientWidth || 640;
    const cssH = parent.clientHeight || 390;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
    }

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    ctx.fillStyle = '#090d14';
    ctx.fillRect(0, 0, cssW, cssH);

    this.signalHitboxes = [];
    this._barrierEdgeNote.hidden = true;

    const visible = this.bars.slice(this.viewStart, this.viewEnd);
    if (visible.length === 0) return;

    const padLeft = 10;
    const padRight = 72;
    const padTop = 14;
    const padBottom = 22;
    const plotW = Math.max(20, cssW - padLeft - padRight);
    const totalPlotH = Math.max(80, cssH - padTop - padBottom);
    const volH = Math.round(totalPlotH * 0.20);
    const priceH = totalPlotH - volH - 8;
    const priceTop = padTop;
    const priceBottom = priceTop + priceH;
    const volTop = priceBottom + 8;
    const volBottom = volTop + volH;

    // Compute Y bounds from candles & nearby overlays
    let minP = Infinity;
    let maxP = -Infinity;
    let maxVol = 0;

    for (const b of visible) {
      if (b.low < minP) minP = b.low;
      if (b.high > maxP) maxP = b.high;
      if (b.volume > maxVol) maxVol = b.volume;
      if (this.overlays.ema4 && b.ema4 !== null) {
        minP = Math.min(minP, b.ema4);
        maxP = Math.max(maxP, b.ema4);
      }
      if (this.overlays.ema50 && b.ema50 !== null) {
        minP = Math.min(minP, b.ema50);
        maxP = Math.max(maxP, b.ema50);
      }
      if (this.overlays.vwap && b.vwap !== null) {
        minP = Math.min(minP, b.vwap);
        maxP = Math.max(maxP, b.vwap);
      }
    }

    const baseSpan = Math.max(0.01, maxP - minP);
    const includeIfNear = (val) => {
      if (!Number.isFinite(val) || val <= 0) return;
      if (val >= minP - baseSpan * 0.65 && val <= maxP + baseSpan * 0.65) {
        minP = Math.min(minP, val);
        maxP = Math.max(maxP, val);
      }
    };

    if (this.overlays.orLevels) {
      includeIfNear(this.levels.or_high);
      includeIfNear(this.levels.or_low);
    }
    if (this.overlays.pdLevels) {
      includeIfNear(this.levels.pd_high);
      includeIfNear(this.levels.pd_low);
    }
    if (this.overlays.stopTarget) {
      includeIfNear(this.levels.hard_stop_price);
      includeIfNear(this.levels.take_profit_price);
    }

    // Mini barriers can be 15-20% away; only expand Y-scale if near, otherwise display sticky edge pill
    const barrierNotes = [];
    if (this.overlays.miniBarrier) {
      if (this.levels.mini_barrier) {
        if (this.levels.mini_barrier >= minP * 0.92 && this.levels.mini_barrier <= maxP * 1.08) {
          minP = Math.min(minP, this.levels.mini_barrier);
          maxP = Math.max(maxP, this.levels.mini_barrier);
        } else {
          barrierNotes.push(`MINI L Bar: ${formatNumber(this.levels.mini_barrier)}`);
        }
      }
      if (this.levels.mini_short_barrier) {
        if (this.levels.mini_short_barrier >= minP * 0.92 && this.levels.mini_short_barrier <= maxP * 1.08) {
          minP = Math.min(minP, this.levels.mini_short_barrier);
          maxP = Math.max(maxP, this.levels.mini_short_barrier);
        } else {
          barrierNotes.push(`MINI S Bar: ${formatNumber(this.levels.mini_short_barrier)}`);
        }
      }
      if (barrierNotes.length > 0) {
        this._barrierEdgeNote.textContent = barrierNotes.join(' · ');
        this._barrierEdgeNote.hidden = false;
      }
    }

    const finalSpan = Math.max(0.01, maxP - minP);
    minP -= finalSpan * 0.06;
    maxP += finalSpan * 0.06;

    const yForPrice = (p) => priceTop + ((maxP - p) / Math.max(1e-6, maxP - minP)) * priceH;
    const stepX = plotW / visible.length;
    const candleW = Math.max(2, Math.min(12, stepX * 0.66));
    const xForIndex = (i) => padLeft + (i + 0.5) * stepX;

    // Grid & Right Price Axis
    ctx.strokeStyle = '#151d2e';
    ctx.lineWidth = 1;
    ctx.font = '10.5px ui-monospace, Menlo, Consolas, monospace';
    ctx.fillStyle = '#66758f';
    ctx.textBaseline = 'middle';

    const gridSteps = 5;
    for (let g = 0; g <= gridSteps; g++) {
      const ratio = g / gridSteps;
      const y = Math.round(priceTop + ratio * priceH) + 0.5;
      const priceVal = maxP - ratio * (maxP - minP);
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(padLeft + plotW, y);
      ctx.stroke();
      ctx.fillText(formatNumber(priceVal, 2), padLeft + plotW + 6, y);
    }

    // Volume Pane Separator
    ctx.strokeStyle = '#1d263b';
    ctx.beginPath();
    ctx.moveTo(padLeft, priceBottom + 4);
    ctx.lineTo(padLeft + plotW, priceBottom + 4);
    ctx.stroke();

    // Time Axis Ticks
    const tickEvery = Math.max(1, Math.floor(visible.length / 6));
    ctx.textBaseline = 'top';
    for (let i = 0; i < visible.length; i += tickEvery) {
      const x = xForIndex(i);
      ctx.strokeStyle = '#141c2c';
      ctx.beginPath();
      ctx.moveTo(x, priceTop);
      ctx.lineTo(x, volBottom);
      ctx.stroke();
      ctx.fillStyle = '#66758f';
      ctx.fillText(formatAxisTime(visible[i].epochMs), Math.max(padLeft, x - 24), volBottom + 5);
    }

    // Volume Bars
    if (maxVol > 0) {
      for (let i = 0; i < visible.length; i++) {
        const b = visible[i];
        const x = xForIndex(i);
        const h = Math.max(1, (b.volume / maxVol) * (volH - 2));
        ctx.fillStyle = b.close >= b.open ? 'rgba(34, 197, 94, 0.36)' : 'rgba(239, 68, 68, 0.36)';
        ctx.fillRect(x - candleW / 2, volBottom - h, candleW, h);
      }
    }

    // Horizontal Levels Helper
    const drawHLine = (priceVal, label, color, dash = [5, 4]) => {
      if (!Number.isFinite(priceVal) || priceVal < minP || priceVal > maxP) return;
      const y = yForPrice(priceVal);
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.1;
      ctx.setLineDash(dash);
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(padLeft + plotW, y);
      ctx.stroke();
      ctx.setLineDash([]);

      // Right axis tag
      const tagTxt = `${label} ${formatNumber(priceVal, 2)}`;
      ctx.fillStyle = '#0b0f17';
      ctx.fillRect(padLeft + plotW + 2, y - 8, padRight - 4, 16);
      ctx.strokeStyle = color;
      ctx.strokeRect(padLeft + plotW + 2, y - 8, padRight - 4, 16);
      ctx.fillStyle = color;
      ctx.font = '10px ui-monospace, Menlo, Consolas, monospace';
      ctx.textBaseline = 'middle';
      ctx.fillText(tagTxt, padLeft + plotW + 5, y);
      ctx.restore();
    };

    if (this.overlays.orLevels) {
      drawHLine(this.levels.or_high, 'ORH', 'rgba(45, 212, 191, 0.78)', [2, 3]);
      drawHLine(this.levels.or_low, 'ORL', 'rgba(45, 212, 191, 0.78)', [2, 3]);
    }
    if (this.overlays.pdLevels) {
      drawHLine(this.levels.pd_high, 'PDH', 'rgba(148, 163, 184, 0.75)', [6, 4]);
      drawHLine(this.levels.pd_low, 'PDL', 'rgba(148, 163, 184, 0.75)', [6, 4]);
    }
    if (this.overlays.stopTarget) {
      drawHLine(this.levels.hard_stop_price, 'STP', '#ef4444', [4, 3]);
      drawHLine(this.levels.take_profit_price, 'TP', '#22c55e', [4, 3]);
    }
    if (this.overlays.miniBarrier) {
      drawHLine(this.levels.mini_barrier, 'MLB', '#fb7185', [3, 3]);
      drawHLine(this.levels.mini_short_barrier, 'MSB', '#fb7185', [3, 3]);
    }

    // Candlesticks
    for (let i = 0; i < visible.length; i++) {
      const b = visible[i];
      const x = xForIndex(i);
      const isUp = b.close >= b.open;
      const color = isUp ? '#22c55e' : '#ef4444';

      const yH = yForPrice(b.high);
      const yL = yForPrice(b.low);
      const yO = yForPrice(b.open);
      const yC = yForPrice(b.close);

      ctx.strokeStyle = color;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.moveTo(x, yH);
      ctx.lineTo(x, yL);
      ctx.stroke();

      const bodyTop = Math.min(yO, yC);
      const bodyH = Math.max(1.5, Math.abs(yC - yO));
      ctx.fillStyle = color;
      ctx.fillRect(x - candleW / 2, bodyTop, candleW, bodyH);
    }

    // Indicator Line Overlays
    const drawSeries = (field, color, width = 1.4, dash = []) => {
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.setLineDash(dash);
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < visible.length; i++) {
        const val = visible[i][field];
        if (val === null || !Number.isFinite(val)) {
          started = false;
          continue;
        }
        const x = xForIndex(i);
        const y = yForPrice(val);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
      ctx.restore();
    };

    if (this.overlays.ema50) drawSeries('ema50', '#a78bfa', 1.4);
    if (this.overlays.vwap) drawSeries('vwap', '#f59e0b', 1.5, [5, 3]);
    if (this.overlays.ema4) drawSeries('ema4', '#38bdf8', 1.5);

    // Audit Signal Markers
    if (this.overlays.auditSignals && this.signals.length > 0) {
      for (const sig of this.signals) {
        if (!sig.epochMs) continue;
        // Match closest bar in visible window within 15m tolerance (or closest bar overall inside visible range)
        const minTs = visible[0].epochMs - 15 * 60 * 1000;
        const maxTs = visible[visible.length - 1].epochMs + 15 * 60 * 1000;
        if (sig.epochMs < minTs || sig.epochMs > maxTs) continue;

        let bestIdx = 0;
        let bestDiff = Infinity;
        for (let i = 0; i < visible.length; i++) {
          const diff = Math.abs(visible[i].epochMs - sig.epochMs);
          if (diff < bestDiff) {
            bestDiff = diff;
            bestIdx = i;
          }
        }

        const bar = visible[bestIdx];
        const x = xForIndex(bestIdx);
        const isBuy = sig.action.startsWith('BUY');
        const isSell = sig.action.startsWith('SELL');
        const y = isBuy
          ? Math.min(priceBottom - 10, yForPrice(bar.low) + 12)
          : isSell
            ? Math.max(priceTop + 10, yForPrice(bar.high) - 12)
            : Math.max(priceTop + 10, yForPrice(sig.price ?? bar.close));

        ctx.save();
        if (isBuy) {
          ctx.fillStyle = '#22c55e';
          ctx.beginPath();
          ctx.moveTo(x, y - 7);
          ctx.lineTo(x - 6, y + 5);
          ctx.lineTo(x + 6, y + 5);
          ctx.closePath();
          ctx.fill();
        } else if (isSell) {
          ctx.fillStyle = '#ef4444';
          ctx.beginPath();
          ctx.moveTo(x, y + 7);
          ctx.lineTo(x - 6, y - 5);
          ctx.lineTo(x + 6, y - 5);
          ctx.closePath();
          ctx.fill();
        } else {
          ctx.fillStyle = '#38bdf8';
          ctx.beginPath();
          ctx.moveTo(x, y - 5);
          ctx.lineTo(x + 5, y);
          ctx.lineTo(x, y + 5);
          ctx.lineTo(x - 5, y);
          ctx.closePath();
          ctx.fill();
        }
        ctx.strokeStyle = '#0b0f17';
        ctx.lineWidth = 1.2;
        ctx.stroke();
        ctx.restore();

        this.signalHitboxes.push({ x, y, signal: sig });
      }
    }

    // Hover Crosshair
    if (this.hoverIdx >= this.viewStart && this.hoverIdx < this.viewEnd) {
      const localIdx = this.hoverIdx - this.viewStart;
      const bar = visible[localIdx];
      const cx = xForIndex(localIdx);
      const cy = yForPrice(bar.close);

      ctx.save();
      ctx.strokeStyle = 'rgba(148, 163, 184, 0.45)';
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(cx, priceTop);
      ctx.lineTo(cx, volBottom);
      ctx.moveTo(padLeft, cy);
      ctx.lineTo(padLeft + plotW, cy);
      ctx.stroke();
      ctx.setLineDash([]);

      // Price pill on right axis
      ctx.fillStyle = '#38bdf8';
      ctx.fillRect(padLeft + plotW + 2, cy - 9, padRight - 4, 18);
      ctx.fillStyle = '#090d14';
      ctx.font = 'bold 10.5px ui-monospace, Menlo, Consolas, monospace';
      ctx.textBaseline = 'middle';
      ctx.fillText(formatNumber(bar.close, 2), padLeft + plotW + 6, cy);
      ctx.restore();
    }
  }
}
