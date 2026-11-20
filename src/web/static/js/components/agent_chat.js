/**
 * Plain-English Agent Chat & Ticker Analysis Panel (`agent_chat.js`).
 * Modes: Ask Gemina | Quant Advisor | Analyze [Ticker] (quick vs committee).
 * Renders Bottom-Line Verdict Banner, plain-English Why bullets,
 * collapsible Avanza Step-by-Step Guide (ONLY on BUY or SELL), and Trace Waterfall link.
 */
import { clearEl, el, formatCetTime, formatNumber, renderSafeMarkdown } from '../dom.js';

function classifyVerdictTone(action) {
  const act = String(action || 'HOLD').toUpperCase();
  if (act.startsWith('SELL')) return { cls: 'is-sell', badge: 'badge-red', label: `EXIT / ${act}` };
  if (act === 'BUY_MINI_SHORT') return { cls: 'is-short', badge: 'badge-cyan', label: `SHORT / ${act}` };
  if (act.startsWith('BUY')) return { cls: 'is-buy', badge: 'badge-green', label: `BUY / ${act}` };
  return { cls: 'is-hold', badge: '', label: 'WAIT / HOLD' };
}

function extractSentencesAsBullets(text, maxItems = 4) {
  const clean = String(text || '')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .trim();
  if (!clean) return [];
  const lines = clean
    .split(/\n+/)
    .map((l) => l.replace(/^[-*•\d.)]+\s*/, '').trim())
    .filter((l) => l.length > 10);
  if (lines.length >= 2) return lines.slice(0, maxItems);
  return clean
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 12)
    .slice(0, maxItems);
}

export class AgentChatController {
  constructor(rootEl, { onSendChat, onClearChat, onAnalyze, onInspectTrace, onPrefillOrder } = {}) {
    this.root = rootEl;
    this.onSendChat = onSendChat;
    this.onClearChat = onClearChat;
    this.onAnalyze = onAnalyze;
    this.onInspectTrace = onInspectTrace;
    this.onPrefillOrder = onPrefillOrder;

    this.mode = 'ask'; // 'ask' | 'quant' | 'analyze'
    this.depth = 'quick'; // 'quick' | 'committee'
    this.selectedTicker = 'NVDA';
    this.draftPrompt = '';
    this.pending = false;
    this.messages = [
      {
        id: 'welcome',
        role: 'assistant',
        mode: 'ask',
        ts: new Date().toISOString(),
        plainText:
          'Ask Gemina any portfolio, macro, or host health question, switch to **Quant Advisor** for trade-rule guidance, or click **Analyze** to run a quick or 4-level committee check.',
      },
    ];

    this._messagesEl = null;
    this._render();
  }

  setSelectedTicker(ticker) {
    const next = ticker || 'NVDA';
    if (next === this.selectedTicker) return;
    this.selectedTicker = next;
    this._render();
  }

  async _submit() {
    if (this.pending) return;

    if (this.mode === 'analyze') {
      const ticker = this.selectedTicker;
      const depth = this.depth;
      const note = this.draftPrompt.trim();
      const userLabel = note
        ? `Analyze ${ticker} (${depth}): ${note}`
        : `Analyze ${ticker} (${depth === 'committee' ? '4-Level Committee' : 'Quick Brief'})`;

      this.messages.push({
        id: `u-${Date.now()}`,
        role: 'user',
        mode: 'analyze',
        ts: new Date().toISOString(),
        text: userLabel,
      });
      this.draftPrompt = '';
      this.pending = true;
      this._render();

      try {
        const res = await this.onAnalyze?.(ticker, depth);
        this.messages.push(this._normalizeAssistantResponse(res, 'analyze'));
      } catch (err) {
        this.messages.push({
          id: `e-${Date.now()}`,
          role: 'assistant',
          mode: 'analyze',
          ts: new Date().toISOString(),
          error: err?.message || 'Analysis failed',
        });
      } finally {
        this.pending = false;
        this._render();
      }
      return;
    }

    const text = this.draftPrompt.trim();
    if (!text) return;

    this.messages.push({
      id: `u-${Date.now()}`,
      role: 'user',
      mode: this.mode,
      ts: new Date().toISOString(),
      text,
    });
    this.draftPrompt = '';
    this.pending = true;
    this._render();

    try {
      const res = await this.onSendChat?.(text, this.mode);
      this.messages.push(this._normalizeAssistantResponse(res, this.mode));
    } catch (err) {
      this.messages.push({
        id: `e-${Date.now()}`,
        role: 'assistant',
        mode: this.mode,
        ts: new Date().toISOString(),
        error: err?.message || 'Request failed',
      });
    } finally {
      this.pending = false;
      this._render();
    }
  }

