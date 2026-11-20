/**
 * Interactive Avanza Ledger Forms & Live SEK Unit Calculator (`ledger_panel.js`).
 * Tabs: Fill Mini | Close Mini | Stop & Target | Shares | Add Ticker.
 */
import { clearEl, el, formatNumber, formatPrice, formatSek } from '../dom.js';

const LEDGER_TABS = [
  { id: 'fill-mini', label: 'Fill Mini' },
  { id: 'close-mini', label: 'Close Mini' },
  { id: 'stop', label: 'Stop & Target' },
  { id: 'shares', label: 'Shares' },
  { id: 'add-ticker', label: 'Add Ticker' },
];

export class LedgerPanelController {
  constructor(rootEl, { onCalculate, onSubmitAction, onSelectTicker } = {}) {
    this.root = rootEl;
    this.onCalculate = onCalculate;
    this.onSubmitAction = onSubmitAction;
    this.onSelectTicker = onSelectTicker;

    this.activeTab = 'fill-mini';
    this.portfolio = null;
    this.risk = {};
    this.latestByTicker = {};
    this.selectedTicker = 'NVDA';
    this.usdSek = 10.16;
    this.submitting = false;

    // Fill Mini state
    this.fillMini = {
      side: 'LONG',
      budgetSek: '5000',
      units: '',
      priceSek: '',
      stopUnderlying: '',
      riskParityMode: false,
      manualUnitsOverride: false,
      manualPriceOverride: false,
      calcResult: null,
      calcLoading: false,
      calcError: '',
    };

    // Close Mini state
    this.closeMini = { exitSek: '' };

    // Stop & Target + Mini Params state
    this.stopForm = {
      hardStop: '',
      takeProfit: '',
      finLvl: '',
      barrier: '',
      parity: '',
      shortFinLvl: '',
      shortBarrier: '',
    };

    // Shares state
    this.sharesForm = {
      actionMode: 'fill', // 'fill' | 'close'
      qty: '',
      avgPrice: '',
      exitPrice: '',
    };

    // Add Ticker state
    this.addTickerForm = {
      ticker: '',
      currency: 'USD',
      hardStop: '',
      takeProfit: '',
      miniFin: '',
      parity: '10',
    };

    this._calcDebounce = null;
    this._calcAbort = null;
    this._calcSeq = 0;

    this._render();
  }

  updateContext({ portfolio, risk, latestByTicker, selectedTicker, usdSek }) {
    const tickerChanged = Boolean(selectedTicker && selectedTicker !== this.selectedTicker);
    if (portfolio !== undefined) this.portfolio = portfolio;
    if (risk !== undefined) this.risk = risk || {};
    if (latestByTicker !== undefined) this.latestByTicker = latestByTicker || {};
    if (selectedTicker !== undefined) this.selectedTicker = selectedTicker;
    if (usdSek !== undefined && usdSek) this.usdSek = usdSek;

    if (tickerChanged || (!this.stopForm.hardStop && this.portfolio)) {
      this._prefillFromSelection();
      return;
    }

    const activeEl = document.activeElement;
    const isEditingInside =
      activeEl &&
      this.root.contains(activeEl) &&
      (activeEl.tagName === 'INPUT' || activeEl.tagName === 'TEXTAREA' || activeEl.tagName === 'SELECT');

    if (isEditingInside) {
      if (this.activeTab === 'fill-mini') this._renderFillMiniCalcBox();
      return;
    }
    this._render();
  }

  prefillFromSignal(msg) {
    if (!msg) return;
    const act = String(msg.action || '').toUpperCase();
    if (act === 'BUY_MINI_LONG' || act === 'BUY_MINI_SHORT') {
      this.activeTab = 'fill-mini';
      this.fillMini.side = act === 'BUY_MINI_SHORT' ? 'SHORT' : 'LONG';
      if (msg.quantity > 0) {
        this.fillMini.units = String(msg.quantity);
        this.fillMini.manualUnitsOverride = true;
      }
      if (msg.limitPrice) {
        this.fillMini.priceSek = String(msg.limitPrice);
        this.fillMini.manualPriceOverride = true;
      }
      if (msg.stopPrice) {
        this.fillMini.stopUnderlying = String(msg.stopPrice);
      }
      this._render();
      this._triggerCalculator(true);
    } else if (act === 'SELL_MINI_LONG' || act === 'SELL_MINI_SHORT') {
      this.activeTab = 'close-mini';
      if (msg.limitPrice) this.closeMini.exitSek = String(msg.limitPrice);
      this._render();
    } else if (act === 'BUY_SHARES' || act === 'SELL_SHARES') {
      this.activeTab = 'shares';
      this.sharesForm.actionMode = act === 'BUY_SHARES' ? 'fill' : 'close';
      if (msg.quantity > 0) this.sharesForm.qty = String(msg.quantity);
      if (msg.limitPrice) {
        if (act === 'BUY_SHARES') this.sharesForm.avgPrice = String(msg.limitPrice);
        else this.sharesForm.exitPrice = String(msg.limitPrice);
      }
      this._render();
    }
  }

