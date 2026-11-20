---
name: system-diagnostics
description: "Use when checking host GPU, CPU, RAM, or disk health, summarizing systemd service status, or formatting the compact GREEN vs auto-expanded RED Telegram Status Ticker."
version: "1.0.0"
---

# System Diagnostics & Traffic-Light Status Ticker

## Workflow

1. **Execute whitelisted read-only host diagnostics.**
   - Run `nvidia-smi`, `systemctl is-active nvda-copilot.service ollama.service`, `uptime`, `free -h`, `df -h /`, `journalctl`, and `ollama ps` to capture live workstation health.
   - **Done when:** GPU temperature, VRAM usage, RAM/disk percentages, and `ollama.service` state are collected.

2. **Evaluate traffic-light status (`🟢 GREEN` vs `🔴 RED`).**
   - **Compact `🟢 GREEN` Mode:** If hardware is healthy (`GPU < 85°C`, `RAM < 92%`, `Disk < 92%`, `ollama.service == active`, Apps Script webhook `OK`) and no risk/trade trigger is active, the daemon prepends only the 1-line status ticker:
     `🟢 [TICKER $PRICE | Stop STOP | PnL PNL SEK | GPU TEMP°C | SYS:OK]`
   - **Auto-Expanded `🔴 RED` Mode:** Expand with full hardware metrics, holdings, and alert bullets **only** when a stop breach/proximity (`<= 0.35 * ATR14`), active `BUY_*`/`SELL_*` signal, daily loss lockout, cooldown, or host/service fault occurs.
   - **Done when:** the outbound message header matches the evaluated `🟢` or `🔴` state without duplicating the ticker line in the body.

3. **Keep diagnostic summaries plain-English and mobile-friendly.**
   - Lead with the bottom-line system state (`Healthy` vs specific issue), list 2–3 concise bullets, never emit raw LaTeX or internal pipeline tokens, and never repeat the status ticker line in body prose.
   - **Done when:** the user gets an immediate, readable summary with zero clutter.
