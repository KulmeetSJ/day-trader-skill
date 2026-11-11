#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines and SEC filings for US stocks, and company news, corporate
announcements, and filings for Indian equities (NSE and BSE). Prints only what
is new since the last check, newest first, with how far each stock has moved
today and a flag on anything that touches your tickers.

    python3 news.py                                  # watchlist + plan + portfolio tickers
    python3 news.py --tickers TSLA NVDA AMD
    python3 news.py --tickers RELIANCE TCS INFY      # Indian equities (NSE/BSE)
    python3 news.py --tickers RELIANCE --research    # include low-relevance & incidental stories
    python3 news.py --tickers TSLA --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-sec                         # headlines only
    python3 news.py --no-moves                       # skip the price line per ticker

Your tickers, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json.

Sources:
  US (SEC and Yahoo):
    - Yahoo Finance headline RSS:
      https://feeds.finance.yahoo.com/rss/2.0/headline?s=<TICKER>&region=US&lang=en-US
    - SEC EDGAR filings Atom feed for the company (8-K by default: earnings,
      executive changes, deals, restatements):
      https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<CIK>&type=8-K&count=10&output=atom
      SEC needs a User-Agent with your name and email (sec_user_agent in
      trader/config.json or SEC_USER_AGENT). Without one the SEC part is skipped
      and the tool says so; headlines still run.

  India (NSE and BSE corporate news and disclosures):
    - Indian financial news and corporate announcements RSS:
      https://news.google.com/rss/search?q=<QUERY>&hl=en-IN&gl=IN&ceid=IN:en
    - Yahoo Finance headline RSS for Indian symbols:
      https://feeds.finance.yahoo.com/rss/2.0/headline?s=<TICKER.NS>&region=US&lang=en-US

  The move (both US and Indian equities):
    - Yahoo Finance chart endpoint (unofficial, can lag), the last
      price against yesterday's close:
      https://query2.finance.yahoo.com/v8/finance/chart/<TICKER>?range=1d&interval=5m

Times print in New York time (ET) for US stocks and Indian Standard Time (IST)
for Indian stocks. State: trader/state/news.json remembers what you have seen
(30 days), so an item shows once. Offline: --offline DIR reads feeds and charts
from DIR. The tests use it.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# Sibling path resolution for Indian market symbols (../trader-alerts)
try:
    import symbols_in  # type: ignore
    import data_in  # type: ignore
except ImportError:
    try:
        _alerts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-alerts")
        if os.path.isdir(_alerts_dir) and _alerts_dir not in sys.path:
            sys.path.insert(0, _alerts_dir)
        import symbols_in  # type: ignore
        import data_in  # type: ignore
    except ImportError:
        symbols_in = None
        data_in = None

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"
GOOGLE_NEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
SEC_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%s&type=%s"
            "&dateb=&owner=include&count=%d&output=atom")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
PLACEHOLDER = "you@example.com"
BROWSER_UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/KulmeetSJ/day-trader-skill)"
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving

CATEGORIES = ("corporate", "business", "analyst", "market", "low_relevance")

CATEGORY_RANK = {
    "corporate": 400,
    "business": 300,
    "analyst": 200,
    "market": 100,
    "low_relevance": 0,
}

ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement ended", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale of assets",
    "2.02": "earnings results", "2.03": "new debt", "2.04": "debt acceleration",
    "2.05": "restructuring costs", "2.06": "impairment", "3.01": "delisting notice",
    "3.02": "unregistered share sale", "3.03": "shareholder rights change",
    "4.01": "auditor change", "4.02": "past financials no longer reliable",
    "5.01": "change in control", "5.02": "director or officer change",
    "5.03": "bylaws or fiscal year change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other events", "9.01": "exhibits",
}
IMPORTANT_ITEMS = {"1.03", "1.05", "2.02", "2.04", "2.06", "3.01", "4.02", "5.01", "5.02"}

KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bforecast\b|\bquarterly results\b|\bfinancial results\b|\bnet profit\b|\bnet sales\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b|\bunderperform\b|\bbuy rating\b|\bsell rating\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEC charges\b|\bantitrust\b|\bjudge\b|\bcourt\b|\bNCLT\b|\bNCLAT\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b|\bjoint venture\b"),
    ("capital", r"\bdividend\b|\bbuyback\b|\brepurchase\b|\bstock split\b|\boffering\b|\bQIP\b|\brights issue\b|\bbonus issue\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bresign|\bsteps down\b|\bappoint|\bMD\b|\bboard of directors\b"),
    ("risk", r"\brecall\b|\bbankrupt|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)|\bcircuit breaker\b|\bupper circuit\b|\blower circuit\b"),
    ("regulatory", r"\bFDA\b|\bapproval\b|\bapproves\b|\bclinical\b|\bFTC\b|\bDOJ\b|\bSEBI\b|\bCCI\b"),
    ("macro", r"\bFed\b|\bFOMC\b|\bCPI\b|\binflation\b|\bjobs report\b|\bpayrolls\b|\btariffs?\b|\bfutures\b|\bRBI\b|\brepo rate\b|\bmonetary policy\b"),
    ("corporate", r"\bcorporate announcement\b|\bboard meeting\b|\bAGM\b|\bEGM\b|\bexchange filing\b|\binvestor presentation\b|\bclarification\b|\bdisclosure\b"),
]