  _getAsset(sym = this.selectedTicker) {
    return this.portfolio?.watchlist?.[sym] || {};
  }

  _getSpot(sym = this.selectedTicker) {
    const asset = this._getAsset(sym);
    const latest = this.latestByTicker?.[sym] || this.latestByTicker?.[String(sym).toUpperCase()];
    const raw = latest?.telemetry?.close ?? latest?.close ?? asset?.spot ?? asset?.last_price;
    const val = Number(raw);
    return Number.isFinite(val) && val > 0 ? val : null;
  }

  _computeTheoreticalMiniSek(asset, spot, side = 'LONG') {
    if (!spot) return null;
    const fx = String(asset.currency || 'USD').toUpperCase() === 'USD' ? this.usdSek : 1.0;
    const parity = Number(asset.mini_parity || 10) || 10;
    if (side === 'SHORT') {
      const sFin = Number(asset.mini_short_financing_level || 0);
      if (sFin > spot) return Number((((sFin - spot) * fx) / parity).toFixed(2));
      return null;
    }
    const fin = Number(asset.mini_financing_level || 0);
    if (fin > 0 && spot > fin) return Number((((spot - fin) * fx) / parity).toFixed(2));
    return null;
  }

  _prefillFromSelection() {
    const asset = this._getAsset();
    const spot = this._getSpot();

    this.fillMini.manualUnitsOverride = false;
    this.fillMini.manualPriceOverride = false;
    this.fillMini.stopUnderlying = asset.hard_stop_price ? String(asset.hard_stop_price) : '';
    const theo = this._computeTheoreticalMiniSek(asset, spot, this.fillMini.side);
    this.fillMini.priceSek = theo ? String(theo) : '';

    const heldMiniSide = String(asset.mini_side || 'LONG').toUpperCase();
    const heldTheo = this._computeTheoreticalMiniSek(asset, spot, heldMiniSide);
    this.closeMini.exitSek = heldTheo ? String(heldTheo) : asset.mini_avg_sek ? String(asset.mini_avg_sek) : '';

    this.stopForm = {
      hardStop: asset.hard_stop_price ? String(asset.hard_stop_price) : '',
      takeProfit: asset.take_profit_price ? String(asset.take_profit_price) : '',
      finLvl: asset.mini_financing_level ? String(asset.mini_financing_level) : '',
      barrier: asset.mini_barrier ? String(asset.mini_barrier) : '',
      parity: asset.mini_parity ? String(asset.mini_parity) : '10',
      shortFinLvl: asset.mini_short_financing_level ? String(asset.mini_short_financing_level) : '',
      shortBarrier: asset.mini_short_barrier ? String(asset.mini_short_barrier) : '',
    };

    this.sharesForm.qty = asset.regular_shares_held ? String(asset.regular_shares_held) : '10';
    this.sharesForm.avgPrice = spot ? String(spot) : asset.blended_avg_price ? String(asset.blended_avg_price) : '';
    this.sharesForm.exitPrice = spot ? String(spot) : '';

    this._render();
    if (this.activeTab === 'fill-mini') {
      this._triggerCalculator(true);
    }
  }

