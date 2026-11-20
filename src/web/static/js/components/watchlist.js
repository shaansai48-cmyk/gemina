/**
 * Left Rail Watchlist & Account KPIs Component (`watchlist.js`).
 * Displays tactical cash, daily limits, cooldown, focus star toggles,
 * spot/share PnL, Mini holdings, stop-proximity bars, and +0.85R -> +0.15R ratchet status.
 */
import { clearEl, el, formatNumber, formatPrice, formatSek } from '../dom.js';

function computeSharesNetPnlSek(asset, spot, usdSek) {
  const qty = Number(asset.regular_shares_held || 0);
  const avg = Number(asset.blended_avg_price || 0);
  if (qty <= 0 || !Number.isFinite(spot) || spot <= 0 || avg <= 0) return null;
  const isUsd = String(asset.currency || 'USD').toUpperCase() === 'USD';
  const fxMult = isUsd ? Number(usdSek || 10.16) : 1.0;
  const fxDragRate = isUsd ? 0.005 : 0.0;
  const grossPnl = (spot - avg) * qty;
  const fxDrag = (spot + avg) * qty * (fxDragRate / 2.0);
  return Math.round((grossPnl - fxDrag) * fxMult);
}

function computeMiniRatchet(asset, spot, risk) {
  const units = Number(asset.mini_units_held || 0);
  const ratchetMult = Number(risk?.breakeven_ratchet_r_mult ?? 0.85);
  const lockMult = Number(risk?.breakeven_lock_r_mult ?? 0.15);
  const labelBase = `+${ratchetMult.toFixed(2)}R → +${lockMult.toFixed(2)}R`;

  if (units <= 0 || !Number.isFinite(spot) || spot <= 0) {
    return { text: labelBase, state: 'idle', title: 'Breakeven ratchet rule when Mini reaches +0.85R' };
  }

  const entryU = Number(asset.mini_entry_underlying_price || 0);
  const risk1R = Number(asset.mini_initial_risk_r || 0);
  if (entryU <= 0 || risk1R <= 0) {
    return { text: `Mini Open · ${labelBase}`, state: 'armed', title: 'Active Mini position' };
  }

  const isShort = String(asset.mini_side || '').toUpperCase() === 'SHORT';
  const curR = isShort ? (entryU - spot) / risk1R : (spot - entryU) / risk1R;
  if (curR >= ratchetMult) {
    return {
      text: `RATCHET +${lockMult.toFixed(2)}R (${curR.toFixed(2)}R)`,
      state: 'locked',
      title: `Reached +${ratchetMult.toFixed(2)}R! Move Avanza stop to +${lockMult.toFixed(2)}R`,
    };
  }
  return {
    text: `${curR >= 0 ? '+' : ''}${curR.toFixed(2)}R / ${labelBase}`,
    state: 'armed',
    title: 'Current open Mini R-multiple progress toward breakeven ratchet',
  };
}

export class WatchlistController {
  constructor(rootEl, { onSelectTicker, onToggleFocus } = {}) {
    this.root = rootEl;
    this.onSelectTicker = onSelectTicker;
    this.onToggleFocus = onToggleFocus;

    this.state = {
      portfolio: null,
      risk: {},
      latestByTicker: {},
      selectedTicker: 'NVDA',
      usdSek: 10.16,
      focusPending: false,
    };

    this._cooldownTimer = null;
    this._cooldownNode = null;
    this._render();
    this._cooldownTimer = setInterval(() => this._refreshCooldownOnly(), 15000);
  }

  update(patch) {
    Object.assign(this.state, patch);
    this._render();
  }

  _getCooldownStatus() {
    const stats = this.state.portfolio?.daily_stats || {};
    const risk = this.state.risk || {};
    const lastStop = Number(stats.last_stopout_epoch || 0);
    const cooldownMins = Number(risk.cooldown_minutes_after_stopout ?? 45);
    if (!lastStop) return { active: false, label: 'Ready', detail: 'No stop-out cooldown' };
    const elapsedMins = (Date.now() / 1000 - lastStop) / 60;
    if (elapsedMins >= cooldownMins) {
      return { active: false, label: 'Ready', detail: 'Cooldown cleared' };
    }
    const left = Math.max(1, Math.ceil(cooldownMins - elapsedMins));
    return { active: true, label: `${left}m left`, detail: `${cooldownMins}m stop-out cooldown` };
  }

