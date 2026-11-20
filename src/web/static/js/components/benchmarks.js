/**
 * 60-Day IMOM Benchmarks & Per-Ticker Experience Memory Component (`benchmarks.js`).
 */
import { clearEl, el, formatNumber, formatPct, formatSek } from '../dom.js';

export class BenchmarksController {
  constructor(rootEl) {
    this.root = rootEl;
    this.benchmarks = null;
    this.selectedTicker = 'NVDA';
    this._render();
  }

  setBenchmarks(data) {
    this.benchmarks = data;
    this._render();
  }

  setSelectedTicker(ticker) {
    const next = ticker || 'NVDA';
    if (next === this.selectedTicker) return;
    this.selectedTicker = next;
    this._render();
  }

  _extractGlobalStats() {
    const raw = this.benchmarks || {};
    const exp = raw.experience_memory && typeof raw.experience_memory === 'object' ? raw.experience_memory : raw;
    const vm = exp.validation_methodology || raw.validation_methodology || {};
    const imom = raw.imom || vm.global_portfolio_imom_uncapped_1_mini || {};
    const orb = raw.orb || vm.global_portfolio_orb_uncapped_1_mini || {};
    const pbo = raw.cscv_pbo || vm.cscv_pbo || {};
    const tickers = raw.tickers || exp.tickers || {};
    return { vm, imom, orb, pbo, tickers };
  }

  _render() {
    clearEl(this.root);

    if (!this.benchmarks) {
      this.root.appendChild(
        el(
          'div',
          { className: 'panel' },
          el('div', { className: 'panel-head' }, el('h2', { className: 'panel-title', text: '60-Day Benchmarks' })),
          el('div', { className: 'panel-body txt-secondary', text: 'Loading empirical walk-forward stats...' }),
        ),
      );
      return;
    }

    const { vm, imom, orb, pbo, tickers } = this._extractGlobalStats();
    const trades = Number(imom.trades ?? 0);
    const minTrl = Number(imom.min_trl_trades ?? 155);
    const trlPct = minTrl > 0 ? Math.min(100, (trades / minTrl) * 100) : 0;

    const trlFill = el('div', { className: 'mintrl-bar-fill' });
    trlFill.style.width = `${trlPct.toFixed(1)}%`;

    const globalGrid = el(
      'div',
      { className: 'bench-grid' },
      this._statTile('PF (IMOM)', formatNumber(imom.profit_factor, 2), imom.profit_factor >= 1.5 ? 'txt-green' : ''),
      this._statTile('Win Rate', formatPct(imom.win_rate_pct, false, 1)),
      this._statTile('Wilson 95% LB', formatPct(imom.wilson_95_lower_wr_pct, false, 1)),
      this._statTile('PSR', formatPct(imom.psr_pct, false, 1), imom.psr_pct >= 80 ? 'txt-cyan' : ''),
      this._statTile('DSR', formatPct(imom.dsr_pct, false, 1), imom.dsr_pct >= 90 ? 'txt-green' : 'txt-amber'),
      this._statTile('CSCV PBO', formatPct(pbo.pbo_pct, false, 1), pbo.pbo_pct <= 25 ? 'txt-green' : 'txt-amber'),
    );

    const netPnl = Number(imom.net_pnl_sek ?? 0);
    const orbPf = orb.profit_factor !== undefined ? ` · ORB PF ${formatNumber(orb.profit_factor, 2)}` : '';

    const globalCard = el(
      'div',
      { className: 'panel' },
      el(
        'div',
        { className: 'panel-head' },
        el('h2', { className: 'panel-title', text: '60-Day Benchmarks' }),
        el('span', {
          className: `badge ${netPnl >= 0 ? 'badge-green' : 'badge-red'} mono`,
          text: `IMOM ${formatSek(netPnl, true)}`,
        }),
      ),
      el(
        'div',
        { className: 'panel-body' },
        globalGrid,
        el(
          'div',
          { className: 'stop-prox-labels mono' },
          el('span', { text: `MinTRL ${trades}/${minTrl} trades` }),
          el('span', { text: `${trlPct.toFixed(0)}% sample${orbPf}` }),
        ),
        el('div', { className: 'mintrl-bar-track' }, trlFill),
        vm.caveat ? el('div', { className: 'bench-caveat', text: vm.caveat }) : null,
      ),
    );

    // Selected Ticker Experience Memory Card
    const sym = String(this.selectedTicker || 'NVDA').toUpperCase();
    const tExp = tickers[sym] || tickers[this.selectedTicker];
    const expBody = el('div', { className: 'panel-body' });

    if (!tExp) {
      expBody.appendChild(
        el('div', { className: 'txt-secondary', text: `No 60-day OOS experience memory recorded yet for ${sym}.` }),
      );
    } else {
      const tPnl = Number(tExp.net_backtest_pnl_sek ?? 0);
      const tTrades = Number(tExp.trades_tested ?? 0);
      const tMinTrl = Number(tExp.min_trl_trades ?? 30);

      expBody.append(
        el(
          'div',
          { className: 'pill mono', title: tExp.preferred_regime_strategy || '' },
          tExp.preferred_regime_strategy || 'ADAPTIVE_ER_SWITCH',
        ),
        el(
          'div',
          { className: 'bench-grid' },
          this._statTile('PF', formatNumber(tExp.verified_profit_factor, 2), tExp.verified_profit_factor >= 1.5 ? 'txt-green' : 'txt-amber'),
          this._statTile('Win Rate', formatPct(tExp.verified_win_rate_pct, false, 1)),
          this._statTile('Wilson LB', formatPct(tExp.wilson_95_lower_wr_pct, false, 1)),
          this._statTile('PSR', formatPct(tExp.psr_pct, false, 1)),
          this._statTile('DSR', formatPct(tExp.dsr_pct, false, 1)),
          this._statTile('MinTRL', `${tTrades}/${tMinTrl}`),
        ),
        el(
          'div',
          { className: 'panel-subtitle mono' },
          `60d Net: ${formatSek(tPnl, true)} (L ${formatSek(tExp.long_pnl_sek ?? 0, true)} / S ${formatSek(tExp.short_pnl_sek ?? 0, true)}) · Live: ${tExp.live_trades_logged ?? 0} trades (${formatSek(tExp.live_cum_pnl_sek ?? 0, true)})`,
        ),
      );

      const rules = Array.isArray(tExp.key_learned_rules) ? tExp.key_learned_rules : [];
      if (rules.length > 0) {
        const ul = el('ul', { className: 'exp-rules-list' });
        for (const rule of rules) {
          ul.appendChild(el('li', { text: rule }));
        }
        expBody.appendChild(ul);
      }
    }

    const expCard = el(
      'div',
      { className: 'panel' },
      el(
        'div',
        { className: 'panel-head' },
        el('h2', { className: 'panel-title', text: `${sym} Experience Memory` }),
        tExp
          ? el('span', {
              className: `badge ${tExp.statistically_significant ? 'badge-green' : 'badge-amber'} mono`,
              text: tExp.statistically_significant ? 'SIG: YES' : 'BELOW MinTRL',
            })
          : null,
      ),
      expBody,
    );

    this.root.append(globalCard, expCard);
  }

  _statTile(label, val, valClass = '') {
    return el(
      'div',
      { className: 'bench-stat' },
      el('div', { className: 'bench-stat-label', text: label }),
      el('div', { className: `bench-stat-val mono ${valClass}`.trim(), text: val }),
    );
  }
}
