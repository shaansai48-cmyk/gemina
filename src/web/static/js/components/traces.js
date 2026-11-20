/**
 * Live Trace Waterfall Component (`traces.js`).
 * Streams live trace.stage & trace.completed SSE events and renders proportional
 * horizontal duration bars across L1 -> L2 -> L3a -> L3b -> L4 with safe expandable JSON drawers.
 */
import { clearEl, el, formatCetTime, formatDurationMs, formatJsonPretty } from '../dom.js';

const COMMITTEE_STEPS = [
  { id: 'L1', label: 'L1 Catalyst', match: ['L1_CATALYST', 'SEARCH_GROUNDING'] },
  { id: 'L2', label: 'L2 Strategist', match: ['L2_ALPHA', 'LLM_INFERENCE'] },
  { id: 'L3a', label: 'L3a Math', match: ['L3A_PYTHON_MATH'] },
  { id: 'L3b', label: 'L3b Critic', match: ['L3_ADVERSARIAL_CRITIC'] },
  { id: 'L4', label: 'L4 Guardrails', match: ['L4_HARD_PYTHON', 'GUARDRAIL_TRANSFORM'] },
];

function classifyStageTone(status) {
  const s = String(status || '').toUpperCase();
  if (s.includes('ERROR') || s.includes('REJECT') || s.includes('VETOED') || s.includes('FAIL')) {
    return { badge: 'badge-red', bar: 'is-err' };
  }
  if (s.includes('MUTATED') || s.includes('REVISED') || s.includes('CORRECTED') || s.includes('DEGRADED') || s.includes('OLLAMA')) {
    return { badge: 'badge-amber', bar: 'is-warn' };
  }
  if (s.includes('OK') || s.includes('APPROVED') || s.includes('PASSED') || s.includes('VERIFIED') || s.includes('SENT') || s.includes('COMPLETED')) {
    return { badge: 'badge-green', bar: 'is-ok' };
  }
  return { badge: 'badge-cyan', bar: '' };
}

export class TracesController {
  constructor(rootEl, { onFetchTraceDetail, onRefreshTraces } = {}) {
    this.root = rootEl;
    this.onFetchTraceDetail = onFetchTraceDetail;
    this.onRefreshTraces = onRefreshTraces;

    this.activeTraces = new Map();
    this.completedTraces = [];
    this.selectedTraceId = null;
    this.expandedStages = new Set();
    this.filterText = '';

    this._layoutEl = null;
    this._buildShell();
    this._render();
  }

  _buildShell() {
    clearEl(this.root);

    const filterInput = el('input', {
      type: 'search',
      className: 'input mono',
      placeholder: 'Filter trace_id, flow, ticker...',
      ariaLabel: 'Filter pipeline traces',
      value: this.filterText,
      onInput: (e) => {
        this.filterText = e.target.value;
        this._render();
      },
    });

    const toolbar = el(
      'div',
      { className: 'chart-controls' },
      el('div', { className: 'form-row' }, filterInput),
      el('button', {
        type: 'button',
        className: 'btn btn-sm',
        text: '↻ Refresh Traces',
        onClick: () => this.onRefreshTraces?.(),
      }),
    );

    this._layoutEl = el('div', { className: 'trace-layout' });
    this.root.append(toolbar, this._layoutEl);
  }

  setTraces({ traces = [], activeTraces = [] }) {
    this.activeTraces.clear();
    for (const t of activeTraces) {
      if (t && t.trace_id) {
        this.activeTraces.set(t.trace_id, t);
      }
    }
    this.completedTraces = Array.isArray(traces) ? [...traces] : [];
    this.completedTraces.sort((a, b) => String(b.arrival_ts || '').localeCompare(String(a.arrival_ts || '')));

    if (!this.selectedTraceId) {
      const firstActive = this.activeTraces.values().next().value;
      const firstDone = this.completedTraces[0];
      this.selectedTraceId = firstActive?.trace_id || firstDone?.trace_id || null;
    }
    this._render();
  }

  handleTraceStage(payload) {
    if (!payload || !payload.trace_id || !payload.stage) return;
    const id = payload.trace_id;
    let trace = this.activeTraces.get(id);
    if (!trace) {
      trace = {
        trace_id: id,
        flow: payload.flow || 'live_pipeline',
        source: payload.source || 'daemon',
        final_status: 'IN_PROGRESS',
        arrival_ts: payload.arrival_ts || new Date().toISOString(),
        leaving_ts: null,
        total_duration_ms: 0,
        stages: [],
      };
      this.activeTraces.set(id, trace);
    }
    const stage = payload.stage;
    const existsIdx = trace.stages.findIndex((s) => s.seq === stage.seq && s.stage === stage.stage);
    if (existsIdx >= 0) {
      trace.stages[existsIdx] = stage;
    } else {
      trace.stages.push(stage);
    }
    trace.stage_count = trace.stages.length;
    trace.total_duration_ms = trace.stages.reduce((acc, s) => acc + Number(s.duration_ms || 0), 0);

    // Auto-focus live incoming trace if user is currently on latest or nothing selected
    if (!this.selectedTraceId) {
      this.selectedTraceId = id;
    }
    this._render();
  }

