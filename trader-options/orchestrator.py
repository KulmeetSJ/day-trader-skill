#!/usr/bin/env python3
"""
trader-options: Natural Language & Multi-Asset Market Intelligence Orchestrator.

Acts as a "second pair of eyes" for the human trader during live market sessions.
Orchestrates Market Clock, Sector Breadth, Intraday Scanner, and Options Specialist
to evaluate, rank, and communicate actionable trading opportunities in plain English
and structured contracts.
"""

import argparse
import datetime
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

for path in [
    os.path.join(BASE_DIR, "trader-alerts"),
    os.path.join(BASE_DIR, "trader-entry"),
    os.path.join(BASE_DIR, "trader-scanner"),
    os.path.join(BASE_DIR, "trader-budget"),
    os.path.join(BASE_DIR, "trader-options"),
]:
    if path not in sys.path:
        sys.path.insert(0, path)

import data_in
import instruments
import calendar_in
import scan
from options import OptionsIntelligenceSpecialist, DEFAULT_MAX_LOSS_INR


DEFAULT_SCAN_UNIVERSE = ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "INFY", "HDFCBANK", "ICICIBANK", "TCS"]


def get_fo_scan_universe(include_stocks: bool = True) -> List[str]:
    """Dynamically load eligible Indian F&O universe (indices + liquid F&O stocks)."""
    core_indices = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    if not include_stocks:
        return core_indices
    try:
        c = instruments.load_instrument_cache()
        dm = c.get("derivatives_meta", {})
        fo_syms = []
        for k in dm.keys():
            if ":" not in k and k.isalpha() and not any(k.startswith(curr) for curr in ["USD", "EUR", "GBP", "JPY", "GOLD", "SILVER", "CRUDE", "COPPER", "ZINC", "NATURALGAS"]):
                fo_syms.append(k)
        if fo_syms:
            combined = core_indices + [s for s in sorted(list(set(fo_syms))) if s not in core_indices]
            return combined
    except Exception:
        pass
    return ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "INFY", "HDFCBANK", "ICICIBANK", "TCS", "SBIN", "BHARTIARTL", "LT", "TATAMOTORS"]


