import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-10-14T00:00:00+00:00"


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_yahoo_rss(self):
        items = news.parse_rss(raw("yahoo-AAPL.xml"), "AAPL")
        self.assertEqual(len(items), 6)
        first = items[0]
        self.assertEqual(first["title"], "Apple may have a way to dominate the smart home space")
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 13, 19, 46, 52,
                                                          tzinfo=datetime.timezone.utc))
        self.assertTrue(first["link"].startswith("https://finance.yahoo.com/"))
        self.assertTrue(first["id"].startswith("rss:"))

    def test_sec_atom_8k(self):
        items = news.parse_sec_atom(raw("sec-8-K-0000320193.xml"), "AAPL")
        self.assertEqual([i["form"] for i in items], ["8-K/A", "8-K", "8-K"])
        earn = items[1]
        self.assertEqual(earn["items"], ["2.02", "9.01"])
        self.assertTrue(earn["important"])
        self.assertEqual(earn["title"], "Apple Inc. filed a 8-K: earnings results")
        self.assertEqual(earn["id"], "sec:0000320193-26-000018")
        self.assertIn("000032019326000018", earn["link"])
        # 16:30:28 New York time is 20:30:28 UTC
        self.assertEqual(earn["time"].isoformat(), "2026-07-30T20:30:28+00:00")
        self.assertEqual(items[0]["title"], "Apple Inc. filed a 8-K/A: director or officer change")

    def test_times(self):
        self.assertEqual(news.parse_time("Tue, 13 Oct 2026 20:02:03 +0000").hour, 20)
        self.assertEqual(news.parse_time("2026-09-01T16:30:35-04:00").hour, 20)
        self.assertIsNone(news.parse_time("soon"))

    def test_yahoo_symbols(self):
        self.assertEqual(news.yahoo_symbol("BRK.B"), "BRK-B")


