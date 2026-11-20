/**
 * Pure-DOM Constructors, Safe Markdown Renderer, and Tabular Quant Formatters.
 * Strictly avoids raw HTML string sinks to uphold strict CSP (script-src 'self').
 */

const SECRET_KEYS = new Set([
  'secret',
  'api_key',
  'apikey',
  'token',
  'password',
  'authorization',
  'ui_token',
  'gemina_ui_token',
]);

const BOOLEAN_PROPS = new Set([
  'disabled',
  'checked',
  'selected',
  'readOnly',
  'required',
  'hidden',
  'multiple',
  'open',
]);

/**
 * Creates a DOM element with safe attribute/event/child binding.
 * Event listeners must be passed as `onClick`, `onInput`, etc.
 */
export function el(tag, attrs = null, ...children) {
  const node = document.createElement(tag);
  if (attrs && typeof attrs === 'object') {
    for (const [key, val] of Object.entries(attrs)) {
      if (val === undefined || val === null) {
        continue;
      }
      if (key === 'className' || key === 'class') {
        node.className = String(val);
      } else if (key === 'text' || key === 'textContent') {
        node.textContent = String(val);
      } else if (key === 'htmlFor' || key === 'for') {
        node.setAttribute('for', String(val));
      } else if (key === 'value') {
        node.value = String(val);
      } else if (key === 'dataset' && typeof val === 'object') {
        for (const [dKey, dVal] of Object.entries(val)) {
          if (dVal !== undefined && dVal !== null) {
            node.dataset[dKey] = String(dVal);
          }
        }
      } else if (key.startsWith('on') && typeof val === 'function') {
        const eventName = key.slice(2).toLowerCase();
        node.addEventListener(eventName, val);
      } else if (BOOLEAN_PROPS.has(key)) {
        node[key] = Boolean(val);
      } else if (key === 'style') {
        // Ignore inline style strings to honor strict style-src 'self' CSP
        continue;
      } else if (key === 'tabIndex') {
        node.tabIndex = Number(val);
      } else if (/^aria[A-Z]/.test(key)) {
        const ariaAttr = `aria-${key.slice(4).toLowerCase()}`;
        node.setAttribute(ariaAttr, String(val));
      } else if (!key.toLowerCase().startsWith('on')) {
        node.setAttribute(key, String(val));
      }
    }
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(parent, items) {
  for (const item of items) {
    if (item === null || item === undefined || item === false) {
      continue;
    }
    if (Array.isArray(item)) {
      appendChildren(parent, item);
    } else if (item instanceof Node) {
      parent.appendChild(item);
    } else {
      parent.appendChild(document.createTextNode(String(item)));
    }
  }
}

/** Safely clears all child nodes from a container element. */
export function clearEl(node) {
  if (!node) return;
  node.replaceChildren();
}

/** Sets textContent safely on an element. */
export function setText(node, value) {
  if (!node) return;
  node.textContent = value === undefined || value === null ? '' : String(value);
}

function sanitizeInlineUrl(rawUrl) {
  const trimmed = String(rawUrl || '').trim();
  if (!trimmed) return null;
  if (trimmed.startsWith('/')) return trimmed;
  try {
    const parsed = new URL(trimmed, window.location.origin);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return parsed.href;
    }
  } catch {
    return null;
  }
  return null;
}

/**
 * Parses inline markdown tokens into safe DOM nodes (strong, em, code, safe links).
 */
function parseInlineNodes(rawText) {
  const text = String(rawText || '').replace(/\$\\rightarrow\$|\\rightarrow/g, '→');
  const nodes = [];
  const tokenRe = /(`[^`]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|_[^_\n]+_|\[[^\]\n]+\]\([^)\n]+\))/g;
  let lastIdx = 0;
  let match;

  while ((match = tokenRe.exec(text)) !== null) {
    if (match.index > lastIdx) {
      nodes.push(document.createTextNode(text.slice(lastIdx, match.index)));
    }
    const tok = match[0];
    if (tok.startsWith('`') && tok.endsWith('`')) {
      nodes.push(el('code', { className: 'mono', text: tok.slice(1, -1) }));
    } else if (tok.startsWith('**') && tok.endsWith('**')) {
      nodes.push(el('strong', null, parseInlineNodes(tok.slice(2, -2))));
    } else if (tok.startsWith('*') && tok.endsWith('*') && tok.length > 2) {
      nodes.push(el('strong', null, parseInlineNodes(tok.slice(1, -1))));
    } else if (tok.startsWith('_') && tok.endsWith('_') && tok.length > 2) {
      nodes.push(el('em', null, parseInlineNodes(tok.slice(1, -1))));
    } else if (tok.startsWith('[')) {
      const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      if (linkMatch) {
        const safeHref = sanitizeInlineUrl(linkMatch[2]);
        if (safeHref) {
          nodes.push(
            el('a', {
              href: safeHref,
              target: '_blank',
              rel: 'noopener noreferrer',
              className: 'txt-cyan',
              text: linkMatch[1],
            }),
          );
        } else {
          nodes.push(document.createTextNode(linkMatch[1]));
        }
      } else {
        nodes.push(document.createTextNode(tok));
      }
    } else {
      nodes.push(document.createTextNode(tok));
    }
    lastIdx = tokenRe.lastIndex;
  }

  if (lastIdx < text.length) {
    nodes.push(document.createTextNode(text.slice(lastIdx)));
  }
  return nodes;
}

