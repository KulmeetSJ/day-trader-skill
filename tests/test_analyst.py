import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

facts = load("trader-analyst", "facts")
DIR = os.path.join(FIX, "trader-analyst")


def companyfacts(cik):
    with open(os.path.join(DIR, "companyfacts-CIK%s.json" % cik)) as fh:
        return json.load(fh)


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(args)
    return code, out.getvalue(), err.getvalue()


class Apple(unittest.TestCase):
    def setUp(self):
        self.r = facts.analyse(companyfacts("0000320193"), quarters=8)

    def test_latest_quarter(self):
        q = self.r["rows"][0]
        self.assertEqual(q["end"], "2026-06-27")
        self.assertEqual(q["fiscal"], "Q3 2026")
        self.assertEqual(q["revenue"], 109417000000)
        self.assertEqual(q["net_income"], 29789000000)
        self.assertEqual(q["eps_diluted"], 2.02)
        self.assertEqual(q["revenue_yoy"], 16.4)    # vs 94,036 a year earlier
        self.assertEqual(q["eps_yoy"], 28.7)        # vs 1.57
        self.assertEqual(q["gross_margin"], 50.1)
        self.assertEqual(q["operating_margin"], 32.6)
        self.assertEqual(q["net_margin"], 27.2)
        self.assertFalse(q["derived"])

    def test_fiscal_labels_come_from_the_first_filing(self):
        labels = [r["fiscal"] for r in self.r["rows"][:6]]
        self.assertEqual(labels, ["Q3 2026", "Q2 2026", "Q1 2026", "Q4 2025", "Q3 2025", "Q2 2025"])

    def test_q4_is_derived_from_the_10k(self):
        q4 = next(r for r in self.r["rows"] if r["end"] == "2025-09-27")
        self.assertTrue(q4["derived"])
        # FY2025 416,161 minus Q1 124,300, Q2 95,359, Q3 94,036
        self.assertEqual(q4["revenue"], 102466000000)
        self.assertEqual(q4["revenue_yoy"], 7.9)

    def test_trailing_twelve_months(self):
        self.assertEqual(self.r["ttm"]["revenue"], 466823000000)
        self.assertEqual(self.r["ttm"]["net_income"], 128930000000)
        self.assertEqual(self.r["ttm"]["eps_diluted"], 8.71)

    def test_what_changed_in_words(self):
        lines = facts.changes(self.r)
        self.assertIn("revenue $109.42B (+16.4% vs a year earlier)", lines[0])
        self.assertIn("diluted EPS $2.02 (+28.7%)", lines[0])
        self.assertTrue(any(l.startswith("Gross margin 50.1% (+0.8 points") for l in lines))

    def test_restated_values_win_but_labels_stay(self):
        facts_ = [
            {"start": "2025-01-01", "end": "2025-03-31", "val": 100, "fy": 2025, "fp": "Q1",
             "form": "10-Q", "filed": "2025-05-01", "accn": "a"},
            {"start": "2025-01-01", "end": "2025-03-31", "val": 90, "fy": 2026, "fp": "Q1",
             "form": "10-Q", "filed": "2026-05-01", "accn": "b"},
            {"start": "2025-01-01", "end": "2025-03-31", "val": 1, "fy": 2025, "fp": "Q1",
             "form": "8-K", "filed": "2025-04-20", "accn": "c"},
        ]
        q = facts.quarterly(facts_)
        self.assertEqual(q["2025-03-31"]["value"], 90)
        self.assertEqual((q["2025-03-31"]["fy"], q["2025-03-31"]["fp"]), (2025, "Q1"))


class Filings(unittest.TestCase):
    def test_latest_10q_and_earnings_press_release(self):
        with open(os.path.join(DIR, "submissions-CIK0000320193.json")) as fh:
            sub = json.load(fh)
        f = facts.latest_filings(sub, "0000320193", facts.Fetcher(offline=DIR))
        self.assertEqual(f["report"]["form"], "10-Q")
        self.assertEqual(f["report"]["period"], "2026-06-27")
        self.assertTrue(f["report"]["url"].endswith("/000032019326000020/aapl-20260627.htm"))
        e = f["earnings_8k"]
        self.assertEqual(e["filed"], "2026-07-30")
        self.assertEqual(e["exhibit"], "https://www.sec.gov/Archives/edgar/data/320193/"
                                       "000032019326000018/a8-kex991q3202606272026.htm")

    def test_exhibit_names_seen_on_edgar(self):
        pick = facts.exhibit_name
        self.assertEqual(pick(["axp-20260724.htm", "q226exhibit991.htm", "q226exhibit992.htm",
                               "R1.htm"], "axp-20260724.htm"), "q226exhibit991.htm")
        self.assertEqual(pick(["goog-20260722.htm", "googexhibit991q22026.htm"],
                              "goog-20260722.htm"), "googexhibit991q22026.htm")
        self.assertEqual(pick(["a2026q2earningsreleaseex-9.htm", "ko-20260728.htm"],
                              "ko-20260728.htm"), "a2026q2earningsreleaseex-9.htm")
        self.assertEqual(pick(["bac-06302026ex993.htm", "bac-06302026ex991.htm"]),
                         "bac-06302026ex991.htm")
        self.assertIsNone(pick(["only-8k.htm"], "only-8k.htm"))