  _refreshCooldownOnly() {
    if (!this._cooldownNode) return;
    const cd = this._getCooldownStatus();
    this._cooldownNode.textContent = cd.label;
    this._cooldownNode.className = `kpi-value mono ${cd.active ? 'txt-amber' : 'txt-green'}`;
  }

  _getTickerSpot(symbol, asset) {
    const latest = this.state.latestByTicker?.[symbol] || this.state.latestByTicker?.[symbol.toUpperCase()];
    const rawSpot = latest?.telemetry?.close ?? latest?.close ?? asset?.spot ?? asset?.last_price;
    const spot = Number(rawSpot);
    return Number.isFinite(spot) && spot > 0 ? spot : null;
  }

  _getLatestDecision(symbol) {
    const latest = this.state.latestByTicker?.[symbol] || this.state.latestByTicker?.[symbol.toUpperCase()];
    return latest?.decision || null;
  }

  _renderKpis(portfolio, risk) {
    const cash = portfolio?.cash_reserves || {};
    const stats = portfolio?.daily_stats || {};
    const watchlist = portfolio?.watchlist || {};

    const availCash = Number(cash.available_tactical_mini_sek ?? 0);
    const pendingCash = Number(cash.pending_fund_sale_sek ?? 0);
    const settleDate = cash.pending_fund_settlement_date || '';

    const tradesToday = Number(stats.trades_executed_today ?? 0);
    const maxTrades = Number(risk.max_daily_trades ?? 2);

    const realizedPnl = Number(stats.realized_pnl_today_sek ?? 0);
    const lossLimit = Number(risk.daily_loss_lockout_sek ?? -900);

    const activeMinis = Object.entries(watchlist).filter(([, a]) => Number(a.mini_units_held || 0) > 0);
    const maxMinis = Number(risk.max_concurrent_leveraged_minis ?? 1);
    const cd = this._getCooldownStatus();

    this._cooldownNode = el('div', {
      className: `kpi-value mono ${cd.active ? 'txt-amber' : 'txt-green'}`,
      text: cd.label,
    });

    const tradeClass = tradesToday >= maxTrades ? 'txt-red' : tradesToday > 0 ? 'txt-amber' : 'txt-green';
    const pnlClass = realizedPnl <= lossLimit ? 'txt-red' : realizedPnl < 0 ? 'txt-amber' : realizedPnl > 0 ? 'txt-green' : '';
    const miniClass = activeMinis.length >= maxMinis ? 'txt-amber' : 'txt-cyan';

    return el(
      'div',
      { className: 'panel' },
      el(
        'div',
        { className: 'panel-head' },
        el('h2', { className: 'panel-title', text: 'Account Risk & Capital' }),
        el('span', { className: 'pill mono', text: stats.date || 'Today' }),
      ),
      el(
        'div',
        { className: 'kpi-grid' },
        el(
          'div',
          { className: 'kpi-tile is-wide' },
          el('div', { className: 'kpi-label', text: 'Available Tactical Cash' }),
          el('div', { className: 'kpi-value mono txt-cyan', text: formatSek(availCash) }),
          pendingCash > 0
            ? el('div', {
                className: 'kpi-sub mono',
                text: `Pending +${formatSek(pendingCash)} (${settleDate || 'settling'})`,
              })
            : null,
        ),
        el(
          'div',
          { className: 'kpi-tile' },
          el('div', { className: 'kpi-label', text: 'Daily Trades' }),
          el('div', { className: `kpi-value mono ${tradeClass}`, text: `${tradesToday}/${maxTrades}` }),
          el('div', { className: 'kpi-sub', text: 'L4 Quota' }),
        ),
        el(
          'div',
          { className: 'kpi-tile' },
          el('div', { className: 'kpi-label', text: 'Realized PnL / Limit' }),
          el('div', {
            className: `kpi-value mono ${pnlClass}`,
            text: `${formatSek(realizedPnl, true)} / ${formatNumber(lossLimit, 0)}`,
          }),
          el('div', { className: 'kpi-sub mono', text: `Lockout ${formatSek(lossLimit)}` }),
        ),
        el(
          'div',
          { className: 'kpi-tile' },
          el('div', { className: 'kpi-label', text: 'Concurrent Minis' }),
          el('div', { className: `kpi-value mono ${miniClass}`, text: `${activeMinis.length}/${maxMinis}` }),
          el('div', {
            className: 'kpi-sub mono',
            text: activeMinis.length > 0 ? activeMinis.map(([s]) => s).join(', ') : 'Flat (Slot Free)',
          }),
        ),
        el(
          'div',
          { className: 'kpi-tile' },
          el('div', { className: 'kpi-label', text: 'Cooldown' }),
          this._cooldownNode,
          el('div', { className: 'kpi-sub', text: cd.detail }),
        ),
      ),
    );
  }

