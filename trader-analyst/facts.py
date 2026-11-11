#!/usr/bin/env python3
"""Earnings and fundamentals reader for trader-analyst. Standard library only.

Pulls a company's reported numbers from SEC's XBRL "company facts" API for US stocks,
and from public exchange corporate disclosures for Indian-listed equities (NSE & BSE):
revenue, gross, operating and net margins, net income, diluted EPS, YoY growth,
annual statements, and balance sheet / cash flow metrics.

    python3 facts.py AAPL
    python3 facts.py RELIANCE TCS INFY
    python3 facts.py RELIANCE --annual
    python3 facts.py AAPL KO --quarters 8
    python3 facts.py RELIANCE --json
    python3 facts.py AAPL RELIANCE --metrics trader/metrics.json

Sources:
  US (SEC):
    https://www.sec.gov/files/company_tickers.json            ticker -> CIK
    https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
    https://data.sec.gov/submissions/CIK##########.json        filings list
    https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/index.json
  India (NSE / BSE corporate disclosures via Yahoo Finance API):
    quoteSummary (modules: financialData, defaultKeyStatistics, earnings, price)
    fundamentals-timeseries (quarterly & annual statements, balance sheet, cash flow)

How quarters are built:
  - A quarter is a reported value covering about three months (80 to 100 days).
  - For US filers, fourth quarter is derived from full year (10-K) minus 3 quarters.
  - For Indian filers, quarters come directly from reported quarterly financial results
    filed with NSE/BSE (with publication dates and actual reported values).
  - Missing figures remain None (rendered as '-') and are never fabricated.

--metrics FILE merges one row per ticker into a JSON file (for notes or a sheet).
Offline: --offline DIR reads company_tickers.json, companyfacts-CIK*.json or
indian-*.json files from DIR without making network requests.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime as _dt
import gzip
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# Sibling path resolution for Indian market symbols (../trader-alerts)
try:
    import symbols_in  # type: ignore
except ImportError:
    try:
        _alerts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-alerts")
        if os.path.isdir(_alerts_dir) and _alerts_dir not in sys.path:
            sys.path.insert(0, _alerts_dir)
        import symbols_in  # type: ignore
    except ImportError:
        symbols_in = None

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK%s.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/%d/%s/"
PLACEHOLDER = "you@example.com"
UA_HELP = ("SEC requires a User-Agent with your name and email. Set \"sec_user_agent\": "
           "\"Your Name you@example.com\" in trader/config.json (your real name and email), "
           "or export SEC_USER_AGENT, or pass --user-agent.")

CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenuesNetOfInterestExpense", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
}
FLOWS = ("revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income")


class FactsError(Exception):
    pass


def user_agent(cli=None, config="trader/config.json"):
    ua = (cli or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua and config and os.path.isfile(config):
        try:
            with open(config, encoding="utf-8") as fh:
                ua = str(json.load(fh).get("sec_user_agent") or "").strip()
        except (OSError, ValueError):
            ua = ""
    if not ua or "@" not in ua or PLACEHOLDER in ua:
        return None
    return ua


class Fetcher(object):
    def __init__(self, ua=None, offline=None, pause=0.15):
        self.ua, self.offline, self.pause, self._last = ua, offline, pause, 0.0
        self._yahoo_crumb = None
        self._yahoo_opener = None
        self._yahoo_cj = None

    def json(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise FactsError("offline: no file %s" % path)
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        if not self.ua:
            raise FactsError(UA_HELP)
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": self.ua,
                                                   "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FactsError("SEC has no data at %s (no XBRL filings for this CIK?)" % url)
            raise FactsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise FactsError("could not reach %s: %s" % (url, exc.reason))
        return json.loads(data.decode("utf-8"))

    def get_indian(self, ticker, exchange=None):
        clean_tick = ticker.upper().strip()
        base = clean_tick
        for sfx in (".NS", ".BO", "-EQ", ":NSE", ":BSE"):
            if base.endswith(sfx):
                base = base[:-len(sfx)]
                break
        if ":" in base:
            parts = base.split(":")
            if len(parts) == 2 and parts[0] in ("NSE", "BSE"):
                base = parts[1]
                if not exchange:
                    exchange = parts[0]
        if not exchange:
            if clean_tick.endswith(".BO") or clean_tick.startswith("BSE:"):
                exchange = "BSE"
            else:
                exchange = "NSE"
        
        provider_symbol = base + (".BO" if exchange == "BSE" else ".NS")
        
        if self.offline:
            candidates = [
                "indian-%s.json" % base,
                "indian-%s.json" % clean_tick,
                "%s.NS.json" % base,
                "%s.json" % base,
                "%s.json" % clean_tick,
            ]
            for cand in candidates:
                path = os.path.join(self.offline, cand)
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    if "exchange" not in data:
                        data["exchange"] = exchange
                    return data
            raise FactsError("offline: no file %s" % os.path.join(self.offline, candidates[0]))
            
        return self._fetch_indian_online(provider_symbol, exchange, base)

    def _fetch_indian_online(self, provider_symbol, exchange, base_symbol):
        if self._yahoo_opener is None:
            self._yahoo_cj = http.cookiejar.CookieJar()
            self._yahoo_opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self._yahoo_cj)
            )
            self._yahoo_crumb = None
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        
        if self._yahoo_crumb is None:
            try:
                init_url = "https://finance.yahoo.com/quote/%s" % provider_symbol
                req1 = urllib.request.Request(init_url, headers=headers)
                self._yahoo_opener.open(req1, timeout=15)
                crumb_url = "https://query1.finance.yahoo.com/v1/test/getcrumb"
                req2 = urllib.request.Request(crumb_url, headers=headers)
                with self._yahoo_opener.open(req2, timeout=15) as resp2:
                    self._yahoo_crumb = resp2.read().decode("utf-8").strip()
            except urllib.error.HTTPError as exc:
                raise FactsError("could not initialize market session (HTTP %s)" % exc.code)
            except Exception as exc:
                raise FactsError("could not initialize market session: %s" % exc)
        
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        
        json_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        }
        
        qs_url = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/%s?crumb=%s&modules=%s" % (
            provider_symbol, self._yahoo_crumb,
            "financialData,defaultKeyStatistics,earnings,price,summaryProfile"
        )
        try:
            req_qs = urllib.request.Request(qs_url, headers=json_headers)
            with self._yahoo_opener.open(req_qs, timeout=15) as resp:
                qs_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FactsError("no financial data found for %s (HTTP 404)" % provider_symbol)
            raise FactsError("HTTP %s for %s" % (exc.code, qs_url))
        except Exception as exc:
            raise FactsError("failed to fetch quote summary for %s: %s" % (provider_symbol, exc))
            
        ts_types = (
            "quarterlyTotalRevenue,quarterlyOperatingIncome,quarterlyNetIncome,quarterlyGrossProfit,"
            "quarterlyDilutedEPS,quarterlyBasicEPS,quarterlyEBITDA,quarterlyOperatingExpense,"
            "annualTotalRevenue,annualOperatingIncome,annualNetIncome,annualGrossProfit,annualDilutedEPS,"
            "annualBasicEPS,annualEBITDA,quarterlyTotalAssets,quarterlyTotalDebt,quarterlyCashAndCashEquivalents,"
            "quarterlyCommonStockEquity,quarterlyTotalLiabilitiesNetMinorityInterest,annualTotalAssets,"
            "annualTotalDebt,quarterlyOperatingCashFlow,quarterlyFreeCashFlow,quarterlyCapitalExpenditure,"
            "annualOperatingCashFlow,annualFreeCashFlow"
        )
        ts_url = "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s?crumb=%s&type=%s&merge=false&period1=1500000000&period2=1800000000" % (
            provider_symbol, self._yahoo_crumb, ts_types
        )
        try:
            req_ts = urllib.request.Request(ts_url, headers=json_headers)
            with self._yahoo_opener.open(req_ts, timeout=15) as resp:
                ts_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FactsError("no financial timeseries found for %s (HTTP 404)" % provider_symbol)
            raise FactsError("HTTP %s for %s" % (exc.code, ts_url))
        except Exception as exc:
            raise FactsError("failed to fetch financial timeseries for %s: %s" % (provider_symbol, exc))
            
        return {
            "symbol": provider_symbol,
            "exchange": exchange,
            "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "source": "Yahoo Finance Fundamentals API",
            "quoteSummary": qs_data.get("quoteSummary", {}).get("result", [{}])[0],
            "timeseries": ts_data.get("timeseries", {}).get("result", []),
        }


# ---------------------------------------------------------------- facts

def _date(s):
    return _dt.date.fromisoformat(s)


def facts_for(companyfacts, concept, unit_hint=None):
    """Raw facts for one us-gaap concept (USD, or USD/shares for EPS)."""
    node = companyfacts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not node:
        return []
    units = node.get("units", {})
    unit = unit_hint if unit_hint in units else next(iter(units), None)
    return units.get(unit, []) if unit else []


def periods(facts):
    """{(start, end): fact} for duration facts from 10-Q/10-K.

    The value comes from the latest filing (restatements win). The fiscal
    label (fy, fp) comes from the first filing that reported the period: a
    later 10-Q that repeats it as a comparison carries its own fy and fp."""
    latest, first = {}, {}
    for f in facts:
        if "start" not in f or f.get("form", "").split("/")[0] not in ("10-Q", "10-K", "20-F", "40-F"):
            continue
        key = (f["start"], f["end"])
        stamp = (f.get("filed", ""), f.get("accn", ""))
        if key not in latest or stamp > (latest[key].get("filed", ""), latest[key].get("accn", "")):
            latest[key] = f
        if key not in first or stamp < (first[key].get("filed", ""), first[key].get("accn", "")):
            first[key] = f
    out = {}
    for key, f in latest.items():
        g = dict(f)
        g["fy"], g["fp"] = first[key].get("fy"), first[key].get("fp")
        g["first_filed"] = first[key].get("filed")
        out[key] = g
    return out


def quarterly(facts, derive_q4=True):
    """{end: {"value", "start", "end", "derived", "fy", "fp", "form"}} three-month values."""
    per = periods(facts)
    q = {}
    years = []
    for (start, end), f in per.items():
        days = (_date(end) - _date(start)).days
        if 80 <= days <= 100:
            q[end] = {"value": f["val"], "start": start, "end": end, "derived": False,
                      "fy": f.get("fy"), "fp": f.get("fp"), "form": f.get("form"),
                      "filed": f.get("filed")}
        elif 350 <= days <= 380:
            years.append((start, end, f))
    if derive_q4:
        for start, end, f in years:
            if end in q:
                continue
            inside = [v for v in q.values() if v["start"] >= start and v["end"] < end]
            inside.sort(key=lambda v: v["end"])
            if len(inside) != 3:
                continue
            last_q_start = _date(inside[-1]["end"]) + _dt.timedelta(days=1)
            q[end] = {"value": f["val"] - sum(v["value"] for v in inside),
                      "start": last_q_start.isoformat(), "end": end, "derived": True,
                      "fy": f.get("fy"), "fp": "Q4", "form": f.get("form"),
                      "filed": f.get("filed")}
    return q


def series(companyfacts, key, derive_q4=True):
    """Quarterly series for a metric, trying its concepts in order and filling gaps."""
    unit = "USD/shares" if key == "eps_diluted" else "USD"
    merged, used = {}, []
    for concept in CONCEPTS[key]:
        q = quarterly(facts_for(companyfacts, concept, unit), derive_q4)
        if not q:
            continue
        added = False
        for end, v in q.items():
            if end not in merged:
                merged[end] = dict(v, concept=concept)
                added = True
        if added:
            used.append(concept)
    return merged, used


def _yoy_match(series_, end, days=365, slack=10):
    target = _date(end) - _dt.timedelta(days=days)
    best = None
    for e in series_:
        gap = abs((_date(e) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, e)
    return series_[best[1]] if best else None


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def analyse(companyfacts, quarters=6, name=None):
    """Every number in the report, as a dict."""
    if isinstance(companyfacts, dict) and (
        "timeseries" in companyfacts or "quoteSummary" in companyfacts or companyfacts.get("market") == "IN"
    ):
        return analyse_indian(companyfacts, quarters=quarters, name=name)
    s = {}
    concepts = {}
    for key in CONCEPTS:
        s[key], concepts[key] = series(companyfacts, key, derive_q4=True)
    rev = s["revenue"]
    if not rev and not s["net_income"]:
        raise FactsError("no quarterly revenue or net income in the XBRL facts")
    ends = sorted(set(rev) | set(s["net_income"]), reverse=True)[:quarters]
    rows = []
    for end in ends:
        r = rev.get(end, {}).get("value")
        gp = s["gross_profit"].get(end, {}).get("value")
        if gp is None and r is not None and end in s["cost_of_revenue"]:
            gp = r - s["cost_of_revenue"][end]["value"]
        oi = s["operating_income"].get(end, {}).get("value")
        ni = s["net_income"].get(end, {}).get("value")
        eps = s["eps_diluted"].get(end, {}).get("value")
        prior = {k: _yoy_match(s[k], end) for k in ("revenue", "net_income", "eps_diluted")}
        base = rev.get(end) or s["net_income"].get(end) or {}
        rows.append({
            "end": end, "start": base.get("start"),
            "fiscal": "%s %s" % (base.get("fp") or "", base.get("fy") or ""),
            "derived": any(s[k].get(end, {}).get("derived") for k in ("revenue", "net_income")),
            "revenue": r, "gross_profit": gp, "operating_income": oi, "net_income": ni,
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gp, r), "operating_margin": margin(oi, r),
            "net_margin": margin(ni, r),
            "revenue_yoy": pct(r, (prior["revenue"] or {}).get("value")),
            "net_income_yoy": pct(ni, (prior["net_income"] or {}).get("value")),
            "eps_yoy": pct(eps, (prior["eps_diluted"] or {}).get("value")),
            "filed": base.get("filed"), "form": base.get("form"),
        })
    ttm = {}
    for key in ("revenue", "net_income", "eps_diluted"):
        last4 = sorted(s[key])[-4:]
        if len(last4) == 4 and (_date(last4[-1]) - _date(last4[0])).days < 300:
            ttm[key] = sum(s[key][e]["value"] for e in last4)
    if "eps_diluted" in ttm:
        ttm["eps_diluted"] = round(ttm["eps_diluted"], 2)
    return {"entity": name or companyfacts.get("entityName"),
            "cik": str(companyfacts.get("cik", "")).zfill(10),
            "rows": rows, "ttm": ttm, "concepts": concepts}


def analyse_indian(data, quarters=6, annual=False, name=None):
    """Analyze Indian equity reported financials from public exchange data."""
    if not isinstance(data, dict):
        raise FactsError("invalid data format for Indian financials")

    quote_summary = data.get("quoteSummary", {})
    price = quote_summary.get("price", {})
    fin_data = quote_summary.get("financialData", {})
    key_stats = quote_summary.get("defaultKeyStatistics", {})
    earnings = quote_summary.get("earnings", {})
    earnings_chart = earnings.get("earningsChart", {})

    trading_currency = price.get("currency") or "INR"
    statement_currency = fin_data.get("financialCurrency") or trading_currency
    currency = statement_currency
    clean_sym = data.get("symbol", "").split(".")[0]
    entity = (name or price.get("shortName") or price.get("longName") or clean_sym)

    timeseries = data.get("timeseries", [])
    series_map = {}
    for item in timeseries:
        typ = item.get("meta", {}).get("type", [""])[0]
        if not typ:
            continue
        pts = item.get(typ, [])
        series_map[typ] = {
            p["asOfDate"]: p.get("reportedValue", {}).get("raw")
            for p in pts
            if "asOfDate" in p and p.get("reportedValue", {}).get("raw") is not None
        }

    q_rev = series_map.get("quarterlyTotalRevenue", {})
    q_oi = series_map.get("quarterlyOperatingIncome", {})
    q_ni = series_map.get("quarterlyNetIncome", {})
    q_eps = series_map.get("quarterlyDilutedEPS", {})
    if not q_eps:
        q_eps = series_map.get("quarterlyBasicEPS", {})
    q_gp = series_map.get("quarterlyGrossProfit", {})

    if not q_rev and not q_ni and not series_map.get("annualTotalRevenue"):
        raise FactsError("no quarterly or annual revenue/net income found in financial statements")

    filing_info = {}
    for q in earnings_chart.get("quarterly", []):
        pe = q.get("periodEndDate", {}).get("fmt")
        if pe:
            filing_info[pe] = {
                "reported_date": q.get("reportedDate", {}).get("fmt"),
                "fiscal": q.get("fiscalQuarter") or q.get("date"),
            }

    def _indian_fiscal_label(end_date_str):
        try:
            d = _date(end_date_str)
            m, y = d.month, d.year
            if m == 3:
                return "Q4 FY%d" % y
            elif m == 6:
                return "Q1 FY%d" % (y + 1)
            elif m == 9:
                return "Q2 FY%d" % (y + 1)
            elif m == 12:
                return "Q3 FY%d" % (y + 1)
        except Exception:
            pass
        return "Q %s" % end_date_str[:7]

    q_dates = sorted(set(q_rev) | set(q_ni) | set(q_oi), reverse=True)
    rows = []
    for end in q_dates:
        r = q_rev.get(end)
        oi = q_oi.get(end)
        gp = q_gp.get(end)
        ni = q_ni.get(end)
        eps = q_eps.get(end)

        prior_r = _yoy_match(q_rev, end)
        prior_oi = _yoy_match(q_oi, end)
        prior_ni = _yoy_match(q_ni, end)
        prior_eps = _yoy_match(q_eps, end)

        finfo = filing_info.get(end, {})
        fiscal_lbl = finfo.get("fiscal") or _indian_fiscal_label(end)

        rows.append({
            "end": end,
            "fiscal": fiscal_lbl,
            "revenue": r,
            "operating_income": oi,
            "gross_profit": gp,
            "net_income": ni,
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gp, r),
            "operating_margin": margin(oi, r),
            "net_margin": margin(ni, r),
            "revenue_yoy": pct(r, prior_r),
            "operating_yoy": pct(oi, prior_oi),
            "net_income_yoy": pct(ni, prior_ni),
            "eps_yoy": pct(eps, prior_eps),
            "reported_date": finfo.get("reported_date"),
            "derived": False,
        })

    a_rev = series_map.get("annualTotalRevenue", {})
    a_oi = series_map.get("annualOperatingIncome", {})
    a_ni = series_map.get("annualNetIncome", {})
    a_gp = series_map.get("annualGrossProfit", {})
    a_eps = series_map.get("annualDilutedEPS", {}) or series_map.get("annualBasicEPS", {})

    a_dates = sorted(set(a_rev) | set(a_ni), reverse=True)
    annual_rows = []
    for end in a_dates:
        r = a_rev.get(end)
        oi = a_oi.get(end)
        gp = a_gp.get(end)
        ni = a_ni.get(end)
        eps = a_eps.get(end)

        prior_r = _yoy_match(a_rev, end)
        prior_oi = _yoy_match(a_oi, end)
        prior_ni = _yoy_match(a_ni, end)
        prior_eps = _yoy_match(a_eps, end)

        fiscal_lbl = "FY%s" % end[:4]
        annual_rows.append({
            "end": end,
            "fiscal": fiscal_lbl,
            "revenue": r,
            "operating_income": oi,
            "gross_profit": gp,
            "net_income": ni,
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gp, r),
            "operating_margin": margin(oi, r),
            "net_margin": margin(ni, r),
            "revenue_yoy": pct(r, prior_r),
            "operating_yoy": pct(oi, prior_oi),
            "net_income_yoy": pct(ni, prior_ni),
            "eps_yoy": pct(eps, prior_eps),
        })

    ttm = {}
    if len(rows) >= 4:
        last4_dates = sorted([r["end"] for r in rows[:4]])
        if (_date(last4_dates[-1]) - _date(last4_dates[0])).days < 380:
            sub = rows[:4]
            if all(x.get("revenue") is not None for x in sub):
                ttm["revenue"] = sum(x["revenue"] for x in sub)
            if all(x.get("operating_income") is not None for x in sub):
                ttm["operating_income"] = sum(x["operating_income"] for x in sub)
            if all(x.get("net_income") is not None for x in sub):
                ttm["net_income"] = sum(x["net_income"] for x in sub)
            if all(x.get("eps_diluted") is not None for x in sub):
                ttm["eps_diluted"] = round(sum(x["eps_diluted"] for x in sub), 2)
    if "eps_diluted" not in ttm and key_stats.get("trailingEps", {}).get("raw") is not None:
        ttm["eps_diluted"] = round(float(key_stats["trailingEps"]["raw"]), 2)
    if "revenue" not in ttm and fin_data.get("totalRevenue", {}).get("raw") is not None:
        ttm["revenue"] = float(fin_data["totalRevenue"]["raw"])

    bs_debt = series_map.get("quarterlyTotalDebt", {}) or series_map.get("annualTotalDebt", {})
    bs_assets = series_map.get("quarterlyTotalAssets", {}) or series_map.get("annualTotalAssets", {})
    bs_cash = series_map.get("quarterlyCashAndCashEquivalents", {})
    bs_equity = series_map.get("quarterlyCommonStockEquity", {})

    latest_bs_date = max(bs_assets.keys()) if bs_assets else (max(bs_debt.keys()) if bs_debt else None)

    total_debt = bs_debt.get(latest_bs_date) if latest_bs_date else None
    if total_debt is None and fin_data.get("totalDebt", {}).get("raw") is not None:
        total_debt = float(fin_data["totalDebt"]["raw"])

    total_cash = bs_cash.get(latest_bs_date) if latest_bs_date else None
    if total_cash is None and fin_data.get("totalCash", {}).get("raw") is not None:
        total_cash = float(fin_data["totalCash"]["raw"])

    balance_sheet = {
        "as_of_date": latest_bs_date,
        "total_assets": bs_assets.get(latest_bs_date) if latest_bs_date else None,
        "total_debt": total_debt,
        "cash_and_equivalents": total_cash,
        "common_equity": bs_equity.get(latest_bs_date) if latest_bs_date else None,
        "debt_to_equity": fin_data.get("debtToEquity", {}).get("raw"),
        "current_ratio": fin_data.get("currentRatio", {}).get("raw"),
        "book_value": key_stats.get("bookValue", {}).get("raw"),
        "book_value_currency": trading_currency,
        "price_to_book": key_stats.get("priceToBook", {}).get("raw"),
    }

    cf_ocf = series_map.get("annualOperatingCashFlow", {}) or series_map.get("quarterlyOperatingCashFlow", {})
    cf_fcf = series_map.get("annualFreeCashFlow", {}) or series_map.get("quarterlyFreeCashFlow", {})
    latest_cf_date = max(cf_ocf.keys()) if cf_ocf else (max(cf_fcf.keys()) if cf_fcf else None)

    operating_cf = cf_ocf.get(latest_cf_date) if latest_cf_date else None
    if operating_cf is None and fin_data.get("operatingCashflow", {}).get("raw") is not None:
        operating_cf = float(fin_data["operatingCashflow"]["raw"])

    free_cf = cf_fcf.get(latest_cf_date) if latest_cf_date else None
    if free_cf is None and fin_data.get("freeCashflow", {}).get("raw") is not None:
        free_cf = float(fin_data["freeCashflow"]["raw"])

    cash_flow = {
        "as_of_date": latest_cf_date,
        "operating_cash_flow": operating_cf,
        "free_cash_flow": free_cf,
    }

    ed_list = earnings_chart.get("earningsDate", [])
    next_earnings = ed_list[0].get("fmt") if ed_list and isinstance(ed_list[0], dict) else None

    return {
        "market": "IN",
        "entity": entity,
        "symbol": data.get("symbol", ""),
        "canonical_symbol": clean_sym,
        "exchange": data.get("exchange", "NSE"),
        "currency": currency,
        "statement_currency": statement_currency,
        "trading_currency": trading_currency,
        "as_of": data.get("as_of"),
        "source": data.get("source", "Yahoo Finance Fundamentals API"),
        "data_quality": "REPORTED",
        "freshness": "FRESH",
        "rows": rows[:quarters],
        "annual_rows": annual_rows,
        "ttm": ttm,
        "balance_sheet": balance_sheet,
        "cash_flow": cash_flow,
        "next_earnings_date": next_earnings,
        "concepts": {"source": "Yahoo Finance Fundamentals API"},
    }


def stale_note(result, filings):
    """A note when SEC's facts lag a report that is already filed."""
    rep = (filings or {}).get("report")
    if not rep or not result["rows"] or not rep.get("period"):
        return None
    latest = result["rows"][0]["end"]
    if rep["period"] > latest and (_date(rep["period"]) - _date(latest)).days > 20:
        return ("SEC's XBRL facts stop at %s, but a %s for the period ending %s was filed %s. "
                "Read it directly: %s" % (latest, rep["form"], rep["period"], rep["filed"],
                                         rep["url"]))
    return None


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    if result.get("market") == "IN":
        return changes_indian(result)
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    out = []

    def money(v):
        return "n/a" if v is None else "$%.2fB" % (v / 1e9) if abs(v) >= 1e9 else "$%.1fM" % (v / 1e6)

    label = "Quarter ending %s%s" % (cur["end"], " (derived from the 10-K)" if cur["derived"] else "")
    line = "%s: revenue %s" % (label, money(cur["revenue"]))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    line += ", net income %s" % money(cur["net_income"])
    if cur["net_income_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_income_yoy"]
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS $%.2f" % cur["eps_diluted"]
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur["net_income"] is not None and cur["operating_income"] is not None \
            and cur["net_income"] > cur["operating_income"]:
        out.append("Net income is above operating income: it includes gains outside the "
                   "business (investments, tax items). Check the 10-Q before reading it as growth.")
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append("Revenue %+.1f%% vs the quarter before (many businesses are seasonal: "
                   "compare with a year earlier first)." % pct(cur["revenue"], prev["revenue"]))
    return out


