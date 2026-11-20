/**
 * Declarative Agents & Skills Markdown Preview/Editor + Hot Reload (`declarative.js`).
 */
import { clearEl, el, formatNumber, renderSafeMarkdown } from '../dom.js';

export class DeclarativeController {
  constructor(rootEl, { onReload, onSaveFile, onRefresh } = {}) {
    this.root = rootEl;
    this.onReload = onReload;
    this.onSaveFile = onSaveFile;
    this.onRefresh = onRefresh;

    this.agents = [];
    this.skills = [];
    this.selectedKey = '';
    this.viewMode = 'preview'; // 'preview' | 'edit'
    this.draftContent = '';
    this.dirty = false;
    this.busy = false;

    this._render();
  }

  setData(payload) {
    const rawAgents = Array.isArray(payload?.agents) ? payload.agents : [];
    const rawSkills = Array.isArray(payload?.skills) ? payload.skills : [];

    this.agents = rawAgents.map((a) => ({
      ...a,
      _group: 'agents',
      _kindParam: a.kind || 'agents',
      _nameParam: a.filename || a.name || a.id,
      _key: `agents:${a.filename || a.name || a.id}`,
    }));

    this.skills = rawSkills.map((s) => ({
      ...s,
      _group: 'skills',
      _kindParam: s.kind || 'skills',
      _nameParam: s.name || s.filename || s.id,
      _key: `skills:${s.name || s.filename || s.id}`,
    }));

    const all = [...this.agents, ...this.skills];
    if (!this.selectedKey && all.length > 0) {
      this.selectedKey = all[0]._key;
      this.draftContent = this._extractContent(all[0]);
      this.dirty = false;
    } else if (this.selectedKey && !this.dirty) {
      const current = all.find((item) => item._key === this.selectedKey);
      if (current) {
        this.draftContent = this._extractContent(current);
      }
    }

    this._render();
  }

  setBusy(busy) {
    this.busy = busy;
    this._render();
  }

  _extractContent(item) {
    if (!item) return '';
    return item.raw_markdown ?? item.raw_text ?? item.raw_content ?? item.content ?? item.body ?? '';
  }

  _getSelected() {
    const all = [...this.agents, ...this.skills];
    return all.find((x) => x._key === this.selectedKey) || all[0] || null;
  }

  _selectItem(item) {
    this.selectedKey = item._key;
    this.draftContent = this._extractContent(item);
    this.dirty = false;
    this._render();
  }

  async _handleSave() {
    const sel = this._getSelected();
    if (!sel || !this.onSaveFile) return;
    await this.onSaveFile(sel._kindParam, sel._nameParam, this.draftContent);
    this.dirty = false;
    this._render();
  }

