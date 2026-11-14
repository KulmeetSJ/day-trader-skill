"""Regression coverage for India paper-only and risk approval integrity."""

import copy
import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from _load import load, ROOT

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
budget_size = load("trader-budget", "size")

TODAY = "2026-10-19"
NOW = "2026-10-19 10:15"


def us_config():
    with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
        return json.load(fh)


def us_order():
    with open(os.path.join(ROOT, "tests", "fixtures", "trader-risk", "orders-tsla.json"), encoding="utf-8") as fh:
        return copy.deepcopy(json.load(fh)["orders"][0])


def in_order():
    return {
        "symbol": "RELIANCE", "market": "IN", "exchange": "NSE", "currency": "INR",
        "asset_type": "EQUITY", "side": "buy", "qty": "1", "type": "market",
        "time_in_force": "day", "order_class": "bracket", "product": "MIS",
        "tick_size": "0.05", "stop_loss_stop_price": "95.00",
        "take_profit_limit_price": "110.00",
        "plan": {"entry": "100.00", "stop": "95.00", "target": "110.00", "exchange": "NSE"},
    }


def costed_in_order(target=120.0):
    order = in_order()
    order["qty"] = "100"
    order["take_profit_limit_price"] = "%.2f" % target
    order["plan"].update({"entry": "100.00", "stop": "95.00", "target": "%.2f" % target})
    order["transaction_costs"] = budget_size.calculate_indian_transaction_charges(
        asset_type="EQUITY", side="buy", entry_price=100.0, exit_price=target,
        qty=100, trading_style="intraday", as_of_date=TODAY,
    )
    order["transaction_costs_at_stop"] = budget_size.calculate_indian_transaction_charges(
        asset_type="EQUITY", side="buy", entry_price=100.0, exit_price=95.0,
        qty=100, trading_style="intraday", as_of_date=TODAY,
    )
    order["transaction_costs"]["currency"] = "INR"
    order["transaction_costs_at_stop"]["currency"] = "INR"
    return order


def indian_risk_config(**overrides):
    cfg = {
        "market": "IN", "currency": "INR", "exchange": "NSE", "broker": "paper",
        "paper": True, "budget": 100000, "cash_buffer_pct": 0,
        "max_position_pct": 100, "max_order_inr": 100000,
        "min_order_inr": 1, "max_loss_per_trade_inr": 1000,
        "max_daily_loss_inr": 5000, "min_reward_risk": 2,
        "require_stop": True, "no_entries_before": "09:30", "no_entries_after": "15:00",
        "allow_off_hours": True,
    }
    cfg.update(overrides)
    return cfg


def in_risk(order, cfg):
    identity = check.order_fingerprint.resolved_identity(order)
    return {"date": TODAY, "results": [{
        "index": 0, "symbol": order["symbol"], "side": order["side"],
        "fingerprint": check.fingerprint(order, config=cfg, market="IN", identity=identity),
        "resolved_identity": identity, "verdict": "PASS", "kind": "entry",
        "entry_ref": 100.0, "stop": 95.0, "target": 110.0, "max_loss": 5.0,
        "reasons": [], "warnings": [],
    }]}


