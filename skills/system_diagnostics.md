---
name: system_diagnostics
description: Host hardware telemetry execution and traffic-light Status Ticker rules (compact GREEN vs auto-expanded RED).
version: 1.0.0
---

# System Diagnostics & Traffic-Light Status Ticker Skill

## 1. Whitelisted Host Diagnostics
The daemon executes read-only Linux and GPU diagnostic commands (`nvidia-smi`, `systemctl is-active nvda-copilot.service ollama.service`, `uptime`, `free -h`, `df -h /`, `journalctl`, `ollama ps`) to provide real-time hardware observability.

## 2. Traffic-Light Status Ticker Rules
- **Compact `🟢 GREEN` Mode:** When hardware is healthy (`GPU < 85°C`, `RAM < 92%`, `Disk < 92%`, `ollama.service == active`, Apps Script webhook `OK`) and no portfolio risk/trade trigger is active, output ONLY the 1-line status ticker header:
  `🟢 [TICKER $PRICE | Stop STOP | PnL PNL SEK | GPU TEMP°C | SYS:OK]`
- **Auto-Expanded `🔴 RED` Mode:** Expand with full hardware diagnostics, holdings, and alert bullets ONLY when at least one red condition is active (stop-loss breach or proximity within `0.35 * ATR14`, active `BUY_*` or `SELL_*` trade action, daily loss lockout `<= -900 SEK`, active stop-out cooldown, or hardware/service degradation).