class Tagging(unittest.TestCase):
    def test_touches_and_tags(self):
        items = news.parse_rss(raw("yahoo-KO.xml"), "KO")
        watch = {"KO": "Coca-Cola", "CVX": "Chevron", "AAPL": "Apple"}
        news.tag(items, watch)
        by = {i["title"][:20]: i for i in items}
        chevron = by["Coca-Cola or Chevron"]
        self.assertEqual(chevron["touches"], ["CVX", "KO"])
        self.assertIn("capital", chevron["tags"])  # dividend
        self.assertEqual(by["Is Coca-Cola (KO) St"]["touches"], ["KO"])

    def test_earnings_filings_are_tagged(self):
        items = news.tag(news.parse_sec_atom(raw("sec-8-K-0000021344.xml"), "KO"), {"KO": None})
        self.assertEqual(items[0]["tags"][0], "earnings")
        self.assertFalse(items[1]["important"])  # 8.01 other events

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("yahoo-AAPL.xml"), "AAPL")
        b = news.parse_rss(raw("yahoo-AAPL.xml"), "GOOGL")
        items = news.dedupe(news.tag(a + b, {"AAPL": None, "GOOGL": None}))
        self.assertEqual(len(items), 6)
        self.assertEqual(items[0]["touches"], ["AAPL", "GOOGL"])


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", os.path.join(self.tmp.name, "none.json"),
                     "--watchlist", os.path.join(self.tmp.name, "none.json"),
                     "--plan", os.path.join(self.tmp.name, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "AAPL", "KO", "--hours", "24", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(data["items"]), 10)  # 6 Apple + 4 Coca-Cola headlines, filings are older
        code, out, _ = run(["--tickers", "AAPL", "KO", "--hours", "24"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_filings_show_in_a_wider_window(self):
        code, out, err = run(["--tickers", "AAPL", "--hours", "2400"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("! 2026-07-30 16:30 ET  AAPL", out)  # 20:30 UTC, New York time
        self.assertIn("earnings results", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "KO", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "AAPL"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["AAPL", "KO"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "24"] + args)
        self.assertEqual(code, 0, err)
        tickers = {t for i in json.loads(out)["items"] for t in i["touches"]}
        self.assertEqual(tickers, {"KO"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_tickers_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)

    def test_sec_needs_a_user_agent(self):
        old = os.environ.pop("SEC_USER_AGENT", None)
        try:
            self.assertIsNone(news.sec_user_agent(None, "/nonexistent.json"))
            self.assertIsNone(news.sec_user_agent("Your Name you@example.com"))
            self.assertEqual(news.sec_user_agent("Ada ada@example.org"), "Ada ada@example.org")
            with self.assertRaises(news.NewsError):
                news.Fetcher(None).get("https://www.sec.gov/x", "x", sec=True)
        finally:
            if old is not None:
                os.environ["SEC_USER_AGENT"] = old

    def test_the_reel_tsla_morning(self):
        # The reel's real feed: Yahoo Finance headlines for TSLA, Monday Oct 19 2026, checked at 10:15 AM ET
        code, out, err = run(["--tickers", "TSLA", "--hours", "6", "--no-sec", "--no-moves",
                              "--now", "2026-10-19T10:15:00-04:00"] + self.base[:4])
        self.assertEqual(code, 0, err)
        self.assertIn("2026-10-19 08:04 ET  TSLA", out)
        self.assertNotIn("10:32 ET", out)  # published after the pinned 10:15 clock
        self.assertIn("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", out)
        merger = [l for l in out.splitlines() if "Merger Mania" in l]
        tags = out.splitlines()[out.splitlines().index(merger[0]) + 1]
        self.assertIn("earnings", tags)
        self.assertIn("deal", tags)
        self.assertIn("Checked 2026-10-19 10:15 ET", out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("  2026-10-19")]), 16)  # 18 in the feed, 2 after 10:15
        # the price line, from the chart fetched at 10:52
        code, out, err = run(["--tickers", "TSLA", "--hours", "6", "--no-sec", "--dry-run",
                              "--now", "2026-10-19T10:52:00-04:00"] + self.base[:4])
        self.assertRegex(out, r"TSLA\s+366\.4[78] \+3\.31% vs yesterday's close 354\.72 at 10:52 ET  MOVING")

    def test_moves(self):
        m = news.parse_chart(raw("chart-TSLA.json"), "TSLA")
        self.assertEqual(m["prev_close"], 354.72)
        self.assertEqual(m["pct"], 3.31)
        self.assertEqual(news.to_eastern(m["time"]).strftime("%H:%M"), "10:52")
        quiet = dict(m, pct=1.2)
        self.assertNotIn("MOVING", news.render_moves([quiet])[0])
        self.assertIn("MOVING", news.render_moves([quiet], move_pct=1.0)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        # no chart file offline: no move line, no error note
        code, out, _ = run(["--tickers", "KO", "--hours", "24", "--dry-run"] + self.base)
        self.assertNotIn("vs yesterday's close", out)
        self.assertNotIn("note:", out)

    def test_new_york_time_follows_daylight_saving(self):
        utc = datetime.datetime(2026, 1, 15, 15, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(news.to_eastern(utc).hour, 10)  # EST, UTC-5
        utc = datetime.datetime(2026, 10, 19, 14, 15, tzinfo=datetime.timezone.utc)
        self.assertEqual(news.to_eastern(utc).hour, 10)  # EDT, UTC-4
        utc = datetime.datetime(2026, 11, 1, 6, 30)       # 1:30 EST, just after the switch back
        self.assertEqual(news.to_eastern(utc).hour, 1)

    def test_day_trading_tags(self):
        items = [{"id": "x", "source": "Yahoo Finance", "kind": "headline", "ticker": "TSLA",
                  "title": "Trading halted in XYZ after FDA approval; futures rise before CPI",
                  "summary": "", "link": "", "time": None}]
        tags = news.tag(items, {"TSLA": None})[0]["tags"]
        for t in ("halt", "regulatory", "macro"):
            self.assertIn(t, tags)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-08-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


class IndianNews(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        self.now_in = "2026-11-10T22:00:00+00:00"
        self.base = ["--offline", DIR, "--state", self.state, "--now", self.now_in,
                     "--portfolio", os.path.join(self.tmp.name, "none.json"),
                     "--watchlist", os.path.join(self.tmp.name, "none.json"),
                     "--plan", os.path.join(self.tmp.name, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_symbol_normalization(self):
        # Case insensitivity, suffixes, exchange prefixes
        self.assertEqual(news.normalize_ticker("reliance"), ("RELIANCE", "NSE"))
        self.assertEqual(news.normalize_ticker("TCS.NS"), ("TCS", "NSE"))
        self.assertEqual(news.normalize_ticker("BSE:INFY"), ("INFY", "BSE"))
        self.assertEqual(news.normalize_ticker("HDFCBANK.BO"), ("HDFCBANK", "BSE"))
        self.assertEqual(news.normalize_ticker("AAPL"), ("AAPL", None))

        # Yahoo Finance ticker conversion
        self.assertEqual(news.yahoo_symbol("RELIANCE"), "RELIANCE.NS")
        self.assertEqual(news.yahoo_symbol("INFY", exchange="BSE"), "INFY.BO")
        self.assertEqual(news.yahoo_symbol("TCS.NS"), "TCS.NS")
        self.assertEqual(news.yahoo_symbol("AAPL"), "AAPL")
        self.assertEqual(news.yahoo_symbol("BRK.B"), "BRK-B")

    def test_ist_time_and_timestamp_preservation(self):
        utc = datetime.datetime(2026, 11, 10, 7, 21, 45, tzinfo=datetime.timezone.utc)
        ist = news.to_ist(utc)
        self.assertEqual(ist.hour, 12)
        self.assertEqual(ist.minute, 51)
        self.assertEqual(ist.second, 45)

    def test_google_news_rss_parsing_and_attribution(self):
        items = news.parse_rss(raw("google-RELIANCE.xml"), "RELIANCE", default_source="Google News")
        self.assertGreater(len(items), 10)
        first = items[0]
        self.assertTrue(first["publisher"])
        self.assertTrue(first["source_url"].startswith("http"))
        self.assertEqual(first["ticker"], "RELIANCE")
        self.assertIsNotNone(first["time"])
        self.assertIn("IST", first["time_ist"])

    def test_distinguish_price_movement_vs_actual_news(self):
        # Automated quote tracker headlines must be flagged
        tracker1 = "Reliance Industries Share Price - Live NSE: RELIANCE Stock Price & Chart - Upstox"
        tracker2 = "INFY Share Price Today - Infosys Ltd. Stock Price Live NSE/BSE - Univest"
        tracker3 = "HDFC Bank Limited (HDFCBANK.NS) stock price, news, quote and history - Yahoo Finance Australia"
        self.assertTrue(news.is_price_movement_headline(tracker1))
        self.assertTrue(news.is_price_movement_headline(tracker2))
        self.assertTrue(news.is_price_movement_headline(tracker3))

        # Genuine news and corporate announcements must not be flagged as price movements
        news1 = "Retail investors shed stocks, but crowd into Reliance, HDFC Bank - Livemint"
        news2 = "TCS schedules analyst meetings in Mumbai and Singapore for November 2026 - scanx.trade"
        news3 = "Infosys Consolidated September 2026 Net Sales at Rs 49,244.00 crore - Moneycontrol"
        self.assertFalse(news.is_price_movement_headline(news1))
        self.assertFalse(news.is_price_movement_headline(news2))
        self.assertFalse(news.is_price_movement_headline(news3))

    def test_deduplication_across_feeds(self):
        story1 = {
            "id": "rss:111", "source": "Livemint", "source_url": "https://www.livemint.com",
            "publisher": "Livemint", "kind": "headline", "ticker": "RELIANCE",
            "title": "Retail investors shed stocks, but crowd into Reliance, HDFC Bank - Livemint",
            "summary": "Report on investor activity", "link": "https://news.google.com/1",
            "time": datetime.datetime(2026, 11, 10, 7, 21, 45, tzinfo=datetime.timezone.utc),
            "touches": ["RELIANCE"]
        }
        story2 = {
            "id": "rss:222", "source": "TradingView", "source_url": "https://www.tradingview.com",
            "publisher": "TradingView", "kind": "headline", "ticker": "RELIANCE",
            "title": "Retail investors shed stocks, but crowd into Reliance, HDFC Bank - TradingView",
            "summary": "Syndicated article on investor activity", "link": "https://news.google.com/2",
            "time": datetime.datetime(2026, 11, 10, 10, 35, 11, tzinfo=datetime.timezone.utc),
            "touches": ["HDFCBANK", "RELIANCE"]
        }
        deduped = news.dedupe([story1, story2])
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["touches"], ["HDFCBANK", "RELIANCE"])

    def test_three_indian_companies_offline_announcements(self):
        # Validate multi-ticker invocation for RELIANCE, TCS and INFY with actual corporate announcements
        code, out, err = run(["--tickers", "RELIANCE", "TCS", "INFY", "--hours", "72"] + self.base)
        self.assertEqual(code, 0, err)

        # Check moves lines with IST timestamps
        self.assertIn("RELIANCE", out)
        self.assertIn("TCS", out)
        self.assertIn("INFY", out)
        self.assertIn("at 15:28 IST", out)

        # Check actual known corporate developments retrieved from fixtures
        self.assertIn("Reliance", out)
        self.assertIn("TCS", out)
        self.assertIn("Infosys", out)

        # Check IST publication timestamps and checked line
        self.assertIn("IST", out)
        self.assertIn("Checked 2026-11-11 03:30 IST", out)

        # Verify no em-dash in output
        self.assertNotIn(chr(0x2014), out)

    def test_indian_json_output_fields(self):
        code, out, err = run(["--tickers", "RELIANCE", "TCS", "--hours", "72", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("moves", data)
        self.assertIn("items", data)
        self.assertGreater(len(data["items"]), 5)
        for item in data["items"]:
            self.assertIn("source", item)
            self.assertIn("publisher", item)
            self.assertIn("time", item)
            self.assertIn("link", item)
            self.assertIn("ticker", item)

    def test_mixed_us_and_indian_tickers(self):
        # Test TSLA (US) and RELIANCE (India) in one run
        code, out, err = run(["--tickers", "TSLA", "RELIANCE", "--hours", "2400", "--no-sec"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("TSLA", out)
        self.assertIn("RELIANCE", out)
        self.assertIn("ET", out)
        self.assertIn("IST", out)

    def test_incidental_mentions_and_low_relevance(self):
        # Incidental mentions, marathon sponsorships, and peripheral coverage should be classified as low_relevance
        item1 = {"title": "Environmentalists criticise choice of Earthshot prize venue owned by fossil fuel firm - The Guardian", "summary": "", "kind": "headline"}
        cat1, sub1, score1 = news.classify_news_item(item1, "RELIANCE")
        self.assertEqual(cat1, "low_relevance")

        item2 = {"title": "TCS drove a 38.6% media surge via AI activations at the marathon - The Drum", "summary": "", "kind": "headline"}
        cat2, sub2, score2 = news.classify_news_item(item2, "TCS")
        self.assertEqual(cat2, "low_relevance")

        item3 = {"title": "TCS-Partnered Marathons Generate US$3.2 Billion for Host Cities and Raise US$315 Million for Charity", "summary": "", "kind": "headline"}
        cat3, sub3, score3 = news.classify_news_item(item3, "TCS")
        self.assertEqual(cat3, "low_relevance")

        item4 = {"title": "Buy, Sell Or Hold: Divi's Labs, Tata Power, Kalyan Jewellers, Reliance Industries, L&T And Shriram Finance - Ask Profit", "summary": "", "kind": "headline"}
        cat4, sub4, score4 = news.classify_news_item(item4, "RELIANCE")
        self.assertEqual(cat4, "low_relevance")

    def test_subsidiary_and_group_company_detection(self):
        # Reliance Consumer Products FMCG subsidiary
        item1 = {"title": "Why Britannia isn't losing sleep over Reliance's entry into biscuits - Fortune India", "summary": "", "kind": "headline"}
        cat1, sub1, _ = news.classify_news_item(item1, "RELIANCE")
        self.assertEqual(sub1, "Reliance Consumer Products")
        self.assertEqual(cat1, "business")

        # Reliance New Energy / Clean Energy subsidiary
        item2 = {"title": "ALMM-II: Reliance Revises HJT Solar Cell Production, Gets Enlistment For 3.4 GW Capacity - Saur Energy", "summary": "", "kind": "headline"}
        cat2, sub2, _ = news.classify_news_item(item2, "RELIANCE")
        self.assertEqual(sub2, "Reliance New Energy")
        self.assertEqual(cat2, "corporate")

        # Reliance Jio / Jio Platforms subsidiary
        item3 = {"title": "AMFI Rejig: Reliance's Jio Platforms, NSE, Lenskart, LG among 7 stocks set for large cap upgrade - Livemint", "summary": "", "kind": "headline"}
        cat3, sub3, _ = news.classify_news_item(item3, "RELIANCE")
        self.assertEqual(sub3, "Jio Platforms / Reliance Jio")

        # Reliance ADAG (separate listed group entity) flagged as low_relevance for RIL
        item4 = {"title": "5 Reliance Group Stocks Trading at Discounts of Up to 50% From Their 52-Week Highs - Trade Brains", "summary": "", "kind": "headline"}
        cat4, sub4, _ = news.classify_news_item(item4, "RELIANCE")
        self.assertEqual(cat4, "low_relevance")
        self.assertIn("ADAG", sub4)

    def test_analyst_opinions_categorization(self):
        # Brokerage ratings and price targets
        item1 = {"title": "Jefferies sees largecaps benefiting from FPI revival; Reliance, HDFC Bank, Axis Bank among potential benef - The Economic Times", "summary": "", "kind": "headline"}
        cat1, _, _ = news.classify_news_item(item1, "RELIANCE")
        self.assertEqual(cat1, "analyst")

        item2 = {"title": "Tata Consultancy Services Limited: 29 of 48 analysts buy - Multibagg AI", "summary": "", "kind": "headline"}
        cat2, _, _ = news.classify_news_item(item2, "TCS")
        self.assertEqual(cat2, "analyst")

        item3 = {"title": "Analysts Have Made A Financial Statement On Tata Consultancy Services Limited's (NSE:TCS) Second-Quarter Report", "summary": "", "kind": "headline"}
        cat3, _, _ = news.classify_news_item(item3, "TCS")
        self.assertEqual(cat3, "analyst")

    def test_corporate_announcements_categorization(self):
        # Regulatory/tax tribunal orders, capital investments, contract wins, earnings calls
        item1 = {"title": "GSTAT Rajkot Dismisses 8 Revenue Appeals, Upholds ITC Refund Relief To Reliance Industries - LiveLawBiz", "summary": "", "kind": "headline"}
        cat1, _, _ = news.classify_news_item(item1, "RELIANCE")
        self.assertEqual(cat1, "corporate")

        item2 = {"title": "India's TCS unit to invest up to $7.4 billion in AI data center campus - Reuters", "summary": "", "kind": "headline"}
        cat2, _, _ = news.classify_news_item(item2, "TCS")
        self.assertEqual(cat2, "corporate")

        item3 = {"title": "TCS schedules analyst meetings in Mumbai and Singapore for November 2026 - scanx.trade", "summary": "", "kind": "headline"}
        cat3, _, _ = news.classify_news_item(item3, "TCS")
        self.assertEqual(cat3, "corporate")

        item4 = {"title": "What Infosys (INFY) Said on Its Q2 Earnings Call - TradingView", "summary": "", "kind": "headline"}
        cat4, _, _ = news.classify_news_item(item4, "INFY")
        self.assertEqual(cat4, "corporate")

    def test_relevance_ranking_and_research_flag(self):
        # Verify corporate > business > analyst > market ranking, and research flag inclusion
        code, out_default, err = run(["--tickers", "RELIANCE", "--hours", "72", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertNotIn("Environmentalists criticise", out_default)
        self.assertIn("low-relevance excluded (pass --research to view)", out_default)

        # When --research is provided, low relevance stories appear
        code, out_research, err = run(["--tickers", "RELIANCE", "--hours", "72", "--dry-run", "--research"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("Environmentalists criticise", out_research)
        self.assertIn("[LOW_RELEVANCE]", out_research)

    def test_coverage_transparency(self):
        watch = {"RELIANCE": None, "TCS": None, "XYZ": None}
        sample_items = [
            {"ticker": "RELIANCE", "touches": ["RELIANCE"], "category": "corporate", "subsidiary": None},
            {"ticker": "RELIANCE", "touches": ["RELIANCE"], "category": "business", "subsidiary": "Reliance Consumer Products"},
            {"ticker": "TCS", "touches": ["TCS"], "category": "corporate", "subsidiary": None},
        ]
        cov = news.compute_coverage(watch, sample_items, ["price for XYZ: HTTP 404"])
        self.assertEqual(cov["RELIANCE"]["relevant"], 2)
        self.assertEqual(cov["RELIANCE"]["subsidiaries"], ["Reliance Consumer Products"])
        self.assertEqual(cov["RELIANCE"]["status"], "OK")
        self.assertEqual(cov["TCS"]["relevant"], 1)
        self.assertEqual(cov["XYZ"]["status"], "UNAVAILABLE")

    def test_publisher_attribution_and_url_preservation(self):
        items = news.parse_rss(raw("google-RELIANCE.xml"), "RELIANCE", default_source="Google News")
        item = next(i for i in items if "Livemint" in i["title"])
        self.assertEqual(item["publisher"], "Livemint")
        self.assertNotEqual(item["publisher"], "Google News")
        self.assertEqual(item["publisher_url"], "https://www.livemint.com")
        self.assertTrue(item["link"].startswith("https://news.google.com/rss/articles/"))
        self.assertEqual(item["article_url"], item["link"])


if __name__ == "__main__":
    unittest.main()