  _normalizeAssistantResponse(res, mode) {
    const raw = res && typeof res === 'object' ? res : { reply: String(res || '') };
    const r = raw.result && typeof raw.result === 'object' ? raw.result : raw;
    const dec = r.decision && typeof r.decision === 'object' ? r.decision : null;
    const action = String(r.action ?? dec?.action ?? r.verdict ?? '').toUpperCase();
    const isTradeDecision = Boolean(
      dec ||
        action === 'HOLD' ||
        action.startsWith('BUY') ||
        action.startsWith('SELL'),
    );

    const rationale = r.rationale ?? dec?.rationale ?? '';
    const reply = r.reply ?? r.answer ?? r.summary ?? r.text ?? '';
    const plainReasons = Array.isArray(r.plain_reasons)
      ? r.plain_reasons
      : Array.isArray(dec?.plain_reasons)
        ? dec.plain_reasons
        : Array.isArray(r.why)
          ? r.why
          : [];

    return {
      id: `a-${Date.now()}`,
      role: 'assistant',
      mode,
      ts: new Date().toISOString(),
      isTradeDecision,
      action: action || (isTradeDecision ? 'HOLD' : ''),
      ticker: r.ticker ?? dec?.ticker ?? this.selectedTicker,
      instrument: r.instrument ?? dec?.instrument ?? '',
      quantity: r.quantity ?? dec?.quantity ?? 0,
      limitPrice: r.limit_price ?? dec?.limit_price ?? null,
      stopPrice: r.underlying_stop_price ?? dec?.underlying_stop_price ?? null,
      targetPrice: r.take_profit_target ?? dec?.take_profit_target ?? null,
      confidence: r.confidence_score ?? dec?.confidence_score ?? null,
      criticVerdict: r.critic_verdict ?? dec?.critic_verdict ?? '',
      criticNotes: r.critic_notes ?? dec?.critic_notes ?? '',
      regime: r.market_regime ?? dec?.market_regime ?? '',
      netRr: r.net_risk_reward_ratio ?? dec?.net_risk_reward_ratio ?? null,
      plainReasons,
      avanzaGuide: r.avanza_execution_guide ?? dec?.avanza_execution_guide ?? '',
      rationale,
      plainText: reply || rationale,
      traceId: r.trace_id ?? r.trace?.trace_id ?? '',
      model: r.model ?? '',
    };
  }