/**
 * Pure-DOM Markdown renderer producing <p>, <ul>/<li>, <ol>/<li>,
 * <h1>-<h4>, <blockquote>, <strong>, <em>, <code>, <pre>, and <hr>.
 */
export function renderSafeMarkdown(container, mdText) {
  if (!container) return;
  clearEl(container);
  container.classList.add('md-body');

  const lines = String(mdText || '').replace(/\r\n/g, '\n').split('\n');
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    // Fenced code block
    if (trimmed.startsWith('```')) {
      const codeLines = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      container.appendChild(
        el('pre', null, el('code', { className: 'mono', text: codeLines.join('\n') })),
      );
      continue;
    }

    // Horizontal rule
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      container.appendChild(el('hr'));
      i += 1;
      continue;
    }

    // Heading
    const headingMatch = /^(#{1,4})\s+(.+)$/.exec(trimmed);
    if (headingMatch) {
      const level = Math.min(4, headingMatch[1].length);
      container.appendChild(el(`h${level}`, null, parseInlineNodes(headingMatch[2])));
      i += 1;
      continue;
    }

    // Blockquote
    if (trimmed.startsWith('>')) {
      const quoteLines = [];
      while (i < lines.length && lines[i].trim().startsWith('>')) {
        quoteLines.push(lines[i].trim().replace(/^>\s?/, ''));
        i += 1;
      }
      container.appendChild(el('blockquote', null, parseInlineNodes(quoteLines.join(' '))));
      continue;
    }

    // Unordered list (-, *, or Telegram bullet •)
    if (/^[-*•]\s+/.test(trimmed)) {
      const ul = el('ul');
      while (i < lines.length && /^[-*•]\s+/.test(lines[i].trim())) {
        const itemText = lines[i].trim().replace(/^[-*•]\s+/, '');
        ul.appendChild(el('li', null, parseInlineNodes(itemText)));
        i += 1;
      }
      container.appendChild(ul);
      continue;
    }

    // Ordered list
    if (/^\d+\.\s+/.test(trimmed)) {
      const ol = el('ol');
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        const itemText = lines[i].trim().replace(/^\d+\.\s+/, '');
        ol.appendChild(el('li', null, parseInlineNodes(itemText)));
        i += 1;
      }
      container.appendChild(ol);
      continue;
    }

    // Paragraph (coalesce consecutive non-block lines)
    const paraLines = [];
    while (i < lines.length) {
      const cur = lines[i].trim();
      if (
        !cur ||
        cur.startsWith('```') ||
        cur.startsWith('>') ||
        /^#{1,4}\s+/.test(cur) ||
        /^[-*•]\s+/.test(cur) ||
        /^\d+\.\s+/.test(cur) ||
        /^(-{3,}|\*{3,}|_{3,})$/.test(cur)
      ) {
        break;
      }
      paraLines.push(cur);
      i += 1;
    }
    container.appendChild(el('p', null, parseInlineNodes(paraLines.join(' '))));
  }
}