def changes_indian(result):
    """Plain sentences: what changed in the latest reported period for Indian equity."""
    rows = result.get("rows", [])
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    curr = result.get("currency", "INR")
    sym_curr = "Rs " if curr == "INR" else "$"

    def _val(v):
        if v is None:
            return "n/a"
        if curr == "INR":
            return "%s%s Cr" % (sym_curr, _cr(v))
        return "%s%sM" % (sym_curr, _m(v))

    out = []
    line = "%s (quarter ending %s): revenue %s" % (
        cur.get("fiscal") or "Latest quarter", cur["end"], _val(cur["revenue"])
    )
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    if cur.get("operating_income") is not None:
        line += ", operating profit %s" % _val(cur["operating_income"])
        if cur.get("operating_yoy") is not None:
            line += " (%+.1f%%)" % cur["operating_yoy"]
    line += ", net profit %s" % _val(cur["net_income"])
    if cur["net_income_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_income_yoy"]
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS %s%.2f" % (sym_curr, cur["eps_diluted"])
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")

    if cur.get("operating_margin") is not None:
        s = "Operating margin %.1f%%" % cur["operating_margin"]
        if prev and prev.get("operating_margin") is not None:
            s += " (%+.1f points vs the quarter before)" % (cur["operating_margin"] - prev["operating_margin"])
        out.append(s + ".")

    if cur.get("net_margin") is not None:
        s = "Net margin %.1f%%" % cur["net_margin"]
        if prev and prev.get("net_margin") is not None:
            s += " (%+.1f points vs the quarter before)" % (cur["net_margin"] - prev["net_margin"])
        out.append(s + ".")

    if cur.get("reported_date"):
        out.append("Quarterly results published on %s (reported via Yahoo Finance)." % cur["reported_date"])

    bs = result.get("balance_sheet", {})
    if bs.get("total_assets") is not None or bs.get("total_debt") is not None:
        parts = []
        if bs.get("total_assets") is not None:
            parts.append("total assets %s" % _val(bs["total_assets"]))
        if bs.get("total_debt") is not None:
            parts.append("total debt %s" % _val(bs["total_debt"]))
        if bs.get("cash_and_equivalents") is not None:
            parts.append("cash %s" % _val(bs["cash_and_equivalents"]))
        if bs.get("debt_to_equity") is not None:
            parts.append("debt-to-equity %.1f%%" % bs["debt_to_equity"])
        if parts:
            out.append("Balance sheet: " + ", ".join(parts) + ".")

    cf = result.get("cash_flow", {})
    if cf.get("operating_cash_flow") is not None or cf.get("free_cash_flow") is not None:
        parts = []
        if cf.get("operating_cash_flow") is not None:
            parts.append("annual operating cash flow %s" % _val(cf["operating_cash_flow"]))
        if cf.get("free_cash_flow") is not None:
            parts.append("free cash flow %s" % _val(cf["free_cash_flow"]))
        if parts:
            out.append("Cash flow: " + ", ".join(parts) + ".")

    return out