  _render() {
    clearEl(this.root);

    const headerBar = el(
      'div',
      { className: 'waterfall-header' },
      el(
        'div',
        null,
        el('strong', { text: 'Declarative Markdown Agents & Skills' }),
        el('div', {
          className: 'panel-subtitle',
          text: `${this.agents.length} agents · ${this.skills.length} skills`,
        }),
      ),
      el(
        'div',
        { className: 'chart-controls' },
        el('button', {
          type: 'button',
          className: 'btn btn-sm',
          disabled: this.busy,
          text: '↻ Refresh',
          onClick: () => this.onRefresh?.(),
        }),
        el('button', {
          type: 'button',
          className: 'btn btn-sm btn-cyan',
          disabled: this.busy,
          text: this.busy ? 'Reloading...' : '⚡ 1-Click Hot Reload',
          onClick: () => this.onReload?.(),
        }),
      ),
    );

    const all = [...this.agents, ...this.skills];
    if (all.length === 0) {
      this.root.append(headerBar, el('div', { className: 'txt-secondary', text: 'No declarative markdown files loaded yet.' }));
      return;
    }

    const listEl = el('div', { className: 'decl-list' });
    const appendGroup = (title, items) => {
      if (items.length === 0) return;
      listEl.appendChild(el('div', { className: 'kpi-label', text: title }));
      for (const item of items) {
        const isSel = item._key === this.selectedKey;
        const chars = item.compiled_chars ?? item.compiled_prompt_chars ?? this._extractContent(item).length;
        listEl.appendChild(
          el(
            'button',
            {
              type: 'button',
              className: `decl-item-btn ${isSel ? 'is-selected' : ''}`.trim(),
              onClick: () => this._selectItem(item),
            },
            el(
              'div',
              { className: 'trace-item-top' },
              el('strong', { className: 'mono', text: item.name || item._nameParam }),
              el('span', { className: 'badge mono', text: `${formatNumber(chars, 0)} ch` }),
            ),
            el('div', {
              className: 'panel-subtitle mono',
              text: item.model
                ? `${item.model}${item.fallback_model ? ` → ${item.fallback_model}` : ''}`
                : item.version
                  ? `v${item.version}`
                  : item.source_path || '',
            }),
          ),
        );
      }
    };

    appendGroup('Agents (agents/*.md)', this.agents);
    appendGroup('Skills (skills/*/SKILL.md)', this.skills);

    const sel = this._getSelected();
    const detailEl = el('div', { className: 'waterfall-detail' });

    if (sel) {
      const compiledChars = sel.compiled_chars ?? sel.compiled_prompt_chars ?? this.draftContent.length;
      const dirtyBadge = el('span', { className: 'badge badge-amber', text: 'Unsaved Changes', hidden: !this.dirty });
      const metaBadges = el(
        'div',
        { className: 'pipeline-pills' },
        el('span', { className: 'badge badge-cyan mono', text: sel.source_path || sel._nameParam }),
        el('span', { className: 'badge mono', text: `Compiled: ${formatNumber(compiledChars, 0)} chars` }),
        sel.model ? el('span', { className: 'badge badge-green mono', text: `Model: ${sel.model}` }) : null,
        sel.temperature !== undefined ? el('span', { className: 'badge mono', text: `Temp: ${sel.temperature}` }) : null,
        dirtyBadge,
      );

      const modeToggle = el(
        'div',
        { className: 'chart-controls' },
        el('button', {
          type: 'button',
          className: `btn btn-xs ${this.viewMode === 'preview' ? 'btn-cyan' : 'btn-ghost'}`,
          text: 'Preview',
          onClick: () => {
            this.viewMode = 'preview';
            this._render();
          },
        }),
        el('button', {
          type: 'button',
          className: `btn btn-xs ${this.viewMode === 'edit' ? 'btn-cyan' : 'btn-ghost'}`,
          text: 'Edit Markdown',
          onClick: () => {
            this.viewMode = 'edit';
            this._render();
          },
        }),
      );

      detailEl.append(
        el(
          'div',
          { className: 'waterfall-header' },
          el('strong', { className: 'mono', text: sel.name || sel._nameParam }),
          modeToggle,
        ),
        metaBadges,
      );

      if (this.viewMode === 'preview') {
        const previewBox = el('div', { className: 'decl-preview-box' });
        renderSafeMarkdown(previewBox, this.draftContent);
        detailEl.appendChild(previewBox);
      } else {
        const charCountNode = el('span', {
          className: 'panel-subtitle mono',
          text: `${this.draftContent.length} chars · ${this.draftContent.split('\n').length} lines`,
        });

        const revertBtn = el('button', {
          type: 'button',
          className: 'btn btn-sm btn-ghost',
          disabled: !this.dirty || this.busy,
          text: 'Revert',
          onClick: () => {
            this.draftContent = this._extractContent(sel);
            this.dirty = false;
            this._render();
          },
        });

        const editor = el('textarea', {
          className: 'textarea mono decl-editor-area',
          value: this.draftContent,
          onInput: (e) => {
            this.draftContent = e.target.value;
            this.dirty = true;
            dirtyBadge.hidden = false;
            revertBtn.disabled = this.busy;
            charCountNode.textContent = `${this.draftContent.length} chars · ${this.draftContent.split('\n').length} lines`;
          },
        });

        const saveBar = el(
          'div',
          { className: 'tc-row-top' },
          charCountNode,
          el(
            'div',
            { className: 'chart-controls' },
            revertBtn,
            el('button', {
              type: 'button',
              className: 'btn btn-sm btn-green',
              disabled: this.busy,
              text: 'Save & Apply',
              onClick: () => this._handleSave(),
            }),
          ),
        );

        detailEl.append(editor, saveBar);
      }
    }

    this.root.append(headerBar, el('div', { className: 'decl-layout' }, listEl, detailEl));
  }
}