TOP_INDIAN_SYMBOLS = {
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL", "SBIN",
    "ITC", "KOTAKBANK", "LT", "HINDUNILVR", "AXISBANK", "BAJFINANCE", "MARUTI",
    "TATAMOTORS", "SUNPHARMA", "NTPC", "ONGC", "TITAN", "ADANIENT", "ADANIPORTS",
    "POWERGRID", "ULTRACEMCO", "COALINDIA", "TATASTEEL", "BAJAJFINSV", "M&M",
    "SIEMENS", "PFC", "RECLTD", "IOC", "GAIL", "ABB", "HAVELLS", "PIDILITIND",
    "INDIGO", "BANKBARODA", "PNB", "CANBK", "UNIONBANK", "IDFCFIRSTB", "FEDERALBNK",
    "WIPRO", "HCLTECH", "TECHM", "DIVISLAB", "CIPLA", "APOLLOHOSP", "DRREDDY",
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"
}

INDIAN_COMPANY_NAMES = {
    "RELIANCE": "Reliance Industries",
    "TCS": "Tata Consultancy Services",
    "HDFCBANK": "HDFC Bank",
    "INFY": "Infosys",
    "ICICIBANK": "ICICI Bank",
    "BHARTIARTL": "Bharti Airtel",
    "SBIN": "State Bank of India",
    "ITC": "ITC Limited",
    "KOTAKBANK": "Kotak Mahindra Bank",
    "LT": "Larsen & Toubro",
    "HINDUNILVR": "Hindustan Unilever",
    "AXISBANK": "Axis Bank",
    "BAJFINANCE": "Bajaj Finance",
    "MARUTI": "Maruti Suzuki",
    "TATAMOTORS": "Tata Motors",
    "SUNPHARMA": "Sun Pharma",
    "NTPC": "NTPC Limited",
    "ONGC": "ONGC",
    "TITAN": "Titan Company",
    "ADANIENT": "Adani Enterprises",
    "ADANIPORTS": "Adani Ports",
    "POWERGRID": "Power Grid Corporation",
    "ULTRACEMCO": "UltraTech Cement",
    "COALINDIA": "Coal India",
    "TATASTEEL": "Tata Steel",
    "BAJAJFINSV": "Bajaj Finserv",
    "M&M": "Mahindra & Mahindra",
    "WIPRO": "Wipro",
    "HCLTECH": "HCL Technologies",
    "TECHM": "Tech Mahindra",
    "DIVISLAB": "Divi's Laboratories",
    "CIPLA": "Cipla",
    "APOLLOHOSP": "Apollo Hospitals",
    "DRREDDY": "Dr. Reddy's Laboratories",
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY Bank",
    "FINNIFTY": "NIFTY Financial Services",
    "MIDCPNIFTY": "NIFTY Midcap Select",
    "SENSEX": "BSE Sensex",
    "BANKEX": "BSE Bankex",
}

SUBSIDIARIES = {
    "RELIANCE": [
        ("Reliance Consumer Products", re.compile(r"\b(?:Reliance\s+Consumer(?:\s+Products)?|RCPL|Independence\s+biscuits?|entry\s+into\s+biscuits?|biscuit(?:s)?|cola\s+business)\b", re.I)),
        ("Reliance Retail", re.compile(r"\b(?:Reliance\s+Retail(?:\s+Ventures)?|RRVL)\b", re.I)),
        ("Jio Platforms / Reliance Jio", re.compile(r"\b(?:Reliance\s+Jio(?:\s+Infocomm)?|Jio\s+Platforms|Jio\s+telecom|Jio\s+Cinema)\b", re.I)),
        ("Reliance New Energy", re.compile(r"\b(?:Reliance\s+New\s+Energy|Reliance\s+Green|HJT\s+Solar|ALMM-II)\b", re.I)),
        ("Reliance ADAG (Separate Group)", re.compile(r"\b(?:Reliance\s+Power|Reliance\s+Infra(?:structure)?|Reliance\s+Capital|ADAG|Anil\s+Ambani|5\s+Reliance\s+Group\s+Stocks)\b", re.I)),
    ],
    "TCS": [
        ("Tata Group Parent", re.compile(r"\b(?:Tata\s+Sons|Tata\s+Group)\b", re.I)),
    ],
    "INFY": [
        ("Infosys BPM", re.compile(r"\b(?:Infosys\s+BPM)\b", re.I)),
        ("EdgeVerve", re.compile(r"\bEdgeVerve\b", re.I)),
    ],
    "HDFCBANK": [
        ("HDFC Life", re.compile(r"\b(?:HDFC\s+Life)\b", re.I)),
        ("HDFC AMC", re.compile(r"\b(?:HDFC\s+AMC|HDFC\s+Asset\s+Management)\b", re.I)),
        ("HDFC ERGO", re.compile(r"\b(?:HDFC\s+ERGO)\b", re.I)),
        ("HDFC Securities", re.compile(r"\b(?:HDFC\s+Securities)\b", re.I)),
    ]
}

