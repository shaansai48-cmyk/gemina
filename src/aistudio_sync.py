#!/usr/bin/env python3
"""
Google AI Studio Bundle Exporter (`src/aistudio_sync.py` / `gemina-aistudio`).
Exports compiled system instructions, strict JSON schemas, and real 15m bar test cases
into `/home/bb/gemina/aistudio_bundle/` so you can upload or paste them directly into
Google AI Studio (`https://aistudio.google.com`).
"""
import json
from pathlib import Path
from src.loader import REPO_ROOT
from traderagent.agents.prompts import (
    build_chat_assistant_prompt,
    build_l2_strategist_prompt,
    build_l3_critic_prompt,
)
from traderagent.config import TraderConfig
from traderagent.domain.models import AdversarialCritique, TradeDecision, TradeDecisionDraft
from traderagent.domain.quant_math import compute_quant_metrics
from traderagent.infrastructure.persistence import StateRepository

BUNDLE_DIR = REPO_ROOT / "aistudio_bundle"


def export_aistudio_bundle() -> Path:
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Export compiled System Instructions (Agent + Skills combined)
    main_prompt, _ = build_chat_assistant_prompt()
    l2_prompt, _ = build_l2_strategist_prompt()
    l3_prompt, _ = build_l3_critic_prompt()

    (BUNDLE_DIR / "1_Main_Agent_System_Instruction.md").write_text(main_prompt, encoding="utf-8")
    (BUNDLE_DIR / "2_L2_Strategist_System_Instruction.md").write_text(l2_prompt, encoding="utf-8")
    (BUNDLE_DIR / "3_L3_Adversarial_Critic_System_Instruction.md").write_text(l3_prompt, encoding="utf-8")

    # 2. Export Strict JSON Schemas for Google AI Studio "Structured Outputs"
    (BUNDLE_DIR / "schema_TradeDecisionDraft.json").write_text(
        json.dumps(TradeDecisionDraft.model_json_schema(), indent=2), encoding="utf-8"
    )
    (BUNDLE_DIR / "schema_AdversarialCritique.json").write_text(
        json.dumps(AdversarialCritique.model_json_schema(), indent=2), encoding="utf-8"
    )
    (BUNDLE_DIR / "schema_TradeDecision.json").write_text(
        json.dumps(TradeDecision.model_json_schema(), indent=2), encoding="utf-8"
    )

    # 3. Export real 15m bar test payloads ready to paste into AI Studio User Prompt
    cfg = TraderConfig.from_env()
    repo = StateRepository(cfg)
    portfolio = repo.load_portfolio()
    risk = repo.load_risk()
    recent = repo.get_recent_audit_tail(5)

    sample_telem = (
        recent[-1]["telemetry"]
        if recent
        else {
            "ticker": "NVDA",
            "time": "2026-11-18T22:45:00Z",
            "close": 150.85,
            "low": 150.10,
            "high": 151.40,
            "ema4": 151.20,
            "vwap": 152.00,
            "or_high": 154.50,
            "or_low": 150.00,
            "pd_low": 149.80,
            "atr14": 2.40,
            "rsi14": 38.5,
        }
    )
    ticker = sample_telem.get("ticker", "NVDA")
    asset = portfolio["watchlist"].get(ticker, portfolio["watchlist"]["NVDA"])
    metrics = compute_quant_metrics(sample_telem, asset)

    user_prompt_payload = {
        "active_ticker": ticker,
        "telemetry_15m": sample_telem,
        "computed_metrics": metrics,
        "asset_state": asset,
        "daily_stats": portfolio["daily_stats"],
        "available_cash_sek": portfolio["cash_reserves"]["available_tactical_mini_sek"],
        "immutable_risk_limits": risk,
    }
    (BUNDLE_DIR / "sample_15m_bar_user_prompt.json").write_text(
        json.dumps(user_prompt_payload, indent=2), encoding="utf-8"
    )

    print(f"✅ Exported Google AI Studio bundle to: {BUNDLE_DIR}")
    for p in sorted(BUNDLE_DIR.iterdir()):
        print(f"   • {p.name} ({p.stat().st_size} bytes)")
    return BUNDLE_DIR


def main() -> None:
    export_aistudio_bundle()


if __name__ == "__main__":
    main()