  _renderStopProximityBar(asset, spot) {
    const stop = Number(asset.hard_stop_price || 0);
    const tp = Number(asset.take_profit_price || 0);
    const cur = asset.currency || 'USD';

    if (!stop && !tp) {
      return el('div', { className: 'stop-prox-labels mono' }, el('span', { text: 'No Stop / Target configured' }));
    }

    let pctPos = 50;
    let distText = '';
    let isBreach = false;

    if (spot && stop > 0) {
      const distPct = ((spot - stop) / stop) * 100;
      isBreach = spot <= stop;
      distText = isBreach ? `BREACH ${distPct.toFixed(1)}%` : `+${distPct.toFixed(1)}% vs Stop`;
      if (tp > stop) {
        pctPos = Math.max(0, Math.min(100, ((spot - stop) / (tp - stop)) * 100));
      } else {
        pctPos = isBreach ? 4 : Math.min(92, 20 + distPct * 5);
      }
    }

    const fill = el('div', { className: 'stop-prox-fill' });
    fill.style.width = `${pctPos.toFixed(1)}%`;

    const marker = el('div', { className: 'stop-prox-marker' });
    marker.style.left = `${pctPos.toFixed(1)}%`;

    return el(
      'div',
      { className: 'stop-prox-wrap' },
      el(
        'div',
        { className: 'stop-prox-labels mono' },
        el('span', { className: isBreach ? 'txt-red' : '', text: `Stop ${formatPrice(stop, cur)}` }),
        distText ? el('span', { className: isBreach ? 'txt-red' : 'txt-secondary', text: distText }) : null,
        el('span', { text: tp > 0 ? `TP ${formatPrice(tp, cur)}` : 'TP -' }),
      ),
      el('div', { className: 'stop-prox-track' }, fill, spot ? marker : null),
    );
  }

