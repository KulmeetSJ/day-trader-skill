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
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

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


# All five NSE/BSE index derivatives this system has authoritative expiry/lot metadata for
# (per trader-alerts/instruments.py and the readiness check's "Derivatives & Expiry Metadata"
# section) -- previously only NIFTY/BANKNIFTY/FINNIFTY were scanned by default, silently omitting
# MIDCPNIFTY and SENSEX from every "scan the market" / "best trade" request.
MAJOR_INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]

DEFAULT_SCAN_UNIVERSE = MAJOR_INDICES + ["RELIANCE", "INFY", "HDFCBANK", "ICICIBANK", "TCS"]

# Rejection-reason prefixes that a human with more capital, a bigger risk budget, or looser
# directional conviction could plausibly override -- i.e. nothing structurally wrong with the
# data/contract/liquidity, just "didn't clear the configured bar today." A rejected candidate is
# shown in full in the report only if EVERY one of its reasons falls in this set; otherwise it's
# collapsed to a one-line count so the report highlights near-misses instead of burying them under
# candidates that failed for hard, non-negotiable reasons (missing data, naked-option prohibition,
# stale quotes, etc).
SOFT_REJECTION_PREFIXES = (
    "RISK_EXCEEDS_BUDGET", "DAILY_LOSS_LIMIT_EXCEEDED", "INSUFFICIENT_CAPITAL",
    "RR_BELOW_MINIMUM", "ZERO_LOT_ALLOCATION", "INSUFFICIENT_REMAINING_CAPITAL",
    "TRADE_RISK_EXCEEDS_BUDGET", "MAX_OPEN_POSITIONS_REACHED", "INSUFFICIENT_PORTFOLIO_CASH",
    "MAX_CORRELATED_EXPOSURE_EXCEEDED",
)


def _is_soft_rejection(reasons):
    """True only if every rejection reason is budget/capital/R:R-driven (see SOFT_REJECTION_PREFIXES)."""
    if not reasons:
        return False
    return all(any(r.startswith(p) for p in SOFT_REJECTION_PREFIXES) for r in reasons)


class ScanLockError(RuntimeError):
    """Raised when concurrent scan lock cannot be acquired or contention occurs."""
    pass