  _triggerCalculator(immediate = false) {
    if (!this.onCalculate || !this.portfolio) return;
    if (this._calcDebounce) clearTimeout(this._calcDebounce);

    const run = async () => {
      if (this._calcAbort) this._calcAbort.abort();
      const controller = new AbortController();
      this._calcAbort = controller;
      const seq = ++this._calcSeq;

      this.fillMini.calcLoading = true;
      this.fillMini.calcError = '';
      this._renderFillMiniCalcBox();

      try {
        const params = {
          ticker: this.selectedTicker,
          side: this.fillMini.side,
          budget_sek: this.fillMini.riskParityMode
            ? Number(this.risk?.max_risk_per_trade_sek ?? 450)
            : Number(String(this.fillMini.budgetSek).replace(',', '.')) || 0,
        };
        if (this.fillMini.riskParityMode) {
          params.mode = 'risk_parity';
          params.risk_parity = '1';
        }
        if (this.fillMini.manualPriceOverride && this.fillMini.priceSek) {
          params.limit_price = this.fillMini.priceSek;
        }
        if (this.fillMini.stopUnderlying) {
          params.stop_price = this.fillMini.stopUnderlying;
        }

        const res = await this.onCalculate(params, controller.signal);
        if (seq !== this._calcSeq) return;

        this.fillMini.calcResult = res;
        this.fillMini.calcLoading = false;

        if (!this.fillMini.manualUnitsOverride && res?.units !== undefined) {
          this.fillMini.units = String(res.units);
          if (this._unitsInput) this._unitsInput.value = this.fillMini.units;
        }
        if (!this.fillMini.manualPriceOverride) {
          const calcPx = res?.limit_price_sek ?? res?.theoretical_sek ?? res?.price_sek;
          if (calcPx !== undefined && calcPx !== null) {
            this.fillMini.priceSek = String(calcPx);
            if (this._priceInput) this._priceInput.value = this.fillMini.priceSek;
          }
        }
        if (this.fillMini.riskParityMode && res?.notional_sek) {
          this.fillMini.budgetSek = String(Math.round(Number(res.notional_sek)));
          if (this._budgetInput) this._budgetInput.value = this.fillMini.budgetSek;
        }
        this._renderFillMiniCalcBox();
      } catch (err) {
        if (err?.name === 'AbortError' || seq !== this._calcSeq) return;
        this.fillMini.calcLoading = false;
        this.fillMini.calcError = err?.message || 'Calculator unavailable';
        this._renderFillMiniCalcBox();
      }
    };

    if (immediate) {
      run();
    } else {
      this._calcDebounce = setTimeout(run, 180);
    }
  }

  _buildLocalCalcFallback() {
    const asset = this._getAsset();
    const spot = this._getSpot();
    const isShort = this.fillMini.side === 'SHORT';
    const fx = String(asset.currency || 'USD').toUpperCase() === 'USD' ? this.usdSek : 1.0;
    const parity = Number(asset.mini_parity || 10) || 10;
    const fin = isShort
      ? Number(asset.mini_short_financing_level || (spot ? spot * 1.22 : 0))
      : Number(asset.mini_financing_level || 0);

    const theo =
      Number(this.fillMini.priceSek) ||
      this._computeTheoreticalMiniSek(asset, spot, this.fillMini.side) ||
      0;

    const budget = Number(String(this.fillMini.budgetSek).replace(',', '.')) || 0;
    const units = this.fillMini.manualUnitsOverride
      ? Number(this.fillMini.units) || 0
      : theo > 0
        ? Math.floor(budget / theo)
        : 0;
    const notional = Math.round(units * theo);

    const stopU = Number(this.fillMini.stopUnderlying || asset.hard_stop_price || 0);
    let trigSek = 0;
    if (stopU > 0 && fin > 0) {
      trigSek = isShort ? ((fin - stopU) * fx) / parity : ((stopU - fin) * fx) / parity;
      trigSek = Math.max(0, Number(trigSek.toFixed(2)));
    }
    const sellSek = trigSek > 0 ? Math.max(0.01, Number((trigSek - 0.2).toFixed(2))) : 0;
    const riskSek = units > 0 && theo > 0 && sellSek > 0 ? Math.round(units * Math.max(theo - sellSek, 0.2)) : 0;

    return {
      units,
      limit_price_sek: theo,
      notional_sek: notional,
      zero_courtage: notional >= 1000,
      courtage_sek: notional > 0 && notional < 1000 ? 19 : 0,
      trigger_sek: trigSek,
      sell_sek: sellSek,
      total_risk_sek: riskSek,
      warnings: [],
    };
  }