# ---------------------------------------------------------------- filings

EXHIBIT_RANK = [r"ex(?:hibit)?[-_]?99[-_.]?0?1(?!\d)", r"ex(?:hibit)?[-_]?99",
                r"earnings|release|press", r"ex(?:hibit)?[-_]?\d"]


def exhibit_name(names, primary=None):
    """The press release among a filing's files (EX-99.1 by its usual names)."""
    docs = [n for n in names if n.lower().endswith((".htm", ".html", ".txt", ".pdf"))
            and n != primary and not re.match(r"^(R\d+\.htm|\d{10}-\d{2}-\d{6})", n)]
    for rx in EXHIBIT_RANK:
        hits = sorted(n for n in docs if re.search(rx, n, re.I))
        if hits:
            return hits[0]
    return None


def latest_filings(submissions, cik, fetcher=None):
    """Newest 10-Q/10-K and newest earnings 8-K (item 2.02) with its EX-99 exhibit."""
    rec = submissions.get("filings", {}).get("recent", {})
    out = {"report": None, "earnings_8k": None}
    base_cik = int(cik)
    for i, form in enumerate(rec.get("form", [])):
        acc = rec["accessionNumber"][i]
        folder = ARCHIVE_URL % (base_cik, acc.replace("-", ""))
        if out["report"] is None and form in ("10-Q", "10-K", "20-F", "40-F"):
            out["report"] = {"form": form, "filed": rec["filingDate"][i],
                             "period": rec["reportDate"][i], "accession": acc,
                             "url": folder + rec["primaryDocument"][i]}
        if out["earnings_8k"] is None and form == "8-K" and "2.02" in (rec.get("items", [""] * (i + 1))[i] or ""):
            e = {"form": form, "filed": rec["filingDate"][i], "accession": acc,
                 "url": folder + rec["primaryDocument"][i], "exhibit": None,
                 "index": folder + "%s-index.htm" % acc}
            if fetcher is not None:
                try:
                    idx = fetcher.json(folder + "index.json", os.path.join(acc.replace("-", ""), "index.json"))
                    names = [it["name"] for it in idx.get("directory", {}).get("item", [])]
                    pick = exhibit_name(names, rec["primaryDocument"][i])
                    if pick:
                        e["exhibit"] = folder + pick
                except FactsError:
                    pass
            out["earnings_8k"] = e
        if out["report"] and out["earnings_8k"]:
            break
    return out