class LaggingData(unittest.TestCase):
    def test_says_when_sec_facts_trail_a_filed_10q(self):
        code, out, err = run(["KO", "--offline", DIR])
        self.assertEqual(code, 0, err)
        self.assertIn("COCA COLA CO (KO)", out)
        self.assertIn("NOTE: SEC's XBRL facts stop at 2026-04-03, but a 10-Q for the period "
                      "ending 2026-07-03 was filed 2026-07-29", out)

    def test_one_offs_are_flagged(self):
        result = {"rows": [
            {"end": "2026-06-30", "derived": False, "revenue": 100.0, "net_income": 90.0,
             "operating_income": 30.0, "eps_diluted": 1.0, "revenue_yoy": 10.0,
             "net_income_yoy": 200.0, "eps_yoy": 200.0, "gross_margin": None,
             "operating_margin": 30.0, "net_margin": 90.0}]}
        self.assertTrue(any("above operating income" in l for l in facts.changes(result)))


class Cli(unittest.TestCase):
    def test_metrics_file_for_the_screener(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trader", "metrics.json")
            code, out, err = run(["AAPL", "KO", "--offline", DIR, "--metrics", path])
            self.assertEqual(code, 0, err)
            with open(path) as fh:
                data = json.load(fh)
        rows = {r["ticker"]: r for r in data["companies"]}
        self.assertEqual(sorted(rows), ["AAPL", "KO"])
        self.assertEqual(rows["AAPL"]["eps_ttm"], 8.71)
        self.assertEqual(rows["AAPL"]["revenue_growth_yoy"], 16.4)
        self.assertEqual(rows["KO"]["period_end"], "2026-04-03")
        self.assertIn("wrote", out)
        self.assertNotIn(chr(0x2014), out)

    def test_unknown_ticker_and_no_user_agent(self):
        code, _, err = run(["ZZZZ", "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("no SEC CIK for ZZZZ", err)
        old = os.environ.pop("SEC_USER_AGENT", None)
        try:
            code, _, err = run(["AAPL", "--config", "/nonexistent.json"])
            self.assertEqual(code, 2)
            self.assertIn("User-Agent", err)
            self.assertIsNone(facts.user_agent("Your Name you@example.com"))
        finally:
            if old is not None:
                os.environ["SEC_USER_AGENT"] = old

    def test_json_output(self):
        code, out, err = run(["AAPL", "--offline", DIR, "--json", "--quarters", "4"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data[0]["result"]["rows"]), 4)
        self.assertEqual(data[0]["result"]["concepts"]["revenue"],
                         ["RevenueFromContractWithCustomerExcludingAssessedTax"])


class RelianceAnalysis(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(DIR, "indian-RELIANCE.json")) as fh:
            data = json.load(fh)
        self.r = facts.analyse(data, quarters=6)

    def test_quarterly_metrics(self):
        self.assertEqual(self.r["market"], "IN")
        self.assertEqual(self.r["entity"], "RELIANCE INDUSTRIES LTD")
        self.assertEqual(self.r["currency"], "INR")
        self.assertEqual(self.r["exchange"], "NSE")
        q = self.r["rows"][0]
        self.assertEqual(q["end"], "2026-09-30")
        self.assertEqual(q["fiscal"], "2Q2027")
        self.assertEqual(q["revenue"], 3306010000000.0)
        self.assertEqual(q["operating_income"], 413010000000.0)
        self.assertEqual(q["net_income"], 255480000000.0)
        self.assertEqual(q["eps_diluted"], 18.88)
        self.assertEqual(q["operating_margin"], 12.5)
        self.assertEqual(q["net_margin"], 7.7)
        self.assertEqual(q["reported_date"], "2026-10-30")
        self.assertFalse(q["derived"])

    def test_yoy_growth_and_margins(self):
        q1 = next(r for r in self.r["rows"] if r["end"] == "2026-06-30")
        self.assertEqual(q1["revenue_yoy"], 27.0)
        self.assertEqual(q1["operating_yoy"], 11.5)
        self.assertEqual(q1["net_income_yoy"], -22.4)
        self.assertEqual(q1["eps_yoy"], -22.4)
        self.assertEqual(q1["operating_margin"], 10.5)
        self.assertEqual(q1["net_margin"], 6.8)

    def test_annual_financials(self):
        a = self.r["annual_rows"]
        self.assertEqual(len(a), 4)
        fy26 = a[0]
        self.assertEqual(fy26["fiscal"], "FY2026")
        self.assertEqual(fy26["end"], "2026-03-31")
        self.assertEqual(fy26["revenue"], 10572190000000.0)
        self.assertEqual(fy26["operating_income"], 1213770000000.0)
        self.assertEqual(fy26["net_income"], 807750000000.0)
        self.assertEqual(fy26["eps_diluted"], 59.69)
        self.assertEqual(fy26["revenue_yoy"], 9.6)
        self.assertEqual(fy26["operating_yoy"], 7.9)
        self.assertEqual(fy26["net_income_yoy"], 16.0)

    def test_balance_sheet_and_cash_flow(self):
        bs = self.r["balance_sheet"]
        self.assertEqual(bs["as_of_date"], "2026-09-30")
        self.assertEqual(bs["total_assets"], 22750330000000.0)
        self.assertEqual(bs["total_debt"], 4053220000000.0)
        self.assertEqual(bs["cash_and_equivalents"], 1817230000000.0)
        self.assertEqual(bs["debt_to_equity"], 44.325)
        self.assertEqual(bs["current_ratio"], 1.135)
        self.assertEqual(bs["book_value"], 697.916)

        cf = self.r["cash_flow"]
        self.assertEqual(cf["operating_cash_flow"], 1921130000000.0)
        self.assertEqual(cf["free_cash_flow"], 691970000000.0)

    def test_trailing_twelve_months(self):
        self.assertEqual(self.r["ttm"]["revenue"], 11990330000000.0)
        self.assertEqual(self.r["ttm"]["operating_income"], 1344470000000.0)
        self.assertEqual(self.r["ttm"]["net_income"], 821100000000.0)
        self.assertEqual(self.r["ttm"]["eps_diluted"], 60.68)

    def test_what_changed_in_words(self):
        lines = facts.changes(self.r)
        self.assertTrue(any("revenue Rs 330,601 Cr" in l for l in lines))
        self.assertTrue(any("operating profit Rs 41,301 Cr" in l for l in lines))
        self.assertTrue(any("Operating margin 12.5%" in l for l in lines))
        self.assertTrue(any("total assets Rs 2,275,033 Cr" in l for l in lines))
        self.assertTrue(any("annual operating cash flow Rs 192,113 Cr" in l for l in lines))

    def test_source_attribution(self):
        self.assertEqual(self.r["source"], "Yahoo Finance Fundamentals API")
        rendered = facts.render_indian("RELIANCE", self.r)
        self.assertIn("[SOURCE: Yahoo Finance Fundamentals API | DATA: REPORTED | STATUS: FRESH]", rendered)
        self.assertNotIn("filed with NSE/BSE", rendered)


class TcsAnalysis(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(DIR, "indian-TCS.json")) as fh:
            data = json.load(fh)
        self.r = facts.analyse(data, quarters=6)

    def test_quarterly_and_annual_financials(self):
        self.assertEqual(self.r["entity"], "TATA CONSULTANCY SERV LT")
        q = self.r["rows"][0]
        self.assertEqual(q["end"], "2026-09-30")
        self.assertEqual(q["revenue"], 731880000000.0)
        self.assertEqual(q["operating_income"], 175530000000.0)
        self.assertEqual(q["net_income"], 138840000000.0)
        self.assertEqual(q["eps_diluted"], 38.37)
        self.assertEqual(q["operating_margin"], 24.0)
        self.assertEqual(q["net_margin"], 19.0)
        self.assertEqual(q["reported_date"], "2026-10-08")

        q1 = self.r["rows"][1]
        self.assertEqual(q1["revenue_yoy"], 13.9)
        self.assertEqual(q1["eps_yoy"], 4.6)

        self.assertEqual(self.r["ttm"]["revenue"], 2832480000000.0)
        self.assertEqual(self.r["ttm"]["operating_income"], 697500000000.0)
        self.assertEqual(self.r["ttm"]["net_income"], 516080000000.0)
        self.assertEqual(self.r["ttm"]["eps_diluted"], 142.66)


class InfosysAnalysis(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(DIR, "indian-INFY.json")) as fh:
            data = json.load(fh)
        self.r = facts.analyse(data, quarters=6)

    def test_quarterly_and_missing_values_preserved(self):
        self.assertEqual(self.r["entity"], "INFOSYS LIMITED")
        self.assertEqual(self.r["currency"], "USD")
        q0 = self.r["rows"][0]
        self.assertEqual(q0["end"], "2026-09-30")
        self.assertEqual(q0["revenue"], 5145000000.0)
        self.assertEqual(q0["revenue_yoy"], 1.4)
        self.assertEqual(q0["operating_income"], 1079000000.0)
        self.assertEqual(q0["operating_yoy"], 1.3)
        self.assertEqual(q0["net_income"], 817000000.0)
        self.assertEqual(q0["net_income_yoy"], -2.6)
        self.assertEqual(q0["eps_diluted"], 0.20)
        self.assertEqual(q0["reported_date"], "2026-10-23")

        # 2025-09-30 quarter in fixture has no diluted EPS
        q_old = next(r for r in self.r["rows"] if r["end"] == "2025-09-30")
        self.assertIsNone(q_old["eps_diluted"])
        # Because prior EPS is None, latest quarter eps_yoy must be None, not 0.0
        self.assertIsNone(q0["eps_yoy"])

    def test_infy_currency_and_book_value_distinction(self):
        self.assertEqual(self.r["statement_currency"], "USD")
        self.assertEqual(self.r["trading_currency"], "INR")
        self.assertEqual(self.r["balance_sheet"]["book_value_currency"], "INR")
        rendered = facts.render_indian("INFY", self.r)
        # Book value is in INR (Rs), not in USD ($)
        self.assertIn("Book Value per share: Rs 240.59", rendered)
        self.assertNotIn("Book Value per share: $240.59", rendered)
        self.assertIn("[CURRENCY NOTE]", rendered)
        self.assertIn("[SOURCE: Yahoo Finance Fundamentals API", rendered)
        self.assertNotIn("filed with NSE/BSE", rendered)


class IndianCli(unittest.TestCase):
    def test_cli_offline_rendering(self):
        code, out, err = run(["RELIANCE", "--offline", DIR])
        self.assertEqual(code, 0, err)
        self.assertIn("RELIANCE INDUSTRIES LTD (RELIANCE)", out)
        self.assertIn("Exchange: NSE", out)
        self.assertIn("INR Crores", out)
        self.assertIn("330,601", out)
        self.assertIn("2026-10-30", out)
        self.assertIn("[SOURCE:", out)
        self.assertIn("DATA: REPORTED", out)
        self.assertIn("STATUS: FRESH", out)
        self.assertNotIn(chr(0x2014), out)

    def test_cli_offline_annual(self):
        code, out, err = run(["RELIANCE", "--offline", DIR, "--annual"])
        self.assertEqual(code, 0, err)
        self.assertIn("Annual Financials (Fiscal Years):", out)
        self.assertIn("FY2026", out)
        self.assertIn("1,057,219", out)
        self.assertIn("+9.6%", out)

    def test_cli_offline_json(self):
        code, out, err = run(["RELIANCE", "--offline", DIR, "--json", "--quarters", "3"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        res = data[0]["result"]
        self.assertEqual(res["market"], "IN")
        self.assertEqual(len(res["rows"]), 3)
        self.assertEqual(len(res["annual_rows"]), 4)
        self.assertEqual(res["balance_sheet"]["total_debt"], 4053220000000.0)

    def test_cli_offline_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trader", "metrics.json")
            code, out, err = run(["RELIANCE", "TCS", "--offline", DIR, "--metrics", path])
            self.assertEqual(code, 0, err)
            with open(path) as fh:
                data = json.load(fh)
        rows = {r["ticker"]: r for r in data["companies"]}
        self.assertEqual(sorted(rows), ["RELIANCE", "TCS"])
        self.assertEqual(rows["RELIANCE"]["exchange"], "NSE")
        self.assertEqual(rows["RELIANCE"]["currency"], "INR")
        self.assertEqual(rows["RELIANCE"]["period_end"], "2026-09-30")
        self.assertEqual(rows["RELIANCE"]["publication_date"], "2026-10-30")
        self.assertEqual(rows["RELIANCE"]["revenue_ttm"], 11990330000000.0)
        self.assertEqual(rows["RELIANCE"]["operating_profit_ttm"], 1344470000000.0)
        self.assertEqual(rows["RELIANCE"]["eps_ttm"], 60.68)
        self.assertEqual(rows["TCS"]["eps_ttm"], 142.66)
        self.assertEqual(rows["RELIANCE"]["data_quality"], "REPORTED")


if __name__ == "__main__":
    unittest.main()
