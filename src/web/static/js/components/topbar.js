/**
 * Top Traffic-Light Header & Auto-Expanding Red Alert Drawer (`topbar.js`).
 * Renders a compact 1-line 🟢 strip when healthy and expands 🔴 details ONLY when is_red === true.
 */
import { clearEl, el, formatNumber, formatSek, getStockholmMarketSession, renderSafeMarkdown } from '../dom.js';

export class TopbarController {
  constructor(rootEl, { onOpenAuth, onNavigateTab } = {}) {
    this.root = rootEl;
    this.onOpenAuth = onOpenAuth;
    this.onNavigateTab = onNavigateTab;

    this.state = {
      accountId: 'ISK 5872428',
      status: { is_red: false, color: '🟢', ticker_line: '', red_reasons: [], diagnostics: {} },
      usdSek: null,
      sseStatus: 'connecting',
      focusTicker: 'NVDA',
      spot: null,
      currency: 'USD',
      hardStop: null,
      realizedPnlSek: 0,
    };

    this._clockEl = null;
    this._sessionEl = null;
    this._clockTimer = null;

    this._render();
    this._startClock();
  }

  _startClock() {
    if (this._clockTimer) clearInterval(this._clockTimer);
    this._clockTimer = setInterval(() => this._updateClockOnly(), 1000);
  }

  _updateClockOnly() {
    if (!this._clockEl || !this._sessionEl) return;
    const session = getStockholmMarketSession();
    this._clockEl.textContent = session.clock;
    this._sessionEl.textContent = session.label;
    this._sessionEl.title = session.detail;
    const toneMap = {
      green: 'badge-green',
      amber: 'badge-amber',
      cyan: 'badge-cyan',
      muted: '',
    };
    this._sessionEl.className = `badge ${toneMap[session.tone] || ''}`.trim();
  }

  update(patch) {
    Object.assign(this.state, patch);
    if (patch.status) {
      this.state.status = {
        is_red: Boolean(patch.status.is_red),
        color: patch.status.color || (patch.status.is_red ? '🔴' : '🟢'),
        ticker_line: patch.status.ticker_line || '',
        red_reasons: Array.isArray(patch.status.red_reasons) ? patch.status.red_reasons : [],
        diagnostics: patch.status.diagnostics || {},
      };
    }
    this._render();
  }

  _renderSseBadge() {
    const map = {
      live: { cls: 'badge-green', dot: 'is-green', text: 'Live' },
      connecting: { cls: 'badge-amber', dot: 'is-amber', text: 'Connecting' },
      reconnecting: { cls: 'badge-amber', dot: 'is-amber', text: 'Reconnecting' },
      auth_required: { cls: 'badge-red', dot: 'is-red', text: 'Locked' },
      offline: { cls: 'badge-red', dot: 'is-red', text: 'Offline' },
    };
    const cfg = map[this.state.sseStatus] || map.connecting;
    return el(
      'span',
      { className: `badge ${cfg.cls}`, title: 'Real-time SSE stream status' },
      el('span', { className: `status-dot ${cfg.dot}` }),
      cfg.text,
    );
  }