class ProtectiveStopTests(unittest.TestCase):
    def test_plan_only_stop_cannot_pass_when_required(self):
        order = us_order()
        for key in ("order_class", "stop_loss_stop_price", "stop_loss_limit_price", "take_profit_limit_price"):
            order.pop(key, None)
        risk = check.check([order], us_config(), now=check.parse_now(NOW))
        self.assertEqual(risk["results"][0]["verdict"], "BLOCK")
        self.assertIn("stop: no stop", " ".join(risk["results"][0]["reasons"]))
        self.assertIsNone(risk["results"][0]["stop"])
        self.assertIsNone(risk["results"][0]["max_loss"])

    def test_broker_supported_bracket_protective_stop_passes(self):
        order = us_order()
        risk = check.check([order], us_config(), now=check.parse_now(NOW))
        self.assertEqual(risk["results"][0]["verdict"], "PASS", risk["results"][0]["reasons"])
        self.assertEqual(risk["results"][0]["stop"], float(order["stop_loss_stop_price"]))
        self.assertEqual(risk["results"][0]["max_loss"], 46.88)

    def test_orphaned_stop_field_without_supported_order_class_cannot_pass(self):
        order = us_order()
        order["order_class"] = "advanced"
        risk = check.check([order], us_config(), now=check.parse_now(NOW))
        self.assertEqual(risk["results"][0]["verdict"], "BLOCK")
        self.assertIsNone(risk["results"][0]["stop"])
        self.assertIsNone(risk["results"][0]["max_loss"])

    def test_ticket_rejects_risk_result_without_order_stop(self):
        order = us_order()
        cfg = us_config()
        risk = check.check([order], cfg, now=check.parse_now(NOW))
        no_stop = copy.deepcopy(order)
        for key in ("order_class", "stop_loss_stop_price", "stop_loss_limit_price", "take_profit_limit_price"):
            no_stop.pop(key, None)
        risk["results"][0]["fingerprint"] = check.fingerprint(
            no_stop, config=cfg, market="US", identity=risk["results"][0]["resolved_identity"])
        risk["results"][0]["stop"] = float(order["stop_loss_stop_price"])
        ready, label, _ = ticket.status_of(no_stop, risk, {"orders": []}, TODAY, cfg=cfg, market="US")
        self.assertFalse(ready)
        self.assertIn("no protective stop in the actual order", label)


class CanonicalFingerprintTests(unittest.TestCase):
    def test_financial_decimal_spellings_are_canonical(self):
        order = us_order()
        other = copy.deepcopy(order)
        other["stop_loss_stop_price"] = "358.3200"
        other["plan"]["entry"] = "364.1800"
        cfg = us_config()
        self.assertEqual(check.fingerprint(order, cfg, "US"), ticket.fingerprint(other, cfg, "US"))

    def test_each_risk_sensitive_mutation_invalidates_risk_pass(self):
        order = us_order()
        cfg = us_config()
        risk = check.check([order], cfg, now=check.parse_now(NOW))
        ready, _, _ = ticket.status_of(order, risk, {"orders": []}, TODAY, cfg=cfg, market="US")
        self.assertTrue(ready)
        variants = []
        for path, value in (
            (("plan", "stop"), "358.31"),
            (("plan", "target"), "370.05"),
            (("exchange",), "BSE"),
            (("tick_size",), "0.005"),
            (("derivative_identity",), {"underlying": "NIFTY", "expiry": "2026-10-29",
                                         "strike": "25000", "option_type": "CE", "lot_size": 65}),
        ):
            changed = copy.deepcopy(order)
            if len(path) == 1:
                changed[path[0]] = value
            else:
                changed[path[0]][path[1]] = value
            variants.append(changed)
        for changed in variants:
            ready, label, _ = ticket.status_of(changed, risk, {"orders": []}, TODAY, cfg=cfg, market="US")
            self.assertFalse(ready, changed)
            self.assertTrue(label.startswith(("NOT READY:", "BLOCKED:")), label)

    def test_shared_risk_and_broker_hashes_include_policy_and_identity(self):
        order = us_order()
        cfg = us_config()
        identity = check.order_fingerprint.resolved_identity(order)
        self.assertEqual(check.fingerprint(order, cfg, "US", identity),
                         ticket.fingerprint(order, cfg, "US", identity))
        self.assertNotEqual(check.fingerprint(order, cfg, "US", identity),
                            ticket.fingerprint(order, dict(cfg, max_loss_per_trade_usd=49), "US", identity))
        self.assertNotEqual(check.fingerprint(order, cfg, "US", identity),
                            ticket.fingerprint(order, cfg, "US", dict(identity, instrument_key="different")))

    def test_risk_cli_invocation_overrides_survive_broker_fingerprint_check(self):
        order = us_order()
        cfg = us_config()
        risk = check.check([order], cfg, now=check.parse_now(NOW),
                           fingerprint_overrides={"allow_off_hours": True, "exchange": "NASDAQ"})
        ready, label, _ = ticket.status_of(order, risk, {"orders": []}, TODAY,
                                           cfg=cfg, market="US")
        self.assertTrue(ready, label)


