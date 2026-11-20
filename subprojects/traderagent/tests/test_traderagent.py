"""
Automated Verification Suite for Gemina Core & TraderAgent Subproject.
Verifies:
  1. Gemina AgentRegistry & TraderAgentPlugin import/mounting contract
  2. All 7 Level-4 Deterministic Python Quant Guardrails
  3. Level-3a Deterministic Fee-Adjusted R:R Math Auditor
  4. End-to-End PipelineTracer lifecycle
"""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from gemina import AgentRegistry, PipelineTracer
from traderagent import (
    AdversarialCritique,
    TradeDecision,
    TradeDecisionDraft,
    TraderAgentPlugin,
    compute_quant_metrics,
    compute_statistical_confidence,
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
        gemini_skill = load_skill("gemini-interaction")

        self.assertEqual(main_agent.name, "gemina_main_agent")
        self.assertEqual(main_agent.model, "gemini-4-argon")
        self.assertTrue(len(main_agent.goal) > 20)
        self.assertTrue(len(main_agent.backstory) > 20)
        self.assertTrue(len(main_agent.instructions) > 20)
        self.assertGreaterEqual(len(main_agent.loaded_skills), 3)

        self.assertEqual(sub_agent.name, "traderagent_subagent")
        self.assertEqual(gemini_skill.name, "gemini-interaction")

        compiled = sub_agent.compile_system_prompt()
        self.assertIn("## Goal", compiled)
        self.assertIn("## Backstory", compiled)
        self.assertIn("## Instructions", compiled)
        self.assertIn("[SKILL: gemini-interaction]", compiled)

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

        # 6. Structural Stop-Loss Floor Below pd_low (149.80 - 0.20*2.40 = 149.32) & Avanza Order Placement Guide
        d6 = enforce_hard_quant_guardrails(self._make_buy_decision(), telem_bull, self.portfolio, self.nvda_asset, r_open, 10.16)
        self.assertEqual(d6.action, "BUY_MINI_LONG")
        self.assertEqual(d6.underlying_stop_price, 149.32)
        self.assertEqual(round(d6.avanza_stop_trigger_sek - d6.avanza_stop_sell_sek, 2), 0.20)
        self.assertIn("HOW TO PLACE THIS ORDER IN AVANZA", d6.avanza_execution_guide)
        self.assertIn("Triggerkurs:", d6.avanza_execution_guide)
        self.assertIn("Säljkurs:", d6.avanza_execution_guide)
        self.assertIn("/fill_mini", d6.avanza_execution_guide)

        # 6b. Honest 60d Experience Memory Repository Check (no 99.0 placeholders)
        exp = self.plugin.daemon.repo.experience.get_ticker_experience("AMD")
        self.assertGreater(exp["verified_win_rate_pct"], 0.0)
        self.assertLessEqual(exp["verified_win_rate_pct"], 100.0)
        self.assertIn("wilson_95_lower_wr_pct", exp)
        self.assertIn("dsr_pct", exp)
        self.assertNotEqual(exp["verified_profit_factor"], 99.0)

        # 7. Auto-Exit Active Mini Long on Combined EMA4 + VWAP Failure (close < 150.84 and close < 152.00)
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

    def test_certificate_equity_mini_spread_and_statistical_confidence(self):
        # 0.38% Mini spread must multiply certificate intrinsic equity |spot - fin_lvl|, NOT full underlying spot
        telem = copy.deepcopy(self.sample_telemetry)
        telem["close"] = 156.0
        draft = TradeDecisionDraft(
            ticker="NVDA",
            market_regime="Momentum_Breakout",
            action="BUY_MINI_LONG",
            instrument="MINI L NVIDIA AVA 308",
            quantity=100,
            limit_price=38.2,
            avanza_stop_trigger_sek=36.0,
            avanza_stop_sell_sek=35.8,
            underlying_stop_price=154.0,
            take_profit_target=160.0,
            net_risk_reward_ratio=2.0,
            overnight_gap_warning=False,
            rationale="Certificate equity spread check",
        )
        audit = compute_verified_fee_adjusted_rr(draft, telem, self.nvda_asset, self.risk)
        # cert_equity = 156.0 - 118.415 = 37.585 -> spread_cost = 37.585 * 0.0038 = 0.1428 (vs full spot 0.5928!)
        self.assertGreater(audit["python_verified_net_rr"], 1.80)

        # Short R:R calculation check
        short_draft = TradeDecisionDraft(
            ticker="NVDA",
            market_regime="Momentum_Breakout",
            action="BUY_MINI_SHORT",
            instrument="MINI S NVIDIA AVA",
            quantity=100,
            limit_price=39.6,
            avanza_stop_trigger_sek=37.0,
            avanza_stop_sell_sek=36.8,
            underlying_stop_price=158.0,
            take_profit_target=152.0,
            net_risk_reward_ratio=2.0,
            overnight_gap_warning=False,
            rationale="Short setup check",
        )
        short_audit = compute_verified_fee_adjusted_rr(short_draft, telem, self.nvda_asset, self.risk)
        self.assertGreater(short_audit["python_verified_net_rr"], 1.80)

        # Bailey & Lopez de Prado Statistical Confidence: small n=4 vs large n=120
        small_stats = compute_statistical_confidence([300.0, -200.0, 400.0, 250.0], num_trials=8)
        self.assertFalse(small_stats["statistically_significant"])
        self.assertLess(small_stats["wilson_95_lower_wr_pct"], small_stats["raw_win_rate_pct"])

        strong_pnls = [420.0 if i % 5 != 0 else -180.0 for i in range(120)]
        strong_stats = compute_statistical_confidence(strong_pnls, num_trials=8)
        self.assertGreater(strong_stats["psr_pct"], 95.0)
        self.assertGreater(strong_stats["dsr_pct"], 90.0)
        self.assertTrue(strong_stats["statistically_significant"])

    def test_concurrent_minis_er_rvol_short_and_ratchet(self):
        r_open = copy.deepcopy(self.risk)
        r_open["nordic_etp_cutoff_cet"] = "24:00"
        r_open["us_open_blackout_start_cet"] = "00:00"
        r_open["us_open_blackout_end_cet"] = "00:00"

        telem_ok = copy.deepcopy(self.sample_telemetry)
        telem_ok.update({"close": 153.0, "low": 152.4, "high": 153.4, "ema4": 152.7, "vwap": 152.0, "rvol": 1.25, "efficiency_ratio": 0.35})

        # 1. Max Concurrent Leveraged Minis = 1
        p_busy = copy.deepcopy(self.portfolio)
        p_busy["watchlist"]["TSLA"]["mini_units_held"] = 50
        d_max = enforce_hard_quant_guardrails(self._make_buy_decision(), telem_ok, p_busy, self.nvda_asset, r_open, 10.16)
        self.assertEqual(d_max.action, "HOLD")
        self.assertIn("VETO: MAX CONCURRENT MINIS", d_max.rationale)

        # 2. Low ER Chop Veto on Momentum_Breakout
        telem_chop = copy.deepcopy(telem_ok)
        telem_chop["efficiency_ratio"] = 0.18
        d_er = enforce_hard_quant_guardrails(self._make_buy_decision(), telem_chop, self.portfolio, self.nvda_asset, r_open, 10.16)
        self.assertEqual(d_er.action, "HOLD")
        self.assertIn("VETO: LOW ER CHOP", d_er.rationale)

        # 3. Scoped Low RVOL Veto on breakout, but NOT on sweep
        telem_low_rvol = copy.deepcopy(telem_ok)
        telem_low_rvol["rvol"] = 0.72
        d_rvol = enforce_hard_quant_guardrails(self._make_buy_decision(), telem_low_rvol, self.portfolio, self.nvda_asset, r_open, 10.16)
        self.assertEqual(d_rvol.action, "HOLD")
        self.assertIn("VETO: LOW RVOL", d_rvol.rationale)

        # Sweep entry below VWAP near pd_low passes RVOL check
        telem_sweep = copy.deepcopy(self.sample_telemetry)
        telem_sweep.update({"close": 151.30, "ema4": 151.0, "low": 149.90, "vwap": 152.0, "pd_low": 149.80, "rvol": 0.72, "efficiency_ratio": 0.18})
        d_sweep = enforce_hard_quant_guardrails(self._make_buy_decision(), telem_sweep, self.portfolio, self.nvda_asset, r_open, 10.16)
        self.assertEqual(d_sweep.action, "BUY_MINI_LONG")

        # 4. Stateful +0.85R -> +0.15R Breakeven Ratchet & combined EMA4+VWAP exit preservation above VWAP
        asset_ratchet = copy.deepcopy(self.nvda_asset)
        asset_ratchet.update({
            "mini_units_held": 100,
            "mini_side": "LONG",
            "mini_entry_underlying_price": 150.0,
            "mini_initial_risk_r": 2.0,
        })
        telem_up = copy.deepcopy(telem_ok)
        # Reached +0.90R (151.80) AND dips below fast EMA4 (close=152.20 vs ema4=153.0 -> ema4-0.15*2.4=152.64)
        # MUST NOT exit because close (152.20) >= VWAP (152.00); instead must ratchet stop to 150.30!
        telem_up.update({"close": 152.20, "high": 152.50, "low": 151.80, "ema4": 153.00, "vwap": 152.00})
        d_hold = TradeDecision(
            ticker="NVDA",
            market_regime="Momentum_Breakout",
            action="HOLD",
            instrument="MINI L NVIDIA AVA 308",
            quantity=100,
            limit_price=None,
            avanza_stop_trigger_sek=30.0,
            avanza_stop_sell_sek=29.8,
            underlying_stop_price=148.5,
            take_profit_target=160.0,
            net_risk_reward_ratio=2.0,
            overnight_gap_warning=False,
            rationale="Trailing check",
            confidence_score=80,
            critic_verdict="APPROVED",
        )
        d_rat = enforce_hard_quant_guardrails(d_hold, telem_up, self.portfolio, asset_ratchet, r_open, 10.16)
        self.assertEqual(d_rat.action, "HOLD")
        self.assertEqual(d_rat.underlying_stop_price, 150.3)
        self.assertIn("BREAKEVEN RATCHET", d_rat.rationale)

        # 5. Symmetric Short EMA4 + VWAP Exit (close > ema4 + 0.15*atr AND close > vwap)
        asset_short = copy.deepcopy(self.nvda_asset)
        asset_short.update({"mini_units_held": 80, "mini_side": "SHORT", "mini_instrument": "MINI S NVIDIA AVA"})
        telem_short_fail = copy.deepcopy(telem_ok)
        telem_short_fail.update({"close": 153.5, "ema4": 152.5, "vwap": 152.0, "atr14": 2.0})
        d_sh_hold = self._make_buy_decision()
        d_sh_hold.action = "HOLD"
        d_sh_exit = enforce_hard_quant_guardrails(d_sh_hold, telem_short_fail, self.portfolio, asset_short, r_open, 10.16)
        self.assertEqual(d_sh_exit.action, "SELL_MINI_SHORT")
        self.assertEqual(d_sh_exit.quantity, 80)

    def test_allow_live_search_false_pit_isolation(self):
        fake_llm = MagicMock()
        draft = TradeDecisionDraft(
            ticker="NVDA",
            market_regime="Volatile_Chop",
            action="HOLD",
            instrument="NVDA",
            quantity=0,
            limit_price=None,
            avanza_stop_trigger_sek=None,
            avanza_stop_sell_sek=None,
            underlying_stop_price=148.5,
            take_profit_target=164.5,
            net_risk_reward_ratio=0.0,
            overnight_gap_warning=False,
            rationale="PIT hold",
        )
        critique = AdversarialCritique(
            verdict="APPROVED",
            flaws_found="None",
            confidence_score=50,
            refined_decision=draft,
        )
        fake_llm.generate_structured.side_effect = [(draft, "mock", "OK"), (critique, "mock", "OK")]

        from traderagent.agents.committee import QuantAgentCommittee

        committee = QuantAgentCommittee(fake_llm)
        tracer = PipelineTracer(flow="pit_test", source="unit_test")
        telem = copy.deepcopy(self.sample_telemetry)
        telem["time"] = "2026-10-15T18:00:00Z"
        metrics = compute_quant_metrics(telem, self.nvda_asset)
        dec = committee.evaluate_bar(
            telemetry=telem,
            portfolio=self.portfolio,
            asset=self.nvda_asset,
            risk=self.risk,
            metrics=metrics,
            recent_ticker_bars=[],
            allow_live_search=False,
            tracer=tracer,
        )
        self.assertEqual(dec.action, "HOLD")
        fake_llm.generate_grounded_text.assert_not_called()
        l1_stages = [s for s in tracer.stages if s["stage"] == "L1_CATALYST_AGENT"]
        self.assertEqual(len(l1_stages), 1)
        self.assertEqual(l1_stages[0]["status"], "HISTORICAL_PIT_NO_SEARCH")

    def test_daemon_multi_focus_and_command_router_sync(self):
        # Verify daemon handles multi-focus "TSLA,NVDA" without NameError on accepted OR ignored tickers,
        # and verify TelegramCommandRouter handles /fullstatus, /fill_mini SHORT, and /close_mini cleanly.
        daemon = self.plugin.daemon
        router = daemon.command_router
        orig_repo = daemon.repo
        orig_apps = daemon.apps_script
        orig_comm = daemon.committee
        orig_tg = daemon.telegram
        orig_stat = daemon.status_evaluator

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                p_copy = copy.deepcopy(self.portfolio)
                p_copy["active_focus_ticker"] = "TSLA,NVDA"
                p_copy["last_seen_ts_by_ticker"] = {}
                p_file = tmp_path / "portfolio.json"
                r_file = tmp_path / "risk.json"
                a_file = tmp_path / "audit.jsonl"
                t_file = tmp_path / "trace.jsonl"
                e_file = tmp_path / "exp.json"
                p_file.write_text(json.dumps(p_copy), encoding="utf-8")
                r_file.write_text(json.dumps(self.risk), encoding="utf-8")
                e_file.write_text(Path(daemon.config.experience_memory_path).read_text(encoding="utf-8"), encoding="utf-8")

                cfg_tmp = copy.deepcopy(daemon.config)
                cfg_tmp.portfolio_path = str(p_file)
                cfg_tmp.risk_path = str(r_file)
                cfg_tmp.audit_log_path = str(a_file)
                cfg_tmp.pipeline_trace_path = str(t_file)
                cfg_tmp.experience_memory_path = str(e_file)

                from traderagent.infrastructure.persistence import StateRepository
                from traderagent.infrastructure.system_status import StatusEvaluator

                tmp_repo = StateRepository(cfg_tmp)
                mock_apps = MagicMock()
                mock_apps.last_ok = True
                mock_apps.poll_bars.return_value = [
                    ("2026-11-19T18:00:00Z", {**self.sample_telemetry, "ticker": "TSLA", "close": 395.0, "vwap": 392.0, "ema4": 394.0}),
                    ("2026-11-19T18:00:00Z", {**self.sample_telemetry, "ticker": "IBM", "close": 210.0}),
                ]
                mock_comm = MagicMock()
                mock_comm.evaluate_bar.return_value = TradeDecision(
                    ticker="TSLA",
                    market_regime="Momentum_Breakout",
                    action="BUY_MINI_SHORT",
                    instrument="MINI S TSLA AVA",
                    quantity=50,
                    limit_price=15.20,
                    avanza_stop_trigger_sek=14.10,
                    avanza_stop_sell_sek=13.90,
                    underlying_stop_price=398.0,
                    take_profit_target=386.0,
                    net_risk_reward_ratio=2.2,
                    overnight_gap_warning=False,
                    rationale="Mock multi-focus bar",
                    confidence_score=80,
                    critic_verdict="APPROVED",
                )
                mock_tg = MagicMock()
                tmp_eval = StatusEvaluator(tmp_repo, mock_apps)

                daemon.repo = tmp_repo
                daemon.apps_script = mock_apps
                daemon.committee = mock_comm
                daemon.telegram = mock_tg
                daemon.status_evaluator = tmp_eval
                router.repo = tmp_repo
                router.status_evaluator = tmp_eval
                router.telegram = mock_tg

                count = daemon.process_incoming_bars()
                self.assertEqual(count, 1)

                # Test multi-focus router commands
                body_full, st_full = router.execute_text_command("/fullstatus")
                self.assertIn("TSLA,NVDA", body_full)
                self.assertIsNotNone(st_full)

                msg_bad, _ = router.execute_text_command("/fill_mini AAPL 5000 long")
                self.assertIn("Missing Mini unit fill price", msg_bad)

                msg_fill, _ = router.execute_text_command("/fill_mini TSLA 50 15.20 SHORT")
                self.assertIn("MINI", msg_fill.upper())
                p_after_fill = tmp_repo.load_portfolio()
                self.assertEqual(p_after_fill["watchlist"]["TSLA"]["mini_units_held"], 50)
                self.assertEqual(p_after_fill["watchlist"]["TSLA"]["mini_side"], "SHORT")

                msg_close, _ = router.execute_text_command("/close_mini 16.40")
                self.assertIn("60.0 SEK", msg_close)
                p_after_close = tmp_repo.load_portfolio()
                self.assertEqual(p_after_close["watchlist"]["TSLA"]["mini_units_held"], 0)
                self.assertEqual(p_after_close["watchlist"]["TSLA"]["mini_side"], "")
        finally:
            daemon.repo = orig_repo
            daemon.apps_script = orig_apps
            daemon.committee = orig_comm
            daemon.telegram = orig_tg
            daemon.status_evaluator = orig_stat
            router.repo = orig_repo
            router.status_evaluator = orig_stat
            router.telegram = orig_tg


if __name__ == "__main__":
    unittest.main()