  handleTraceCompleted(record) {
    if (!record || !record.trace_id) return;
    this.activeTraces.delete(record.trace_id);
    const idx = this.completedTraces.findIndex((t) => t.trace_id === record.trace_id);
    if (idx >= 0) {
      this.completedTraces[idx] = record;
    } else {
      this.completedTraces.unshift(record);
      if (this.completedTraces.length > 60) {
        this.completedTraces.length = 60;
      }
    }
    if (!this.selectedTraceId || this.selectedTraceId === record.trace_id) {
      this.selectedTraceId = record.trace_id;
    }
    this._render();
  }

  async selectTrace(traceId) {
    if (!traceId) return;
    this.selectedTraceId = traceId;
    this.expandedStages.clear();
    const existing = this._findTrace(traceId);
    if (!existing && this.onFetchTraceDetail) {
      this._render();
      try {
        const fetched = await this.onFetchTraceDetail(traceId);
        if (fetched && fetched.trace_id) {
          this.handleTraceCompleted(fetched);
          return;
        }
      } catch {
        // Ignore fetch errors; render fallback
      }
    }
    this._render();
  }

  _findTrace(traceId) {
    if (!traceId) return null;
    if (this.activeTraces.has(traceId)) return this.activeTraces.get(traceId);
    return this.completedTraces.find((t) => t.trace_id === traceId) || null;
  }

  _allTracesOrdered() {
    const active = Array.from(this.activeTraces.values()).sort((a, b) =>
      String(b.arrival_ts || '').localeCompare(String(a.arrival_ts || '')),
    );
    const all = [...active, ...this.completedTraces];
    const q = this.filterText.trim().toLowerCase();
    if (!q) return all;
    return all.filter(
      (t) =>
        String(t.trace_id || '').toLowerCase().includes(q) ||
        String(t.flow || '').toLowerCase().includes(q) ||
        String(t.source || '').toLowerCase().includes(q) ||
        String(t.final_status || '').toLowerCase().includes(q),
    );
  }

  _renderCommitteeStrip(stages) {
    const strip = el('div', { className: 'pipeline-pills' });
    for (const step of COMMITTEE_STEPS) {
      const matched = stages.find((s) => step.match.some((m) => String(s.stage || '').toUpperCase().includes(m)));
      let cls = '';
      let suffix = '';
      if (matched) {
        cls = classifyStageTone(matched.status).badge;
        suffix = ` (${formatDurationMs(matched.duration_ms)})`;
      }
      strip.appendChild(
        el('span', {
          className: `badge ${cls} mono`.trim(),
          title: matched ? `${matched.stage}: ${matched.status}` : `${step.label} not present in this flow`,
          text: `${step.label}${suffix}`,
        }),
      );
    }
    return strip;
  }

  _renderStageRow(traceId, stage, offsetMs, totalMs) {
    const key = `${traceId}:${stage.seq}:${stage.stage}`;
    const isExpanded = this.expandedStages.has(key);
    const tone = classifyStageTone(stage.status);
    const dur = Math.max(0, Number(stage.duration_ms || 0));
    const safeTotal = Math.max(1, totalMs);
    const leftPct = Math.min(98, (offsetMs / safeTotal) * 100);
    const widthPct = Math.max(1.5, Math.min(100 - leftPct, (dur / safeTotal) * 100));

    const barFill = el('div', { className: `wf-bar-fill ${tone.bar}`.trim() });
    barFill.style.left = `${leftPct.toFixed(2)}%`;
    barFill.style.width = `${widthPct.toFixed(2)}%`;

    const row = el(
      'div',
      {
        className: 'wf-stage-row',
        role: 'button',
        tabIndex: 0,
        ariaExpanded: isExpanded,
        onClick: () => {
          if (this.expandedStages.has(key)) {
            this.expandedStages.delete(key);
          } else {
            this.expandedStages.add(key);
          }
          this._render();
        },
        onKeyDown: (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            row.click();
          }
        },
      },
      el('div', { className: 'wf-stage-name mono', title: stage.stage, text: `${stage.seq}. ${stage.stage}` }),
      el('div', { className: 'wf-bar-track', title: `${formatDurationMs(dur)} @ +${formatDurationMs(offsetMs)}` }, barFill),
      el('div', { className: 'wf-dur mono', text: formatDurationMs(dur) }),
      el('span', { className: `badge ${tone.badge} mono`, text: stage.status || 'OK' }),
    );