class IndianPaperOnlyTests(unittest.TestCase):
    def test_mis_render_and_json_never_emit_external_tool_for_any_broker_setting(self):
        order = in_order()
        for broker in ("paper", "alpaca", "ibkr", "paper_in", "simulated", "mock"):
            cfg = {"market": "IN", "currency": "INR", "paper": True, "broker": broker,
                   "budget": 100000, "min_reward_risk": 0}
            risk = in_risk(order, dict(cfg, broker="paper"))
            # Even an explicit US override cannot reclassify the INR order.
            rendered, _ = ticket.render(order, 1, 1, cfg, risk, {"orders": []}, TODAY,
                                        market="US", now=check.parse_now(NOW))
            payload = ticket.ticket_json(order, 1, cfg, risk, {"orders": []}, TODAY, market="US")
            self.assertNotIn("place_stock_order", rendered, broker)
            self.assertNotEqual(payload["tool"], "place_stock_order")
            self.assertFalse(payload["tool_input"] is None and broker == "paper")
            if broker == "paper":
                self.assertIn("Paper MIS simulation", rendered)
                self.assertEqual(payload["tool"], "paper_mis")
            else:
                self.assertIn("BLOCKED CONFIG", rendered)
                self.assertIsNone(payload["tool"])
                self.assertIsNone(payload["tool_input"])


class ExternalIdentityBoundaryTests(unittest.TestCase):
    def test_unclassified_bare_ticker_cannot_render_or_approve_external_order(self):
        order = us_order()
        for key in ("market", "currency", "asset_type", "exchange", "product", "instrument_key"):
            order.pop(key, None)
        for key in ("market", "currency", "asset_type", "exchange", "product", "instrument_key"):
            order.get("plan", {}).pop(key, None)
        cfg = us_config()
        risk = check.check([order], cfg, now=check.parse_now(NOW))
        self.assertEqual(risk["results"][0]["verdict"], "PASS")
        ready, label, _ = ticket.status_of(order, risk, {"orders": []}, TODAY, cfg=cfg, market="US")
        self.assertFalse(ready)
        self.assertIn("explicit order market='US'", label)
        rendered, _ = ticket.render(order, 1, 1, cfg, risk, {"orders": []}, TODAY,
                                    market="US", now=check.parse_now(NOW))
        payload = ticket.ticket_json(order, 1, cfg, risk, {"orders": []}, TODAY, market="US")
        self.assertNotIn("place_stock_order", rendered)
        self.assertIn("No external order payload prepared", rendered)
        self.assertIsNone(payload["tool"])
        self.assertIsNone(payload["tool_input"])

        with tempfile.TemporaryDirectory() as temp:
            order_path = os.path.join(temp, "orders.json")
            with open(order_path, "w", encoding="utf-8") as fh:
                json.dump({"orders": [order]}, fh)
            args = type("Args", (), {"index": 1, "reply": "yes", "orders": order_path,
                                      "state": os.path.join(temp, "state.json"), "now": None})()
            with patch.object(ticket, "_load", return_value=([order], cfg, risk, {"orders": []},
                                                              TODAY, check.parse_now(NOW), "US")), \
                    patch.object(ticket, "write_json") as writer:
                with self.assertRaises(ticket.TicketError):
                    ticket.cmd_approve(args)
                writer.assert_not_called()