LOW_RELEVANCE_PATTERNS = [
    re.compile(r"\b(?:marathon|marathons|media\s+surge\s+via\s+AI\s+activations)\b", re.I),
    re.compile(r"\b(?:earthshot|prize\s+venue|ceremony\s+to\s+be\s+held|venue\s+owned|wedding\s+venue)\b", re.I),
    re.compile(r"\b(?:wedding|cricket|ipl|charity\s+run|csr\s+award|marathons\s+generate)\b", re.I),
    re.compile(r"\b(?:5\s+Reliance\s+Group\s+Stocks\s+Trading)\b", re.I),
    re.compile(r"\b(?:Buy,\s+Sell\s+Or\s+Hold:.*Ask\s+Profit)\b", re.I),
]

CORPORATE_PATTERNS = [
    re.compile(r"\b(?:earnings|results|quarterly\s+results|net\s+profit|net\s+sales|revenue|PAT|EBITDA|financial\s+results|dividend|buyback|bonus\s+issue|stock\s+split|annual\s+report|second-quarter\s+report|earnings\s+call)\b", re.I),
    re.compile(r"\b(?:filing|8-K|10-Q|SEBI|circular|intimations?|exchange\s+filing|disclosures?|enlistment|ALMM|compliance)\b", re.I),
    re.compile(r"\b(?:invest(?:s|ment|ing)?\s+up\s+to|invests?\s+in|acquisition|acquires?|merger|demerger|takeover|stake\s+sale|stake\s+buy|divestment|QIP|rights\s+issue)\b", re.I),
    re.compile(r"\b(?:CEO|CFO|MD|appoints?|resigns?|steps?\s+down|board\s+meeting|AGM|EGM|shareholders?\s+meeting|variable\s+pay)\b", re.I),
    re.compile(r"\b(?:court|tribunal|NCLT|NCLAT|GSTAT|refund\s+relief|tax\s+order|ED\s+probe|CBI\s+probe|penalty|dismisses\s+.*appeals?)\b", re.I),
    re.compile(r"\b(?:schedules?\s+analyst\s+meetings?|investor\s+meetings?|offshore\s+gas\s+hub|shared\s+offshore\s+infrastructure|data\s+center\s+campus|Mahanadi\s+project)\b", re.I),
]

BUSINESS_PATTERNS = [
    re.compile(r"\b(?:launches|launching|unveils|rolls?\s+out|new\s+product|portfolio\s+expands?|entry\s+into\s+biscuits|biscuits?\s+portfolio|biscuit\s+market|cola\s+business|Industrial\s+Intelligence\s+Foundry|sovereign\s+cloud|AI\s+deployment\s+engineers)\b", re.I),
    re.compile(r"\b(?:partners?\s+with|partnership|alliances?|collaborat(?:e|ion)|joint\s+venture|tie-up|MOU)\b", re.I),
    re.compile(r"\b(?:higher\s+bench|subcontracting|expands?\s+operations|new\s+facility|opens?\s+new\s+campus)\b", re.I),
    re.compile(r"\b(?:secures?\s+contract|wins?\s+deal|bags?\s+order|multi-million\s+deal|commercial\s+agreement)\b", re.I),
]

ANALYST_PATTERNS = [
    re.compile(r"\b(?:target\s+price|price\s+target|Jefferies|Morgan\s+Stanley|Goldman\s+Sachs|Bernstein|CLSA|Nomura|Motilal\s+Oswal|ICICI\s+Direct|Kotak\s+Institutional|Emkay|Nuvama|Axis\s+Capital|Macquarie|UBS)\b", re.I),
    re.compile(r"\b(?:upgrade[sd]?|downgrade[sd]?|outperform|underperform|buy\s+rating|sell\s+rating|neutral\s+rating|accumulate|analysts?\s+(?:have\s+made|made|say|buy|predict|estimate|expect)|initiates?\s+coverage)\b", re.I),
]

MARKET_PATTERNS = [
    re.compile(r"\b(?:share\s+price|shares?\s+rally|shares?\s+drop|shares?\s+surge|shares?\s+fall|slides|slips|plunges|down\s+\d+%|falls?\s+\d+%|gain(?:s|ed)?\s+over|mcap\s+erosion|profit\s+booking|52-week\s+high|52-week\s+low|all-time\s+high|at\s+17-month\s+low|bottoming\s+out|market\s+correct|undervaluing|stock\s+radar|stocks?\s+in\s+focus|top\s+losers?|top\s+gainers?|Nifty\s+IT\s+falls|Sensex\s+declines|stock\s+markets?\s+today)\b", re.I),
    re.compile(r"\b(?:FPI\s+revival|FII|DII|retail\s+investors?\s+shed|AMFI\s+Rejig|large\s*cap\s+upgrade|index\s+rejig)\b", re.I),
]