  _renderFillMiniCalcBox() {
    if (!this._calcBoxNode) return;
    clearEl(this._calcBoxNode);

    const res = this.fillMini.calcResult || this._buildLocalCalcFallback();
    const units = Number(this.fillMini.units || res.units || 0);
    const priceSek = Number(this.fillMini.priceSek || res.limit_price_sek || res.theoretical_sek || 0);
    const notional = res.notional_sek !== undefined && !this.fillMini.manualUnitsOverride && !this.fillMini.manualPriceOverride
      ? Number(res.notional_sek)
      : Math.round(units * priceSek);
    const zeroCourtage = res.zero_courtage !== undefined ? Boolean(res.zero_courtage) : notional >= 1000;
    const trigSek = Number(res.trigger_sek ?? res.avanza_stop_trigger_sek ?? 0);
    const sellSek = Number(res.sell_sek ?? res.avanza_stop_sell_sek ?? (trigSek > 0 ? Math.max(0.01, trigSek - 0.2) : 0));
    const totalRisk = Number(res.total_risk_sek ?? res.risk_1r_sek ?? 0);

    const statusBadge = this.fillMini.calcLoading
      ? el('span', { className: 'badge badge-cyan', text: 'Calculating...' })
      : this.fillMini.calcError
        ? el('span', { className: 'badge badge-amber', title: this.fillMini.calcError, text: 'Est. Local' })
        : el('span', { className: 'badge badge-green', text: 'Synced' });

    const courtageNode = zeroCourtage
      ? el('div', { className: 'calc-cell-val mono txt-green', text: '0 SEK (>= 1,000)' })
      : el('div', { className: 'calc-cell-val mono txt-amber', text: `${res.courtage_sek ?? 19} SEK (< 1,000)` });

    this._calcBoxNode.append(
      el(
        'div',
        { className: 'tc-row-top' },
        el('span', { className: 'kpi-label', text: 'Live Avanza Calculator' }),
        statusBadge,
      ),
      el(
        'div',
        { className: 'calc-grid' },
        el('div', null, el('div', { className: 'calc-cell-label', text: 'Units (st)' }), el('div', { className: 'calc-cell-val mono txt-cyan', text: `${formatNumber(units, 0)} st` })),
        el('div', null, el('div', { className: 'calc-cell-label', text: 'Notional SEK' }), el('div', { className: 'calc-cell-val mono', text: formatSek(notional) })),
        el('div', null, el('div', { className: 'calc-cell-label', text: 'Courtage Check' }), courtageNode),
        el('div', null, el('div', { className: 'calc-cell-label', text: '1R Est. Risk' }), el('div', { className: `calc-cell-val mono ${totalRisk > 450 ? 'txt-amber' : ''}`, text: totalRisk ? formatSek(totalRisk) : '-' })),
        el('div', null, el('div', { className: 'calc-cell-label', text: 'Triggerkurs' }), el('div', { className: 'calc-cell-val mono', text: trigSek > 0 ? `${formatNumber(trigSek, 2)} SEK` : '-' })),
        el('div', null, el('div', { className: 'calc-cell-label', text: 'Säljkurs (-0.20)' }), el('div', { className: 'calc-cell-val mono', text: sellSek > 0 ? `${formatNumber(sellSek, 2)} SEK` : '-' })),
      ),
    );

    const warnings = []
      .concat(Array.isArray(res.warnings) ? res.warnings : [])
      .concat(Array.isArray(res.l4_warnings) ? res.l4_warnings : [])
      .concat(Array.isArray(res.guardrail_warnings) ? res.guardrail_warnings : []);

    if (!zeroCourtage && notional > 0) {
      warnings.push('Order < 1,000 SEK incurs Avanza courtage fee.');
    }

    if (warnings.length > 0) {
      const ul = el('ul');
      for (const w of warnings) {
        ul.appendChild(el('li', { text: String(w) }));
      }
      this._calcBoxNode.appendChild(
        el(
          'div',
          { className: `warning-box ${res.blocked ? 'is-blocked' : ''}`.trim() },
          el('strong', { text: res.blocked ? 'L4 Guardrail Block' : 'L4 Pre-Flight Warnings' }),
          ul,
        ),
      );
    }
  }

  async _submit(actionName, payload) {
    if (this.submitting || !this.onSubmitAction) return;
    this.submitting = true;
    this._render();
    try {
      await this.onSubmitAction(actionName, payload);
    } finally {
      this.submitting = false;
      this._render();
    }
  }