# ---------------------------------------------------------------- output

def _m(v):
    if v is None:
        return "-"
    return "{:,.0f}".format(v / 1e6)


def _cr(v):
    if v is None:
        return "-"
    return "{:,.0f}".format(v / 1e7)


def _p(v):
    return "-" if v is None else "%+.1f%%" % v if v < 0 or v > 0 else "0.0%"


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def render(ticker, result, filings=None):
    if result.get("market") == "IN":
        return render_indian(ticker, result)
    out = ["%s (%s), CIK %s. US$ millions except EPS. Source: SEC XBRL company facts." % (
        result["entity"], ticker, result["cik"]), ""]
    out.append("%-11s %-9s %10s %8s %7s %7s %7s %10s %8s %7s" % (
        "quarter end", "fiscal", "revenue", "rev yoy", "gross", "oper", "net",
        "net inc", "EPS dil", "EPS yoy"))
    for r in result["rows"]:
        out.append("%-11s %-9s %10s %8s %7s %7s %7s %10s %8s %7s%s" % (
            r["end"], r["fiscal"].strip()[:9], _m(r["revenue"]), _p(r["revenue_yoy"]),
            _pm(r["gross_margin"]), _pm(r["operating_margin"]), _pm(r["net_margin"]),
            _m(r["net_income"]), "-" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"],
            _p(r["eps_yoy"]), "  derived" if r["derived"] else ""))
    t = result["ttm"]
    if t:
        out.append("")
        out.append("Last 4 quarters: revenue %s, net income %s, diluted EPS %s." % (
            "$" + _m(t.get("revenue")) + "M" if t.get("revenue") is not None else "-",
            "$" + _m(t.get("net_income")) + "M" if t.get("net_income") is not None else "-",
            "$%.2f" % t["eps_diluted"] if t.get("eps_diluted") is not None else "-"))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    if filings:
        out.append("")
        rep, e8 = filings.get("report"), filings.get("earnings_8k")
        if rep:
            out.append("Latest %s: period %s, filed %s  %s" % (rep["form"], rep["period"],
                                                              rep["filed"], rep["url"]))
        if e8:
            out.append("Latest earnings 8-K (item 2.02): filed %s  %s" % (
                e8["filed"], e8["exhibit"] or e8["index"]))
    note = stale_note(result, filings)
    if note:
        out.append("")
        out.append("NOTE: " + note)
    if any(r["derived"] for r in result["rows"]):
        out.append("")
        out.append("Derived quarters are the 10-K year minus the three 10-Qs; derived EPS is "
                   "approximate.")
    return "\n".join(out) + "\n"