# Regex to detect automated stock price tracker pages and separate them from real news
PRICE_TRACKER_PATTERN = re.compile(
    r"\b(?:share|stock)\s+price\s+(?:today|live)\b|"
    r"\bstock\s+price\s*&?\s*chart\b|"
    r"\b(?:share|stock)\s+price\s*:\s*.*(?:stock\s+price|live\s+nse|live\s+bse)|"
    r"\b(?:live\s+nse|live\s+bse)\s*:\s*|"
    r"\bstock\s+price,\s*news,\s*quote\s+and\s+history\b|"
    r"\bshare\s+price\s+today\s*\([a-z]+\),\s*market\s+news\b",
    re.I
)


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

IST_OFFSET = _dt.timedelta(hours=5, minutes=30)
IST_TZ = _dt.timezone(IST_OFFSET, name="IST")


def _nth_sunday(year, month, n):
    d = _dt.date(year, month, 1)
    d += _dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + _dt.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
    y = utc.year
    start = _dt.datetime.combine(_nth_sunday(y, 3, 2), _dt.time(7, 0))   # 2:00 EST
    end = _dt.datetime.combine(_nth_sunday(y, 11, 1), _dt.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    """Aware or naive UTC -> naive New York wall-clock time."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    return utc.replace(tzinfo=None) + _dt.timedelta(hours=eastern_offset(utc))


def to_ist(utc):
    """Aware or naive UTC -> naive Indian Standard Time (UTC+5:30)."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    return utc.replace(tzinfo=None) + IST_OFFSET


# ---------------------------------------------------------------- symbols & classification

def is_indian_ticker(ticker, exchange=None, offline=None):
    """Check if ticker corresponds to an Indian equity or index."""
    if not ticker or not isinstance(ticker, str):
        return False
    t = ticker.strip().upper()
    if (exchange or "").upper() in ("NSE", "BSE") or os.environ.get("BHARAT_MARKET") == "IN":
        return True
    if t.endswith((".NS", ".BO")) or t.startswith(("NSE:", "BSE:")):
        return True
    clean = t.replace(".NS", "").replace(".BO", "").split(":")[-1]
    if offline:
        for name in ("google-%s.xml" % clean, "yahoo-%s.NS.xml" % clean,
                     "chart-%s.NS.json" % clean, "indian-%s.json" % clean):
            if os.path.isfile(os.path.join(offline, name)):
                return True
    if clean in TOP_INDIAN_SYMBOLS or clean in INDIAN_COMPANY_NAMES:
        return True
    if symbols_in and hasattr(symbols_in, "is_index_symbol") and symbols_in.is_index_symbol(clean):
        return True
    return False


def normalize_ticker(ticker, default_exchange=None):
    """Normalize input ticker string into canonical symbol and exchange.

    e.g. 'RELIANCE.NS' -> ('RELIANCE', 'NSE')
         'BSE:INFY'    -> ('INFY', 'BSE')
         'AAPL'        -> ('AAPL', None)
    """
    if not ticker or not isinstance(ticker, str):
        return "", None
    sym = ticker.strip().upper()
    if sym.endswith(".NS"):
        return sym[:-3], "NSE"
    if sym.endswith(".BO"):
        return sym[:-3], "BSE"
    if sym.startswith("NSE:"):
        return sym[4:], "NSE"
    if sym.startswith("BSE:"):
        return sym[4:], "BSE"
    if is_indian_ticker(sym, exchange=default_exchange):
        return sym, default_exchange or "NSE"
    return sym, None


def is_price_movement_headline(title):
    """Identify automated price quotes / ticker tracker pages."""
    return bool(PRICE_TRACKER_PATTERN.search(title or ""))


def classify_news_item(item, ticker):
    """Classify an item into one of the 5 standardized categories:

    corporate, business, analyst, market, or low_relevance.
    Detects subsidiaries, group companies, and incidental mentions.
    Returns (category, subsidiary_name_or_None, relevance_score).
    """
    title = item.get("title", "")
    summary = item.get("summary", "")
    hay = "%s %s" % (title, summary)
    kind = item.get("kind", "")
    t = ticker.upper()

    # SEC filings are authoritative corporate disclosures
    if kind == "filing":
        score = CATEGORY_RANK["corporate"] + (50 if item.get("important") else 20)
        return "corporate", None, score

    # Detect subsidiaries or related group entities first
    sub_name = None
    if t in SUBSIDIARIES:
        for name, rx in SUBSIDIARIES[t]:
            if rx.search(hay):
                sub_name = name
                if "Separate Group" in name:
                    return "low_relevance", sub_name, CATEGORY_RANK["low_relevance"]
                break

    # Check incidental / peripheral topics
    for rx in LOW_RELEVANCE_PATTERNS:
        if rx.search(title):
            return "low_relevance", sub_name, CATEGORY_RANK["low_relevance"]

    # Determine primary subject focus
    is_primary = bool(re.search(r"^\b(?:%s|%s)\b" % (re.escape(t), re.escape(INDIAN_COMPANY_NAMES.get(t, t))), title, re.I))

    # Pattern based categorization
    for rx in ANALYST_PATTERNS:
        if rx.search(title):
            score = CATEGORY_RANK["analyst"] + (10 if is_primary else 0)
            return "analyst", sub_name, score

    for rx in CORPORATE_PATTERNS:
        if rx.search(title):
            score = CATEGORY_RANK["corporate"] + (20 if is_primary else 0)
            return "corporate", sub_name, score

    for rx in BUSINESS_PATTERNS:
        if rx.search(title):
            score = CATEGORY_RANK["business"] + (20 if is_primary else 0)
            return "business", sub_name, score

    for rx in MARKET_PATTERNS:
        if rx.search(title):
            score = CATEGORY_RANK["market"]
            return "market", sub_name, score

    # Default fallback
    score = CATEGORY_RANK["market"]
    return "market", sub_name, score


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{ticker: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


def sec_user_agent(cli=None, config="trader/config.json"):
    ua = (cli or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua:
        cfg = load_json(config) or {}
        ua = str(cfg.get("sec_user_agent") or "").strip()
    if not ua or "@" not in ua or PLACEHOLDER in ua:
        return None
    return ua


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, sec_ua=None, offline=None, pause=0.15):
        self.sec_ua = sec_ua
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, sec=False):
        if self.offline:
            candidates = [name]
            clean = name.replace(".NS", "").replace(".BO", "")
            if clean != name:
                candidates.append(clean)
            if name.endswith(".json") and ".NS" not in name and ".BO" not in name:
                candidates.append(name.replace(".json", ".NS.json"))
            if name.startswith("yahoo-") and ".NS" not in name:
                candidates.append(name.replace("yahoo-", "yahoo-").replace(".xml", ".NS.xml"))
            if name.startswith("google-"):
                candidates.append(name.replace("google-", "yahoo-"))
            elif name.startswith("yahoo-"):
                candidates.append(name.replace("yahoo-", "google-"))

            for cand in candidates:
                p = os.path.join(self.offline, cand)
                if os.path.isfile(p):
                    with open(p, "rb") as fh:
                        return fh.read()
            raise NewsError("offline: no file %s" % os.path.join(self.offline, name))
        if sec and not self.sec_ua:
            raise NewsError("no SEC User-Agent set")
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": self.sec_ua if sec else BROWSER_UA, "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


def yahoo_symbol(ticker, exchange=None):
    s = str(ticker).upper()
    if s.endswith((".NS", ".BO")) or s.startswith("^"):
        return s
    if is_indian_ticker(s, exchange=exchange):
        if symbols_in and hasattr(symbols_in, "to_yahoo_symbol"):
            try:
                return symbols_in.to_yahoo_symbol(s, exchange=exchange or "NSE")
            except Exception:
                pass
        return "%s.BO" % s if (exchange or "").upper() == "BSE" else "%s.NS" % s
    return s.replace(".", "-")


def cik_map(fetcher, state):
    """ticker -> 10-digit CIK, from SEC's company_tickers.json (cached in state)."""
    cached = state.setdefault("ciks", {})
    if cached:
        return cached
    data = json.loads(fetcher.get(TICKERS_URL, "company_tickers.json", sec=True).decode("utf-8"))
    for v in data.values():
        cached.setdefault(v["ticker"].upper().replace("-", "."), str(v["cik_str"]).zfill(10))
    return cached


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker, default_source="Yahoo Finance"):
    """RSS feed (Yahoo Finance or Google News) -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title").replace("\u2014", " - ").replace("\u2013", "-")
        link = _text(it, "link")
        guid = _text(it, "guid") or link

        src_el = None
        for c in it.iter():
            if _local(c.tag) == "source":
                src_el = c
                break
        source_name = (src_el.text or "").strip() if src_el is not None and (src_el.text or "").strip() else ""
        source_url = src_el.get("url", "") if src_el is not None else ""

        # Extract publisher from title suffix if missing or if generic aggregator name
        if not source_name or source_name == "Google News":
            if " - " in title:
                candidate = title.rsplit(" - ", 1)[-1].strip()
                if candidate and len(candidate) <= 35:
                    source_name = candidate

        publisher_name = source_name if source_name and source_name != "Google News" else None
        source_name = publisher_name or default_source

        if not source_url and default_source == "Yahoo Finance":
            source_url = "https://finance.yahoo.com"

        pub_time = parse_time(_text(it, "pubDate"))
        is_move = is_price_movement_headline(title)

        out.append({
            "id": "rss:" + guid,
            "source": source_name,
            "source_url": source_url,
            "publisher": publisher_name,
            "publisher_url": source_url if source_url and source_url != link else None,
            "article_url": link,
            "kind": "price_movement" if is_move else "headline",
            "is_price_movement": is_move,
            "ticker": ticker,
            "title": title,
            "summary": _text(it, "description").replace("\u2014", " - ").replace("\u2013", "-"),
            "link": link,
            "time": pub_time,
            "time_utc": pub_time.isoformat() if pub_time else None,
            "time_ist": to_ist(pub_time).strftime("%Y-%m-%d %H:%M:%S IST") if pub_time else None
        })
    return out


def parse_sec_atom(data, ticker):
    """EDGAR company filings Atom -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("SEC feed for %s is not valid XML: %s" % (ticker, exc))
    company = ""
    for el in root.iter():
        if _local(el.tag) == "conformed-name":
            company = (el.text or "").strip()
            break
    out = []
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        form = _text(entry, "filing-type")
        acc = _text(entry, "accession-number")
        items = re.findall(r"\d+\.\d+", _text(entry, "items-desc"))
        labels = [ITEMS_8K.get(i, "item " + i) for i in items if i != "9.01"]
        link = ""
        for c in entry.iter():
            if _local(c.tag) == "link":
                link = c.get("href", "")
        title = "%s filed a %s" % (company or ticker, form)
        if labels:
            title += ": " + ", ".join(labels)
        pub_time = parse_time(_text(entry, "updated"))
        out.append({
            "id": "sec:" + acc,
            "source": "SEC EDGAR",
            "source_url": "https://www.sec.gov",
            "publisher": "SEC EDGAR",
            "publisher_url": "https://www.sec.gov",
            "article_url": link,
            "kind": "filing",
            "form": form,
            "ticker": ticker,
            "title": title,
            "summary": _text(entry, "form-name"),
            "items": items,
            "important": bool(set(items) & IMPORTANT_ITEMS),
            "link": link,
            "time": pub_time,
            "time_utc": pub_time.isoformat() if pub_time else None,
            "is_price_movement": False,
            "category": "corporate",
            "relevance_score": CATEGORY_RANK["corporate"] + (50 if bool(set(items) & IMPORTANT_ITEMS) else 20)
        })
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time", "currency", "timezone"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    tz = meta.get("exchangeTimezoneName") or meta.get("timezone") or ""
    curr = meta.get("currency") or "USD"
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None,
            "currency": curr, "timezone": tz}