/** Recursively redacts credential-like keys before rendering JSON in the UI. */
export function redactForDisplay(value) {
  if (Array.isArray(value)) {
    return value.map(redactForDisplay);
  }
  if (value && typeof value === 'object') {
    const clean = {};
    for (const [k, v] of Object.entries(value)) {
      if (SECRET_KEYS.has(k.toLowerCase())) {
        clean[k] = '[REDACTED]';
      } else {
        clean[k] = redactForDisplay(v);
      }
    }
    return clean;
  }
  return value;
}

export function formatJsonPretty(value) {
  if (value === undefined || value === null) return '-';
  try {
    return JSON.stringify(redactForDisplay(value), null, 2);
  } catch {
    return String(value);
  }
}

export function formatNumber(val, decimals = 2) {
  const num = Number(val);
  if (!Number.isFinite(num)) return '-';
  return num.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function formatPrice(val, currency = 'USD', decimals = 2) {
  const num = Number(val);
  if (!Number.isFinite(num) || num === 0) return '-';
  const formatted = formatNumber(num, decimals);
  return currency === 'SEK' ? `${formatted} SEK` : `$${formatted}`;
}

export function formatSek(val, showSign = false, decimals = 0) {
  const num = Number(val);
  if (!Number.isFinite(num)) return '-';
  const absFormatted = Math.abs(num).toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  const sign = num < 0 ? '-' : showSign && num > 0 ? '+' : '';
  return `${sign}${absFormatted} SEK`;
}

export function formatPct(val, showSign = false, decimals = 1) {
  const num = Number(val);
  if (!Number.isFinite(num)) return '-';
  const sign = num > 0 && showSign ? '+' : '';
  return `${sign}${num.toFixed(decimals)}%`;
}

export function formatDurationMs(ms) {
  const num = Number(ms);
  if (!Number.isFinite(num)) return '-';
  if (num >= 1000) return `${(num / 1000).toFixed(2)} s`;
  if (num >= 10) return `${num.toFixed(1)} ms`;
  return `${num.toFixed(2)} ms`;
}

export function formatCetTime(input, withDate = false) {
  if (!input) return '-';
  const date = input instanceof Date ? input : new Date(input);
  if (Number.isNaN(date.getTime())) return String(input);
  const opts = {
    timeZone: 'Europe/Stockholm',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  };
  if (withDate) {
    opts.month = '2-digit';
    opts.day = '2-digit';
  }
  return new Intl.DateTimeFormat('sv-SE', opts).format(date);
}

/** Computes current Stockholm CET wall clock components and Avanza/US trading session. */
export function getStockholmMarketSession(now = new Date()) {
  const fmt = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/Stockholm',
    weekday: 'short',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
  const parts = Object.fromEntries(fmt.formatToParts(now).map((p) => [p.type, p.value]));
  const weekday = parts.weekday || 'Mon';
  const hour = Number(parts.hour || 0);
  const minute = Number(parts.minute || 0);
  const mins = hour * 60 + minute;
  const clock = `${parts.hour}:${parts.minute}:${parts.second} CET`;
  const isWeekend = weekday === 'Sat' || weekday === 'Sun';

  if (isWeekend) {
    return { clock, label: 'Closed', detail: 'Weekend', tone: 'muted' };
  }
  // 15:30 - 15:45 CET: US Open Blackout
  if (mins >= 15 * 60 + 30 && mins < 15 * 60 + 45) {
    return { clock, label: 'Pre-Open', detail: 'US Open Blackout (15:30–15:45)', tone: 'amber' };
  }
  // 15:45 - 20:30 CET: US + Nordic ETP Open
  if (mins >= 15 * 60 + 45 && mins < 20 * 60 + 30) {
    return { clock, label: 'US Open', detail: 'US + Avanza ETP Active', tone: 'green' };
  }
  // 20:30 - 22:00 CET: Past Nordic ETP Cutoff while US Cash Open
  if (mins >= 20 * 60 + 30 && mins < 22 * 60) {
    return { clock, label: 'Nordic Cutoff', detail: 'Past 20:30 CET ETP Cutoff', tone: 'amber' };
  }
  // 09:00 - 15:30 CET: Nordic Open / US Pre-Open
  if (mins >= 9 * 60 && mins < 15 * 60 + 30) {
    return { clock, label: 'Pre-Open', detail: 'OMX Open / US Pre-Market', tone: 'cyan' };
  }
  return { clock, label: 'Closed', detail: 'After Hours', tone: 'muted' };
}
