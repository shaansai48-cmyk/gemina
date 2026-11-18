# TraderAgent (`gemina-traderagent`)

**TraderAgent** is a modular quantitative trading subproject of the **Gemina** Personal Agent Framework. It manages an Avanza ISK multi-ticker portfolio (`NVDA`, `ERIC B`, `MSFT`, etc.) using a 4-level focused agentic committee (`gemini-4-argon` + Google Search grounding with 16GB VRAM `qwen2.5-coder:14b` Ollama failover) and deterministic Python circuit breakers.

---

## Layered Package Layout

* `traderagent.domain`:
  * `models.py` — Strict Pydantic schemas (`TradeDecisionDraft`, `AdversarialCritique`, `TradeDecision`).
  * `quant_math.py` — Avanza Mini Future theoretical SEK pricing, effective leverage, FX drag, and Level-3a deterministic fee-adjusted R:R verification.
  * `guardrails.py` — Level-4 immutable Python circuit breakers enforcing `data/risk_profile.json`.
* `traderagent.agents`:
  * `committee.py` — 4-Level Focused Agentic Loop (`L1 Catalyst` $\rightarrow$ `L2 Alpha Strategist` $\rightarrow$ `L3a Python Math Auditor` + `L3b Adversarial Red-Team Critic` $\rightarrow$ `L4 Guardrails`).
  * `qa_assistant.py` — General & portfolio-aware conversational AI assistant with live Linux/GPU diagnostic command context.
* `traderagent.infrastructure`:
  * `persistence.py` — Crash-safe atomic JSON state repository + automatic midnight CET rollover.
  * `apps_script_client.py` — Multi-ticker Google Apps Script webhook poller.
  * `system_status.py` — Traffic-light status ticker evaluator (`🟢 GREEN` 1-line compact, auto-expanding with full details only when `🔴 RED`).
  * `telegram_adapter.py` — Telegram Bot API gateway with 4,000-char chunking, Markdown-to-plain fallback, and auto-binding `TELEGRAM_CHAT_ID`.
* `traderagent.application`:
  * `command_router.py` — Dispatches all Telegram slash commands (`/status`, `/fullstatus`, `/trace`, `/watchlist`, `/focus`, `/add_ticker`, `/fill_mini`, `/close_mini`, `/fill_shares`, `/close_shares`, `/set_stop`, `/set_mini`, `/analyze`, `/ask`, `/clear`, `/help`) and free-form Q&A.
  * `daemon.py` — Runtime loop & CLI entrypoint (`traderagent-daemon`).
* `traderagent.plugin`:
  * `TraderAgentPlugin` & `create_trader_agent()` — Implements `gemina.core.interfaces.SubAgentPlugin` so `traderagent` can be imported directly into `gemina`.