def _move_from_provider_quote(q, ticker):
    price = q.get("price") if q.get("price") is not None else q.get("ltp")
    prev = q.get("prev_close")
    if price is None or not prev:
        raise NewsError("provider quote for %s has no price or prev_close" % ticker)
    ts = q.get("timestamp")
    when = _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc) if isinstance(ts, (int, float)) else None
    return {
        "ticker": ticker,
        "price": round(float(price), 2),
        "prev_close": round(float(prev), 2),
        "pct": round((float(price) / float(prev) - 1.0) * 100.0, 2),
        "time": when,
        "currency": q.get("currency") or "INR",
        "timezone": "IST",
        "provider": q.get("provider") or q.get("source"),
        "data_status": q.get("data_status"),
    }


def collect_moves(fetcher, watch, errors=None, exchange=None, config=None):
    errors = [] if errors is None else errors
    out = []
    in_prov = None
    in_prov_err = None
    if not getattr(fetcher, "offline", None) and data_in is not None:
        try:
            in_prov = data_in.get_provider(config=config)
        except Exception as exc:
            in_prov_err = str(exc)

    for t in sorted(watch):
        if not getattr(fetcher, "offline", None) and is_indian_ticker(t, exchange=exchange) and (in_prov is not None or in_prov_err):
            if in_prov_err:
                errors.append("price for %s: %s" % (t, in_prov_err))
                continue
            try:
                exch = exchange or (config or {}).get("exchange") or "NSE"
                q = in_prov.get_quote(t, exchange=exch)
                out.append(_move_from_provider_quote(q, t))
            except Exception as exc:
                errors.append("price for %s: %s" % (t, exc))
            continue

        y_sym = yahoo_symbol(t, exchange=exchange)
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", y_sym), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", y_sym), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\$%s\b" % re.escape(t),
                r"\b(?:NYSE|NASDAQ|Nasdaq|NSE|BSE)\s*:\s*%s\b" % re.escape(t),
                r"\b%s\.(?:NS|BO)\b" % re.escape(t)]
        if len(t) >= 4:
            alts.append(r"\b%s\b" % re.escape(t))
        resolved_name = name or INDIAN_COMPANY_NAMES.get(t)
        if resolved_name and len(resolved_name) >= 4:
            alts.append(r"\b%s\b" % re.escape(resolved_name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts), re.I)
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, why it matters, category and subsidiary."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "filing" and "2.02" in it.get("items", []) and "earnings" not in it["tags"]:
            it["tags"].insert(0, "earnings")

        # Classify news category and detect subsidiary / group company
        cat, sub, score = classify_news_item(it, it["ticker"])
        it["category"] = cat
        it["subsidiary"] = sub
        it["relevance_score"] = score
        if sub:
            it["tags"].append("subsidiary: %s" % sub)
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        # Strip trailing publisher suffix (e.g. ' - Livemint') for title deduplication
        norm_title = re.sub(r"\s+-\s+[A-Za-z0-9\.\s&]+$", "", it["title"]).strip()
        clean_title = re.sub(r"\W+", " ", norm_title.lower()).strip()
        day = it["time"].date() if it["time"] else ""
        keys = [it["id"], it["link"], "%s|%s" % (clean_title, day)]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, sec=True, forms=("8-K",), count=10, errors=None,
            exchange=None, include_quotes=False):
    errors = [] if errors is None else errors
    items = []
    has_indian = False
    us_watch = {}

    for t in sorted(watch):
        if is_indian_ticker(t, exchange=exchange, offline=fetcher.offline):
            has_indian = True
            ticker_items = []
            # 1. Google News Indian corporate query
            q = "%s (%s OR NSE OR BSE) news" % (t, INDIAN_COMPANY_NAMES.get(t, t))
            g_url = GOOGLE_NEWS % urllib.parse.quote(q)
            try:
                g_data = fetcher.get(g_url, "google-%s.xml" % t)
                ticker_items += parse_rss(g_data, t, default_source="Google News")
            except NewsError as exc:
                if not (fetcher.offline and "no file" in str(exc)):
                    errors.append("Indian corporate news for %s: %s" % (t, exc))

            # 2. Yahoo Finance RSS
            y_sym = yahoo_symbol(t, exchange=exchange)
            try:
                y_data = fetcher.get(YAHOO % y_sym, "yahoo-%s.xml" % y_sym)
                ticker_items += parse_rss(y_data, t, default_source="Yahoo Finance")
            except NewsError as exc:
                if not (fetcher.offline and "no file" in str(exc)):
                    pass

            if not ticker_items:
                if not fetcher.offline:
                    errors.append("Public feeds returned 0 items for %s. Delayed syndication possible; verify official disclosures on NSE/BSE." % t)
            else:
                if not include_quotes:
                    real_news = [it for it in ticker_items if not it.get("is_price_movement")]
                    items += real_news if real_news else ticker_items
                else:
                    items += ticker_items
        else:
            us_watch[t] = watch[t]
            try:
                items += parse_rss(fetcher.get(YAHOO % yahoo_symbol(t), "yahoo-%s.xml" % t), t)
            except NewsError as exc:
                errors.append("headlines for %s: %s" % (t, exc))

    if sec and us_watch:
        try:
            ciks = cik_map(fetcher, state)
        except NewsError as exc:
            errors.append("SEC ticker list: %s" % exc)
            ciks = {}
        for t in sorted(us_watch):
            cik = ciks.get(t)
            if not cik:
                if ciks:
                    errors.append("no SEC CIK for %s" % t)
                continue
            for form in forms:
                try:
                    data = fetcher.get(SEC_FEED % (cik, form, count),
                                       "sec-%s-%s.xml" % (form, cik), sec=True)
                    items += parse_sec_atom(data, t)
                except NewsError as exc:
                    errors.append("SEC %s for %s: %s" % (form, t, exc))

    return items