  _renderFillMiniTab(asset, spot) {
    const availCash = Number(this.portfolio?.cash_reserves?.available_tactical_mini_sek ?? 0);
    const isShort = this.fillMini.side === 'SHORT';
    const instName = isShort
      ? asset.mini_short_instrument || `MINI S ${this.selectedTicker} AVA`
      : asset.mini_instrument || `MINI L ${this.selectedTicker} AVA`;

    const setBudget = (val, isRp = false) => {
      this.fillMini.riskParityMode = isRp;
      this.fillMini.manualUnitsOverride = false;
      this.fillMini.budgetSek = String(Math.max(0, Math.round(val)));
      if (this._budgetInput) this._budgetInput.value = this.fillMini.budgetSek;
      this._triggerCalculator(true);
    };

    this._budgetInput = el('input', {
      type: 'number',
      step: '100',
      min: '0',
      className: 'input mono',
      value: this.fillMini.budgetSek,
      onInput: (e) => {
        this.fillMini.budgetSek = e.target.value;
        this.fillMini.riskParityMode = false;
        this.fillMini.manualUnitsOverride = false;
        this._triggerCalculator(false);
      },
    });

    this._unitsInput = el('input', {
      type: 'number',
      step: '1',
      min: '1',
      className: 'input mono',
      value: this.fillMini.units,
      onInput: (e) => {
        this.fillMini.units = e.target.value;
        this.fillMini.manualUnitsOverride = true;
        this._renderFillMiniCalcBox();
      },
    });

    this._priceInput = el('input', {
      type: 'number',
      step: '0.01',
      min: '0.01',
      className: 'input mono',
      value: this.fillMini.priceSek,
      onInput: (e) => {
        this.fillMini.priceSek = e.target.value;
        this.fillMini.manualPriceOverride = true;
        this._triggerCalculator(false);
      },
    });

    const stopInput = el('input', {
      type: 'number',
      step: '0.01',
      className: 'input mono',
      value: this.fillMini.stopUnderlying,
      onInput: (e) => {
        this.fillMini.stopUnderlying = e.target.value;
        this._triggerCalculator(false);
      },
    });

    this._calcBoxNode = el('div', { className: 'calc-card' });

    const wrap = el(
      'form',
      {
        onSubmit: (e) => {
          e.preventDefault();
          const units = parseInt(this.fillMini.units, 10);
          const priceSek = parseFloat(String(this.fillMini.priceSek).replace(',', '.'));
          if (!Number.isFinite(units) || units <= 0 || !Number.isFinite(priceSek) || priceSek <= 0) return;
          this._submit('fill-mini', {
            ticker: this.selectedTicker,
            units,
            price_sek: priceSek,
            side: this.fillMini.side,
          });
        },
      },
      el(
        'div',
        { className: 'tc-row-top' },
        el('span', { className: 'badge badge-cyan mono', text: instName }),
        el('span', { className: 'panel-subtitle mono', text: spot ? `Spot ${formatPrice(spot, asset.currency)}` : '' }),
      ),
      el(
        'div',
        { className: 'segmented-bar form-row' },
        el('button', {
          type: 'button',
          className: `seg-btn ${!isShort ? 'is-active' : ''}`,
          text: 'LONG (MINI L)',
          onClick: () => {
            this.fillMini.side = 'LONG';
            this.fillMini.manualPriceOverride = false;
            this.fillMini.manualUnitsOverride = false;
            this._triggerCalculator(true);
            this._render();
          },
        }),
        el('button', {
          type: 'button',
          className: `seg-btn ${isShort ? 'is-active' : ''}`,
          text: 'SHORT (MINI S)',
          onClick: () => {
            this.fillMini.side = 'SHORT';
            this.fillMini.manualPriceOverride = false;
            this.fillMini.manualUnitsOverride = false;
            this._triggerCalculator(true);
            this._render();
          },
        }),
      ),
      el(
        'div',
        { className: 'form-row' },
        el('label', { className: 'field-label', text: 'SEK Budget (Auto-Sizes Units)' }),
        this._budgetInput,
        el(
          'div',
          { className: 'preset-row' },
          el('button', { type: 'button', className: 'btn btn-xs', text: '25%', onClick: () => setBudget(availCash * 0.25) }),
          el('button', { type: 'button', className: 'btn btn-xs', text: '50%', onClick: () => setBudget(availCash * 0.5) }),
          el('button', { type: 'button', className: 'btn btn-xs', text: 'Max', onClick: () => setBudget(availCash * 0.95) }),
          el('button', {
            type: 'button',
            className: `btn btn-xs ${this.fillMini.riskParityMode ? 'btn-cyan' : 'btn-amber'}`,
            text: `Risk-Parity ${this.risk?.max_risk_per_trade_sek ?? 450} SEK 1R`,
            onClick: () => setBudget(Number(this.risk?.max_risk_per_trade_sek ?? 450), true),
          }),
        ),
      ),
      el(
        'div',
        { className: 'form-grid form-row' },
        el('div', null, el('label', { className: 'field-label', text: 'Units (st)' }), this._unitsInput),
        el('div', null, el('label', { className: 'field-label', text: 'Fill Price (SEK)' }), this._priceInput),
      ),
      el(
        'div',
        { className: 'form-row' },
        el('label', { className: 'field-label', text: `Underlying Stop (${asset.currency || 'USD'})` }),
        stopInput,
      ),
      this._calcBoxNode,
      el('button', {
        type: 'submit',
        className: 'btn btn-green',
        disabled: this.submitting,
        text: this.submitting ? 'Logging Fill...' : `Log Fill (${this.selectedTicker} ${this.fillMini.side})`,
      }),
    );

    this._renderFillMiniCalcBox();
    return wrap;
  }