  _renderAssistantMessage(msg) {
    const wrap = el('div', { className: 'chat-msg is-assistant' });

    const metaLeft = msg.mode === 'analyze' ? `Analysis · ${msg.ticker}` : msg.mode === 'quant' ? 'Quant Advisor' : 'Gemina AI';
    const metaParts = [el('span', { text: metaLeft })];
    if (msg.traceId && this.onInspectTrace) {
      metaParts.push(
        el('button', {
          type: 'button',
          className: 'btn btn-xs btn-ghost mono',
          text: `Trace ${msg.traceId} →`,
          onClick: () => this.onInspectTrace(msg.traceId),
        }),
      );
    } else {
      metaParts.push(el('span', { className: 'mono', text: formatCetTime(msg.ts) }));
    }
    wrap.appendChild(el('div', { className: 'chat-msg-meta' }, metaParts));

    if (msg.error) {
      wrap.appendChild(el('div', { className: 'form-error', text: msg.error }));
      return wrap;
    }

    if (msg.isTradeDecision) {
      const tone = classifyVerdictTone(msg.action);
      const isActiveOrder = msg.action.startsWith('BUY_') || msg.action.startsWith('SELL_');

      const bannerLeft = isActiveOrder && msg.quantity > 0
        ? `${tone.label} · ${msg.quantity}x ${msg.instrument || msg.ticker}`
        : `${tone.label} · ${msg.ticker}`;

      const statsBits = [];
      if (msg.confidence !== null && msg.confidence !== undefined) statsBits.push(`${msg.confidence}/100`);
      if (msg.netRr !== null && msg.netRr !== undefined && Number(msg.netRr) > 0) {
        statsBits.push(`R:R 1:${formatNumber(msg.netRr, 2)}`);
      }
      if (msg.criticVerdict) statsBits.push(msg.criticVerdict);

      wrap.appendChild(
        el(
          'div',
          { className: `verdict-banner ${tone.cls}` },
          el('span', null, bannerLeft),
          statsBits.length > 0 ? el('span', { className: 'mono', text: statsBits.join(' · ') }) : null,
        ),
      );

      const bullets = msg.plainReasons.length > 0
        ? msg.plainReasons
        : extractSentencesAsBullets(msg.plainText || msg.rationale, 4);

      if (bullets.length > 0) {
        const ul = el('ul', { className: 'why-bullets' });
        for (const b of bullets) {
          ul.appendChild(el('li', { text: b }));
        }
        wrap.appendChild(ul);
      } else if (msg.plainText) {
        const bodyDiv = el('div');
        renderSafeMarkdown(bodyDiv, msg.plainText);
        wrap.appendChild(bodyDiv);
      }

      // Collapsible Avanza Step-by-Step Guide ONLY on active BUY_* / SELL_* signals
      if (isActiveOrder && (msg.avanzaGuide || this.onPrefillOrder)) {
        const guideContent = el('div');
        const fallbackGuide =
          `Place Limit order on \`${msg.instrument || msg.ticker}\` (${msg.quantity || 0} units` +
          `${msg.limitPrice ? ` @ ${msg.limitPrice}` : ''}), then attach Avanza Stop-Loss (` +
          `${msg.stopPrice ? `underlying stop ${msg.stopPrice}` : 'Säljkurs 0.20 SEK below Triggerkurs'}).`;
        renderSafeMarkdown(guideContent, msg.avanzaGuide || fallbackGuide);
        const guideActions = this.onPrefillOrder
          ? el('div', { className: 'preset-row' },
              el('button', {
                type: 'button',
                className: 'btn btn-xs btn-green',
                text: 'Prefill Order Form ↓',
                onClick: () => this.onPrefillOrder(msg),
              }),
            )
          : null;

        wrap.appendChild(
          el(
            'details',
            { className: 'guide-details', open: true },
            el('summary', { text: 'Avanza Step-by-Step Order Guide' }),
            guideContent,
            guideActions,
          ),
        );
      }

      // Collapsible technical rationale / trace link
      if (msg.rationale || msg.criticNotes || msg.traceId) {
        const techInner = el('div');
        const techLines = [];
        if (msg.regime) techLines.push(`**Regime:** \`${msg.regime}\``);
        if (msg.criticNotes) techLines.push(`**Critic Notes:** ${msg.criticNotes}`);
        if (msg.rationale) techLines.push(`**Technical Rationale:** ${msg.rationale}`);
        renderSafeMarkdown(techInner, techLines.join('\n\n'));

        const traceBtn = msg.traceId && this.onInspectTrace
          ? el('button', {
              type: 'button',
              className: 'btn btn-xs btn-cyan mono',
              text: `Open Trace ${msg.traceId} in Waterfall →`,
              onClick: () => this.onInspectTrace(msg.traceId),
            })
          : null;

        wrap.appendChild(
          el(
            'details',
            { className: 'tech-details' },
            el('summary', { text: 'Raw Technical Rationale & Trace' }),
            techInner,
            traceBtn,
          ),
        );
      }

      return wrap;
    }

    // Standard plain-English Q&A answer
    const mdNode = el('div');
    renderSafeMarkdown(mdNode, msg.plainText || '');
    wrap.appendChild(mdNode);
    return wrap;
  }