  _cleanTickerSnippet() {
    const raw = String(this.state.status.ticker_line || '').replace(/`/g, '').trim();
    if (raw) {
      return raw.replace(/^[🟢🔴]\s*/, '');
    }
    const diag = this.state.status.diagnostics || {};
    const gpu = diag.gpu_temp_c !== undefined ? `${diag.gpu_temp_c}°C` : 'GPU -';
    const sysTag = diag.ollama_service === 'active' && diag.apps_script_ok !== false ? 'SYS:OK' : 'SYS:CHECK';
    const spotTxt = this.state.spot ? (this.state.currency === 'SEK' ? `${this.state.spot} SEK` : `$${this.state.spot}`) : 'N/A';
    return `[${this.state.focusTicker} ${spotTxt} | Stop ${this.state.hardStop ?? '-'} | PnL ${formatSek(this.state.realizedPnlSek, true)} | ${gpu} | ${sysTag}]`;
  }

  _render() {
    clearEl(this.root);
    const st = this.state.status;
    const isRed = Boolean(st.is_red);
    const session = getStockholmMarketSession();

    const trafficPill = el(
      'span',
      { className: `traffic-pill ${isRed ? 'is-red' : 'is-green'}` },
      el('span', { className: `status-dot ${isRed ? 'is-red' : 'is-green'}` }),
      isRed ? `🔴 RED (${st.red_reasons.length || 1})` : '🟢 GREEN',
    );

    this._clockEl = el('strong', { className: 'mono', text: session.clock });
    const toneMap = {
      green: 'badge-green',
      amber: 'badge-amber',
      cyan: 'badge-cyan',
      muted: '',
    };
    this._sessionEl = el('span', {
      className: `badge ${toneMap[session.tone] || ''}`.trim(),
      title: session.detail,
      text: session.label,
    });

    const fxText = this.state.usdSek ? formatNumber(this.state.usdSek, 2) : '10.16';
    const fxTitle = this.state.usdSek ? 'Live USD/SEK exchange rate' : 'Reference USD/SEK rate';

    const strip = el(
      'div',
      { className: 'topbar-strip' },
      el(
        'div',
        { className: 'topbar-left' },
        el(
          'div',
          { className: 'brand-mark' },
          el('span', { className: 'brand-accent', text: 'GEMINA' }),
          el('span', { text: '// QUANT' }),
          el('span', { className: 'pill mono', text: this.state.accountId }),
        ),
        trafficPill,
        el('span', {
          className: 'topbar-ticker-line mono',
          title: this._cleanTickerSnippet(),
          text: this._cleanTickerSnippet(),
        }),
      ),
      el(
        'div',
        { className: 'topbar-right' },
        el(
          'span',
          { className: 'topbar-metric', title: fxTitle },
          el('span', { text: 'USD/SEK' }),
          el('strong', { className: 'mono', text: fxText }),
        ),
        el('span', { className: 'topbar-metric' }, this._clockEl),
        this._sessionEl,
        this._renderSseBadge(),
        el('button', {
          type: 'button',
          className: 'btn btn-xs btn-ghost',
          title: 'Manage Bearer Auth Token',
          text: 'Auth',
          onClick: () => this.onOpenAuth?.(),
        }),
      ),
    );

    this.root.appendChild(strip);

    // Auto-expanding Red Alert Drawer ONLY when is_red === true
    if (isRed) {
      const reasonsList = el('ul', { className: 'red-reasons-list' });
      const reasons = st.red_reasons.length > 0 ? st.red_reasons : ['System or risk threshold alert active.'];
      for (const reason of reasons) {
        const li = el('li');
        renderSafeMarkdown(li, reason);
        reasonsList.appendChild(li);
      }

      const diag = st.diagnostics || {};
      const hwLine =
        `GPU ${diag.gpu_temp_c ?? '-'}°C (${diag.gpu_vram_mb ?? '-'} MB, ${diag.gpu_util_pct ?? '-'}%) · ` +
        `RAM ${diag.ram_used_pct ?? '-'}% · Disk ${diag.disk_used_pct ?? '-'}% · Ollama ${diag.ollama_service ?? '-'}`;

      const drawer = el(
        'div',
        { className: 'red-alert-drawer', role: 'alert' },
        el(
          'div',
          { className: 'red-drawer-inner' },
          el(
            'div',
            null,
            el('h2', { className: 'red-drawer-title', text: '🚨 RED STATUS ALERT — IMMEDIATE ATTENTION' }),
            reasonsList,
          ),
          el(
            'div',
            { className: 'red-drawer-meta' },
            el('span', { className: 'red-drawer-hw mono', text: hwLine }),
            el(
              'div',
              { className: 'red-drawer-actions' },
              el('button', {
                type: 'button',
                className: 'btn btn-xs btn-red',
                text: 'Inspect Traces',
                onClick: () => this.onNavigateTab?.('traces'),
              }),
              el('button', {
                type: 'button',
                className: 'btn btn-xs',
                text: 'System Health',
                onClick: () => this.onNavigateTab?.('health'),
              }),
            ),
          ),
        ),
      );

      this.root.appendChild(drawer);
    }
  }
}