  _renderCloseMiniTab(asset) {
    const heldEntry = Object.entries(this.portfolio?.watchlist || {}).find(([, a]) => Number(a.mini_units_held || 0) > 0);
    const units = Number(asset.mini_units_held || 0);
    const avgSek = Number(asset.mini_avg_sek || 0);

    if (units <= 0 && heldEntry && heldEntry[0] !== this.selectedTicker) {
      return el(
        'div',
        { className: 'panel-body' },
        el('div', { className: 'txt-secondary', text: `${this.selectedTicker} has 0 Mini units held.` }),
        el('button', {
          type: 'button',
          className: 'btn btn-sm btn-cyan',
          text: `Switch to Held Mini (${heldEntry[0]} · ${heldEntry[1].mini_units_held}u)`,
          onClick: () => this.onSelectTicker?.(heldEntry[0]),
        }),
      );
    }

    const pnlNode = el('div', { className: 'kpi-sub mono' });
    const updateProjectedPnl = () => {
      const exitSek = Number(String(this.closeMini.exitSek).replace(',', '.')) || 0;
      if (units > 0 && exitSek > 0) {
        const estPnl = (exitSek - avgSek) * units;
        pnlNode.textContent = `Projected Realized PnL: ${formatSek(estPnl, true)}`;
        pnlNode.className = `kpi-sub mono ${estPnl >= 0 ? 'txt-green' : 'txt-red'}`;
      } else {
        pnlNode.textContent = '';
      }
    };
    updateProjectedPnl();

    return el(
      'form',
      {
        onSubmit: (e) => {
          e.preventDefault();
          const px = parseFloat(String(this.closeMini.exitSek).replace(',', '.'));
          if (!Number.isFinite(px) || px <= 0) return;
          this._submit('close-mini', { ticker: this.selectedTicker, exit_sek: px });
        },
      },
      el(
        'div',
        { className: 'calc-card' },
        el('div', { className: 'calc-cell-label', text: 'Open Position' }),
        el('div', {
          className: 'calc-cell-val mono',
          text: units > 0 ? `${units}u ${asset.mini_side || 'LONG'} @ ${formatNumber(avgSek, 2)} SEK` : `Flat (0 units on ${this.selectedTicker})`,
        }),
        pnlNode,
      ),
      el(
        'div',
        { className: 'form-row' },
        el('label', { className: 'field-label', text: 'Exit Price (SEK)' }),
        el('input', {
          type: 'number',
          step: '0.01',
          min: '0.01',
          className: 'input mono',
          value: this.closeMini.exitSek,
          required: true,
          onInput: (e) => {
            this.closeMini.exitSek = e.target.value;
            updateProjectedPnl();
          },
        }),
      ),
      el('button', {
        type: 'submit',
        className: 'btn btn-red',
        disabled: this.submitting || units <= 0,
        text: this.submitting ? 'Closing...' : `Close ${this.selectedTicker} Mini`,
      }),
    );
  }

  _renderStopTab(asset) {
    return el(
      'div',
      null,
      el(
        'form',
        {
          onSubmit: (e) => {
            e.preventDefault();
            const stopVal = parseFloat(String(this.stopForm.hardStop).replace(',', '.'));
            const tpVal = parseFloat(String(this.stopForm.takeProfit).replace(',', '.'));
            if (!Number.isFinite(stopVal) || stopVal <= 0) return;
            const payload = { ticker: this.selectedTicker, hard_stop_price: stopVal };
            if (Number.isFinite(tpVal) && tpVal > 0) payload.take_profit_price = tpVal;
            this._submit('stop', payload);
          },
        },
        el(
          'div',
          { className: 'form-grid form-row' },
          el(
            'div',
            null,
            el('label', { className: 'field-label', text: `Hard Stop (${asset.currency || 'USD'})` }),
            el('input', {
              type: 'number',
              step: '0.01',
              className: 'input mono',
              value: this.stopForm.hardStop,
              required: true,
              onInput: (e) => {
                this.stopForm.hardStop = e.target.value;
              },
            }),
          ),
          el(
            'div',
            null,
            el('label', { className: 'field-label', text: `Take Profit (${asset.currency || 'USD'})` }),
            el('input', {
              type: 'number',
              step: '0.01',
              className: 'input mono',
              value: this.stopForm.takeProfit,
              onInput: (e) => {
                this.stopForm.takeProfit = e.target.value;
              },
            }),
          ),
        ),
        el('button', {
          type: 'submit',
          className: 'btn btn-cyan',
          disabled: this.submitting,
          text: `Update ${this.selectedTicker} Stop & Target`,
        }),
      ),
      el(
        'details',
        { className: 'tech-details' },
        el('summary', { text: 'Edit Avanza Mini Parameters (Fin / Barrier / Parity)' }),
        el(
          'form',
          {
            onSubmit: (e) => {
              e.preventDefault();
              this._submit('mini-params', {
                ticker: this.selectedTicker,
                mini_financing_level: parseFloat(this.stopForm.finLvl) || 0,
                mini_barrier: parseFloat(this.stopForm.barrier) || 0,
                mini_parity: parseFloat(this.stopForm.parity) || 10,
                mini_short_financing_level: parseFloat(this.stopForm.shortFinLvl) || 0,
                mini_short_barrier: parseFloat(this.stopForm.shortBarrier) || 0,
              });
            },
          },
          el(
            'div',
            { className: 'form-grid form-row' },
            el('div', null, el('label', { className: 'field-label', text: 'Long Fin Lvl' }), el('input', { type: 'number', step: '0.01', className: 'input mono', value: this.stopForm.finLvl, onInput: (e) => { this.stopForm.finLvl = e.target.value; } })),
            el('div', null, el('label', { className: 'field-label', text: 'Long Barrier' }), el('input', { type: 'number', step: '0.01', className: 'input mono', value: this.stopForm.barrier, onInput: (e) => { this.stopForm.barrier = e.target.value; } })),
            el('div', null, el('label', { className: 'field-label', text: 'Short Fin Lvl' }), el('input', { type: 'number', step: '0.01', className: 'input mono', value: this.stopForm.shortFinLvl, onInput: (e) => { this.stopForm.shortFinLvl = e.target.value; } })),
            el('div', null, el('label', { className: 'field-label', text: 'Short Barrier' }), el('input', { type: 'number', step: '0.01', className: 'input mono', value: this.stopForm.shortBarrier, onInput: (e) => { this.stopForm.shortBarrier = e.target.value; } })),
          ),
          el(
            'div',
            { className: 'form-row' },
            el('label', { className: 'field-label', text: 'Parity (e.g. 10 or 100 or 1)' }),
            el('input', { type: 'number', step: '1', className: 'input mono', value: this.stopForm.parity, onInput: (e) => { this.stopForm.parity = e.target.value; } }),
          ),
          el('button', { type: 'submit', className: 'btn btn-sm', disabled: this.submitting, text: 'Save Mini Params' }),
        ),
      ),
    );
  }

