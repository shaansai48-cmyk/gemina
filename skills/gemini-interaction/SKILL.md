---
name: gemini-interaction
description: "Use when querying the Gemini API with Google Search grounding, validating structured Pydantic trade schemas, or failing over to local 16GB VRAM Ollama inference."
version: "1.0.0"
---

# Gemini API Interaction

## Workflow

1. **Decouple Google Search grounding from structured JSON output.**
   - Execute live catalyst and `USD/SEK` searches in a dedicated text call (`tools=[{"google_search": {}}]`) so web grounding never conflicts with strict JSON schema generation.
   - **Done when:** live news sentiment and `USD/SEK` context are retrieved as plain text before structured inference begins.

2. **Enforce strict Pydantic schema validation.**
   - Pass `response_mime_type="application/json"` and `response_schema` (`TradeDecisionDraft` or `AdversarialCritique`), then validate with `model_validate_json()`.
   - **Done when:** the parsed response passes Pydantic validation without missing or malformed fields.

3. **Respect deterministic ground-truth precedence.**
   - Treat injected JSON state (`portfolio.json`, `risk_profile.json`, `computed_metrics`, `python_verified_net_rr`, `ticker_experience_memory`, and Linux diagnostic outputs) as immutable ground truth.
   - **Done when:** no account balance, share count, financing level, or Python-verified arithmetic is altered by LLM output.

4. **Fail over to local 16GB VRAM GPU when cloud inference is unavailable.**
   - If the Gemini API fails or hits a rate limit, route the identical compiled system prompt and JSON schema to local Ollama (`qwen2.5-coder:14b` on the RTX 3080 Ti).
   - **Done when:** a valid `TradeDecision` or Q&A response is returned from either cloud or local GPU fallback.
