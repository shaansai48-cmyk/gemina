---
name: gemini_interaction
description: Standards for structuring prompts, Google Search grounding, schema validation, and local GPU failover when querying the Gemini API.
version: 1.0.0
---

# Gemini API Interaction Skill

## 1. Decoupled Grounding vs. Structured Output
- **Live Catalyst & Fact Grounding:** When querying the Gemini API (`gemini-4-argon`) with `tools=[{"google_search": {}}]`, execute search grounding in a dedicated text step or conversational pass so real-time web citations, breaking news, and live `USD/SEK` exchange rates are retrieved without conflicting with strict JSON schema generation.
- **Strict Schema Validation:** When generating structured decisions (`TradeDecisionDraft`, `AdversarialCritique`, `TradeDecision`), enforce `response_mime_type="application/json"` and validate the response via Pydantic (`model_validate_json`).

## 2. Deterministic Ground-Truth Precedence
- Treat injected JSON state (`portfolio.json`, `risk_profile.json`, `computed_metrics`, `python_verified_net_rr`, and Linux diagnostic command outputs) as immutable ground truth.
- Never invent or override account balances, share counts, financing levels, or Python-verified arithmetic.

## 3. Cloud-to-Local 16GB VRAM Hardware Failover
- If the Gemini API is unreachable or rate-limited, seamlessly fall back to the local NVIDIA RTX 3080 Ti (16GB VRAM) running `qwen2.5-coder:14b` via Ollama using the identical declarative agent prompt and JSON schema.
