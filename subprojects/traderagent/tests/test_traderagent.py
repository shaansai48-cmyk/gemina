"""
Automated Verification Suite for Gemina Core & TraderAgent Subproject.
Verifies:
  1. Gemina AgentRegistry & TraderAgentPlugin import/mounting contract
  2. All 7 Level-4 Deterministic Python Quant Guardrails
  3. Level-3a Deterministic Fee-Adjusted R:R Math Auditor
  4. End-to-End PipelineTracer lifecycle
"""
import copy
import time
import unittest
from gemina import AgentRegistry, PipelineTracer
from traderagent import (
    TradeDecision,
    TradeDecisionDraft,
    TraderAgentPlugin,
    compute_quant_metrics,
    compute_verified_fee_adjusted_rr,
    create_trader_agent,
    enforce_hard_quant_guardrails,
)


class TestGeminaTraderAgentArchitecture(unittest.TestCase):
    def setUp(self):
        self.plugin = create_trader_agent()
        self.portfolio = self.plugin.daemon.repo.load_portfolio()
        self.risk = self.plugin.daemon.repo.load_risk()
        self.nvda_asset = copy.deepcopy(self.portfolio["watchlist"]["NVDA"])
        self.sample_telemetry = {
            "ticker": "NVDA",
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

    def _make_buy_decision(self) -> TradeDecision:
        return TradeDecision(
            ticker="NVDA",
            market_regime="Momentum_Breakout",
            action="BUY_MINI_LONG",
            instrument="MINI L NVIDIA AVA 308",
            quantity=200,
            limit_price=33.0,
            avanza_stop_trigger_sek=31.5,
            avanza_stop_sell_sek=31.3,
            underlying_stop_price=149.70,
            take_profit_target=160.0,
            net_risk_reward_ratio=2.5,
            overnight_gap_warning=False,
            rationale="Unit test draft",
            confidence_score=85,
            critic_verdict="APPROVED",
        )

    def test_gemina_plugin_registry_mounting(self):
        registry = AgentRegistry()
        registry.register(self.plugin)
        mounted = registry.get("traderagent")
        self.assertIsNotNone(mounted)
        self.assertIsInstance(mounted, TraderAgentPlugin)
        self.assertGreaterEqual(len(mounted.capabilities()), 5)

    def test_declarative_markdown_agent_and_skill_loader(self):
        from src.loader import load_agent, load_skill
        from src.bot import DeclarativeTelegramBot

        main_agent = load_agent("main_agent.md")
        sub_agent = load_agent("subagent.md")
        gemini_skill = load_skill("gemini_interaction.md")

        self.assertEqual(main_agent.name, "gemina_main_agent")
        self.assertEqual(main_agent.model, "gemini-4-argon")
        self.assertTrue(len(main_agent.goal) > 20)
        self.assertTrue(len(main_agent.backstory) > 20)
        self.assertTrue(len(main_agent.instructions) > 20)
        self.assertGreaterEqual(len(main_agent.loaded_skills), 3)

        self.assertEqual(sub_agent.name, "traderagent_subagent")
        self.assertEqual(gemini_skill.name, "gemini_interaction")

        compiled = sub_agent.compile_system_prompt()
        self.assertIn("## Goal", compiled)
        self.assertIn("## Backstory", compiled)
        self.assertIn("## Instructions", compiled)
        self.assertIn("[SKILL: gemini_interaction]", compiled)

        bot = DeclarativeTelegramBot()
        self.assertEqual(bot.main_agent_def.name, "gemina_main_agent")
        self.assertEqual(bot.subagent_def.name, "traderagent_subagent")

    def test_quant_metrics_and_l3a_math_auditor(self):
        metrics = compute_quant_metrics(self.sample_telemetry, self.nvda_asset, usd_sek=10.16)
        self.assertEqual(metrics["mini_theoretical_sek"], 32.95)
        self.assertEqual(metrics["mini_effective_leverage"], 4.65)

        draft = TradeDecisionDraft(
            ticker="NVDA",
            market_regime="Downtrend_Continuation",
            action="HOLD",
            instrument="NVDA",
            quantity=0,
            limit_price=None,
            avanza_stop_trigger_sek=None,
            avanza_stop_sell_sek=None,
            underlying_stop_price=148.50,
            take_profit_target=164.50,
            net_risk_reward_ratio=0.0,
            overnight_gap_warning=False,
            rationale="Draft with 0.0 R:R",
        )
        audit = compute_verified_fee_adjusted_rr(draft, self.sample_telemetry, self.nvda_asset, self.risk)
        self.assertTrue(audit["math_hallucination_detected"])
        self.assertEqual(audit["python_verified_net_rr"], 4.73)

    def test_all_7_hard_python_guardrails(self):
        # 1. Daily Quota Veto
        p1 = copy.deepcopy(self.portfolio)
        p1["daily_stats"]["trades_executed_today"] = 2
        d1 = enforce_hard_quant_guardrails(self._make_buy_decision(), self.sample_telemetry, p1, self.nvda_asset, self.risk, 10.16)
        self.assertEqual(d1.action, "HOLD")
        self.assertIn("VETO: DAILY QUOTA", d1.rationale)

        # 2. Daily Loss Lockout Veto
        p2 = copy.deepcopy(self.portfolio)
        p2["daily_stats"]["realized_pnl_today_sek"] = -950.0
        d2 = enforce_hard_quant_guardrails(self._make_buy_decision(), self.sample_telemetry, p2, self.nvda_asset, self.risk, 10.16)
        self.assertEqual(d2.action, "HOLD")
        self.assertIn("VETO: LOSS LOCKOUT", d2.rationale)

        # 3. Stop-Out Cooldown Veto
        p3 = copy.deepcopy(self.portfolio)
        p3["daily_stats"]["last_stopout_epoch"] = int(time.time()) - 600
        d3 = enforce_hard_quant_guardrails(self._make_buy_decision(), self.sample_telemetry, p3, self.nvda_asset, self.risk, 10.16)
        self.assertEqual(d3.action, "HOLD")
        self.assertIn("VETO: COOLDOWN", d3.rationale)

        # 4. Sub-VWAP Veto
        r_open = copy.deepcopy(self.risk)
        r_open["nordic_etp_cutoff_cet"] = "24:00"
        r_open["us_open_blackout_start_cet"] = "00:00"
        r_open["us_open_blackout_end_cet"] = "00:00"
        d4 = enforce_hard_quant_guardrails(self._make_buy_decision(), self.sample_telemetry, self.portfolio, self.nvda_asset, r_open, 10.16)
        self.assertEqual(d4.action, "HOLD")
        self.assertIn("VETO: SUB-VWAP", d4.rationale)

        # 5. Low Net R:R Veto
        telem_bull = copy.deepcopy(self.sample_telemetry)
        telem_bull["close"] = 153.0
        d5_in = self._make_buy_decision()
        d5_in.net_risk_reward_ratio = 1.4
        d5 = enforce_hard_quant_guardrails(d5_in, telem_bull, self.portfolio, self.nvda_asset, r_open, 10.16)
        self.assertEqual(d5.action, "HOLD")
        self.assertIn("VETO: LOW R:R", d5.rationale)

        # 6. Structural Stop-Loss Floor Below pd_low (149.80 - 0.20*2.40 = 149.32)
        d6 = enforce_hard_quant_guardrails(self._make_buy_decision(), telem_bull, self.portfolio, self.nvda_asset, r_open, 10.16)
        self.assertEqual(d6.action, "BUY_MINI_LONG")
        self.assertEqual(d6.underlying_stop_price, 149.32)
        self.assertEqual(round(d6.avanza_stop_trigger_sek - d6.avanza_stop_sell_sek, 2), 0.20)

        # 7. Auto-Exit Active Mini Long on EMA4 Failure (close < 151.20 - 0.15*2.40 = 150.84)
        asset_held = copy.deepcopy(self.nvda_asset)
        asset_held["mini_units_held"] = 200
        telem_ema_break = copy.deepcopy(self.sample_telemetry)
        telem_ema_break["close"] = 150.80
        d7_in = self._make_buy_decision()
        d7_in.action = "HOLD"
        d7 = enforce_hard_quant_guardrails(d7_in, telem_ema_break, self.portfolio, asset_held, r_open, 10.16)
        self.assertEqual(d7.action, "SELL_MINI_LONG")
        self.assertEqual(d7.quantity, 200)
        self.assertIn("EMA4 FAILURE EXIT", d7.rationale)


if __name__ == "__main__":
    unittest.main()