def render_indian(ticker, result, show_annual=True):
    entity = result.get("entity", ticker)
    exchange = result.get("exchange", "NSE")
    curr = result.get("currency", "INR")
    trading_curr = result.get("trading_currency", "INR")
    unit_label = "INR Crores (Rs Cr = Rs 10M)" if curr == "INR" else "US$ Millions"
    unit_fmt = _cr if curr == "INR" else _m
    curr_sym = "Rs " if curr == "INR" else "$"
    price_curr_sym = "Rs " if trading_curr == "INR" else "$"

    rows = result.get("rows", [])
    cur = rows[0] if rows else {}
    rep_period = cur.get("fiscal") or cur.get("end") or "-"
    pub_date = cur.get("reported_date") or "-"
    freshness = result.get("as_of") or "-"
    source = result.get("source", "Yahoo Finance Fundamentals API")
    quality = result.get("data_quality", "REPORTED")

    out = [
        "=" * 80,
        "%s (%s), Exchange: %s | Currency: %s | Unit: %s" % (
            entity, ticker, exchange, curr, unit_label
        ),
        "[SOURCE: %s | DATA: %s | STATUS: FRESH]" % (source, quality),
        "Reporting Period: %s (ended %s) | Publication Date: %s" % (
            rep_period, cur.get("end", "-"), pub_date
        ),
        "Data Freshness: %s" % freshness,
        "=" * 80,
        "",
        "%-11s %-9s %10s %8s %10s %7s %10s %7s %8s %8s %10s" % (
            "quarter end", "fiscal", "revenue", "rev yoy", "oper prof", "op mar",
            "net prof", "net mar", "EPS dil", "EPS yoy", "pub date"
        ),
    ]
    for r in rows:
        out.append("%-11s %-9s %10s %8s %10s %7s %10s %7s %8s %8s %10s" % (
            r["end"], r["fiscal"][:9], unit_fmt(r["revenue"]), _p(r["revenue_yoy"]),
            unit_fmt(r.get("operating_income")), _pm(r.get("operating_margin")),
            unit_fmt(r["net_income"]), _pm(r.get("net_margin")),
            "-" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"],
            _p(r["eps_yoy"]), r.get("reported_date") or "-"
        ))

    t = result.get("ttm", {})
    if t:
        out.append("")
        out.append("Last 4 quarters (TTM): revenue %s%s %s, operating profit %s%s %s, net profit %s%s %s, diluted EPS %s%s." % (
            curr_sym, unit_fmt(t.get("revenue")), "Cr" if curr == "INR" else "M",
            curr_sym, unit_fmt(t.get("operating_income")), "Cr" if curr == "INR" else "M",
            curr_sym, unit_fmt(t.get("net_income")), "Cr" if curr == "INR" else "M",
            curr_sym, "%.2f" % t["eps_diluted"] if t.get("eps_diluted") is not None else "-"
        ))

    annual_rows = result.get("annual_rows", [])
    if show_annual and annual_rows:
        out.append("")
        out.append("Annual Financials (Fiscal Years):")
        out.append("%-8s %-11s %10s %8s %10s %7s %10s %7s %8s %8s" % (
            "fiscal", "year end", "revenue", "rev yoy", "oper prof", "op mar",
            "net prof", "net mar", "EPS dil", "EPS yoy"
        ))
        for a in annual_rows:
            out.append("%-8s %-11s %10s %8s %10s %7s %10s %7s %8s %8s" % (
                a["fiscal"], a["end"], unit_fmt(a["revenue"]), _p(a["revenue_yoy"]),
                unit_fmt(a.get("operating_income")), _pm(a.get("operating_margin")),
                unit_fmt(a["net_income"]), _pm(a.get("net_margin")),
                "-" if a["eps_diluted"] is None else "%.2f" % a["eps_diluted"],
                _p(a["eps_yoy"])
            ))

    bs = result.get("balance_sheet", {})
    cf = result.get("cash_flow", {})
    if bs or cf:
        out.append("")
        out.append("Balance Sheet & Cash Flow Metrics (reported):")
        if bs.get("total_assets") is not None:
            out.append("  Total Assets:         %s%s %s (as of %s)" % (
                curr_sym, unit_fmt(bs["total_assets"]), "Cr" if curr == "INR" else "M", bs.get("as_of_date", "-")
            ))
        if bs.get("total_debt") is not None:
            out.append("  Total Debt:           %s%s %s" % (
                curr_sym, unit_fmt(bs["total_debt"]), "Cr" if curr == "INR" else "M"
            ))
        if bs.get("cash_and_equivalents") is not None:
            out.append("  Cash & Equivalents:   %s%s %s" % (
                curr_sym, unit_fmt(bs["cash_and_equivalents"]), "Cr" if curr == "INR" else "M"
            ))
        if bs.get("common_equity") is not None:
            out.append("  Stockholders Equity:  %s%s %s" % (
                curr_sym, unit_fmt(bs["common_equity"]), "Cr" if curr == "INR" else "M"
            ))
        if bs.get("debt_to_equity") is not None:
            out.append("  Debt to Equity:       %.1f%%" % bs["debt_to_equity"])
        if bs.get("current_ratio") is not None:
            out.append("  Current Ratio:        %.2fx" % bs["current_ratio"])
        if bs.get("book_value") is not None:
            bv_sym = "Rs " if bs.get("book_value_currency", trading_curr) == "INR" else curr_sym
            out.append("  Book Value per share: %s%.2f" % (bv_sym, bs["book_value"]))
        if bs.get("price_to_book") is not None:
            out.append("  Price to Book:        %.2fx" % bs["price_to_book"])
        if cf.get("operating_cash_flow") is not None:
            out.append("  Annual Operating CF:  %s%s %s" % (
                curr_sym, unit_fmt(cf["operating_cash_flow"]), "Cr" if curr == "INR" else "M"
            ))
        if cf.get("free_cash_flow") is not None:
            out.append("  Annual Free CF:       %s%s %s" % (
                curr_sym, unit_fmt(cf["free_cash_flow"]), "Cr" if curr == "INR" else "M"
            ))
        if curr != trading_curr:
            out.append("  [CURRENCY NOTE] Financial statements are in %s (e.g. US SEC Form 20-F/ADR via Yahoo Finance); market price & book value are in %s." % (curr, trading_curr))

    out.append("")
    out.append("What changed:")
    for line in changes_indian(result):
        out.append("  - " + line)

    if result.get("next_earnings_date"):
        out.append("")
        out.append("Next expected earnings report: %s (estimated date)" % result["next_earnings_date"])

    out.append("")
    out.append("[DATA QUALITY NOTE] Reported financials are sourced from Yahoo Finance API (aggregated/standardized statements).")
    out.append("They do not represent direct exchange filings from NSE/BSE.")
    out.append("Missing figures remain '-' and are never substituted with zero.")
    return "\n".join(out) + "\n"