  _renderTickerCard(symbol, asset, focusSet) {
    const isFocused = focusSet.has(symbol.toUpperCase());
    const isSelected = symbol.toUpperCase() === String(this.state.selectedTicker || '').toUpperCase();
    const spot = this._getTickerSpot(symbol, asset);
    const dec = this._getLatestDecision(symbol);
    const stop = Number(asset.hard_stop_price || 0);
    const hasPosition = Number(asset.regular_shares_held || 0) > 0 || Number(asset.mini_units_held || 0) > 0;
    const isBreach = Boolean(spot && stop > 0 && spot <= stop && hasPosition);

    const sharesQty = Number(asset.regular_shares_held || 0);
    const sharesAvg = Number(asset.blended_avg_price || 0);
    const sharesPnlSek = computeSharesNetPnlSek(asset, spot, this.state.usdSek);

    const miniUnits = Number(asset.mini_units_held || 0);
    const miniSide = String(asset.mini_side || '').toUpperCase() || 'LONG';
    const miniAvgSek = Number(asset.mini_avg_sek || 0);
    const ratchet = computeMiniRatchet(asset, spot, this.state.risk);

    let actionBadge = null;
    if (dec && dec.action) {
      const act = String(dec.action).toUpperCase();
      const cls = act.startsWith('BUY') ? 'badge-green' : act.startsWith('SELL') ? 'badge-red' : '';
      actionBadge = el('span', { className: `badge ${cls} mono`, text: act });
    }

    const starBtn = el('button', {
      type: 'button',
      className: `focus-star-btn ${isFocused ? 'is-focused' : ''}`,
      title: isFocused ? 'Remove from active focus tickers' : 'Add to active focus tickers',
      ariaLabel: `Toggle focus star for ${symbol}`,
      disabled: this.state.focusPending,
      text: isFocused ? '★' : '☆',
      onClick: (e) => {
        e.stopPropagation();
        this.onToggleFocus?.(symbol);
      },
    });

    const holdingsLeft = sharesQty > 0
      ? el(
          'span',
          { className: 'mono' },
          `${sharesQty}sh @ ${formatPrice(sharesAvg, asset.currency)}`,
          sharesPnlSek !== null
            ? el('span', {
                className: sharesPnlSek >= 0 ? ' txt-green' : ' txt-red',
                text: ` (${formatSek(sharesPnlSek, true)})`,
              })
            : null,
        )
      : el('span', { className: 'txt-muted', text: '0 shares' });

    const miniBadge =
      miniUnits > 0
        ? el('span', {
            className: `badge ${miniSide === 'SHORT' ? 'badge-red' : 'badge-green'} mono`,
            text: `MINI ${miniSide === 'SHORT' ? 'S' : 'L'} ${miniUnits}u @ ${formatNumber(miniAvgSek, 2)}`,
          })
        : el('span', { className: 'txt-muted mono', text: 'Mini Flat' });

    const ratchetCls =
      ratchet.state === 'locked' ? 'is-locked' : ratchet.state === 'armed' ? 'is-armed' : '';

    return el(
      'div',
      {
        className: `ticker-card ${isSelected ? 'is-selected' : ''} ${isBreach ? 'is-breach' : ''}`.trim(),
        tabIndex: 0,
        role: 'button',
        ariaPressed: isSelected,
        onClick: () => this.onSelectTicker?.(symbol),
        onKeyDown: (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            this.onSelectTicker?.(symbol);
          }
        },
      },
      el(
        'div',
        { className: 'tc-row-top' },
        el(
          'div',
          { className: 'tc-sym-wrap' },
          starBtn,
          el('span', { className: 'tc-symbol', text: symbol }),
          el('span', { className: 'tc-exch mono', text: `${asset.exchange || ''} ${asset.currency || ''}`.trim() }),
        ),
        el('span', {
          className: 'tc-price mono',
          text: spot ? formatPrice(spot, asset.currency) : 'No quote',
        }),
      ),
      el('div', { className: 'tc-holdings-row' }, holdingsLeft, miniBadge),
      this._renderStopProximityBar(asset, spot),
      el(
        'div',
        { className: 'tc-footer-row' },
        el('span', {
          className: `ratchet-pill mono ${ratchetCls}`.trim(),
          title: ratchet.title,
          text: ratchet.text,
        }),
        actionBadge,
      ),
    );
  }

  _render() {
    clearEl(this.root);
    const portfolio = this.state.portfolio;
    if (!portfolio) {
      this.root.appendChild(
        el(
          'div',
          { className: 'panel' },
          el('div', { className: 'panel-head' }, el('h2', { className: 'panel-title', text: 'Watchlist' })),
          el('div', { className: 'panel-body txt-secondary', text: 'Loading portfolio ledger...' }),
        ),
      );
      return;
    }

    this.root.appendChild(this._renderKpis(portfolio, this.state.risk));

    const focusRaw = String(portfolio.active_focus_ticker || '');
    const focusSet = new Set(
      focusRaw
        .split(',')
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
    );

    const watchlist = portfolio.watchlist || {};
    const symbols = Object.keys(watchlist);
    // Keep held/focused tickers first while preserving all tickers cleanly
    symbols.sort((a, b) => {
      const aHeld = Number(watchlist[a]?.mini_units_held || 0) + Number(watchlist[a]?.regular_shares_held || 0) > 0;
      const bHeld = Number(watchlist[b]?.mini_units_held || 0) + Number(watchlist[b]?.regular_shares_held || 0) > 0;
      if (aHeld !== bHeld) return aHeld ? -1 : 1;
      const aFoc = focusSet.has(a.toUpperCase());
      const bFoc = focusSet.has(b.toUpperCase());
      if (aFoc !== bFoc) return aFoc ? -1 : 1;
      return 0;
    });

    const listEl = el('div', { className: 'watchlist-list' });
    for (const sym of symbols) {
      listEl.appendChild(this._renderTickerCard(sym, watchlist[sym], focusSet));
    }

    this.root.appendChild(
      el(
        'div',
        { className: 'panel' },
        el(
          'div',
          { className: 'panel-head' },
          el('h2', { className: 'panel-title', text: `Watchlist (${symbols.length})` }),
          el('span', { className: 'pill mono', text: `Focus: ${focusRaw || 'None'}` }),
        ),
        listEl,
      ),
    );
  }
}