  _renderSharesTab(asset) {
    const isFill = this.sharesForm.actionMode === 'fill';
    return el(
      'div',
      null,
      el(
        'div',
        { className: 'segmented-bar form-row' },
        el('button', {
          type: 'button',
          className: `seg-btn ${isFill ? 'is-active' : ''}`,
          text: 'Fill / Set Shares',
          onClick: () => {
            this.sharesForm.actionMode = 'fill';
            this._render();
          },
        }),
        el('button', {
          type: 'button',
          className: `seg-btn ${!isFill ? 'is-active' : ''}`,
          text: 'Close Shares',
          onClick: () => {
            this.sharesForm.actionMode = 'close';
            this._render();
          },
        }),
      ),
      isFill
        ? el(
            'form',
            {
              onSubmit: (e) => {
                e.preventDefault();
                const qty = parseInt(this.sharesForm.qty, 10);
                const avgPrice = parseFloat(String(this.sharesForm.avgPrice).replace(',', '.'));
                if (!Number.isFinite(qty) || qty < 0 || !Number.isFinite(avgPrice) || avgPrice <= 0) return;
                this._submit('fill-shares', { ticker: this.selectedTicker, qty, avg_price: avgPrice });
              },
            },
            el(
              'div',
              { className: 'form-grid form-row' },
              el('div', null, el('label', { className: 'field-label', text: 'Quantity (Shares)' }), el('input', { type: 'number', step: '1', min: '0', className: 'input mono', value: this.sharesForm.qty, onInput: (e) => { this.sharesForm.qty = e.target.value; } })),
              el('div', null, el('label', { className: 'field-label', text: `Avg Price (${asset.currency || 'USD'})` }), el('input', { type: 'number', step: '0.01', min: '0.01', className: 'input mono', value: this.sharesForm.avgPrice, onInput: (e) => { this.sharesForm.avgPrice = e.target.value; } })),
            ),
            el('button', { type: 'submit', className: 'btn btn-green', disabled: this.submitting, text: `Log ${this.selectedTicker} Shares` }),
          )
        : el(
            'form',
            {
              onSubmit: (e) => {
                e.preventDefault();
                const exitPrice = parseFloat(String(this.sharesForm.exitPrice).replace(',', '.'));
                if (!Number.isFinite(exitPrice) || exitPrice <= 0) return;
                this._submit('close-shares', { ticker: this.selectedTicker, exit_price: exitPrice });
              },
            },
            el('div', { className: 'calc-card mono' }, `Held: ${asset.regular_shares_held || 0} shares @ ${formatPrice(asset.blended_avg_price || 0, asset.currency)}`),
            el(
              'div',
              { className: 'form-row' },
              el('label', { className: 'field-label', text: `Exit Price (${asset.currency || 'USD'})` }),
              el('input', { type: 'number', step: '0.01', min: '0.01', className: 'input mono', value: this.sharesForm.exitPrice, onInput: (e) => { this.sharesForm.exitPrice = e.target.value; } }),
            ),
            el('button', { type: 'submit', className: 'btn btn-red', disabled: this.submitting || Number(asset.regular_shares_held || 0) <= 0, text: `Close ${this.selectedTicker} Shares` }),
          ),
    );
  }

