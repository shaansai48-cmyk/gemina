"""
Thread-Safe Portfolio Ledger Use-Case Service (`ledger_service.py`).
Single source of truth for focus, stops, Mini/share fills & closes, watchlist additions,
and pre-trade SEK/1R unit calculation. Shared by TelegramCommandRouter and the Web Command Center.
"""
import math
import re
import time
from typing import Any, Optional

from gemina.core.tracing import PipelineTracer
from traderagent.domain.models import normalize_ticker, resolve_focus_ticker
from traderagent.infrastructure.persistence import StateRepository

_TICKER_RE = re.compile(r"^[A-Z0-9 .\-]{1,16}$")


class CommandInputError(ValueError):
    """Raised on malformed user/form ledger arguments."""


def parse_positive_int(raw: str | int, field: str = "quantity") -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        val = raw
    else:
        s = str(raw).strip()
        if not re.fullmatch(r"\d+", s):
            if re.search(r"[a-zA-Z]", s):
                raise CommandInputError(
                    f"'{s}' is not a valid whole-number {field} (do not include currency units like 'sek')."
                )
            raise CommandInputError(f"'{s}' must be a positive whole number for {field}.")
        val = int(s)
    if val <= 0:
        raise CommandInputError(f"{field} must be greater than 0.")
    return val


def parse_positive_float(
    raw: str | float | int,
    field: str = "price",
    allow_zero: bool = False,
) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = float(raw)
    else:
        s = str(raw).strip().replace(",", ".")
        s = re.sub(r"(?i)\s*(sek|usd|\$)$", "", s).strip()
        try:
            val = float(s)
        except ValueError:
            raise CommandInputError(f"'{raw}' is not a valid number for {field}.")
    if not math.isfinite(val):
        raise CommandInputError(f"{field} must be a finite number.")
    if allow_zero:
        if val < 0:
            raise CommandInputError(f"{field} cannot be negative.")
    elif val <= 0:
        raise CommandInputError(f"{field} must be greater than 0.")
    return round(val, 4)


def _default_watchlist_asset(sym: str, currency: str = "", hard_stop: float = 0.0, fin_level: float = 0.0, parity: Optional[float] = None) -> dict[str, Any]:
    inferred_cur = currency.upper() if currency else ("SEK" if sym == "ERIC B" else "USD")
    inferred_ex = "OMXSTO" if inferred_cur == "SEK" else "NASDAQ"
    inferred_par = parity if (parity and parity > 0) else (1.0 if inferred_cur == "SEK" else 10.0)
    return {
        "exchange": inferred_ex,
        "currency": inferred_cur,
        "regular_shares_held": 0,
        "blended_avg_price": 0.0,
        "hard_stop_price": hard_stop,
        "take_profit_price": 0.0,
        "mini_instrument": f"MINI L {sym} AVA",
        "mini_financing_level": fin_level,
        "mini_barrier": round(fin_level * 1.08, 2) if fin_level > 0 else 0.0,
        "mini_short_instrument": f"MINI S {sym} AVA",
        "mini_short_financing_level": round(fin_level * 1.35, 2) if fin_level > 0 else 0.0,
        "mini_short_barrier": round(fin_level * 1.25, 2) if fin_level > 0 else 0.0,
        "mini_parity": inferred_par,
        "mini_units_held": 0,
        "mini_avg_sek": 0.0,
        "mini_side": "",
        "mini_entry_underlying_price": 0.0,
        "mini_initial_risk_r": 0.0,
    }