class MarketIntelligenceOrchestrator:
    """Coordinates specialists to deliver human-readable market intelligence."""

    def __init__(self):
        self.specialist = OptionsIntelligenceSpecialist()

    def detect_intent(self, query: str) -> Dict[str, Any]:
        """Detect trader intent and parameters from natural language query."""
        q = query.lower().strip()
        intent = "scan_all"
        target_symbol = None
        filter_strategy = None
        direction_bias = None

        # Symbol extraction
        for sym in ["banknifty", "finnifty", "nifty", "reliance", "infy", "hdfcbank", "icicibank", "tcs"]:
            if sym in q:
                target_symbol = sym.upper()
                break

        # Strategy / direction extraction
        if "selling" in q or "sell" in q or "credit" in q:
            filter_strategy = "SELLING"
        elif "bearish" in q or "down" in q or "short" in q or "pe" in q:
            direction_bias = "BEARISH"
        elif "bullish" in q or "up" in q or "long" in q or "ce" in q:
            direction_bias = "BULLISH"

        if target_symbol:
            if filter_strategy == "SELLING":
                intent = "symbol_selling"
            else:
                intent = "symbol_analysis"
        elif direction_bias == "BEARISH" and ("f&o" in q or "stock" in q):
            intent = "bearish_fo"
        elif "compare" in q or "top" in q:
            intent = "compare_top"

        return {
            "intent": intent,
            "target_symbol": target_symbol,
            "filter_strategy": filter_strategy,
            "direction_bias": direction_bias,
        }

    def run_market_intelligence(
        self,
        query: str = "Scan the market for the best intraday opportunities.",
        universe: Optional[List[str]] = None,
        top_n: int = 3,
        budget: float = DEFAULT_MAX_LOSS_INR,
    ) -> Dict[str, Any]:
        """
        Execute full multi-specialist workflow based on natural language query or scan mode.
        """
        parsed = self.detect_intent(query)
        intent = parsed["intent"]
        sym = parsed["target_symbol"]
        bias = parsed["direction_bias"]
        selling_filter = parsed["filter_strategy"] == "SELLING"

        # 1. Market Context & Breadth
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        is_open = calendar_in.is_market_hours_in(now_utc)
        session_label = "LIVE TRADING SESSION" if is_open else "MARKET CLOSED (PRE/POST-MARKET REVIEW)"

        # 2. Determine target universe dynamically (DEF-03)
        if sym:
            eval_symbols = [sym]
        elif intent == "bearish_fo":
            eval_symbols = [s for s in get_fo_scan_universe(include_stocks=True) if s not in ("NIFTY", "BANKNIFTY", "FINNIFTY")][:12]
        elif universe:
            eval_symbols = universe
        else:
            eval_symbols = get_fo_scan_universe(include_stocks=True)[:12]

        # 3. DEF-03: Execute Actual Technical Scanner (scan.scan_intraday)
        scan_strategies = [
            "breakout_intraday",
            "breakdown_intraday",
            "vwap_reclaim",
            "vwap_rejection",
            "ema_alignment",
            "orb",
            "orb_breakdown",
        ]
        if bias == "BEARISH":
            scan_strategies = ["breakdown_intraday", "vwap_rejection", "orb_breakdown"]
        elif bias == "BULLISH":
            scan_strategies = ["breakout_intraday", "vwap_reclaim", "orb"]

        try:
            scan_res = scan.scan_intraday(
                symbols=eval_symbols,
                now_utc=now_utc,
                strategies=scan_strategies,
                market="IN",
                interval="5m",
            )
            scanner_hits = scan_res.get("hits", [])
        except Exception:
            scanner_hits = []

        hits_by_symbol = {}
        for h in scanner_hits:
            s_sym = h.get("symbol")
            if s_sym and s_sym not in hits_by_symbol:
                hits_by_symbol[s_sym] = h

        # 4. Evaluate Options Opportunities across Universe
        all_signals = []
        underlying_summaries = {}

        for s in eval_symbols:
            try:
                res = self.specialist.analyze_symbol_for_options(
                    symbol=s,
                    underlying_setup=hits_by_symbol.get(s),
                    max_loss_budget=budget,
                    now_utc=now_utc,
                )
                if res.get("status") == "OPPORTUNITIES_IDENTIFIED":
                    sigs = res.get("signals", [])
                    # Filter by strategy or direction if requested
                    if selling_filter:
                        sigs = [x for x in sigs if "SELL" in x["strategy"] or "SPREAD" in x["strategy"]]
                    elif bias == "BEARISH":
                        sigs = [x for x in sigs if x["direction"] in ("BEARISH", "BEARISH_OR_SIDEWAYS")]
                    elif bias == "BULLISH":
                        sigs = [x for x in sigs if x["direction"] in ("BULLISH", "BULLISH_OR_SIDEWAYS")]

                    all_signals.extend(sigs)
                    underlying_summaries[s] = {
                        "price": res.get("underlying_price"),
                        "atm": res.get("atm_strike"),
                        "pcr": res.get("pcr"),
                        "signal_count": len(sigs),
                    }
                else:
                    underlying_summaries[s] = {
                        "price": res.get("underlying_price"),
                        "atm": res.get("atm_strike"),
                        "pcr": res.get("pcr"),
                        "signal_count": 0,
                        "reason": res.get("reason", "No setups qualified"),
                    }
            except Exception as e:
                underlying_summaries[s] = {"error": str(e)}

        # 5. Rank and Deduplicate
        ranked_signals = self.specialist.rank_opportunities(all_signals)
        shortlist = ranked_signals[:top_n]

        # 6. Format Executive Response
        report_text = self._format_report(
            query=query,
            session_label=session_label,
            underlying_summaries=underlying_summaries,
            shortlist=shortlist,
            total_evaluated=len(eval_symbols),
            total_signals=len(ranked_signals),
        )

        return {
            "query": query,
            "session_status": session_label,
            "total_evaluated": len(eval_symbols),
            "total_signals_identified": len(ranked_signals),
            "underlying_summaries": underlying_summaries,
            "shortlist": shortlist,
            "report_markdown": report_text,
        }

    def _format_report(
        self,
        query: str,
        session_label: str,
        underlying_summaries: Dict[str, Any],
        shortlist: List[Dict[str, Any]],
        total_evaluated: int,
        total_signals: int,
    ) -> str:
        lines = [
            f"# INTRADAY TRADING INTELLIGENCE REPORT",
            f"**Session**: {session_label} | **Query**: *\"{query}\"*",
            f"**Execution Boundary**: Analytical decision-support only. Live broker order placement remains disabled.\n",
            f"## 1. Market Overview & Universe Scanned",
            f"Evaluated **{total_evaluated}** key instruments (core scan universe out of 230 eligible F&O instruments, {(total_evaluated / 230) * 100:.1f}% coverage). Discovered **{total_signals}** actionable options candidates.\n",
            f"| Underlying | Spot Price (₹) | ATM Strike | PCR (OI) | Available Setups |",
            f"| :--- | :--- | :--- | :--- | :--- |",
        ]

        for s, info in underlying_summaries.items():
            if "error" in info:
                lines.append(f"| **{s}** | Data Error | - | - | `{info['error']}` |")
            else:
                p_str = f"{info['price']:.2f}" if info.get("price") is not None else "-"
                a_str = f"{info['atm']}" if info.get("atm") is not None else "-"
                pcr_str = f"{info['pcr']:.2f}" if info.get("pcr") is not None else "-"
                cnt = info.get("signal_count", 0)
                reason_str = f"{cnt} setups" if cnt > 0 else f"`{info.get('reason', 'No trade')}`"
                lines.append(f"| **{s}** | {p_str} | {a_str} | {pcr_str} | {reason_str} |")

        lines.append(f"\n## 2. Top Actionable Opportunities (Ranked Shortlist)\n")

        if not shortlist:
            lines.append("> [!NOTE]\n> No qualified opportunities met liquidity and risk criteria for the requested filters.")
        else:
            for i, sig in enumerate(shortlist, 1):
                m_details = sig.get("margin_details", {})
                is_undef = m_details.get("is_undefined_risk", False)
                struct = m_details.get("structure", "")
                if is_undef:
                    risk_badge = "⚠️ THEORETICALLY UNLIMITED RISK (NAKED CALL)"
                elif struct == "CAPPED_MAX_LOSS_NAKED":
                    risk_badge = "⚠️ SEVERE TAIL RISK (CAPPED NAKED PUT)"
                else:
                    risk_badge = "🛡️ DEFINED RISK"

                evidence = sig.get("underlying_setup_evidence") or {}
                trig = evidence.get("entry_trigger") or {}
                recon = sig.get("execution_reconciliation") or {}
                prov = sig.get("data_provenance") or {}

                ps = sig['position_sizing']
                is_exec = recon.get("is_executable", False)
                exec_title = "EXECUTABLE REAL-TIME" if is_exec else "HISTORICAL INDICATIVE -- NOT EXECUTABLE"
                prem_label = "Executable Entry Premium" if is_exec else "Indicative Reference Premium"
                prem_note = "*(Live verified)*" if is_exec else "*(Post-market closing snapshot -- not live)*"

                lines.extend([
                    f"### #{i}: {sig['underlying_instrument']} -- `{sig['strategy']}` ({risk_badge})",
                    f"- **Contract**: `{sig['contract']}` | **Expiry**: `{sig['expiry']}` | **Strike**: {sig['strike']}",
                    f"- **Direction**: **{sig['direction']}** | **Signal State**: `{sig['signal_state']}`",
                    f"- **Underlying Setup Evidence**:",
                    f"  - Setup: `{evidence.get('setup_name')}` ({evidence.get('scanner_result')}) [Setup Validity: `{evidence.get('setup_validity_state', 'N/A')}`]",
                    f"  - Trigger Level: Trigger Price ₹{trig.get('price', 0):.2f} @ {trig.get('time')} ({trig.get('reason')})",
                    f"  - Latest Available Candle: Spot ₹{trig.get('current_spot', 0):.2f} @ {trig.get('latest_candle_time', 'N/A')}",
                    f"  - Invalidation Level: Spot ₹{evidence.get('invalidation_level', 0):.2f} | Support/Resistance: Spot ₹{evidence.get('support_resistance_level', 0):.2f}",
                    f"  - Strike Selection Rationale: *{evidence.get('strike_selection_rationale')}*",
                    f"- **Execution Price Reconciliation ({exec_title})**:",
                    f"  - {prem_label}: **₹{sig['current_premium']:.2f}** {prem_note}",
                    f"  - Conservative Baseline: `{recon.get('baseline_quote_type')}` ₹{recon.get('baseline_quote_price', 0):.2f} | Slippage Applied: {recon.get('slippage_applied', 0):+.2f}",
                    f"  - Order Book Quotes: Bid: ₹{recon.get('bid', 0):.2f} | Ask: ₹{recon.get('ask', 0):.2f} (Spread: ₹{recon.get('spread_points', 0):.2f} / {recon.get('spread_pct', 0)}%)",
                    f"  - Quote Verifiability: *{recon.get('executable_note', '')}*",
                    f"- **Premium Exit Modeling & Net R:R**:",
                    f"  - Stop Loss: **₹{sig['stop_loss']:.2f}** | Target 1: ₹{sig['target_1']:.2f} | Target 2: ₹{sig['target_2']:.2f}",
                    f"  - Net Risk-to-Reward: **{sig['expected_risk_reward_after_charges']}:1** *(Net of STT, NSE charges, SEBI, Stamp Duty, GST, Brokerage)*",
                    f"- **Risk Reporting & Stop-Loss vs. Maximum Loss**:",
                    f"  - Stop-Loss Risk (Modeled Exit at ₹{sig['stop_loss']:.2f}):",
                    f"    - Per-Lot Stop Risk: **₹{ps.get('stop_loss_risk_per_lot_inr', ps.get('net_risk_per_lot_inr', 0)):,.2f}**",
                    f"    - Total Stop Risk ({ps['lots']} lots): **₹{ps.get('total_stop_loss_risk_inr', ps.get('actual_net_risk_inr', 0)):,.2f}** *(Within ₹{ps['budget_max_loss_inr']:,.0f} stop risk budget)*",
                    f"  - Maximum Possible Loss (Worst-Case Gap / Total Capital at Risk):",
                    f"    - **{sig['maximum_loss']}**",
                    f"  - Risk Budget Policy Notice:",
                    f"    - *{ps.get('risk_budget_policy_explanation', 'Position sized to stop-loss risk. Maximum loss is substantially higher in adverse gap/worthless expiry scenarios.')}*",
                    f"- **Margin & Capital Transparency**:",
                    f"  - Capital / Margin Outlay: ₹{sig['capital_margin_requirement']:,.2f} [`{m_details.get('margin_type', 'HEURISTIC_MARGIN_ESTIMATE')}`]",
                    f"  - Margin Calculation Model: *{m_details.get('formula')}* ({m_details.get('assumptions')})",
                    f"  - Broker Margin Note: *{m_details.get('broker_margin_disclaimer')}*",
                    f"  - Position Sizing: {ps['lots']} lot(s) ({ps['total_quantity']} qty) "
                    f"[Theoretical: {ps.get('theoretical_lots', ps['lots'])} lots, "
                    f"Risk Budget Permitted: {ps.get('risk_budget_permitted_lots', ps['lots'])} lots, "
                    f"Capital Permitted: {ps.get('capital_permitted_lots', ps['lots'])} lots, "
                    f"Final Permitted: {ps.get('final_permitted_lots', ps['lots'])} lots]",
                    f"  - Tail Risk Disclosure: *{m_details.get('tail_risk')}*",
                    f"- **Auditable Data Provenance**:",
                    f"  - Provider: `{prov.get('data_provider')}` | Provenance: `{prov.get('quote_provenance', 'UPSTOX_REST_SNAPSHOT')}` | Tier: `{prov.get('data_tier')}` | Session: `{prov.get('market_session_status')}`",
                    f"  - Quote Timestamp: `{prov.get('exchange_quote_timestamp')}` (Age: {prov.get('quote_age_seconds')}s)",
                    f"  - Candle Timestamp: `{prov.get('underlying_candle_timestamp')}` (Age: {prov.get('candle_age_seconds')}s)",
                    f"  - Retrieval Timestamp: `{prov.get('retrieval_timestamp')}` (Age: {prov.get('retrieval_age_seconds')}s)",
                    f"  - Freshness Validation: `{prov.get('freshness_validation_result')}`",
                    f"- **Selection Rationale & Rejections**: {sig['selection_rationale']}\n",
                ])

        lines.extend([
            f"## 3. Human Trader Execution Checklist",
            f"1. **Confirm Trend & Breadth**: Verify index momentum and PCR bias prior to entry.",
            f"2. **Check Spread Freshness**: Ensure real-time bid-ask spread is within 5% before sending order.",
            f"3. **Position Size Enforcement**: Never exceed your configured max loss per trade.",
        ])

        return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="trader-options: Market Intelligence Orchestrator")
    ap.add_argument("query", nargs="*", default=["Scan the market for the best intraday opportunities."], help="Natural language request")
    ap.add_argument("--top", "-n", type=int, default=3, help="Number of ranked opportunities to return")
    ap.add_argument("--budget", type=float, default=DEFAULT_MAX_LOSS_INR, help="Max loss risk budget in INR")

    args = ap.parse_args(argv)
    query_str = " ".join(args.query) if isinstance(args.query, list) else str(args.query)

    orchestrator = MarketIntelligenceOrchestrator()
    res = orchestrator.run_market_intelligence(query=query_str, top_n=args.top, budget=args.budget)
    print(res["report_markdown"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