def compute_coverage(watch, items, errors):
    """Compute per-ticker coverage breakdown, distinguishing status and categories."""
    coverage = {}
    for t in sorted(watch):
        ticker_items = [it for it in items if t in it.get("touches", [it.get("ticker")])]
        cat_counts = {c: 0 for c in CATEGORIES}
        subs = set()
        for it in ticker_items:
            c = it.get("category") or "market"
            cat_counts[c] = cat_counts.get(c, 0) + 1
            if it.get("subsidiary"):
                subs.add(it["subsidiary"])

        total = len(ticker_items)
        rel_count = sum(cat_counts[c] for c in ("corporate", "business", "analyst", "market"))
        low_count = cat_counts.get("low_relevance", 0)

        ticker_errors = [e for e in errors if t in e]
        if ticker_errors:
            status = "UNAVAILABLE"
        elif total == 0:
            status = "EMPTY"
        else:
            status = "OK"

        coverage[t] = {
            "total": total,
            "relevant": rel_count,
            "low_relevance": low_count,
            "categories": cat_counts,
            "subsidiaries": sorted(subs),
            "status": status,
            "notes": ticker_errors
        }
    return coverage


def select(items, state, now, hours=48, show_all=False, include_low_relevance=False, sort_by=None):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            if not include_low_relevance and it.get("category") == "low_relevance":
                continue
            out.append(it)

    # Sorting: relevance ranking prioritized by category, with chronological fallback
    if sort_by == "time":
        out.sort(key=lambda i: (i["time"] or now), reverse=True)
    elif sort_by == "relevance":
        out.sort(key=lambda i: (CATEGORY_RANK.get(i.get("category"), 0), i.get("relevance_score", 0), i["time"] or now), reverse=True)
    else:
        # Default behavior: rank by relevance if any Indian ticker, else reverse by time (preserving US tests)
        has_indian = any(is_indian_ticker(i.get("ticker", "")) for i in out)
        if has_indian:
            out.sort(key=lambda i: (CATEGORY_RANK.get(i.get("category"), 0), i.get("relevance_score", 0), i["time"] or now), reverse=True)
        else:
            out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        is_in = is_indian_ticker(m["ticker"]) or m.get("currency") == "INR" or m.get("timezone") in ("IST", "Asia/Kolkata")
        if m.get("time"):
            when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if is_in else (" at %s ET" % to_eastern(m["time"]).strftime("%H:%M"))
        else:
            when = ""
        out.append("%-6s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]),
            when, flag))
    return out