def metrics_row(ticker, result):
    if result.get("market") == "IN":
        return metrics_row_indian(ticker, result)
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"ticker": ticker, "name": result["entity"], "cik": result["cik"],
            "period_end": cur.get("end"), "revenue_ttm": t.get("revenue"),
            "net_income_ttm": t.get("net_income"), "eps_ttm": t.get("eps_diluted"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "net_income_growth_yoy": cur.get("net_income_yoy"),
            "gross_margin": cur.get("gross_margin"), "operating_margin": cur.get("operating_margin"),
            "net_margin": cur.get("net_margin"), "source": "SEC XBRL company facts"}


def metrics_row_indian(ticker, result):
    rows = result.get("rows", [])
    cur = rows[0] if rows else {}
    t = result.get("ttm", {})
    bs = result.get("balance_sheet", {})
    cf = result.get("cash_flow", {})
    return {
        "ticker": ticker,
        "name": result.get("entity", ticker),
        "cik": None,
        "exchange": result.get("exchange", "NSE"),
        "currency": result.get("currency", "INR"),
        "period_end": cur.get("end"),
        "publication_date": cur.get("reported_date"),
        "revenue_ttm": t.get("revenue"),
        "operating_profit_ttm": t.get("operating_income"),
        "net_income_ttm": t.get("net_income"),
        "eps_ttm": t.get("eps_diluted"),
        "revenue_growth_yoy": cur.get("revenue_yoy"),
        "operating_growth_yoy": cur.get("operating_yoy"),
        "eps_growth_yoy": cur.get("eps_yoy"),
        "net_income_growth_yoy": cur.get("net_income_yoy"),
        "gross_margin": cur.get("gross_margin"),
        "operating_margin": cur.get("operating_margin"),
        "net_margin": cur.get("net_margin"),
        "total_assets": bs.get("total_assets"),
        "total_debt": bs.get("total_debt"),
        "cash_and_equivalents": bs.get("cash_and_equivalents"),
        "debt_to_equity": bs.get("debt_to_equity"),
        "current_ratio": bs.get("current_ratio"),
        "free_cash_flow": cf.get("free_cash_flow"),
        "operating_cash_flow": cf.get("operating_cash_flow"),
        "source": result.get("source", "Yahoo Finance Fundamentals API"),
        "data_quality": result.get("data_quality", "REPORTED"),
        "freshness": result.get("freshness", "FRESH"),
    }