  _render() {
    clearEl(this.root);

    const header = el(
      'div',
      { className: 'panel-head' },
      el('h2', { className: 'panel-title', text: 'AI Advisor & Signal Brief' }),
      el('button', {
        type: 'button',
        className: 'btn btn-xs btn-ghost',
        disabled: this.pending,
        text: 'Clear Chat',
        onClick: async () => {
          await this.onClearChat?.();
          this.messages = [
            {
              id: `clr-${Date.now()}`,
              role: 'assistant',
              mode: this.mode,
              ts: new Date().toISOString(),
              plainText: 'Conversation memory cleared.',
            },
          ];
          this._render();
        },
      }),
    );

    const segBar = el(
      'div',
      { className: 'segmented-bar', role: 'tablist', ariaLabel: 'Agent interaction mode' },
      el('button', {
        type: 'button',
        role: 'tab',
        ariaSelected: this.mode === 'ask',
        className: `seg-btn ${this.mode === 'ask' ? 'is-active' : ''}`,
        text: 'Ask Gemina',
        onClick: () => {
          this.mode = 'ask';
          this._render();
        },
      }),
      el('button', {
        type: 'button',
        role: 'tab',
        ariaSelected: this.mode === 'quant',
        className: `seg-btn ${this.mode === 'quant' ? 'is-active' : ''}`,
        text: 'Quant Advisor',
        onClick: () => {
          this.mode = 'quant';
          this._render();
        },
      }),
      el('button', {
        type: 'button',
        role: 'tab',
        ariaSelected: this.mode === 'analyze',
        className: `seg-btn ${this.mode === 'analyze' ? 'is-active' : ''}`,
        text: `Analyze ${this.selectedTicker}`,
        onClick: () => {
          this.mode = 'analyze';
          this._render();
        },
      }),
    );

    const subBar = el('div', { className: 'chat-subbar' }, segBar);

    if (this.mode === 'analyze') {
      subBar.appendChild(
        el(
          'div',
          { className: 'chart-controls' },
          el('button', {
            type: 'button',
            className: `btn btn-xs ${this.depth === 'quick' ? 'btn-cyan' : 'btn-ghost'}`,
            text: 'Quick',
            onClick: () => {
              this.depth = 'quick';
              this._render();
            },
          }),
          el('button', {
            type: 'button',
            className: `btn btn-xs ${this.depth === 'committee' ? 'btn-cyan' : 'btn-ghost'}`,
            text: 'Committee (4-Lvl)',
            onClick: () => {
              this.depth = 'committee';
              this._render();
            },
          }),
          el('button', {
            type: 'button',
            className: 'btn btn-xs btn-green',
            disabled: this.pending,
            text: this.pending ? 'Analyzing...' : `Run ${this.selectedTicker}`,
            onClick: () => this._submit(),
          }),
        ),
      );
    }

    this._messagesEl = el('div', { className: 'chat-messages' });
    for (const msg of this.messages) {
      if (msg.role === 'user') {
        this._messagesEl.appendChild(
          el(
            'div',
            { className: 'chat-msg is-user' },
            el('div', { className: 'chat-msg-meta' }, el('span', { text: 'You' }), el('span', { className: 'mono', text: formatCetTime(msg.ts) })),
            el('div', { text: msg.text }),
          ),
        );
      } else {
        this._messagesEl.appendChild(this._renderAssistantMessage(msg));
      }
    }

    if (this.pending) {
      this._messagesEl.appendChild(
        el(
          'div',
          { className: 'chat-msg is-assistant' },
          el('div', { className: 'txt-cyan mono', text: 'Running Gemina reasoning pipeline...' }),
        ),
      );
    }

    const quickPrompts =
      this.mode === 'quant'
        ? [
            `Should I enter ${this.selectedTicker} Mini now?`,
            `Check ${this.selectedTicker} stop & +0.85R ratchet`,
            'Summarize active L4 guardrails',
          ]
        : [
            `Live news & USD/SEK for ${this.selectedTicker}`,
            'Portfolio & host health brief',
            'Any active risk alerts?',
          ];

    const chipsRow = el('div', { className: 'chat-quick-chips' });
    if (this.mode !== 'analyze') {
      for (const q of quickPrompts) {
        chipsRow.appendChild(
          el('button', {
            type: 'button',
            className: 'btn btn-xs btn-ghost',
            disabled: this.pending,
            text: q,
            onClick: () => {
              this.draftPrompt = q;
              this._submit();
            },
          }),
        );
      }
    }

    const textarea = el('textarea', {
      className: 'textarea',
      rows: 2,
      placeholder:
        this.mode === 'analyze'
          ? `Click "Run ${this.selectedTicker}" above for ${this.depth} analysis...`
          : `Ask ${this.mode === 'quant' ? 'Quant Advisor' : 'Gemina'} (Enter to send, Shift+Enter newline)...`,
      value: this.draftPrompt,
      disabled: this.pending,
      onInput: (e) => {
        this.draftPrompt = e.target.value;
      },
      onKeyDown: (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          this._submit();
        }
      },
    });

    const sendBtn = el('button', {
      type: 'button',
      className: 'btn btn-cyan',
      disabled: this.pending,
      text: this.pending ? '...' : this.mode === 'analyze' ? 'Analyze' : 'Send',
      onClick: () => this._submit(),
    });

    const composer = el(
      'div',
      { className: 'chat-composer' },
      this.mode !== 'analyze' ? chipsRow : null,
      el('div', { className: 'chat-input-row' }, textarea, sendBtn),
    );

    this.root.append(header, subBar, this._messagesEl, composer);
    this._messagesEl.scrollTop = this._messagesEl.scrollHeight;
  }
}