    const card = el(
      'div',
      { className: 'wf-stage-card' },
      row,
      stage.transformation
        ? el('div', { className: 'wf-transform-line', text: `${isExpanded ? '▾' : '▸'} ${stage.transformation}` })
        : null,
    );

    if (isExpanded) {
      card.appendChild(
        el(
          'div',
          { className: 'wf-json-drawer' },
          el(
            'div',
            null,
            el('div', { className: 'json-box-title', text: 'Stage Input' }),
            el('pre', { className: 'json-pre mono', text: formatJsonPretty(stage.input) }),
          ),
          el(
            'div',
            null,
            el('div', { className: 'json-box-title', text: 'Stage Output' }),
            el('pre', { className: 'json-pre mono', text: formatJsonPretty(stage.output) }),
          ),
        ),
      );
    }

    return card;
  }

  _render() {
    if (!this._layoutEl) return;
    clearEl(this._layoutEl);

    const traces = this._allTracesOrdered();
    const listEl = el('div', { className: 'trace-list' });
    if (traces.length === 0) {
      listEl.appendChild(el('div', { className: 'txt-secondary', text: 'No pipeline traces match current filter.' }));
    } else {
      for (const tr of traces) {
        const isLive = tr.final_status === 'IN_PROGRESS';
        const isSel = tr.trace_id === this.selectedTraceId;
        const tone = isLive ? { badge: 'badge-cyan' } : classifyStageTone(tr.final_status);
        listEl.appendChild(
          el(
            'button',
            {
              type: 'button',
              className: `trace-item ${isSel ? 'is-selected' : ''}`.trim(),
              onClick: () => this.selectTrace(tr.trace_id),
            },
            el(
              'div',
              { className: 'trace-item-top' },
              el('span', { className: 'trace-id-text mono', text: tr.trace_id }),
              el('span', {
                className: `badge ${tone.badge} mono`,
                text: isLive ? 'LIVE' : tr.final_status || 'DONE',
              }),
            ),
            el(
              'div',
              { className: 'trace-item-meta mono' },
              el('span', { text: `${tr.flow || '-'} · ${tr.stage_count ?? tr.stages?.length ?? 0}st` }),
              el('span', { text: formatDurationMs(tr.total_duration_ms) }),
            ),
            el(
              'div',
              { className: 'trace-item-meta mono' },
              el('span', { text: tr.source || '' }),
              el('span', { text: formatCetTime(tr.arrival_ts, true) }),
            ),
          ),
        );
      }
    }

    const detailEl = el('div', { className: 'waterfall-detail' });
    const selected = this._findTrace(this.selectedTraceId) || traces[0] || null;
    if (!selected) {
      detailEl.appendChild(el('div', { className: 'txt-secondary', text: 'Select a trace to inspect its multi-agent stage waterfall.' }));
    } else {
      const stages = Array.isArray(selected.stages) ? selected.stages : [];
      const sumStageMs = stages.reduce((acc, s) => acc + Math.max(0, Number(s.duration_ms || 0)), 0);
      const totalMs = Math.max(Number(selected.total_duration_ms || 0), sumStageMs, 1);
      const tone = selected.final_status === 'IN_PROGRESS' ? { badge: 'badge-cyan' } : classifyStageTone(selected.final_status);

      detailEl.append(
        el(
          'div',
          { className: 'waterfall-header' },
          el(
            'div',
            null,
            el('div', { className: 'mono', text: selected.trace_id }),
            el('div', {
              className: 'panel-subtitle mono',
              text: `${selected.flow || '-'} · ${selected.source || '-'} · Arrival ${formatCetTime(selected.arrival_ts, true)}`,
            }),
          ),
          el(
            'div',
            { className: 'chart-controls' },
            el('span', { className: 'pill mono', text: `Total ${formatDurationMs(totalMs)}` }),
            el('span', { className: `badge ${tone.badge} mono`, text: selected.final_status || 'COMPLETED' }),
          ),
        ),
        this._renderCommitteeStrip(stages),
      );

      let offset = 0;
      for (const stage of stages) {
        detailEl.appendChild(this._renderStageRow(selected.trace_id, stage, offset, totalMs));
        offset += Math.max(0, Number(stage.duration_ms || 0));
      }
    }

    this._layoutEl.append(listEl, detailEl);
  }
}
