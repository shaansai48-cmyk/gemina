/**
 * REST & Streaming SSE API Client for Gemina Quant Command Center.
 * Tokens are stored exclusively in sessionStorage and sent via Authorization Bearer headers.
 */

const TOKEN_STORAGE_KEY = 'gemina_ui_token';

export class ApiError extends Error {
  constructor(message, status = 0, code = 'API_ERROR', payload = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

export function getToken() {
  try {
    return sessionStorage.getItem(TOKEN_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

export function setToken(token) {
  try {
    const clean = String(token || '').trim();
    if (clean) {
      sessionStorage.setItem(TOKEN_STORAGE_KEY, clean);
    } else {
      sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    // Ignore storage unavailability
  }
}

export function clearToken() {
  try {
    sessionStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // Ignore storage unavailability
  }
}

let authRequiredHandler = null;

export function onAuthRequired(fn) {
  authRequiredHandler = typeof fn === 'function' ? fn : null;
}

function buildHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  return headers;
}

async function parseErrorPayload(res) {
  try {
    return await res.json();
  } catch {
    try {
      const txt = await res.text();
      return { message: txt || res.statusText };
    } catch {
      return { message: res.statusText };
    }
  }
}

export async function requestJson(path, options = {}) {
  const { method = 'GET', body = undefined, signal = undefined, suppressAuthPrompt = false } = options;
  const headers = buildHeaders({ Accept: 'application/json' });
  let encodedBody = undefined;

  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    encodedBody = JSON.stringify(body);
  }

  const res = await fetch(path, {
    method,
    headers,
    body: encodedBody,
    signal,
    credentials: 'same-origin',
  });

  if (res.status === 401) {
    const payload = await parseErrorPayload(res);
    const errObj = typeof payload?.error === 'object' && payload.error ? payload.error : null;
    if (!suppressAuthPrompt && authRequiredHandler) {
      authRequiredHandler(payload);
    }
    throw new ApiError(
      errObj?.message || payload?.detail || payload?.message || 'Authentication required',
      401,
      errObj?.code || (typeof payload?.error === 'string' ? payload.error : '') || payload?.code || 'AUTH_REQUIRED',
      payload,
    );
  }

  if (!res.ok) {
    const payload = await parseErrorPayload(res);
    const errObj = typeof payload?.error === 'object' && payload.error ? payload.error : null;
    const msg =
      errObj?.message ||
      (typeof payload?.detail === 'string' && payload.detail) ||
      payload?.message ||
      (typeof payload?.error === 'string' && payload.error) ||
      `HTTP ${res.status}`;
    throw new ApiError(
      msg,
      res.status,
      errObj?.code || (typeof payload?.error === 'string' ? payload.error : '') || payload?.code || 'HTTP_ERROR',
      payload,
    );
  }

  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  getPortfolio: (opts) => requestJson('/api/portfolio', opts),

  getCalculator: (params, signal) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) {
      if (v !== undefined && v !== null && v !== '') {
        qs.set(k, String(v));
      }
    }
    return requestJson(`/api/portfolio/calculator?${qs.toString()}`, { signal });
  },

  setFocus: (tickers) => requestJson('/api/portfolio/focus', { method: 'POST', body: { tickers } }),
  setStop: (payload) => requestJson('/api/portfolio/stop', { method: 'POST', body: payload }),
  fillMini: (payload) => requestJson('/api/portfolio/fill-mini', { method: 'POST', body: payload }),
  closeMini: (payload) => requestJson('/api/portfolio/close-mini', { method: 'POST', body: payload }),
  fillShares: (payload) => requestJson('/api/portfolio/fill-shares', { method: 'POST', body: payload }),
  closeShares: (payload) => requestJson('/api/portfolio/close-shares', { method: 'POST', body: payload }),
  setMiniParams: (payload) => requestJson('/api/portfolio/mini-params', { method: 'POST', body: payload }),
  addTicker: (payload) => requestJson('/api/portfolio/add-ticker', { method: 'POST', body: payload }),

  getChart: (ticker, range = '5d', interval = '15m', signal) => {
    const qs = new URLSearchParams({ range, interval });
    return requestJson(`/api/chart/${encodeURIComponent(ticker)}?${qs.toString()}`, { signal });
  },

  getBenchmarks: () => requestJson('/api/benchmarks'),

  sendChat: (message, mode = 'ask') =>
    requestJson('/api/chat', {
      method: 'POST',
      body: {
        message,
        persona: mode === 'quant' || mode === 'subagent' ? 'subagent' : 'main',
      },
    }),

  clearChat: () => requestJson('/api/chat/clear', { method: 'POST', body: {} }),

  analyzeTicker: (ticker, depth = 'quick') =>
    requestJson('/api/analyze', {
      method: 'POST',
      body: {
        ticker,
        mode: depth === 'committee' || depth === 'deep' ? 'committee' : 'brief',
      },
    }),

  getTraces: (limit = 30) => requestJson(`/api/traces?limit=${encodeURIComponent(limit)}`),
  getTraceDetail: (traceId) => requestJson(`/api/traces/${encodeURIComponent(traceId)}`),

  getHealth: () => requestJson('/api/health'),

  getDeclarative: () => requestJson('/api/declarative'),
  reloadDeclarative: () => requestJson('/api/declarative/reload', { method: 'POST', body: {} }),
  updateDeclarative: (kind, name, content) =>
    requestJson(`/api/declarative/${encodeURIComponent(kind)}/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: { content },
    }),
};

/**
 * Fetch + ReadableStream SSE Client with Last-Event-ID support and exponential backoff reconnect.
 * Never passes Bearer tokens in URL query strings.
 */
export class SseStreamClient {
  constructor({ onEvent, onStatusChange, onAuthError }) {
    this.onEvent = onEvent;
    this.onStatusChange = onStatusChange;
    this.onAuthError = onAuthError;
    this.lastEventId = '';
    this.abortController = null;
    this.reconnectTimer = null;
    this.stopped = true;
    this.attempt = 0;
  }

  start() {
    this.stopped = false;
    this.attempt = 0;
    this._clearReconnect();
    this._connect();
  }

  stop() {
    this.stopped = true;
    this._clearReconnect();
    if (this.abortController) {
      this.abortController.abort();
      this.abortController = null;
    }
    this.onStatusChange?.('offline');
  }

  restart() {
    this.stop();
    this.start();
  }

  _clearReconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  _scheduleReconnect() {
    if (this.stopped) return;
    this._clearReconnect();
    const baseMs = Math.min(15000, 1000 * 2 ** Math.min(this.attempt, 4));
    const jitterMs = Math.floor(Math.random() * 400);
    this.attempt += 1;
    this.onStatusChange?.('reconnecting');
    this.reconnectTimer = setTimeout(() => {
      if (!this.stopped) this._connect();
    }, baseMs + jitterMs);
  }

  async _connect() {
    if (this.stopped) return;
    if (this.abortController) {
      this.abortController.abort();
    }
    const controller = new AbortController();
    this.abortController = controller;
    this.onStatusChange?.(this.attempt === 0 ? 'connecting' : 'reconnecting');

    const headers = buildHeaders({
      Accept: 'text/event-stream',
      'Cache-Control': 'no-cache',
    });
    if (this.lastEventId) {
      headers['Last-Event-ID'] = this.lastEventId;
    }

    try {
      const response = await fetch('/api/stream', {
        method: 'GET',
        headers,
        signal: controller.signal,
        credentials: 'same-origin',
      });

      if (response.status === 401) {
        const errPayload = await parseErrorPayload(response);
        this.stopped = true;
        this.onStatusChange?.('auth_required');
        this.onAuthError?.(errPayload);
        return;
      }

      if (!response.ok || !response.body) {
        this._scheduleReconnect();
        return;
      }

      this.attempt = 0;
      this.onStatusChange?.('live');
      await this._consumeStream(response.body, controller.signal);

      if (!this.stopped && !controller.signal.aborted) {
        this._scheduleReconnect();
      }
    } catch (err) {
      if (err?.name === 'AbortError' || this.stopped) return;
      this._scheduleReconnect();
    }
  }

  async _consumeStream(readableStream, signal) {
    const reader = readableStream.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let eventName = 'message';
    let dataLines = [];
    let eventId = '';

    const dispatchEvent = () => {
      if (dataLines.length === 0 && !eventName) return;
      const rawData = dataLines.join('\n');
      const finalEvent = eventName || 'message';
      if (eventId) {
        this.lastEventId = eventId;
      }
      dataLines = [];
      eventName = 'message';
      eventId = '';

      if (!rawData) return;
      let parsed = rawData;
      try {
        parsed = JSON.parse(rawData);
      } catch {
        // Keep plain string if non-JSON payload
      }
      this.onEvent?.(finalEvent, parsed, this.lastEventId);
    };

    try {
      while (!signal.aborted && !this.stopped) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let nlIdx;
        while ((nlIdx = buffer.search(/\r\n|\n|\r/)) !== -1) {
          const line = buffer.slice(0, nlIdx);
          const sepLen = buffer.slice(nlIdx, nlIdx + 2) === '\r\n' ? 2 : 1;
          buffer = buffer.slice(nlIdx + sepLen);

          if (line === '') {
            dispatchEvent();
            continue;
          }
          if (line.startsWith(':')) {
            // Heartbeat comment
            continue;
          }
          const colonIdx = line.indexOf(':');
          const field = colonIdx === -1 ? line : line.slice(0, colonIdx);
          let val = colonIdx === -1 ? '' : line.slice(colonIdx + 1);
          if (val.startsWith(' ')) val = val.slice(1);

          if (field === 'event') {
            eventName = val || 'message';
          } else if (field === 'data') {
            dataLines.push(val);
          } else if (field === 'id' && !val.includes('\0')) {
            eventId = val;
          }
        }
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // Ignore release errors
      }
    }
  }
}
