# Gemina — Personal Multi-Agent Framework & Umbrella Repository

**Gemina** is a modular, production-grade personal AI agent ecosystem built around Clean/Hexagonal Architecture, multi-level focused agentic loops, deterministic policy guardrails, and end-to-end message lifecycle observability.

`traderagent` is the first domain subproject under `gemina`, engineered for quantitative multi-ticker trading on an Avanza ISK portfolio with primary cloud reasoning (`gemini-4-argon` + Google Search grounding) and local 16GB VRAM GPU hardware failover (`qwen2.5-coder:14b` via Ollama).

---

## Repository Structure

```text
gemina/                                    # Top-Level Umbrella Repository
├── README.md                              # Umbrella Overview & Quickstart
├── pyproject.toml                         # Core `gemina` package manifest
├── docs/
│   ├── ARCHITECTURE.md                    # Clean Architecture, 4-Level Agent Loop & Layer Guide
│   ├── STRATEGIES_AND_BENCHMARKS.md       # 6 Stock Strategies Capability Analysis & 21-Scenario NVDA Sensitivity Data
│   └── TRADINGVIEW_SETUP.md               # Pine Script v6 & Google Apps Script Setup Guide
├── gemina/                                # Shared Core Agentic Framework (`gemina.core`)
│   ├── __init__.py
│   └── core/
│       ├── interfaces.py                  # SubAgentPlugin contract & AgentRegistry
│       ├── tracing.py                     # End-to-End PipelineTracer (Arrival -> Transforms -> Leaving)
│       ├── llm_router.py                  # Cloud Gemini 4 Argon + Search -> Local 16GB Ollama Failover
│       └── diagnostics.py                 # Whitelisted Linux/GPU Command Executor
└── subprojects/
    └── traderagent/                       # Standalone & Importable Sub-Agent (`gemina-traderagent`)
        ├── README.md                      # TraderAgent Package Documentation
        ├── pyproject.toml                 # Installable package (`traderagent-daemon`, `traderagent-trace`)
        ├── .env.example                   # Environment credentials template
        ├── data/                          # Live state & append-only audit/trace logs
        │   ├── portfolio.json
        │   ├── risk_profile.json
        │   ├── trade_audit.jsonl
        │   └── pipeline_trace.jsonl
        ├── integrations/
        │   ├── tradingview_indicator.pine # Pine Script v6 15m Telemetry Indicator
        │   └── google_apps_script.gs      # Multi-ticker PropertiesService webhook buffer
        ├── traderagent/                   # Clean Architecture Python Package
        │   ├── config.py                  # Environment & path configuration
        │   ├── plugin.py                  # Gemina SubAgentPlugin implementation
        │   ├── domain/                    # Pure quant math, Pydantic schemas & L4 circuit breakers
        │   ├── agents/                    # 4-Level Focused Agentic Committee & Conversational Q&A
        │   ├── infrastructure/            # Atomic persistence, Apps Script poller, Telegram & Status Ticker
        │   ├── application/               # Command router & main daemon loop
        │   └── cli/                       # Arrival-to-leaving trace waterfall visualizer
        └── tests/
            └── test_traderagent.py        # Automated test suite
```

---

## Importing `traderagent` into `gemina` (or Any External Agent Repo)

Both `gemina` and `traderagent` are installable Python packages (`pip install -e . -e subprojects/traderagent`). Any top-level orchestrator can mount `traderagent` via its `SubAgentPlugin` interface:

```python
from gemina import AgentRegistry
from traderagent import create_trader_agent

# 1. Mount traderagent into the Gemina Personal Agent Registry
registry = AgentRegistry()
registry.register(create_trader_agent())

# 2. Query health or dispatch commands/questions through the unified plugin interface
trader = registry.get("traderagent")
print(trader.health_status()["ticker_line"])
print(trader.handle_message("/status"))
print(trader.handle_message("Any post-day trade permitted under our strategy rules?"))
```

---

## Quickstart & Operations

```bash
# Run automated architecture & guardrail verification suite
/home/bb/nvda-quant-copilot/.venv/bin/python -m unittest discover -s /home/bb/gemina/subprojects/traderagent/tests

# View live arrival-to-leaving pipeline trace waterfalls
/home/bb/nvda-quant-copilot/.venv/bin/traderagent-trace -n 5
/home/bb/nvda-quant-copilot/.venv/bin/traderagent-trace -f

# Check systemd daemon status
sudo systemctl status nvda-copilot.service
journalctl -u nvda-copilot.service -f
```