def clean_title(title):
    return re.sub(r"\s*/[A-Z]{2,5}/?\s*$", "", title or "").strip() or None


def ticker_ciks(fetcher):
    """ticker -> (10-digit CIK, company name) from company_tickers.json."""
    data = fetcher.json(TICKERS_URL, "company_tickers.json")
    out = {}
    for v in data.values():
        out.setdefault(v["ticker"].upper().replace("-", "."),
                       (str(v["cik_str"]).zfill(10), clean_title(v.get("title"))))
    return out


TOP_INDIAN_SYMBOLS = {
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL",
    "ITC", "LT", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "MARUTI", "ASIANPAINT",
    "TITAN", "SUNPHARMA", "TATAMOTORS", "ULTRACEMCO", "WIPRO", "NTPC", "HCLTECH",
    "POWERGRID", "ONGC", "ADANIENT", "ADANIPORTS", "COALINDIA", "JSWSTEEL",
    "TATASTEEL", "M&M", "BAJAJFINSV", "EICHERMOT", "DRREDDY", "DIVISLAB",
    "CIPLA", "TECHM", "HEROMOTOCO", "GRASIM", "BPCL", "BRITANNIA", "HINDALCO",
    "APOLLOHOSP", "SHRIRAMFIN", "TATACONSUM", "NESTLEIND", "HINDUNILVR",
    "BEL", "HAL", "ZOMATO", "JIOFIN", "VEDL", "TRENT", "DLF", "VBL", "CHOLAFIN",
    "SIEMENS", "PFC", "RECLTD", "IOC", "GAIL", "ABB", "HAVELLS", "PIDILITIND",
    "INDIGO", "BANKBARODA", "PNB", "CANBK", "UNIONBANK", "IDFCFIRSTB", "FEDERALBNK"
}