  _renderAddTickerTab() {
    return el(
      'form',
      {
        onSubmit: (e) => {
          e.preventDefault();
          const sym = this.addTickerForm.ticker.trim().toUpperCase().replace(/_/g, ' ');
          if (!sym) return;
          this._submit('add-ticker', {
            ticker: sym,
            currency: this.addTickerForm.currency,
            hard_stop_price: parseFloat(this.addTickerForm.hardStop) || 0,
            take_profit_price: parseFloat(this.addTickerForm.takeProfit) || 0,
            mini_financing_level: parseFloat(this.addTickerForm.miniFin) || 0,
            mini_parity: parseFloat(this.addTickerForm.parity) || (this.addTickerForm.currency === 'SEK' ? 1 : 10),
          });
        },
      },
      el(
        'div',
        { className: 'form-grid form-row' },
        el('div', null, el('label', { className: 'field-label', text: 'Ticker Symbol' }), el('input', { type: 'text', className: 'input mono', placeholder: 'e.g. PLTR', value: this.addTickerForm.ticker, required: true, onInput: (e) => { this.addTickerForm.ticker = e.target.value; } })),
        el(
          'div',
          null,
          el('label', { className: 'field-label', text: 'Currency' }),
          el(
            'select',
            {
              className: 'select mono',
              onChange: (e) => {
                this.addTickerForm.currency = e.target.value;
                this.addTickerForm.parity = e.target.value === 'SEK' ? '1' : '10';
                this._render();
              },
            },
            el('option', { value: 'USD', text: 'USD (NASDAQ)', selected: this.addTickerForm.currency === 'USD' }),
            el('option', { value: 'SEK', text: 'SEK (OMXSTO)', selected: this.addTickerForm.currency === 'SEK' }),
          ),
        ),
        el('div', null, el('label', { className: 'field-label', text: 'Hard Stop' }), el('input', { type: 'number', step: '0.01', className: 'input mono', value: this.addTickerForm.hardStop, required: true, onInput: (e) => { this.addTickerForm.hardStop = e.target.value; } })),
        el('div', null, el('label', { className: 'field-label', text: 'Take Profit' }), el('input', { type: 'number', step: '0.01', className: 'input mono', value: this.addTickerForm.takeProfit, onInput: (e) => { this.addTickerForm.takeProfit = e.target.value; } })),
        el('div', null, el('label', { className: 'field-label', text: 'Mini Financing Lvl' }), el('input', { type: 'number', step: '0.01', className: 'input mono', value: this.addTickerForm.miniFin, required: true, onInput: (e) => { this.addTickerForm.miniFin = e.target.value; } })),
        el('div', null, el('label', { className: 'field-label', text: 'Mini Parity' }), el('input', { type: 'number', step: '1', className: 'input mono', value: this.addTickerForm.parity, onInput: (e) => { this.addTickerForm.parity = e.target.value; } })),
      ),
      el('button', { type: 'submit', className: 'btn btn-cyan', disabled: this.submitting, text: 'Add Watchlist Ticker' }),
    );
  }

  _render() {
    clearEl(this.root);

    const header = el(
      'div',
      { className: 'panel-head' },
      el('h2', { className: 'panel-title', text: 'Avanza Ledger & Calculator' }),
      el('span', { className: 'badge badge-cyan mono', text: this.selectedTicker }),
    );

    const tabBar = el('div', { className: 'ledger-tabs', role: 'tablist', ariaLabel: 'Avanza order ledger tabs' });
    for (const t of LEDGER_TABS) {
      tabBar.appendChild(
        el('button', {
          type: 'button',
          role: 'tab',
          ariaSelected: this.activeTab === t.id,
          className: `ledger-tab-btn ${this.activeTab === t.id ? 'is-active' : ''}`,
          text: t.label,
          onClick: () => {
            this.activeTab = t.id;
            this._render();
            if (t.id === 'fill-mini') this._triggerCalculator(true);
          },
        }),
      );
    }

    const asset = this._getAsset();
    const spot = this._getSpot();
    const body = el('div', { className: 'panel-body' });

    if (this.activeTab === 'fill-mini') body.appendChild(this._renderFillMiniTab(asset, spot));
    else if (this.activeTab === 'close-mini') body.appendChild(this._renderCloseMiniTab(asset));
    else if (this.activeTab === 'stop') body.appendChild(this._renderStopTab(asset));
    else if (this.activeTab === 'shares') body.appendChild(this._renderSharesTab(asset));
    else if (this.activeTab === 'add-ticker') body.appendChild(this._renderAddTickerTab());

    this.root.append(header, tabBar, body);
  }
}