class ScanLock:
    """Atomic process-level file lock to prevent concurrent options scans from over-allocating portfolio risk.

    Acquisition uses atomic file creation with O_CREAT | O_EXCL.
    Stale lock recovery is supported if the owning PID is dead or lock age exceeds max_lock_age_seconds.
    """

    def __init__(
        self,
        lock_path: Optional[str] = None,
        max_lock_age_seconds: float = 120.0,
    ):
        self.lock_path = lock_path or os.path.join(BASE_DIR, "trader", "state", "options_scan.lock")
        self.max_lock_age_seconds = max_lock_age_seconds
        self.is_acquired = False
        self.fd = None
        self.lock_token = None

    def acquire(self) -> bool:
        """Atomically acquire the scan lock. Returns True on success, raises ScanLockError on failure."""
        lock_dir = os.path.dirname(self.lock_path)
        if lock_dir and not os.path.exists(lock_dir):
            try:
                os.makedirs(lock_dir, exist_ok=True)
            except Exception:
                pass

        token = f"{os.getpid()}_{time.time()}_{uuid.uuid4().hex[:8]}"
        try:
            self.fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
            payload = json.dumps({
                "pid": os.getpid(),
                "token": token,
                "timestamp": time.time(),
                "acquired_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            os.write(self.fd, payload.encode("utf-8"))
            self.lock_token = token
            self.is_acquired = True
            return True
        except FileExistsError:
            # Check if existing lock is stale
            if self._is_lock_stale():
                self._break_stale_lock()
                try:
                    self.fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
                    payload = json.dumps({
                        "pid": os.getpid(),
                        "token": token,
                        "timestamp": time.time(),
                        "acquired_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    })
                    os.write(self.fd, payload.encode("utf-8"))
                    self.lock_token = token
                    self.is_acquired = True
                    return True
                except FileExistsError:
                    pass

            raise ScanLockError(
                f"CONCURRENT_SCAN_BLOCKED: Another market intelligence scan is currently active "
                f"(lock held at {self.lock_path}). Concurrent risk allocation blocked."
            )
        except Exception as e:
            if isinstance(e, ScanLockError):
                raise
            raise ScanLockError(f"SCAN_LOCK_FAILED: Could not establish atomic scan lock: {str(e)}")

    def _is_lock_stale(self) -> bool:
        """Check if lock file is stale due to dead PID or ancient abandoned lock."""
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
            ts = float(data.get("timestamp") or 0.0)
            pid = int(data.get("pid") or 0)
            now_ts = time.time()
            # If PID belongs to a different process, check process liveness
            if pid > 0 and pid != os.getpid():
                try:
                    os.kill(pid, 0)
                    # Process is alive; valid active scan must not lose its lock
                    return False
                except OSError:
                    # Dead process PID; lock is safely stale
                    return True
            # Abandoned or ancient lock without live PID (> 1 hour)
            if (now_ts - ts) > 3600.0:
                return True
            return False
        except Exception:
            return True

    def _break_stale_lock(self) -> None:
        """Remove stale lock file safely if verified stale."""
        try:
            if os.path.exists(self.lock_path) and self._is_lock_stale():
                os.remove(self.lock_path)
        except Exception:
            pass

    def release(self) -> None:
        """Release lock and remove lockfile only if owned by this instance."""
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
        if self.is_acquired:
            try:
                if os.path.exists(self.lock_path):
                    # Verify lock ownership before removing to prevent deleting another process's lock
                    try:
                        with open(self.lock_path, "r", encoding="utf-8") as f:
                            data = json.loads(f.read())
                        is_owner = (
                            (self.lock_token and data.get("token") == self.lock_token)
                            or data.get("pid") == os.getpid()
                        )
                        if is_owner:
                            os.remove(self.lock_path)
                    except Exception:
                        pass
            except Exception:
                pass
            self.is_acquired = False

    def __enter__(self) -> "ScanLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def get_fo_scan_universe(include_stocks: bool = True) -> List[str]:
    """Dynamically load eligible Indian F&O universe (indices + liquid F&O stocks)."""
    core_indices = list(MAJOR_INDICES)
    if not include_stocks:
        return core_indices
    try:
        c = instruments.load_instrument_cache(max_age_days=7)
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
    return MAJOR_INDICES + ["RELIANCE", "INFY", "HDFCBANK", "ICICIBANK", "TCS", "SBIN", "BHARTIARTL", "LT", "TATAMOTORS"]


class MarketIntelligenceOrchestrator:
    """Coordinates specialists to deliver human-readable market intelligence."""

    def __init__(
        self,
        specialist: Optional[OptionsIntelligenceSpecialist] = None,
        streamer: Optional[Any] = None,
        portfolio_path: Optional[str] = None,
        broker_state_path: Optional[str] = None,
        lock_path: Optional[str] = None,
    ):
        self.portfolio_path = portfolio_path or os.path.join(BASE_DIR, "trader", "portfolio.json")
        self.broker_state_path = broker_state_path or os.path.join(BASE_DIR, "trader", "state", "broker.json")
        self.lock_path = lock_path or os.path.join(BASE_DIR, "trader", "state", "options_scan.lock")
        if specialist is not None:
            self.specialist = specialist
            if streamer is not None and getattr(self.specialist, "streamer", None) is None:
                self.specialist.streamer = streamer
        else:
            if streamer is None:
                try:
                    prov = data_in.get_provider(name="upstox")
                    if prov and hasattr(prov, "create_streamer") and callable(getattr(prov, "create_streamer", None)):
                        has_creds = bool(getattr(prov, "_analytics_token", None)) or (
                            hasattr(prov, "resolve_credentials")
                            and bool(prov.resolve_credentials(config=getattr(prov, "_config", None)))
                        ) or getattr(prov, "has_credentials", False)
                        if has_creds:
                            streamer = prov.create_streamer()
                except Exception:
                    streamer = None
            self.specialist = OptionsIntelligenceSpecialist(streamer=streamer)

        self.streamer = streamer or getattr(self.specialist, "streamer", None)

    def close(self) -> None:
        """Cleanly shutdown active streamer connection if open."""
        if self.streamer is not None and getattr(self.streamer, "is_connected", False):
            try:
                self.streamer.close()
            except Exception:
                pass
        if hasattr(self, "specialist") and hasattr(self.specialist, "streamer"):
            spec_streamer = getattr(self.specialist, "streamer", None)
            if spec_streamer is not None and spec_streamer is not self.streamer and getattr(spec_streamer, "is_connected", False):
                try:
                    spec_streamer.close()
                except Exception:
                    pass

    def __enter__(self) -> "MarketIntelligenceOrchestrator":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

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
        if re.search(r"\b(selling|sell|credit)\b", q):
            filter_strategy = "SELLING"
        elif re.search(r"\b(bearish|put|pe|short)\b", q):
            direction_bias = "BEARISH"
        elif re.search(r"\b(bullish|call|ce|long)\b", q):
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
        Protected by atomic ScanLock to prevent concurrent risk allocation and scan contention.
        """
        try:
            with ScanLock(lock_path=self.lock_path):
                return self._run_market_intelligence_locked(
                    query=query, universe=universe, top_n=top_n, budget=budget
                )
        except ScanLockError as sle:
            err_msg = str(sle)
            return {
                "status": "SCAN_BLOCKED",
                "reason": err_msg,
                "query": query,
                "session_status": "BLOCKED",
                "total_eligible": 0,
                "passed_to_scanner": 0,
                "successfully_scanned": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "coverage_pct": 0.0,
                "total_evaluated": 0,
                "total_signals_identified": 0,
                "underlying_summaries": {},
                "shortlist": [],
                "rejected_signals": [],
                "scan_errors": [err_msg],
                "report_markdown": f"### Scan Blocked\n\n{err_msg}",
            }

    def _run_market_intelligence_locked(
        self,
        query: str = "Scan the market for the best intraday opportunities.",
        universe: Optional[List[str]] = None,
        top_n: int = 3,
        budget: float = DEFAULT_MAX_LOSS_INR,
    ) -> Dict[str, Any]:
        """
        Internal implementation of market intelligence scan executed while holding ScanLock.
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

        # Ensure streamer is connected if available during market hours
        if self.streamer is not None and not getattr(self.streamer, "is_connected", False):
            try:
                self.streamer.connect()
            except Exception:
                pass
        if self.streamer is not None and getattr(self.specialist, "streamer", None) is None:
            self.specialist.streamer = self.streamer

        # 2. Determine target universe dynamically (DEF-03)
        if sym:
            eval_symbols = [sym]
        elif intent == "bearish_fo":
            eval_symbols = [s for s in get_fo_scan_universe(include_stocks=True) if s not in MAJOR_INDICES][:12]
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

        # Broad market regime: computed once per sweep (not once per symbol -- that would mean
        # re-fetching 5 indices' daily bars for every evaluated symbol). Non-blocking context,
        # threaded into every signal below via analyze_symbol_for_options(broad_regime=...).
        try:
            broad_regime = scan.get_broad_market_regime(market="IN", now=now_utc).get("canonical_regime")
        except Exception:
            broad_regime = None
        broad_regime = broad_regime or "UNCERTAIN"

        scan_errors = []
        try:
            scan_res = scan.scan_intraday(
                symbols=eval_symbols,
                now_utc=now_utc,
                strategies=scan_strategies,
                market="IN",
                interval="5m",
            )
            scanner_hits = scan_res.get("hits", [])
            for s_err in (scan_res.get("errors") or []):
                scan_errors.append(str(s_err))
        except Exception as e:
            scanner_hits = []
            scan_errors.append(f"Scanner exception: {str(e)}")

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
                    portfolio_path=self.portfolio_path,
                    broker_state_path=self.broker_state_path,
                    broad_regime=broad_regime,
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
        actionable_signals = [s for s in ranked_signals if s.get("is_actionable")]
        rejected_signals = [s for s in ranked_signals if not s.get("is_actionable")]
        initial_shortlist = actionable_signals[:top_n]

        # Allocate portfolio risk budget and capital across shortlist sequentially (N-4: Recalculate actionable state)
        shortlist, newly_rejected = self.allocate_portfolio_across_shortlist(
            shortlist=initial_shortlist,
            budget=budget,
            available_capital=self.specialist.available_capital,
        )
        rejected_signals.extend(newly_rejected)

        # Dynamic Universe Reporting Stats
        all_fo_symbols = get_fo_scan_universe(include_stocks=True)
        total_eligible = len(all_fo_symbols)
        passed_to_scanner = len(eval_symbols)
        successfully_scanned = len([s for s in eval_symbols if s in underlying_summaries and "error" not in underlying_summaries[s]])
        failed_count = len([s for s in eval_symbols if "error" in underlying_summaries.get(s, {})])
        skipped_count = max(0, total_eligible - passed_to_scanner)
        coverage_pct = round((passed_to_scanner / total_eligible) * 100.0, 1) if total_eligible > 0 else 0.0

        # 6. Format Executive Response
        report_text = self._format_report(
            query=query,
            session_label=session_label,
            underlying_summaries=underlying_summaries,
            shortlist=shortlist,
            rejected_signals=rejected_signals,
            total_eligible=total_eligible,
            passed_to_scanner=passed_to_scanner,
            successfully_scanned=successfully_scanned,
            failed_count=failed_count,
            skipped_count=skipped_count,
            coverage_pct=coverage_pct,
            total_signals=len(actionable_signals),
            scan_errors=scan_errors,
            broad_regime=broad_regime,
        )

        return {
            "query": query,
            "broad_market_regime": broad_regime,
            "session_status": session_label,
            "total_eligible": total_eligible,
            "passed_to_scanner": passed_to_scanner,
            "successfully_scanned": successfully_scanned,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "coverage_pct": coverage_pct,
            "total_evaluated": len(eval_symbols),
            "total_signals_identified": len(actionable_signals),
            "underlying_summaries": underlying_summaries,
            "shortlist": shortlist,
            "rejected_signals": rejected_signals,
            "scan_errors": scan_errors,
            "report_markdown": report_text,
        }

    @staticmethod
    def _correlation_group(underlying: str) -> str:
        """
        Correlation bucket for concentration limiting. All 5 major indices move together as one
        systemic market-risk bet (objectively true -- no data needed to assert this), so they
        share one group. Individual stocks get their own symbol as their group: this system has
        no verified per-stock sector classification data (checked trader-alerts/instruments.py --
        no sector field exists), so sector-level correlation (e.g. two different banking stocks)
        is NOT modeled here. Fabricating a sector map without real data would be exactly the kind
        of guess this project has repeatedly committed to not making.
        """
        return "BROAD_INDEX_NIFTY_FAMILY" if underlying in MAJOR_INDICES else underlying

    def allocate_portfolio_across_shortlist(
        self,
        shortlist: List[Dict[str, Any]],
        budget: float,
        available_capital: Optional[float] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Allocate portfolio risk budget and capital across shortlisted candidates sequentially (N-4),
        now also enforcing config's max_correlated_exposure_pct (previously declared but never
        enforced anywhere in trader-options -- see _correlation_group()'s docstring for scope).
        If a candidate receives 0 lots, marks is_actionable=False, signal_state='SIGNAL REJECTED',
        and moves candidate from shortlist to rejected_signals.
        """
        total_capital = available_capital if available_capital is not None else self.specialist.available_capital
        remaining_budget = budget
        remaining_capital = total_capital
        max_corr_pct = float(self.specialist.config.get("max_correlated_exposure_pct") or 40.0)
        group_cap_capital = total_capital * (max_corr_pct / 100.0)
        group_allocated_capital: Dict[str, float] = {}
        final_shortlist = []
        newly_rejected = []
        for sig in shortlist:
            ps = sig.get("position_sizing", {})
            net_risk_1_lot = ps.get("net_risk_per_lot_inr", 0.0)
            cap_req_1_lot = ps.get("capital_required_per_lot", 0.0)
            group = self._correlation_group(sig.get("underlying_instrument"))
            group_used = group_allocated_capital.get(group, 0.0)
            group_remaining = max(0.0, group_cap_capital - group_used)

            if net_risk_1_lot > 0:
                port_lots_by_risk = int(remaining_budget // net_risk_1_lot)
            else:
                port_lots_by_risk = 0
            if cap_req_1_lot > 0:
                port_lots_by_cap = int(remaining_capital // cap_req_1_lot)
                port_lots_by_corr = int(group_remaining // cap_req_1_lot)
            else:
                port_lots_by_cap = 0
                port_lots_by_corr = 0
            port_permitted_lots = max(0, min(port_lots_by_risk, port_lots_by_cap, port_lots_by_corr))
            initial_lots = ps.get("final_permitted_lots", 0)
            allocated_lots = min(initial_lots, port_permitted_lots)
            ps["portfolio_permitted_lots"] = port_permitted_lots
            ps["final_permitted_lots"] = allocated_lots
            ps["lots"] = allocated_lots
            ps["total_quantity"] = allocated_lots * ps.get("lot_size", 1)
            ps["correlation_group"] = group
            ps["correlation_group_limit_pct"] = max_corr_pct
            ps["correlation_group_capital_used_inr"] = round(group_used, 2)

            if allocated_lots <= 0:
                sig["is_actionable"] = False
                sig["signal_state"] = "SIGNAL REJECTED"
                reasons = list(sig.get("rejection_reasons") or [])
                if port_lots_by_risk <= 0:
                    reasons.append(
                        f"ZERO_LOT_ALLOCATION: Remaining portfolio risk budget (Rs {remaining_budget:,.2f}) insufficient for 1 lot (Rs {net_risk_1_lot:,.2f})"
                    )
                elif port_lots_by_corr <= 0 and port_lots_by_cap > 0:
                    reasons.append(
                        f"MAX_CORRELATED_EXPOSURE_EXCEEDED: Correlated group '{group}' already has Rs {group_used:,.2f} "
                        f"allocated against a {max_corr_pct:.0f}% of capital limit (Rs {group_cap_capital:,.2f}); "
                        f"no room left for 1 more lot (Rs {cap_req_1_lot:,.2f}) in this same group"
                    )
                elif port_lots_by_cap <= 0:
                    reasons.append(
                        f"INSUFFICIENT_REMAINING_CAPITAL: Remaining capital (Rs {remaining_capital:,.2f}) insufficient for 1 lot (Rs {cap_req_1_lot:,.2f})"
                    )
                else:
                    reasons.append("ZERO_LOT_ALLOCATION: Zero lots permitted after sequential portfolio allocation")
                sig["rejection_reasons"] = reasons
                newly_rejected.append(sig)
            else:
                allocated_risk = allocated_lots * net_risk_1_lot
                allocated_cap = allocated_lots * cap_req_1_lot
                remaining_budget = max(0.0, remaining_budget - allocated_risk)
                remaining_capital = max(0.0, remaining_capital - allocated_cap)
                group_allocated_capital[group] = group_used + allocated_cap
                final_shortlist.append(sig)

        return final_shortlist, newly_rejected

    def _format_report(
        self,
        query: str,
        session_label: str,
        underlying_summaries: Dict[str, Any],
        shortlist: List[Dict[str, Any]],
        rejected_signals: Optional[List[Dict[str, Any]]] = None,
        total_eligible: int = 230,
        passed_to_scanner: int = 12,
        successfully_scanned: int = 12,
        failed_count: int = 0,
        skipped_count: int = 218,
        coverage_pct: float = 5.2,
        total_signals: int = 0,
        total_evaluated: Optional[int] = None,
        scan_errors: Optional[List[str]] = None,
        broad_regime: Optional[str] = None,
    ) -> str:
        eval_count = total_evaluated if total_evaluated is not None else passed_to_scanner
        lines = [
            f"# INTRADAY TRADING INTELLIGENCE REPORT",
            f"**Session**: {session_label} | **Query**: *\"{query}\"* | **Broad Market Regime**: {broad_regime or 'UNCERTAIN'}",
            f"**Execution Boundary**: Analytical decision-support only. Live broker order placement remains disabled.",
            f"**On \"Score\" / ranking**: the Score ranking candidates below is a disclosed composite of net reward-to-risk, "
            f"risk structure (defined vs. undefined risk), and data freshness -- it is **not** a probability of profit. "
            f"No historical win-rate or backtested probability exists for live options premiums in this system; the #1-ranked "
            f"candidate is the one that best satisfies the stated risk rules today, not the one most likely to make money.\n",
            f"## 1. Market Overview & Universe Scanned",
            f"Evaluated **{eval_count}** key instruments (core scan universe out of {total_eligible} eligible F&O instruments, {(eval_count / total_eligible) * 100:.1f}% coverage). Discovered **{total_signals}** actionable options candidates.\n",
            f"- **Total Eligible F&O Universe**: {total_eligible} instruments",
            f"- **Passed to Scanner**: {passed_to_scanner} instruments",
            f"- **Successfully Scanned**: {successfully_scanned} instruments",
            f"- **Failed Scans**: {failed_count} instruments",
            f"- **Skipped (Beyond Evaluated Window)**: {skipped_count} instruments",
            f"- **Universe Coverage**: {coverage_pct}%\n",
            f"Discovered **{total_signals}** actionable options candidates across **{eval_count}** evaluated instruments.\n",
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
                    f"- **Regime Context**: *{(sig.get('regime_context') or {}).get('regime_note', 'N/A')}*",
                    f"- **Execution Price Reconciliation ({exec_title})**:",
                    f"  - {prem_label}: **₹{sig['current_premium']:.2f}** {prem_note}",
                    f"  - Conservative Baseline: `{recon.get('baseline_quote_type')}` ₹{recon.get('baseline_quote_price', 0):.2f} | Slippage Applied: {recon.get('slippage_applied', 0):+.2f}",
                    f"  - Order Book Quotes: Bid: ₹{recon.get('bid', 0):.2f} | Ask: ₹{recon.get('ask', 0):.2f} (Spread: ₹{recon.get('spread_points', 0):.2f} / {recon.get('spread_pct', 0)}%)",
                    f"  - Quote Verifiability: *{recon.get('executable_note', '')}*",
                    f"  - **VALID ENTRY RANGE: ₹{(sig.get('option_entry_range') or {}).get('min', 0):.2f} to ₹{(sig.get('option_entry_range') or {}).get('max', 0):.2f}** "
                    f"(ideal ₹{(sig.get('option_entry_range') or {}).get('ideal', 0):.2f}) -- *{(sig.get('option_entry_range') or {}).get('instruction', '')}*",
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
                    f"  - Correlated Exposure: group `{ps.get('correlation_group', 'N/A')}` -- "
                    f"₹{ps.get('correlation_group_capital_used_inr', 0):,.2f} already committed to this group "
                    f"before this trade, capped at {ps.get('correlation_group_limit_pct', 40):.0f}% of capital",
                    f"  - Tail Risk Disclosure: *{m_details.get('tail_risk')}*",
                    f"- **Auditable Data Provenance**:",
                    f"  - Provider: `{prov.get('data_provider')}` | Provenance: `{prov.get('quote_provenance', 'UPSTOX_REST_SNAPSHOT')}` | Tier: `{prov.get('data_tier')}` | Session: `{prov.get('market_session_status')}`",
                    f"  - Quote Timestamp: `{prov.get('exchange_quote_timestamp')}` (Age: {prov.get('quote_age_seconds')}s)",
                    f"  - Candle Timestamp: `{prov.get('underlying_candle_timestamp')}` (Age: {prov.get('candle_age_seconds')}s)",
                    f"  - Retrieval Timestamp: `{prov.get('retrieval_timestamp')}` (Age: {prov.get('retrieval_age_seconds')}s)",
                    f"  - Freshness Validation: `{prov.get('freshness_validation_result')}`",
                    f"- **Selection Rationale & Rejections**: {sig['selection_rationale']}\n",
                ])

        if rejected_signals:
            near_miss = [s for s in rejected_signals if _is_soft_rejection(s.get("rejection_reasons"))]
            hard_blocked = [s for s in rejected_signals if s not in near_miss]

            lines.append("## 3. Near-Miss Candidates (Diagnostic Audit)\n")
            lines.append(
                "Rejected only for budget, capital, or reward-to-risk reasons -- nothing structurally "
                "wrong with the data or contract. These are the ones worth a second look if you can "
                "flex budget or risk tolerance.\n"
            )
            if near_miss:
                lines.append("| Underlying | Strategy | Strike | Regime | Reasons |")
                lines.append("| :--- | :--- | :--- | :--- | :--- |")
                for r_sig in near_miss[:10]:
                    r_reasons = "; ".join(r_sig.get("rejection_reasons", []))
                    rc = r_sig.get("regime_context") or {}
                    regime_tag = "ALIGNED" if rc.get("regime_aligned") else ("MISALIGNED" if rc.get("regime_aligned") is False else "N/A")
                    lines.append(f"| **{r_sig.get('underlying_instrument')}** | `{r_sig.get('strategy')}` | {r_sig.get('strike')} | {regime_tag} | {r_reasons} |")
                if len(near_miss) > 10:
                    lines.append(f"\n*(+{len(near_miss) - 10} more near-miss candidates not shown)*")
            else:
                lines.append("*(None -- every rejected candidate failed for a structural reason, not just budget/R:R.)*")

            if hard_blocked:
                lines.append(
                    f"\n*{len(hard_blocked)} other candidate(s) rejected for structural reasons (missing/stale data, "
                    f"naked-option prohibition, liquidity, expiry-day cutoff, etc.) -- not shown individually; "
                    f"these would not become tradeable with more capital or risk tolerance.*"
                )
            lines.append("")
            checklist_section_num = 4
        else:
            checklist_section_num = 3

        lines.extend([
            f"## {checklist_section_num}. Human Trader Execution Checklist",
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
    ap.add_argument(
        "--indexes-only", action="store_true",
        help="Scan only the 5 major index derivatives (%s) instead of indexes + F&O stocks" % ", ".join(MAJOR_INDICES),
    )

    args = ap.parse_args(argv)
    query_str = " ".join(args.query) if isinstance(args.query, list) else str(args.query)
    universe = list(MAJOR_INDICES) if args.indexes_only else None

    with MarketIntelligenceOrchestrator() as orchestrator:
        res = orchestrator.run_market_intelligence(query=query_str, universe=universe, top_n=args.top, budget=args.budget)
        print(res["report_markdown"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