class IndianNetRewardRiskTests(unittest.TestCase):
    def evaluate(self, order, cfg=None):
        return check.check([order], cfg or indian_risk_config(), now=check.parse_now(NOW), market="IN")

    def test_valid_indian_estimates_pass_and_report_gross_and_net_risk(self):
        order = costed_in_order(target=120.0)
        result = self.evaluate(order)
        row = result["results"][0]
        self.assertEqual(row["verdict"], "PASS", row["reasons"])
        analysis = row["reward_risk_analysis"]
        self.assertEqual(analysis["gross_reward_risk"], 4.0)
        self.assertGreater(analysis["target_charges"], 0)
        self.assertGreater(analysis["stop_charges"], 0)
        self.assertGreater(analysis["net_reward"], 0)
        self.assertGreater(analysis["net_risk"], 0)
        self.assertGreaterEqual(analysis["net_reward_risk"], 2.0)
        self.assertTrue(analysis["minimum_2r_passed"])
        report = check.report(result)
        self.assertIn("gross reward:risk", report)
        self.assertIn("target charges ₹", report)
        self.assertIn("stop charges ₹", report)
        self.assertIn("minimum 2.00: PASS", report)

    def test_indian_net_reward_risk_below_two_is_blocked(self):
        row = self.evaluate(costed_in_order(target=110.0))["results"][0]
        self.assertEqual(row["verdict"], "BLOCK")
        self.assertLess(row["reward_risk_analysis"]["net_reward_risk"], 2.0)
        self.assertFalse(row["reward_risk_analysis"]["minimum_2r_passed"])
        self.assertTrue(any("net " in reason and "under the 2.00 minimum" in reason for reason in row["reasons"]))

    def test_missing_both_or_either_indian_cost_estimate_blocks(self):
        for missing in ((), ("transaction_costs",), ("transaction_costs_at_stop",)):
            with self.subTest(missing=missing):
                order = costed_in_order()
                for key in missing or ("transaction_costs", "transaction_costs_at_stop"):
                    order.pop(key, None)
                row = self.evaluate(order)["results"][0]
                self.assertEqual(row["verdict"], "BLOCK")
                self.assertFalse(row["reward_risk_analysis"]["minimum_2r_passed"])
                self.assertTrue(any("transaction-cost estimate" in reason for reason in row["reasons"]))

    def test_negative_malformed_and_nonfinite_estimates_block(self):
        for field, value in (("stt", -0.01), ("gst", "not-a-number"), ("total", "Infinity")):
            with self.subTest(field=field, value=value):
                order = costed_in_order()
                order["transaction_costs"][field] = value
                row = self.evaluate(order)["results"][0]
                self.assertEqual(row["verdict"], "BLOCK")
                self.assertTrue(any("transaction-cost estimate" in reason for reason in row["reasons"]))

    def test_order_or_estimate_currency_mismatch_blocks(self):
        order = costed_in_order()
        order["transaction_costs"]["currency"] = "USD"
        row = self.evaluate(order)["results"][0]
        self.assertEqual(row["verdict"], "BLOCK")
        self.assertTrue(any("currency" in reason for reason in row["reasons"]))

        order = costed_in_order()
        order["currency"] = "USD"
        row = self.evaluate(order)["results"][0]
        self.assertEqual(row["verdict"], "BLOCK")
        self.assertTrue(any("currency" in reason for reason in row["reasons"]))

    def test_cost_estimate_mutations_invalidate_prior_ticket_approval(self):
        order = costed_in_order()
        cfg = indian_risk_config()
        risk = check.check([order], cfg, now=check.parse_now(NOW), market="IN")
        self.assertEqual(risk["results"][0]["verdict"], "PASS", risk["results"][0]["reasons"])
        ready, label, _ = ticket.status_of(order, risk, {"orders": []}, TODAY, cfg=cfg, market="IN")
        self.assertTrue(ready, label)

        for key in ("transaction_costs", "transaction_costs_at_stop"):
            with self.subTest(key=key):
                changed = copy.deepcopy(order)
                changed[key]["total"] = str(float(changed[key]["total"]) + 0.01)
                ready, label, _ = ticket.status_of(changed, risk, {"orders": []}, TODAY,
                                                   cfg=cfg, market="IN")
                self.assertFalse(ready)
                self.assertTrue(label.startswith(("NOT READY:", "BLOCKED:")), label)

    def test_none_order_returns_validation_block_instead_of_exception(self):
        result = check.check([None], indian_risk_config(), now=check.parse_now(NOW), market="IN")
        self.assertEqual(result["block"], 1)
        self.assertEqual(result["results"][0]["verdict"], "BLOCK")
        self.assertIn("order must be an object", result["results"][0]["reasons"][0])

    def test_us_order_does_not_require_indian_charge_estimates(self):
        order = us_order()
        order.pop("transaction_costs", None)
        order.pop("transaction_costs_at_stop", None)
        row = check.check([order], us_config(), now=check.parse_now(NOW))["results"][0]
        self.assertEqual(row["verdict"], "PASS", row["reasons"])
        self.assertIsNone(row["reward_risk_analysis"]["target_charges"])

    def test_explicitly_identified_us_order_retains_external_three_gate_path(self):
        order = us_order()
        order.update({"market": "US", "currency": "USD", "asset_type": "EQUITY", "exchange": "NASDAQ"})
        cfg = us_config()
        risk = check.check([order], cfg, now=check.parse_now(NOW))
        ready, label, _ = ticket.status_of(order, risk, {"orders": []}, TODAY, cfg=cfg, market="US")
        self.assertTrue(ready, label)
        self.assertEqual(risk["results"][0]["resolved_identity"]["validation_status"],
                         "RISK_VALIDATED_US_EQUITY")
        rendered, _ = ticket.render(order, 1, 1, cfg, risk, {"orders": []}, TODAY,
                                    market="US", now=check.parse_now(NOW))
        payload = ticket.ticket_json(order, 1, cfg, risk, {"orders": []}, TODAY, market="US")
        self.assertIn("Claude will call place_stock_order", rendered)
        self.assertEqual(payload["tool"], "place_stock_order")
        self.assertIsNotNone(payload["tool_input"])

    def test_indian_mis_and_stripped_or_contradictory_markers_never_use_us_route(self):
        explicit = in_order()
        cfg = {"market": "IN", "currency": "INR", "paper": True, "broker": "paper",
               "budget": 100000, "min_reward_risk": 0}
        risk = in_risk(explicit, cfg)
        rendered, _ = ticket.render(explicit, 1, 1, cfg, risk, {"orders": []}, TODAY, market="US")
        payload = ticket.ticket_json(explicit, 1, cfg, risk, {"orders": []}, TODAY, market="US")
        self.assertNotIn("place_stock_order", rendered)
        self.assertEqual(payload["market"], "IN")
        self.assertEqual(payload["tool"], "paper_mis")

        stripped = copy.deepcopy(explicit)
        for key in ("market", "exchange", "currency", "product", "asset_type"):
            stripped.pop(key, None)
            stripped.get("plan", {}).pop(key, None)
        stripped_cfg = {"paper": True, "broker": "alpaca"}
        stripped_payload = ticket.ticket_json(stripped, 1, stripped_cfg, None, {"orders": []}, TODAY, market="US")
        stripped_render, _ = ticket.render(stripped, 1, 1, stripped_cfg, None, {"orders": []}, TODAY, market="US")
        self.assertIsNone(stripped_payload["tool"])
        self.assertNotIn("place_stock_order", stripped_render)

        contradictory = copy.deepcopy(explicit)
        contradictory.update({"market": "US", "currency": "INR", "exchange": "NSE", "product": "MIS"})
        contradictory_payload = ticket.ticket_json(contradictory, 1, cfg, risk, {"orders": []}, TODAY, market="US")
        contradictory_render, _ = ticket.render(contradictory, 1, 1, cfg, risk, {"orders": []}, TODAY, market="US")
        self.assertEqual(contradictory_payload["market"], "IN")
        self.assertEqual(contradictory_payload["tool"], "paper_mis")
        self.assertNotIn("place_stock_order", contradictory_render)

    def test_nse_exchange_cannot_be_reclassified_as_us_by_override(self):
        order = in_order()
        order.pop("market")
        order.pop("currency")
        cfg = {"paper": True, "broker": "alpaca"}
        rendered, _ = ticket.render(order, 1, 1, cfg, None, {"orders": []}, TODAY, market="US")
        payload = ticket.ticket_json(order, 1, cfg, None, {"orders": []}, TODAY, market="US")
        self.assertNotIn("place_stock_order", rendered)
        self.assertEqual(payload["market"], "IN")
        self.assertIsNone(payload["tool"])
        self.assertIsNone(payload["tool_input"])

    def test_cnc_and_nrml_outputs_are_also_paper_only(self):
        cfg = {"market": "IN", "currency": "INR", "paper": True, "broker": "paper"}
        for product in ("CNC", "NRML"):
            order = in_order()
            order.update({"product": product, "trading_style": "positional", "order_class": "gtt_oco",
                          "time_in_force": "gtc",
                          "multi_session_protection": {"persists_across_sessions": True}})
            rendered, _ = ticket.render(order, 1, 1, cfg, None, {"orders": []}, TODAY, market="IN")
            payload = ticket.ticket_json(order, 1, cfg, None, {"orders": []}, TODAY, market="IN")
            self.assertNotIn("place_stock_order", rendered)
            self.assertEqual(payload["tool"], "paper_gtt_oco")

    def test_indian_mis_approval_records_paper_and_never_prepares_external_order(self):
        order = in_order()
        cfg = {"market": "IN", "currency": "INR", "paper": True, "broker": "paper",
               "budget": 100000, "min_reward_risk": 0}
        risk = in_risk(order, cfg)
        state = {"orders": []}
        now = check.parse_now(NOW)
        with tempfile.TemporaryDirectory() as temp:
            order_path = os.path.join(temp, "orders.json")
            with open(order_path, "w", encoding="utf-8") as fh:
                json.dump({"market": "IN", "orders": [order]}, fh)
            args = type("Args", (), {"index": 1, "reply": "yes", "orders": order_path,
                                      "state": os.path.join(temp, "broker.json"), "now": None})()
            writes = []
            out = io.StringIO()
            with patch.object(ticket, "_load", return_value=([order], cfg, risk, state, TODAY, now, "IN")), \
                    patch.object(ticket, "write_json", side_effect=lambda path, value: writes.append((path, value))), \
                    redirect_stdout(out):
                self.assertEqual(ticket.cmd_approve(args), 0)
            self.assertIn("Simulated Indian paper approval recorded", out.getvalue())
            self.assertNotIn("place_stock_order", out.getvalue())
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0][1]["orders"][0]["status"], "approved")

    def test_indian_mis_approval_and_result_stay_in_simulated_paper_workflow(self):
        order = in_order()
        cfg = {"market": "IN", "currency": "INR", "paper": True, "broker": "paper",
               "budget": 100000, "min_reward_risk": 0}
        risk = in_risk(order, cfg)
        state = {"orders": []}
        now = check.parse_now(NOW)
        with tempfile.TemporaryDirectory() as temp:
            order_path = os.path.join(temp, "orders.json")
            with open(order_path, "w", encoding="utf-8") as fh:
                json.dump({"market": "IN", "orders": [order]}, fh)
            state_path = os.path.join(temp, "broker.json")
            common = {"index": 1, "orders": order_path, "state": state_path, "now": None}
            args = type("Args", (), dict(common, reply="yes"))()
            with patch.object(ticket, "_load", return_value=([order], cfg, risk, state, TODAY, now, "IN")), \
                    patch.object(ticket, "write_json", side_effect=lambda path, value: state.update(value)), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(ticket.cmd_approve(args), 0)
            result_args = type("Args", (), dict(common, status="placed", filled_qty=None,
                                                   filled_price=None, broker_id=None, note=None,
                                                   portfolio=os.path.join(temp, "portfolio.json")))()
            result_out = io.StringIO()
            with patch.object(ticket, "_load", return_value=([order], cfg, risk, state, TODAY, now, "IN")), \
                    redirect_stdout(result_out):
                self.assertEqual(ticket.cmd_result(result_args), 0)
            self.assertIn("Recorded order 1 as placed", result_out.getvalue())
            self.assertNotIn("place_stock_order", result_out.getvalue())
            self.assertEqual([r["status"] for r in state["orders"]], ["approved", "placed"])

    def test_inconsistent_indian_broker_config_fails_closed_at_approval(self):
        order = in_order()
        now = check.parse_now(NOW)
        for broker, paper in (("alpaca", True), ("ibkr", True), ("paper", False)):
            cfg = {"market": "IN", "currency": "INR", "paper": paper, "broker": broker}
            risk = in_risk(order, dict(cfg, paper=True, broker="paper"))
            args = type("Args", (), {"index": 1, "reply": "yes", "orders": "unused.json", "now": None})()
            with patch.object(ticket, "_load", return_value=([order], cfg, risk, {"orders": []}, TODAY, now, "IN")), \
                    patch.object(ticket, "write_json") as writer:
                with self.assertRaises(ticket.TicketError):
                    ticket.cmd_approve(args)
                writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