def render_coverage(coverage, show_research=False):
    lines = ["Coverage Summary:"]
    for t, c in sorted(coverage.items()):
        if c["status"] == "UNAVAILABLE":
            err_msg = "; ".join(c["notes"]) if c["notes"] else "feed unavailable"
            lines.append("  %-10s 0 stories (feed unavailable: %s)" % (t + ":", err_msg))
        elif c["status"] == "EMPTY":
            lines.append("  %-10s 0 stories found in public feeds within window" % (t + ":"))
        else:
            cats = []
            for cat in ("corporate", "business", "analyst", "market"):
                cnt = c["categories"].get(cat, 0)
                if cnt > 0:
                    cats.append("%d %s" % (cnt, cat))
            cats_str = ", ".join(cats) if cats else "0"
            sub_str = ("; subsidiaries: " + ", ".join(c["subsidiaries"])) if c["subsidiaries"] else ""
            if c["low_relevance"] > 0 and not show_research:
                lines.append("  %-10s %d relevant (%s%s) | %d low-relevance excluded (pass --research to view)" % (
                    t + ":", c["relevant"], cats_str, sub_str, c["low_relevance"]))
            else:
                lines.append("  %-10s %d stories (%s%s)" % (
                    t + ":", c["relevant"] if not show_research else c["total"], cats_str, sub_str))
    return lines