class LedgerService:
    """Thread-safe atomic mutations & calculators over `StateRepository`."""

    def __init__(self, repo: StateRepository):
        self.repo = repo

    def _clear_dedup(self, ticker: str, actions: tuple[str, ...]) -> None:
        if not hasattr(self.repo, "update_runtime_state"):
            return

        def _mut(state: dict[str, Any]) -> None:
            dedup = state.setdefault("alert_dedup", {})
            for act in actions:
                dedup.pop(f"{ticker}:{act}", None)

        self.repo.update_runtime_state(_mut)

    def switch_focus(self, tickers_input: list[str] | str, tracer: Optional[PipelineTracer] = None) -> dict[str, Any]:
        if isinstance(tickers_input, str):
            raw_items = [x for x in tickers_input.split(",") if x.strip()]
        else:
            raw_items = [str(x) for x in tickers_input if str(x).strip()]
        if not raw_items:
            raise CommandInputError("Specify at least one ticker (e.g. NVDA or AAPL,NVDA,TSLA).")

        normed: list[str] = []
        for raw in raw_items:
            sym = normalize_ticker(raw)
            if not sym or not _TICKER_RE.fullmatch(sym):
                raise CommandInputError(f"Invalid ticker symbol '{raw}'.")
            if sym not in normed:
                normed.append(sym)

        def _mut(portfolio: dict[str, Any]) -> dict[str, Any]:
            old_focus = portfolio.get("active_focus_ticker", "")
            watchlist = portfolio.setdefault("watchlist", {})
            for sym in normed:
                if sym not in watchlist:
                    watchlist[sym] = _default_watchlist_asset(sym)
            new_focus = ",".join(normed)
            portfolio["active_focus_ticker"] = new_focus
            return {"old_focus": old_focus, "new_focus": new_focus}

        portfolio, res = self.repo.update_portfolio(_mut)
        if tracer:
            tracer.add_stage(
                stage="LEDGER_STATE_TRANSFORM",
                status="OK",
                input_data={"active_focus_ticker": res["old_focus"]},
                transformation=f"Switched active_focus_ticker {res['old_focus']} -> {res['new_focus']}",
                output_data={"active_focus_ticker": res["new_focus"]},
            )
        return {"active_focus_ticker": res["new_focus"], "tickers": normed, "portfolio": portfolio}

    def add_ticker(
        self,
        ticker: str,
        currency: str,
        hard_stop: float | str,
        financing_level: float | str,
        parity: float | str | None = None,
        tracer: Optional[PipelineTracer] = None,
    ) -> dict[str, Any]:
        sym = normalize_ticker(ticker)
        if not sym or not _TICKER_RE.fullmatch(sym):
            raise CommandInputError(f"Invalid ticker symbol '{ticker}'.")
        cur = currency.strip().upper()
        if cur not in ("USD", "SEK"):
            raise CommandInputError("Currency must be USD or SEK.")
        stop_val = parse_positive_float(hard_stop, "hard stop", allow_zero=False)
        fin_val = parse_positive_float(financing_level, "financing level", allow_zero=True)
        par_val = parse_positive_float(parity, "parity", allow_zero=False) if parity is not None else (1.0 if cur == "SEK" else 10.0)

        def _mut(portfolio: dict[str, Any]) -> dict[str, Any]:
            entry = _default_watchlist_asset(sym, currency=cur, hard_stop=stop_val, fin_level=fin_val, parity=par_val)
            portfolio.setdefault("watchlist", {})[sym] = entry
            return entry

        portfolio, entry = self.repo.update_portfolio(_mut)
        if tracer:
            tracer.add_stage(
                stage="LEDGER_STATE_TRANSFORM",
                status="OK",
                input_data={"symbol": sym},
                transformation=f"Added {sym} ({cur}) to portfolio watchlist",
                output_data=entry,
            )
        return {"ticker": sym, "asset": entry, "portfolio": portfolio}

    def fill_mini(
        self,
        ticker: Optional[str],
        units: int | str,
        price_sek: float | str,
        side: str = "LONG",
        entry_underlying_price: float | str = 0.0,
        stop_underlying_price: float | str = 0.0,
        tracer: Optional[PipelineTracer] = None,
    ) -> dict[str, Any]:
        qty = parse_positive_int(units, "Mini units")
        px = parse_positive_float(price_sek, "SEK fill price", allow_zero=False)
        side_tag = side.strip().upper()
        if side_tag not in ("LONG", "SHORT"):
            raise CommandInputError("Side must be LONG or SHORT.")
        opt_u = parse_positive_float(entry_underlying_price, "entry underlying price", allow_zero=True) if entry_underlying_price else 0.0
        opt_stop_u = parse_positive_float(stop_underlying_price, "stop underlying price", allow_zero=True) if stop_underlying_price else 0.0

        recent_entry_u = opt_u
        recent_risk_1r = round(abs(opt_u - opt_stop_u), 4) if (opt_u > 0 and opt_stop_u > 0) else 0.0
        resolved_sym_hint = normalize_ticker(ticker) if ticker else ""

        try:
            audit_tail = self.repo.get_recent_audit_tail(10)
        except Exception:
            audit_tail = []

        if isinstance(audit_tail, list) and (not resolved_sym_hint or recent_entry_u <= 0):
            expected_act = "BUY_MINI_SHORT" if side_tag == "SHORT" else "BUY_MINI_LONG"
            for entry in reversed(audit_tail):
                if not isinstance(entry, dict):
                    continue
                e_sym = normalize_ticker(str(entry.get("ticker", "")))
                e_dec = entry.get("decision") or {}
                e_tel = entry.get("telemetry") or {}
                if not resolved_sym_hint and e_sym and e_dec.get("action") == expected_act:
                    resolved_sym_hint = e_sym
                if (not resolved_sym_hint or e_sym == resolved_sym_hint) and isinstance(e_tel, dict) and "close" in e_tel:
                    if recent_entry_u <= 0:
                        recent_entry_u = float(e_tel["close"])
                    if recent_risk_1r <= 0:
                        stop_u = float(e_dec.get("underlying_stop_price", 0.0) or 0.0) if isinstance(e_dec, dict) else 0.0
                        if stop_u > 0 and recent_entry_u > 0:
                            recent_risk_1r = round(abs(recent_entry_u - stop_u), 4)
                    break

        def _mut(portfolio: dict[str, Any]) -> dict[str, Any]:
            watchlist = portfolio.get("watchlist", {})
            focus = resolved_sym_hint if resolved_sym_hint in watchlist else resolve_focus_ticker(portfolio)
            asset = watchlist[focus]
            old_cash = float(portfolio["cash_reserves"]["available_tactical_mini_sek"])
            cost = round(qty * px, 2)
            asset["mini_units_held"] = qty
            asset["mini_avg_sek"] = px
            asset["mini_side"] = side_tag
            if recent_entry_u > 0:
                asset["mini_entry_underlying_price"] = recent_entry_u
                asset["mini_initial_risk_r"] = recent_risk_1r
            portfolio["cash_reserves"]["available_tactical_mini_sek"] = round(old_cash - cost, 2)
            portfolio["daily_stats"]["trades_executed_today"] += 1
            return {
                "ticker": focus,
                "units": qty,
                "price_sek": px,
                "side": side_tag,
                "notional_sek": cost,
                "old_cash_sek": old_cash,
                "new_cash_sek": portfolio["cash_reserves"]["available_tactical_mini_sek"],
                "entry_underlying_price": asset.get("mini_entry_underlying_price", 0.0),
                "initial_risk_r": asset.get("mini_initial_risk_r", 0.0),
            }

        portfolio, res = self.repo.update_portfolio(_mut)
        self._clear_dedup(res["ticker"], ("SELL_MINI_LONG", "SELL_MINI_SHORT"))
        if tracer:
            tracer.add_stage(
                stage="LEDGER_STATE_TRANSFORM",
                status="OK",
                input_data={"cash_sek": res["old_cash_sek"], "mini_units": 0},
                transformation=f"Logged Mini {side_tag} buy: {qty}u {res['ticker']} @ {px} SEK",
                output_data={"cash_sek": res["new_cash_sek"], "mini_units": qty, "mini_side": side_tag},
            )
        return {**res, "portfolio": portfolio}

    def close_mini(
        self,
        ticker: Optional[str],
        exit_sek: float | str,
        tracer: Optional[PipelineTracer] = None,
    ) -> dict[str, Any]:
        px = parse_positive_float(exit_sek, "exit SEK price", allow_zero=False)
        explicit = normalize_ticker(ticker) if ticker else None

        def _mut(portfolio: dict[str, Any]) -> dict[str, Any]:
            focus = explicit if (explicit and explicit in portfolio["watchlist"]) else resolve_focus_ticker(portfolio, prefer_held_mini=True)
            asset = portfolio["watchlist"][focus]
            units = int(asset.get("mini_units_held", 0))
            if units <= 0:
                raise CommandInputError(f"No open Mini position held on {focus} to close.")
            entry_sek = float(asset.get("mini_avg_sek", 0.0))
            pnl = round((px - entry_sek) * units, 2)
            portfolio["cash_reserves"]["available_tactical_mini_sek"] = round(
                float(portfolio["cash_reserves"]["available_tactical_mini_sek"]) + (units * px), 2
            )
            portfolio["daily_stats"]["realized_pnl_today_sek"] = round(
                float(portfolio["daily_stats"]["realized_pnl_today_sek"]) + pnl, 2
            )
            if pnl < 0:
                portfolio["daily_stats"]["last_stopout_epoch"] = int(time.time())
            asset["mini_units_held"] = 0
            asset["mini_avg_sek"] = 0.0
            asset["mini_side"] = ""
            asset["mini_entry_underlying_price"] = 0.0
            asset["mini_initial_risk_r"] = 0.0
            return {
                "ticker": focus,
                "units": units,
                "entry_sek": entry_sek,
                "exit_sek": px,
                "pnl_sek": pnl,
                "new_cash_sek": portfolio["cash_reserves"]["available_tactical_mini_sek"],
                "realized_pnl_today_sek": portfolio["daily_stats"]["realized_pnl_today_sek"],
            }

        portfolio, res = self.repo.update_portfolio(_mut)
        self.repo.experience.record_closed_trade(res["ticker"], "LIVE_AVANZA_MINI", res["pnl_sek"])
        self._clear_dedup(res["ticker"], ("SELL_MINI_LONG", "SELL_MINI_SHORT"))
        if tracer:
            tracer.add_stage(
                stage="LEDGER_STATE_TRANSFORM",
                status="OK",
                input_data={"mini_units": res["units"], "entry_sek": res["entry_sek"]},
                transformation=f"Closed {res['ticker']} Mini @ {px} SEK (PnL: {res['pnl_sek']} SEK) & updated experience_memory.json",
                output_data={"realized_pnl_today_sek": res["realized_pnl_today_sek"]},
            )
        return {**res, "portfolio": portfolio}

    def fill_shares(
        self,
        ticker: Optional[str],
        quantity: int | str,
        avg_price: float | str,
        tracer: Optional[PipelineTracer] = None,
    ) -> dict[str, Any]:
        qty = parse_positive_int(quantity, "share quantity")
        px = parse_positive_float(avg_price, "average price", allow_zero=False)
        explicit = normalize_ticker(ticker) if ticker else None

        def _mut(portfolio: dict[str, Any]) -> dict[str, Any]:
            focus = explicit if (explicit and explicit in portfolio["watchlist"]) else resolve_focus_ticker(portfolio)
            asset = portfolio["watchlist"][focus]
            asset["regular_shares_held"] = qty
            asset["blended_avg_price"] = px
            return {"ticker": focus, "quantity": qty, "avg_price": px, "currency": asset["currency"]}

        portfolio, res = self.repo.update_portfolio(_mut)
        self._clear_dedup(res["ticker"], ("SELL_SHARES",))
        if tracer:
            tracer.add_stage(
                stage="LEDGER_STATE_TRANSFORM",
                status="OK",
                input_data={"ticker": res["ticker"]},
                transformation=f"Set regular_shares_held={qty} @ {px}",
                output_data={"regular_shares_held": qty, "blended_avg_price": px},
            )
        return {**res, "portfolio": portfolio}

    def close_shares(
        self,
        ticker: Optional[str],
        exit_price: float | str,
        tracer: Optional[PipelineTracer] = None,
    ) -> dict[str, Any]:
        px = parse_positive_float(exit_price, "exit price", allow_zero=False)
        explicit = normalize_ticker(ticker) if ticker else None

        def _mut(portfolio: dict[str, Any]) -> dict[str, Any]:
            focus = explicit if (explicit and explicit in portfolio["watchlist"]) else resolve_focus_ticker(portfolio, prefer_held_shares=True)
            asset = portfolio["watchlist"][focus]
            qty = int(asset.get("regular_shares_held", 0))
            if qty <= 0:
                raise CommandInputError(f"No regular shares held on {focus} to close.")
            avg = float(asset.get("blended_avg_price", 0.0))
            pnl_native = round((px - avg) * qty, 2)
            asset["regular_shares_held"] = 0
            asset["blended_avg_price"] = 0.0
            return {
                "ticker": focus,
                "quantity": qty,
                "avg_price": avg,
                "exit_price": px,
                "gross_pnl": pnl_native,
                "currency": asset["currency"],
            }

        portfolio, res = self.repo.update_portfolio(_mut)
        self._clear_dedup(res["ticker"], ("SELL_SHARES",))
        if tracer:
            tracer.add_stage(
                stage="LEDGER_STATE_TRANSFORM",
                status="OK",
                input_data={"qty": res["quantity"], "avg": res["avg_price"]},
                transformation=f"Closed {res['quantity']} shares of {res['ticker']} @ {px}",
                output_data={"gross_pnl": res["gross_pnl"]},
            )
        return {**res, "portfolio": portfolio}

    def set_stop(
        self,
        ticker: Optional[str],
        hard_stop_price: float | str,
        take_profit_price: float | str | None = None,
        tracer: Optional[PipelineTracer] = None,
    ) -> dict[str, Any]:
        stop_val = parse_positive_float(hard_stop_price, "stop price", allow_zero=False)
        tp_val = parse_positive_float(take_profit_price, "target price", allow_zero=True) if take_profit_price is not None else None
        explicit = normalize_ticker(ticker) if ticker else None

        def _mut(portfolio: dict[str, Any]) -> dict[str, Any]:
            focus = explicit if (explicit and explicit in portfolio["watchlist"]) else resolve_focus_ticker(portfolio)
            asset = portfolio["watchlist"][focus]
            old_stop = float(asset.get("hard_stop_price", 0.0))
            asset["hard_stop_price"] = stop_val
            if tp_val is not None:
                asset["take_profit_price"] = tp_val
            return {
                "ticker": focus,
                "old_stop": old_stop,
                "hard_stop_price": stop_val,
                "take_profit_price": float(asset.get("take_profit_price", 0.0)),
            }

        portfolio, res = self.repo.update_portfolio(_mut)
        self._clear_dedup(res["ticker"], ("SELL_SHARES",))
        if tracer:
            tracer.add_stage(
                stage="LEDGER_STATE_TRANSFORM",
                status="OK",
                input_data={"hard_stop_price": res["old_stop"]},
                transformation=f"Updated {res['ticker']} stop {res['old_stop']} -> {stop_val}",
                output_data={"hard_stop_price": stop_val, "take_profit_price": res["take_profit_price"]},
            )
        return {**res, "portfolio": portfolio}

    def set_mini_params(
        self,
        ticker: Optional[str],
        financing_level: float | str,
        barrier: float | str | None = None,
        parity: float | str | None = None,
        side: str = "LONG",
        tracer: Optional[PipelineTracer] = None,
    ) -> dict[str, Any]:
        fin_val = parse_positive_float(financing_level, "financing level", allow_zero=False)
        bar_val = parse_positive_float(barrier, "barrier", allow_zero=False) if barrier is not None else None
        par_val = parse_positive_float(parity, "parity", allow_zero=False) if parity is not None else None
        side_tag = side.strip().upper()
        if side_tag not in ("LONG", "SHORT"):
            raise CommandInputError("Side must be LONG or SHORT.")
        explicit = normalize_ticker(ticker) if ticker else None

        def _mut(portfolio: dict[str, Any]) -> dict[str, Any]:
            focus = explicit if (explicit and explicit in portfolio["watchlist"]) else resolve_focus_ticker(portfolio)
            asset = portfolio["watchlist"][focus]
            if side_tag == "SHORT":
                asset["mini_short_financing_level"] = fin_val
                if bar_val is not None:
                    asset["mini_short_barrier"] = bar_val
            else:
                asset["mini_financing_level"] = fin_val
                if bar_val is not None:
                    asset["mini_barrier"] = bar_val
            if par_val is not None:
                asset["mini_parity"] = par_val
            return {
                "ticker": focus,
                "side": side_tag,
                "mini_financing_level": asset["mini_financing_level"],
                "mini_barrier": asset["mini_barrier"],
                "mini_short_financing_level": asset.get("mini_short_financing_level", 0.0),
                "mini_short_barrier": asset.get("mini_short_barrier", 0.0),
                "mini_parity": asset["mini_parity"],
            }

        portfolio, res = self.repo.update_portfolio(_mut)
        if tracer:
            tracer.add_stage(
                stage="LEDGER_STATE_TRANSFORM",
                status="OK",
                input_data={"ticker": res["ticker"]},
                transformation=f"Updated {side_tag} Mini params for {res['ticker']}",
                output_data=res,
            )
        return {**res, "portfolio": portfolio}

    def calculate_mini_preview(
        self,
        ticker: str,
        side: str = "LONG",
        budget_sek: Optional[float] = None,
        risk_sek: Optional[float] = None,
        unit_price_sek: Optional[float] = None,
        stop_underlying: Optional[float] = None,
        spot: Optional[float] = None,
        usd_sek: float = 10.16,
    ) -> dict[str, Any]:
        """Pre-trade calculator powering the Web UI's instant SEK-to-units & 450 SEK 1R sizing."""
        portfolio = self.repo.load_portfolio()
        risk = self.repo.load_risk()
        sym = normalize_ticker(ticker)
        asset = portfolio.get("watchlist", {}).get(sym)
        if not asset:
            raise CommandInputError(f"Ticker '{ticker}' not found in watchlist.")

        side_tag = side.strip().upper()
        if side_tag not in ("LONG", "SHORT"):
            raise CommandInputError("Side must be LONG or SHORT.")
        is_short = side_tag == "SHORT"

        cur = asset.get("currency", "USD")
        fx = float(usd_sek) if cur == "USD" else 1.0
        parity = float(asset.get("mini_parity", 10.0)) or 10.0

        # Infer spot from latest audit if not supplied
        if spot is None or spot <= 0:
            latest_map = self.repo.get_latest_audit_by_ticker(max_scan_lines=300) if hasattr(self.repo, "get_latest_audit_by_ticker") else {}
            tel = (latest_map.get(sym) or {}).get("telemetry") or {}
            spot = float(tel.get("close", 0.0))
            if spot <= 0:
                ref_stop = float(asset.get("hard_stop_price", 0.0))
                ref_tp = float(asset.get("take_profit_price", 0.0))
                spot = round((ref_stop + ref_tp) / 2.0, 2) if (ref_stop > 0 and ref_tp > 0) else round(float(asset.get("mini_financing_level", 100.0)) * 1.25, 2)

        fin_lvl = float(asset.get("mini_short_financing_level") or round(spot * 1.22, 2)) if is_short else float(asset.get("mini_financing_level", 0.0))
        inst = asset.get("mini_short_instrument", f"MINI S {sym} AVA") if is_short else asset.get("mini_instrument", f"MINI L {sym} AVA")

        if fin_lvl > 0 and ((is_short and fin_lvl > spot) or (not is_short and spot > fin_lvl)):
            theo_sek = round((abs(spot - fin_lvl) * fx) / parity, 2)
            lev = round(spot / abs(spot - fin_lvl), 2)
        else:
            theo_sek = round((spot * 0.22 * fx) / parity, 2)
            lev = 4.5

        input_px = float(unit_price_sek) if (unit_price_sek and unit_price_sek > 0) else theo_sek
        stop_u = float(stop_underlying) if (stop_underlying and stop_underlying > 0) else (
            round(spot * 1.02, 2) if is_short else (
                float(asset["hard_stop_price"]) if (0 < float(asset.get("hard_stop_price", 0.0)) < spot) else round(spot * 0.98, 2)
            )
        )
        trig_sek = round(((fin_lvl - stop_u) * fx) / parity, 2) if (is_short and fin_lvl > stop_u) else (
            round(((stop_u - fin_lvl) * fx) / parity, 2) if (not is_short and stop_u > fin_lvl and fin_lvl > 0) else round(input_px * 0.92, 2)
        )
        sell_sek = round(max(0.01, trig_sek - 0.20), 2)
        per_unit_risk = round(max(input_px - sell_sek, 0.25), 2)

        max_risk_cap = float(risk_sek) if (risk_sek and risk_sek > 0) else float(risk.get("max_risk_per_trade_sek", 450.0))
        risk_max_units = max(1, int(max_risk_cap / per_unit_risk))
        avail_cash = float(portfolio.get("cash_reserves", {}).get("available_tactical_mini_sek", 0.0))
        cash_max_units = max(0, int(avail_cash / max(input_px, 0.01)))

        if budget_sek and budget_sek > 0:
            units = max(1, int(budget_sek / max(input_px, 0.01)))
        else:
            units = min(risk_max_units, cash_max_units) if cash_max_units > 0 else risk_max_units

        notional = round(units * input_px, 2)
        warnings: list[str] = []
        stats = portfolio.get("daily_stats", {})
        if int(stats.get("trades_executed_today", 0)) >= int(risk.get("max_daily_trades", 2)):
            warnings.append(f"Daily trade limit reached ({stats.get('trades_executed_today')}/{risk.get('max_daily_trades')}).")
        active_minis = sum(1 for a in portfolio.get("watchlist", {}).values() if int(a.get("mini_units_held", 0)) > 0)
        if active_minis >= int(risk.get("max_concurrent_leveraged_minis", 1)):
            warnings.append("Max 1 concurrent leveraged Mini position is already open.")
        if notional > avail_cash:
            warnings.append(f"Notional ({notional:.0f} SEK) exceeds available cash ({avail_cash:.0f} SEK).")
        if notional < 1000.0:
            warnings.append("Orders under 1,000 SEK incur Avanza courtage.")
        if round(units * per_unit_risk, 1) > max_risk_cap * 1.1:
            warnings.append(f"1R risk ({round(units * per_unit_risk):.0f} SEK) exceeds {max_risk_cap:.0f} SEK risk-parity limit.")

        return {
            "ticker": sym,
            "side": side_tag,
            "instrument": inst,
            "spot": round(spot, 2),
            "usd_sek": round(fx, 4),
            "theoretical_unit_sek": theo_sek,
            "effective_leverage": lev,
            "input_unit_price_sek": round(input_px, 2),
            "budget_sek": round(float(budget_sek), 2) if budget_sek else None,
            "units_from_budget": units,
            "notional_sek": notional,
            "courtage_free": notional >= 1000.0,
            "risk_parity_1r_cap_sek": max_risk_cap,
            "suggested_stop_underlying": stop_u,
            "stop_trigger_sek": trig_sek,
            "stop_sell_sek": sell_sek,
            "per_unit_risk_sek": per_unit_risk,
            "estimated_1r_risk_sek": round(units * per_unit_risk, 2),
            "risk_parity_max_units": risk_max_units,
            "preflight_warnings": warnings,
        }