def is_indian_ticker(ticker, exchange=None, offline=None, ciks=None):
    if not ticker or not isinstance(ticker, str):
        return False
    t = ticker.strip().upper()
    if exchange in ("NSE", "BSE") or os.environ.get("BHARAT_MARKET") == "IN":
        return True
    if t.endswith((".NS", ".BO")) or t.startswith(("NSE:", "BSE:")):
        return True
    clean = t.replace(".NS", "").replace(".BO", "").split(":")[-1]
    if offline:
        for name in ("indian-%s.json" % clean, "%s.NS.json" % clean, "%s.json" % clean):
            if os.path.isfile(os.path.join(offline, name)):
                return True
    if ciks and t.replace("-", ".") in ciks:
        return False
    if clean in TOP_INDIAN_SYMBOLS:
        return True
    if symbols_in and hasattr(symbols_in, "is_index_symbol") and symbols_in.is_index_symbol(clean):
        return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tickers", nargs="*", help="tickers, e.g. AAPL KO or RELIANCE TCS INFY")
    ap.add_argument("--cik", help="use this CIK instead of a ticker lookup (one company)")
    ap.add_argument("--exchange", choices=["NSE", "BSE"], help="exchange for Indian tickers (default: NSE)")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--annual", action="store_true", help="include annual financials table")
    ap.add_argument("--no-filings", action="store_true", help="skip the 8-K and 10-Q links")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per ticker into this JSON")
    ap.add_argument("--user-agent")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.tickers and not args.cik:
        ap.print_help()
        return 2

    wanted = args.tickers or ([args.cik] if args.cik else [])

    # Check if SEC user-agent is required (only for US filers)
    has_us_tickers = bool(args.cik)
    if not args.offline and not has_us_tickers:
        for t in wanted:
            if not is_indian_ticker(t, exchange=args.exchange, offline=args.offline, ciks=None):
                has_us_tickers = True
                break

    ua = user_agent(args.user_agent, args.config)
    if has_us_tickers and not ua and not args.offline:
        print("facts: " + UA_HELP, file=sys.stderr)
        return 2

    fetcher = Fetcher(ua, offline=args.offline)
    results, rows, failed = [], [], 0
    ciks = {}
    if not args.cik and (has_us_tickers or not wanted or args.offline):
        try:
            ciks = ticker_ciks(fetcher)
        except FactsError:
            ciks = {}

    for t in wanted:
        tick = t.upper()
        # 1. Determine if Indian or US
        if not args.cik and is_indian_ticker(tick, exchange=args.exchange, offline=args.offline, ciks=ciks):
            try:
                data = fetcher.get_indian(tick, exchange=args.exchange)
                result = analyse_indian(data, quarters=args.quarters, annual=args.annual)
                changes_lines = changes_indian(result)
            except FactsError as exc:
                print("facts: %s: %s" % (tick, exc), file=sys.stderr)
                failed += 1
                continue
            results.append({"ticker": tick, "result": result, "filings": None,
                            "changes": changes_lines, "note": None})
            rows.append(metrics_row_indian(tick, result))
            if not args.json:
                sys.stdout.write(render_indian(tick, result, show_annual=args.annual))
                sys.stdout.write("\n")
            continue

        # 2. US SEC XBRL
        if args.cik:
            cik, name = str(args.cik).zfill(10), None
        else:
            cik, name = ciks.get(tick.replace("-", "."), (None, None))
        if not cik:
            # Check if online Indian lookup works as fallback
            if not args.offline:
                try:
                    data = fetcher.get_indian(tick, exchange=args.exchange)
                    result = analyse_indian(data, quarters=args.quarters, annual=args.annual)
                    changes_lines = changes_indian(result)
                    results.append({"ticker": tick, "result": result, "filings": None,
                                    "changes": changes_lines, "note": None})
                    rows.append(metrics_row_indian(tick, result))
                    if not args.json:
                        sys.stdout.write(render_indian(tick, result, show_annual=args.annual))
                        sys.stdout.write("\n")
                    continue
                except FactsError:
                    pass
            print("facts: no SEC CIK for %s (is it a US filer? ETFs and foreign filers "
                  "may not have XBRL facts)" % tick, file=sys.stderr)
            failed += 1
            continue
        try:
            cf = fetcher.json(FACTS_URL % cik, "companyfacts-CIK%s.json" % cik)
            result = analyse(cf, quarters=args.quarters, name=name)
            filings = None
            if not args.no_filings:
                sub = fetcher.json(SUBMISSIONS_URL % cik, "submissions-CIK%s.json" % cik)
                filings = latest_filings(sub, cik, fetcher)
        except FactsError as exc:
            print("facts: %s: %s" % (tick, exc), file=sys.stderr)
            failed += 1
            continue
        results.append({"ticker": tick, "result": result, "filings": filings,
                        "changes": changes(result), "note": stale_note(result, filings)})
        rows.append(metrics_row(tick, result))
        if not args.json:
            sys.stdout.write(render(tick, result, filings))
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps(results, indent=2))
    if args.metrics and rows:
        existing = {}
        if os.path.isfile(args.metrics):
            with open(args.metrics, encoding="utf-8") as fh:
                existing = {r["ticker"]: r for r in json.load(fh).get("companies", [])}
        for r in rows:
            existing[r["ticker"]] = r
        folder = os.path.dirname(args.metrics)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump({"as_of": _dt.date.today().isoformat(),
                       "companies": sorted(existing.values(), key=lambda r: r["ticker"])},
                      fh, indent=2)
            fh.write("\n")
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