def render(items, errors, now, moves=None, move_pct=MOVE_PCT, exchange=None, coverage=None, show_research=False):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")

    all_indian = True
    all_us = True

    if items:
        for it in items:
            it_in = is_indian_ticker(it["ticker"], exchange=exchange)
            if it_in:
                all_us = False
                when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
            else:
                all_indian = False
                when = to_eastern(it["time"]).strftime("%Y-%m-%d %H:%M ET") if it["time"] else "time unknown"
            mark = "!" if it.get("important") else " "
            who = ",".join(it["touches"]) or it["ticker"]
            source_lbl = it.get("source") or "News"
            out.append("%s %s  %-11s %-18s %s" % (mark, when, who, source_lbl, it["title"]))
            extra = []
            if it.get("category"):
                extra.append("[%s]" % it["category"].upper())
            if it.get("subsidiary"):
                extra.append("[subsidiary: %s]" % it["subsidiary"])
            if it.get("tags"):
                extra.append("tags: " + ", ".join(it["tags"]))
            extra.append(it["link"])
            if it.get("publisher_url") and it.get("publisher_url") != it["link"]:
                extra.append("publisher_site: " + it["publisher_url"])
            out.append("      " + "  ".join(extra))
    elif moves:
        for m in moves:
            if is_indian_ticker(m["ticker"], exchange=exchange):
                all_us = False
            else:
                all_indian = False

    if coverage:
        out.append("")
        out.extend(render_coverage(coverage, show_research=show_research))

    for e in errors:
        out.append("note: %s" % e)

    if all_indian and not all_us:
        checked_str = to_ist(now).strftime("%Y-%m-%d %H:%M IST")
    elif all_us:
        checked_str = to_eastern(now).strftime("%Y-%m-%d %H:%M ET")
    else:
        checked_str = "%s IST (%s ET)" % (to_ist(now).strftime("%Y-%m-%d %H:%M"), to_eastern(now).strftime("%H:%M"))

    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % checked_str)
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--forms", default="8-K", help="SEC forms to watch, comma separated")
    ap.add_argument("--no-sec", action="store_true", help="headlines only")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--user-agent", help="SEC User-Agent: \"Your Name you@example.com\"")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--exchange", choices=["NSE", "BSE"], help="exchange for Indian tickers (default: NSE)")
    ap.add_argument("--include-quotes", action="store_true", help="include automated price tracker pages in headlines")
    ap.add_argument("--research", action="store_true", help="include low-relevance and incidental stories for research")
    ap.add_argument("--sort", choices=["relevance", "time"], default=None, help="sort by relevance or time")
    args = ap.parse_args(argv)

    if args.tickers:
        watch = {}
        for rt in args.tickers:
            canon, _ = normalize_ticker(rt, default_exchange=args.exchange)
            watch[canon] = None
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        raw_watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch = {}
        for k, v in raw_watch.items():
            canon, _ = normalize_ticker(k, default_exchange=args.exchange)
            watch[canon] = v

    if not watch:
        print("news: no tickers. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2

    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    state = load_json(args.state) or {}
    errors = []
    ua = sec_user_agent(args.user_agent, args.config)
    sec = not args.no_sec
    if sec and not ua and not args.offline:
        # Check if any US tickers exist before prompting for SEC User-Agent
        has_us = any(not is_indian_ticker(t, exchange=args.exchange, offline=args.offline) for t in watch)
        if has_us:
            errors.append("SEC filings skipped: set sec_user_agent in trader/config.json or "
                          "SEC_USER_AGENT (\"Your Name you@example.com\") to include them")
        sec = False

    fetcher = Fetcher(ua, offline=args.offline)
    forms = [f.strip().upper() for f in args.forms.split(",") if f.strip()]
    items = collect(fetcher, watch, state, sec=sec, forms=forms, errors=errors,
                    exchange=args.exchange, include_quotes=args.include_quotes)
    items = dedupe(tag(items, watch))
    coverage = compute_coverage(watch, items, errors)
    shown = select(items, state, now, hours=args.hours, show_all=args.all,
                   include_low_relevance=args.research, sort_by=args.sort)
    cfg = load_json(args.config) or {}
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors, exchange=args.exchange, config=cfg)

    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "coverage": coverage, "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct, exchange=args.exchange,
                                coverage=coverage, show_research=args.research))

    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")

    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
