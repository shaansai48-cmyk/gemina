/**
 * System Health & Live Hardware Diagnostics Component (`health.js`).
 * Displays GPU temp/VRAM/util bars, RAM/Disk bars, service status cards, and live journalctl tail.
 */
import { clearEl, el, formatNumber } from '../dom.js';

function meterTone(pct, warnThreshold = 75, critThreshold = 90) {
  if (pct >= critThreshold) return 'is-red';
  if (pct >= warnThreshold) return 'is-amber';
  return '';
}

export class HealthController {
  constructor(rootEl, { onRefresh } = {}) {
    this.root = rootEl;
    this.onRefresh = onRefresh;
    this.health = null;
    this.autoScrollLogs = true;
    this._render();
  }

  setHealth(payload) {
    if (!payload || typeof payload !== 'object') {
      this.health = payload;
    } else if (this.health && !payload.commands && !payload.diagnostic_commands) {
      this.health = {
        ...this.health,
        ...payload,
        commands: this.health.commands || this.health.diagnostic_commands,
        diagnostic_commands: this.health.diagnostic_commands || this.health.commands,
      };
    } else {
      this.health = payload;
    }
    this._render();
  }

  _extractDiagnostics() {
    const h = this.health || {};
    const status = h.status || {};
    const diag = h.diagnostics || h.host_metrics || h.host || status.diagnostics || {};
    const cmds = h.commands || h.diagnostic_commands || h.command_outputs || h.services || {};
    return { status, diag, cmds };
  }

  _meterCard(title, valueText, pct, subText, warnAt = 75, critAt = 90, forceCyan = false) {
    const clamped = Math.max(0, Math.min(100, Number(pct) || 0));
    const fillCls = forceCyan ? 'is-cyan' : meterTone(clamped, warnAt, critAt);
    const fill = el('div', { className: `meter-fill ${fillCls}`.trim() });
    fill.style.width = `${clamped.toFixed(1)}%`;

    return el(
      'div',
      { className: 'health-card' },
      el(
        'div',
        { className: 'tc-row-top' },
        el('span', { className: 'kpi-label', text: title }),
        el('span', { className: 'mono kpi-value', text: valueText }),
      ),
      el('div', { className: 'meter-track' }, fill),
      el('div', { className: 'kpi-sub mono', text: subText }),
    );
  }

  _render() {
    clearEl(this.root);

    if (!this.health) {
      this.root.appendChild(el('div', { className: 'txt-secondary', text: 'Loading host & GPU diagnostics...' }));
      return;
    }

    const { status, diag, cmds } = this._extractDiagnostics();

    const gpuTemp = Number(diag.gpu_temp_c ?? 0);
    const gpuUtil = Number(diag.gpu_util_pct ?? 0);
    let vramUsed = Number(diag.gpu_mem_used_mb ?? 0);
    let vramTotal = Number(diag.gpu_mem_total_mb ?? 16384);
    if ((!vramUsed || vramTotal === 16384) && typeof diag.gpu_vram_mb === 'string' && diag.gpu_vram_mb.includes('/')) {
      const [u, t] = diag.gpu_vram_mb.split('/');
      vramUsed = Number(u) || vramUsed;
      vramTotal = Number(t) || vramTotal;
    }
    const vramPct = vramTotal > 0 ? (vramUsed / vramTotal) * 100 : 0;
    const ramPct = Number(diag.ram_used_pct ?? 0);
    const diskPct = Number(diag.disk_used_pct ?? 0);

    const metersGrid = el(
      'div',
      { className: 'health-grid' },
      this._meterCard(
        'GPU Thermal & Compute',
        `${gpuTemp}°C · ${gpuUtil}%`,
        Math.max((gpuTemp / 85) * 100, gpuUtil),
        `Temp limit 85°C · Util ${gpuUtil}%`,
        75,
        85,
      ),
      this._meterCard(
        'GPU VRAM (RTX 3080 Ti)',
        `${formatNumber(vramUsed, 0)} / ${formatNumber(vramTotal, 0)} MB`,
        vramPct,
        `${vramPct.toFixed(1)}% allocated`,
        80,
        94,
        true,
      ),
      this._meterCard(
        'Host RAM & Root Disk',
        `RAM ${ramPct.toFixed(1)}% · Disk ${diskPct.toFixed(1)}%`,
        Math.max(ramPct, diskPct),
        `Thresholds: RAM 92% · Disk 92%`,
        80,
        92,
      ),
    );

    // Service status parsing
    const servicesRaw = String(cmds.services || '');
    const serviceLines = servicesRaw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    const nvdaSvcState =
      diag.nvda_copilot_service ||
      serviceLines[0] ||
      (this.health.daemon_running ? 'active' : 'unknown');
    const ollamaState = diag.ollama_service || serviceLines[1] || 'unknown';
    const appsScriptOk = diag.apps_script_ok !== false;
    const appsScriptErr = diag.apps_script_error || this.health.apps_script_error || '';

    const svcBadge = (val) => {
      const ok = String(val).toLowerCase() === 'active' || val === true || String(val).toUpperCase() === 'OK';
      return el('span', {
        className: `badge ${ok ? 'badge-green' : 'badge-red'} mono`,
        text: String(val),
      });
    };

    const svcGrid = el(
      'div',
      { className: 'svc-grid' },
      el(
        'div',
        { className: 'health-card' },
        el('div', { className: 'tc-row-top' }, el('span', { className: 'kpi-label', text: 'nvda-copilot' }), svcBadge(nvdaSvcState)),
        el('div', { className: 'kpi-sub mono', text: cmds.uptime_load || status.ticker_line || 'Systemd service' }),
      ),
      el(
        'div',
        { className: 'health-card' },
        el('div', { className: 'tc-row-top' }, el('span', { className: 'kpi-label', text: 'ollama.service' }), svcBadge(ollamaState)),
        el('div', { className: 'kpi-sub mono', text: cmds.ollama_models || 'Local 16GB GPU failover' }),
      ),
      el(
        'div',
        { className: 'health-card' },
        el(
          'div',
          { className: 'tc-row-top' },
          el('span', { className: 'kpi-label', text: 'Apps Script' }),
          svcBadge(appsScriptOk ? 'OK' : 'ERROR'),
        ),
        el('div', {
          className: 'kpi-sub mono',
          text: appsScriptOk ? 'Webhook buffer healthy' : appsScriptErr || 'Webhook check failed',
        }),
      ),
    );

    const logsText = cmds.recent_logs || this.health.recent_logs || 'No journalctl output available.';
    const journalPre = el('pre', { className: 'journal-pre mono', text: logsText });

    const headerBar = el(
      'div',
      { className: 'waterfall-header' },
      el(
        'div',
        null,
        el('strong', { text: 'Host Hardware, Services & Journalctl Tail' }),
        el('div', { className: 'panel-subtitle mono', text: cmds.memory ? cmds.memory.split('\n')[1] || '' : '' }),
      ),
      el(
        'div',
        { className: 'chart-controls' },
        el('label', { className: 'overlay-check' },
          el('input', {
            type: 'checkbox',
            checked: this.autoScrollLogs,
            onChange: (e) => {
              this.autoScrollLogs = e.target.checked;
            },
          }),
          'Auto-scroll logs',
        ),
        el('button', {
          type: 'button',
          className: 'btn btn-sm',
          text: '↻ Refresh Health',
          onClick: () => this.onRefresh?.(),
        }),
      ),
    );

    this.root.append(headerBar, metersGrid, svcGrid, journalPre);

    if (this.autoScrollLogs) {
      journalPre.scrollTop = journalPre.scrollHeight;
    }
  }
}
