#!/usr/bin/env python3
"""Opt-in lightweight SVG chart renderer for day-trader-skill.

Pure Python >= 3.8 standard library only (zero external dependencies).
Reuses already-fetched JSON payloads and computed indicators.
Never fabricates candles, prices, timestamps, volume, or indicators.
Every market chart embeds source, timestamp, timeframe, and freshness tier.
"""

import argparse
import datetime
import json
import math
import os
import sys
from xml.sax.saxutils import escape

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)


class VisualError(Exception):
    """Raised when required chart data or metadata is missing or invalid."""
    pass


# ---------------------------------------------------------------- Indicators

def compute_ema(closes, period):
    """Compute EMA series aligned to closes without inventing earlier values.

    Returns a list of same length as closes where indices < period - 1 are None.
    """
    n = int(period)
    if n <= 0 or not closes or len(closes) < n:
        return [None] * len(closes or [])
    vals = [None] * (n - 1)
    sma = sum(float(x) for x in closes[:n]) / float(n)
    vals.append(sma)
    k = 2.0 / float(n + 1)
    prev = sma
    for c in closes[n:]:
        prev = (float(c) - prev) * k + prev
        vals.append(prev)
    return vals


def compute_vwap(bars):
    """Compute cumulative session/series VWAP only when real volume > 0.

    Returns list aligned to bars, or None if total volume <= 0 (e.g. spot index).
    """
    if not bars:
        return None
    total_v = sum(float(b.get("v") or 0) for b in bars)
    if total_v <= 0:
        return None
    out = []
    cum_pv = 0.0
    cum_v = 0.0
    has_session_reset = any(b.get("date") for b in bars)
    prev_date = None
    for b in bars:
        d = b.get("date")
        if has_session_reset and d and d != prev_date:
            cum_pv = 0.0
            cum_v = 0.0
            prev_date = d
        v = float(b.get("v") or 0)
        tp = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        if v > 0:
            cum_pv += tp * v
            cum_v += v
        out.append((cum_pv / cum_v) if cum_v > 0 else None)
    return out if any(x is not None for x in out) else None


def compute_rsi(closes, period=14):
    """Wilder RSI(period) matching scanner/data_in convention. Returns float or None."""
    n = int(period)
    if not closes or len(closes) <= n:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = float(closes[i]) - float(closes[i - 1])
        gains.append(diff if diff > 0 else 0.0)
        losses.append(-diff if diff < 0 else 0.0)
    ag = sum(gains[:n]) / float(n)
    al = sum(losses[:n]) / float(n)
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / float(n)
        al = (al * (n - 1) + l) / float(n)
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def compute_atr(bars, period=14):
    """Average True Range over real OHLC bars."""
    n = int(period)
    if not bars or len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h = float(bars[i]["h"])
        l = float(bars[i]["l"])
        pc = float(bars[i - 1]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / float(n)


def compute_support_resistance(bars, lookback=20):
    """Return (support, resistance) tuple strictly from prior lookback bars."""
    if not bars:
        return None, None
    win = bars[-min(len(bars), max(2, int(lookback))):]
    sup = min(float(b["l"]) for b in win)
    res = max(float(b["h"]) for b in win)
    return sup, res


# ---------------------------------------------------------------- SVG Primitives

PALETTE = {
    "bg": "#0f172a",
    "panel": "#111c33",
    "grid": "#24324d",
    "axis": "#64748b",
    "text": "#e2e8f0",
    "muted": "#94a3b8",
    "up": "#22c55e",
    "down": "#ef4444",
    "ema1": "#38bdf8",
    "ema2": "#f59e0b",
    "vwap": "#c084fc",
    "entry": "#38bdf8",
    "stop": "#ef4444",
    "target": "#22c55e",
    "breakout": "#facc15",
    "sr": "#94a3b8",
    "accent": "#60a5fa",
}


class SvgCanvas:
    """Minimal pure-stdlib SVG document builder."""

    def __init__(self, width=1080, height=620, bg=None):
        self.width = int(width)
        self.height = int(height)
        self.bg = bg or PALETTE["bg"]
        self.elems = []

    def add(self, raw_xml):
        self.elems.append(raw_xml)

    def rect(self, x, y, w, h, fill="none", stroke="none", stroke_width=1, rx=0, opacity=1.0):
        extra = ""
        if rx:
            extra += ' rx="%.1f"' % rx
        if opacity < 1.0:
            extra += ' fill-opacity="%.2f"' % opacity
        self.add(
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" stroke="%s" stroke-width="%.1f"%s />'
            % (x, y, max(0.0, w), max(0.0, h), escape(str(fill)), escape(str(stroke)), stroke_width, extra)
        )

    def line(self, x1, y1, x2, y2, stroke="#64748b", width=1.2, dash=None, opacity=1.0):
        extra = ""
        if dash:
            extra += ' stroke-dasharray="%s"' % escape(str(dash))
        if opacity < 1.0:
            extra += ' stroke-opacity="%.2f"' % opacity
        self.add(
            '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="%.1f"%s />'
            % (x1, y1, x2, y2, escape(str(stroke)), width, extra)
        )

    def polyline(self, pts, stroke="#38bdf8", width=1.8, fill="none", dash=None, opacity=1.0):
        if len(pts) < 2:
            return
        pt_str = " ".join("%.1f,%.1f" % (p[0], p[1]) for p in pts)
        extra = ""
        if dash:
            extra += ' stroke-dasharray="%s"' % escape(str(dash))
        if opacity < 1.0:
            extra += ' stroke-opacity="%.2f"' % opacity
        self.add(
            '<polyline points="%s" fill="%s" stroke="%s" stroke-width="%.1f"%s />'
            % (pt_str, escape(str(fill)), escape(str(stroke)), width, extra)
        )

    def polygon(self, pts, fill="#38bdf8", opacity=0.2, stroke="none"):
        if len(pts) < 3:
            return
        pt_str = " ".join("%.1f,%.1f" % (p[0], p[1]) for p in pts)
        self.add(
            '<polygon points="%s" fill="%s" fill-opacity="%.2f" stroke="%s" />'
            % (pt_str, escape(str(fill)), opacity, escape(str(stroke)))
        )

    def circle(self, cx, cy, r=4, fill="#38bdf8", stroke="none", stroke_width=1):
        self.add(
            '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" stroke="%s" stroke-width="%.1f" />'
            % (cx, cy, r, escape(str(fill)), escape(str(stroke)), stroke_width)
        )

    def text(self, x, y, content, size=12, fill=None, weight="normal", anchor="start", family=None):
        fam = family or "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace"
        self.add(
            '<text x="%.1f" y="%.1f" font-family="%s" font-size="%d" font-weight="%s" fill="%s" text-anchor="%s">%s</text>'
            % (
                x,
                y,
                escape(fam),
                int(size),
                escape(str(weight)),
                escape(str(fill or PALETTE["text"])),
                escape(str(anchor)),
                escape(str(content)),
            )
        )

    def to_svg(self):
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
            % (self.width, self.height, self.width, self.height),
            '<rect width="100%%" height="100%%" fill="%s" />' % escape(str(self.bg)),
        ]
        lines.extend(self.elems)
        lines.append("</svg>")
        return "\n".join(lines) + "\n"


def _fmt_num(val, decimals=2):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "N/A"
    return ("{:,.%df}" % decimals).format(float(val))


def _currency_prefix(meta):
    cur = str((meta or {}).get("currency") or "").upper()
    mkt = str((meta or {}).get("market") or "").upper()
    if cur == "INR" or mkt == "IN":
        return "Rs "
    if cur == "USD" or mkt == "US":
        return "$"
    return ""


def _provenance_footer(canvas, meta, note=None):
    """Draw mandatory provenance strip with source, timestamp, timeframe, and freshness tier."""
    m = meta or {}
    source = m.get("provider") or m.get("source") or "unknown"
    tier = m.get("data_tier") or m.get("tier") or m.get("data_status") or "UNKNOWN"
    status = m.get("data_status")
    tier_str = str(tier).upper()
    if status and str(status).upper() != tier_str:
        tier_str = "%s (%s)" % (tier_str, status)
    if m.get("simulated_at") or m.get("is_simulated"):
        if "SIMULATED" not in tier_str and "HISTORICAL" not in tier_str:
            tier_str += " [SIMULATED]"
    ts = m.get("last_data_timestamp") or m.get("as_of") or m.get("checked_at") or m.get("created_at") or "timestamp unavailable"
    tf = m.get("interval") or m.get("timeframe") or "N/A"
    canvas.line(30, canvas.height - 42, canvas.width - 30, canvas.height - 42, stroke=PALETTE["grid"], width=1.0)
    footer = "Source: %s  |  Timeframe: %s  |  Status: %s  |  Data As Of: %s" % (source, tf, tier_str, ts)
    if note:
        footer += "  |  %s" % note
    canvas.text(30, canvas.height - 18, footer, size=11, fill=PALETTE["muted"])


def _tier_badge_color(tier_text):
    t = str(tier_text or "").upper()
    if "CURRENT" in t or "REAL_TIME" in t:
        return "#22c55e"
    if "DELAYED" in t:
        return "#f59e0b"
    if "SIMULATED" in t or "FIXTURE" in t or "HISTORICAL" in t:
        return "#38bdf8"
    return "#94a3b8"


# ---------------------------------------------------------------- Normalization

def normalize_bars_payload(payload):
    """Extract (bars, metadata) from standard data_in, Yahoo v8, entry, or scanner dicts."""
    if not isinstance(payload, dict):
        raise VisualError("Expected dict payload with market bars")

    # Handle wrapper index scan or history payloads
    if "bars" in payload and isinstance(payload["bars"], list) and payload["bars"]:
        raw_bars = payload["bars"]
        bars = []
        for b in raw_bars:
            if not isinstance(b, dict):
                continue
            o, h, l, c = b.get("o"), b.get("h"), b.get("l"), b.get("c")
            if None in (o, h, l, c):
                continue
            ts = b.get("ts") or b.get("t")
            label = b.get("time") or b.get("start") or ""
            d_str = b.get("date") or ""
            if not label and ts:
                tz_off = datetime.timedelta(hours=5, minutes=30) if str(payload.get("market") or payload.get("exchange") or "") in ("IN", "NSE", "BSE") else datetime.timedelta(0)
                dt = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc) + tz_off
                d_str = d_str or dt.strftime("%Y-%m-%d")
                label = dt.strftime("%H:%M") if str(payload.get("interval") or "1d") not in ("1d", "d", "daily", "1wk", "1mo") else d_str
            bars.append({
                "ts": int(ts) if ts is not None else len(bars),
                "date": str(d_str),
                "label": ("%s %s" % (d_str, label)).strip() if d_str and label and label != d_str else (label or d_str or str(len(bars))),
                "o": float(o),
                "h": float(h),
                "l": float(l),
                "c": float(c),
                "v": float(b.get("v") or 0.0),
            })
        if not bars:
            raise VisualError("Payload contains no valid OHLC bars to plot")
        last_ts = payload.get("last_data_timestamp") or payload.get("as_of")
        if not last_ts and bars:
            last_ts = bars[-1]["label"]
        meta = {
            "symbol": payload.get("symbol") or (payload.get("meta") or {}).get("symbol") or "UNKNOWN",
            "name": payload.get("name") or "",
            "exchange": payload.get("exchange") or (payload.get("meta") or {}).get("exchangeName") or "",
            "market": payload.get("market") or ("IN" if str(payload.get("exchange") or "") in ("NSE", "BSE") else ""),
            "currency": payload.get("currency") or ("INR" if str(payload.get("exchange") or "") in ("NSE", "BSE") else ""),
            "provider": payload.get("provider") or payload.get("source") or "unknown",
            "interval": payload.get("interval") or (payload.get("meta") or {}).get("dataGranularity") or "N/A",
            "data_tier": payload.get("data_tier") or payload.get("tier") or payload.get("data_status") or "UNKNOWN",
            "data_status": payload.get("data_status") or "",
            "last_data_timestamp": last_ts,
            "simulated_at": payload.get("simulated_at"),
            "is_simulated": payload.get("is_simulated"),
            "is_index": str(payload.get("instrument_type") or "").upper() == "INDEX" or payload.get("asset_type") == "INDEX",
        }
        return bars, meta

    # Handle scanner series shape {"t": [...], "o": [...], "h": [...], "l": [...], "c": [...], "v": [...]}
    if "t" in payload and "c" in payload and isinstance(payload["t"], list):
        ts_list = payload["t"]
        c_list = payload["c"]
        o_list = payload.get("o") or c_list
        h_list = payload.get("h") or c_list
        l_list = payload.get("l") or c_list
        v_list = payload.get("v") or [0] * len(ts_list)
        bars = []
        raw_meta = payload.get("meta") or {}
        exch = payload.get("exchange") or raw_meta.get("exchangeName") or ""
        tz_off = datetime.timedelta(hours=5, minutes=30) if exch in ("NSE", "BSE", "NSI", "IN") or payload.get("market") == "IN" else datetime.timedelta(hours=-4)
        iv = payload.get("interval") or raw_meta.get("dataGranularity") or "1d"
        for i, ts_val in enumerate(ts_list):
            if ts_val is None or i >= len(c_list) or c_list[i] is None:
                continue
            o_v = o_list[i] if i < len(o_list) and o_list[i] is not None else c_list[i]
            h_v = h_list[i] if i < len(h_list) and h_list[i] is not None else max(o_v, c_list[i])
            l_v = l_list[i] if i < len(l_list) and l_list[i] is not None else min(o_v, c_list[i])
            v_v = v_list[i] if i < len(v_list) and v_list[i] is not None else 0
            dt = datetime.datetime.fromtimestamp(int(ts_val), tz=datetime.timezone.utc) + tz_off
            d_str = dt.strftime("%Y-%m-%d")
            lbl = d_str if iv in ("1d", "d", "daily", "1wk", "1mo") else dt.strftime("%m-%d %H:%M")
            bars.append({
                "ts": int(ts_val),
                "date": d_str,
                "label": lbl,
                "o": float(o_v),
                "h": float(h_v),
                "l": float(l_v),
                "c": float(c_list[i]),
                "v": float(v_v or 0),
            })
        if not bars:
            raise VisualError("Series payload contains no valid OHLC bars")
        meta = {
            "symbol": payload.get("symbol") or raw_meta.get("symbol") or "UNKNOWN",
            "exchange": exch,
            "market": payload.get("market") or ("IN" if exch in ("NSE", "BSE", "NSI") else ""),
            "currency": payload.get("currency") or ("INR" if exch in ("NSE", "BSE", "NSI") else ""),
            "provider": payload.get("provider") or payload.get("source") or "unknown",
            "interval": iv,
            "data_tier": payload.get("data_tier") or payload.get("tier") or payload.get("data_status") or "UNKNOWN",
            "data_status": payload.get("data_status") or "",
            "last_data_timestamp": payload.get("last_data_timestamp") or bars[-1]["label"],
            "simulated_at": payload.get("simulated_at"),
            "is_simulated": payload.get("is_simulated"),
            "is_index": str(payload.get("instrument_type") or "").upper() == "INDEX",
        }
        return bars, meta

    # Handle Yahoo v8 chart JSON
    chart = payload.get("chart")
    if isinstance(chart, dict) and chart.get("result"):
        res = chart["result"][0]
        raw_meta = res.get("meta") or {}
        stamps = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
        conv = {
            "t": stamps,
            "o": q.get("open") or [],
            "h": q.get("high") or [],
            "l": q.get("low") or [],
            "c": q.get("close") or [],
            "v": q.get("volume") or [],
            "symbol": raw_meta.get("symbol") or "UNKNOWN",
            "exchange": raw_meta.get("exchangeName") or "",
            "currency": raw_meta.get("currency") or "",
            "provider": payload.get("provider") or "yahoo",
            "interval": raw_meta.get("dataGranularity") or "N/A",
            "data_tier": payload.get("data_tier") or "DELAYED",
            "data_status": payload.get("data_status") or "DELAYED",
            "last_data_timestamp": payload.get("last_data_timestamp"),
        }
        return normalize_bars_payload(conv)

    raise VisualError("Unsupported market payload format (expected 'bars', series 't'/'c', or Yahoo v8 chart JSON)")


# ---------------------------------------------------------------- 1 & 2. Candlestick / Entry-Exit Charts

def render_candlestick_svg(
    payload,
    ema_periods=(20, 50),
    show_vwap=True,
    show_sr=True,
    levels=None,
    plan=None,
    entry_doc=None,
    exit_price=None,
    max_bars=120,
    title=None,
):
    """Render an OHLC candlestick chart with optional EMA, VWAP, volume, S/R, and trade levels."""
    bars_all, meta = normalize_bars_payload(payload)
    if len(bars_all) < 2:
        raise VisualError("Need at least 2 OHLC bars to render candlestick chart")

    closes_all = [b["c"] for b in bars_all]
    periods = [int(p) for p in (ema_periods or ()) if int(p) > 0]
    ema_map = {}
    for p in periods[:3]:
        ema_map[p] = compute_ema(closes_all, p)

    vwap_all = compute_vwap(bars_all) if show_vwap else None
    rsi_val = compute_rsi(closes_all, 14)

    bars = bars_all[-int(max_bars):]
    offset = len(bars_all) - len(bars)
    has_vol = sum(float(b.get("v") or 0) for b in bars) > 0

    sup, res = compute_support_resistance(bars, lookback=min(20, len(bars))) if show_sr else (None, None)

    c = SvgCanvas(width=1120, height=640)
    top_pad = 64
    left_pad = 78
    right_pad = 155
    bottom_pad = 86
    chart_w = c.width - left_pad - right_pad
    if has_vol:
        price_h = c.height - top_pad - bottom_pad - 105
        vol_top = top_pad + price_h + 18
        vol_h = 85
    else:
        price_h = c.height - top_pad - bottom_pad
        vol_top = None
        vol_h = 0

    sym = meta.get("symbol") or "CHART"
    curr = _currency_prefix(meta)
    last_bar = bars[-1]
    pct_chg = ((last_bar["c"] / bars[0]["o"]) - 1.0) * 100.0 if bars[0]["o"] else 0.0
    head_title = title or ("%s (%s)  %s%s  %+.2f%%" % (
        sym, meta.get("interval") or "", curr, _fmt_num(last_bar["c"]), pct_chg
    ))
    c.text(30, 32, head_title, size=18, weight="bold", fill=PALETTE["text"])

    badge = str(meta.get("data_tier") or "UNKNOWN").upper()
    c.rect(c.width - 170, 14, 140, 24, fill=PALETTE["panel"], stroke=_tier_badge_color(badge), stroke_width=1.2, rx=4)
    c.text(c.width - 100, 30, badge, size=11, weight="bold", fill=_tier_badge_color(badge), anchor="middle")

    # Determine Y bounds across bars, EMAs, VWAP, levels, plan
    y_vals = []
    for b in bars:
        y_vals.extend([b["h"], b["l"]])
    for p, series in ema_map.items():
        for val in series[offset:]:
            if val is not None:
                y_vals.append(val)
    if vwap_all is not None:
        for val in vwap_all[offset:]:
            if val is not None:
                y_vals.append(val)

    extra_lines = []
    if sup is not None:
        extra_lines.append(("Support %s%s" % (curr, _fmt_num(sup)), sup, PALETTE["sr"], "4,4"))
    if res is not None:
        extra_lines.append(("Resist %s%s" % (curr, _fmt_num(res)), res, PALETTE["sr"], "4,4"))
    for lbl, val, col in (levels or ()):
        if val is not None:
            extra_lines.append((lbl, float(val), col, "6,3"))
    if isinstance(plan, dict):
        if plan.get("entry"):
            extra_lines.append(("Entry %s%s" % (curr, _fmt_num(plan["entry"])), float(plan["entry"]), PALETTE["entry"], "6,3"))
        if plan.get("stop"):
            extra_lines.append(("Stop %s%s" % (curr, _fmt_num(plan["stop"])), float(plan["stop"]), PALETTE["stop"], "5,3"))
        if plan.get("target"):
            extra_lines.append(("Target %s%s" % (curr, _fmt_num(plan["target"])), float(plan["target"]), PALETTE["target"], "5,3"))
    if isinstance(entry_doc, dict) and entry_doc.get("breakout_level"):
        bl = float(entry_doc["breakout_level"])
        extra_lines.append(("Breakout %s%s" % (curr, _fmt_num(bl)), bl, PALETTE["breakout"], "3,3"))
    if exit_price is not None:
        extra_lines.append(("Exit %s%s" % (curr, _fmt_num(exit_price)), float(exit_price), "#f472b6", "2,2"))

    for _, lv, _, _ in extra_lines:
        y_vals.append(lv)

    y_min = min(y_vals)
    y_max = max(y_vals)
    span = (y_max - y_min) or (y_max * 0.02) or 1.0
    y_lo = y_min - span * 0.07
    y_hi = y_max + span * 0.07

    def py(price):
        return top_pad + price_h * (1.0 - (price - y_lo) / (y_hi - y_lo))

    def px(idx):
        return left_pad + (idx + 0.5) * (chart_w / float(len(bars)))

    # Chart background box & grid
    c.rect(left_pad, top_pad, chart_w, price_h, fill=PALETTE["panel"], stroke=PALETTE["grid"])
    for k in range(5):
        frac = k / 4.0
        yv = y_lo + (y_hi - y_lo) * frac
        yp = py(yv)
        c.line(left_pad, yp, left_pad + chart_w, yp, stroke=PALETTE["grid"], width=1.0, dash="2,3")
        c.text(left_pad - 8, yp + 4, _fmt_num(yv), size=11, fill=PALETTE["muted"], anchor="end")

    # Horizontal support / resistance / trade plan lines
    for lbl, lv, col, dash in extra_lines:
        yp = py(lv)
        c.line(left_pad, yp, left_pad + chart_w, yp, stroke=col, width=1.2, dash=dash, opacity=0.9)
        c.text(left_pad + chart_w + 6, yp + 4, lbl, size=10, fill=col)

    # Candlesticks
    bar_w = chart_w / float(len(bars))
    body_w = max(2.0, min(12.0, bar_w * 0.62))
    for i, b in enumerate(bars):
        cx = px(i)
        col = PALETTE["up"] if b["c"] >= b["o"] else PALETTE["down"]
        c.line(cx, py(b["h"]), cx, py(b["l"]), stroke=col, width=1.2)
        y_open = py(b["o"])
        y_close = py(b["c"])
        y_top = min(y_open, y_close)
        h_box = max(1.5, abs(y_close - y_open))
        c.rect(cx - body_w / 2.0, y_top, body_w, h_box, fill=col, stroke=col, stroke_width=0.6)

    # EMAs and VWAP
    ema_colors = [PALETTE["ema1"], PALETTE["ema2"], "#a78bfa"]
    legend_items = []
    for idx_e, p in enumerate(periods[:3]):
        col = ema_colors[idx_e % len(ema_colors)]
        pts = []
        sub = ema_map[p][offset:]
        for i, val in enumerate(sub):
            if val is not None:
                pts.append((px(i), py(val)))
        if len(pts) >= 2:
            c.polyline(pts, stroke=col, width=1.8)
            last_v = next((v for v in reversed(sub) if v is not None), None)
            legend_items.append(("EMA %d: %s" % (p, _fmt_num(last_v)), col))
        else:
            legend_items.append(("EMA %d: insufficient bars (<%d)" % (p, p), PALETTE["muted"]))

    if vwap_all is not None:
        pts_v = [(px(i), py(val)) for i, val in enumerate(vwap_all[offset:]) if val is not None]
        if len(pts_v) >= 2:
            c.polyline(pts_v, stroke=PALETTE["vwap"], width=1.6, dash="5,3")
            last_vwap = next((v for v in reversed(vwap_all) if v is not None), None)
            legend_items.append(("VWAP: %s" % _fmt_num(last_vwap), PALETTE["vwap"]))
    elif show_vwap:
        legend_items.append(("VWAP: N/A (zero volume)", PALETTE["muted"]))

    if rsi_val is not None:
        legend_items.append(("RSI(14): %.1f" % rsi_val, PALETTE["accent"]))

    # Optional Entry / Exit scatter markers when exact price or bar index exists
    if isinstance(entry_doc, dict) and entry_doc.get("price"):
        ep = float(entry_doc["price"])
        ex_idx = len(bars) - 1
        et_str = str(entry_doc.get("bar_start") or "")
        for i, b in enumerate(bars):
            if et_str and et_str in b["label"]:
                ex_idx = i
                break
        c.circle(px(ex_idx), py(ep), r=5.0, fill=PALETTE["entry"], stroke="#ffffff", stroke_width=1.2)
    if exit_price is not None:
        c.circle(px(len(bars) - 1), py(float(exit_price)), r=5.0, fill="#f472b6", stroke="#ffffff", stroke_width=1.2)

    # Volume panel
    if has_vol and vol_top is not None:
        c.rect(left_pad, vol_top, chart_w, vol_h, fill=PALETTE["panel"], stroke=PALETTE["grid"])
        vmax = max(float(b["v"]) for b in bars) or 1.0
        for i, b in enumerate(bars):
            vh = (float(b["v"]) / vmax) * (vol_h - 10)
            col = PALETTE["up"] if b["c"] >= b["o"] else PALETTE["down"]
            c.rect(px(i) - body_w / 2.0, vol_top + vol_h - vh, body_w, vh, fill=col, opacity=0.65)
        c.text(left_pad - 8, vol_top + 14, "Vol", size=10, fill=PALETTE["muted"], anchor="end")

    # X-axis labels (up to 6 evenly spaced real bar timestamps)
    axis_y = (vol_top + vol_h + 16) if has_vol else (top_pad + price_h + 18)
    step_x = max(1, len(bars) // 6)
    tick_indices = list(range(0, len(bars), step_x))
    if (len(bars) - 1) not in tick_indices:
        tick_indices.append(len(bars) - 1)
    for i in tick_indices:
        c.text(px(i), axis_y, bars[i]["label"][-14:], size=10, fill=PALETTE["muted"], anchor="middle")

    # Legend strip under header
    lx = 30
    for txt, col in legend_items:
        c.circle(lx + 4, 49, r=3.5, fill=col)
        c.text(lx + 12, 53, txt, size=11, fill=col)
        lx += max(120, len(txt) * 7 + 26)

    extra_note = "Spot Index (non-tradable equity)" if meta.get("is_index") else None
    _provenance_footer(c, meta, note=extra_note)
    return c.to_svg()


def render_entry_exit_svg(bars_payload, plan=None, entry_doc=None, exit_price=None, ema_periods=(20, 50)):
    """Render trade setup chart with entry, stop-loss, target, breakout, and exit markers."""
    if not plan and not entry_doc:
        raise VisualError("entry-exit chart requires plan.json or entry.json metadata")
    return render_candlestick_svg(
        bars_payload,
        ema_periods=ema_periods,
        show_vwap=True,
        show_sr=True,
        plan=plan,
        entry_doc=entry_doc,
        exit_price=exit_price,
    )


# ---------------------------------------------------------------- Multi-Index Comparison Chart

def render_index_comparison_svg(scan_doc):
    """Render comparative multi-index chart directly from `scan.py indices` JSON output."""
    if not isinstance(scan_doc, dict) or not scan_doc.get("indices"):
        raise VisualError("Index scan JSON must contain non-empty 'indices' list")
    items = [r for r in scan_doc["indices"] if r.get("price") is not None]
    if not items:
        raise VisualError("No valid index price records to chart")

    c = SvgCanvas(width=1080, height=580)
    prov = scan_doc.get("provider") or "unknown"
    iv = scan_doc.get("interval") or "1d"
    c.text(30, 32, "Indian Benchmark Indices Comparison (%s)" % iv, size=18, weight="bold")
    breadth = scan_doc.get("breadth") or {}
    if breadth:
        bline = "Regime: %s | Bullish: %d  Bearish: %d  Neutral: %d | Avg RSI: %s" % (
            breadth.get("regime", "N/A"),
            breadth.get("bullish", 0),
            breadth.get("bearish", 0),
            breadth.get("neutral", 0),
            _fmt_num(breadth.get("avg_rsi"), 1) if breadth.get("avg_rsi") is not None else "N/A",
        )
        c.text(30, 54, bline, size=12, fill=PALETTE["accent"])

    left_pad = 150
    right_pad = 240
    top_pad = 80
    row_h = min(38, (c.height - top_pad - 70) / float(len(items)))
    chart_w = c.width - left_pad - right_pad

    pcts = [float(r.get("change_pct") or 0.0) for r in items]
    max_abs = max([abs(p) for p in pcts] + [0.5]) * 1.15
    zero_x = left_pad + chart_w / 2.0

    c.rect(left_pad, top_pad - 8, chart_w, row_h * len(items) + 12, fill=PALETTE["panel"], stroke=PALETTE["grid"])
    c.line(zero_x, top_pad - 8, zero_x, top_pad + row_h * len(items) + 4, stroke=PALETTE["axis"], width=1.2)

    for i, item in enumerate(items):
        y = top_pad + i * row_h + row_h * 0.5
        pct = float(item.get("change_pct") or 0.0)
        bar_len = (abs(pct) / max_abs) * (chart_w * 0.45)
        col = PALETTE["up"] if pct >= 0 else PALETTE["down"]
        bx = zero_x if pct >= 0 else (zero_x - bar_len)
        c.rect(bx, y - 9, max(2.0, bar_len), 18, fill=col, rx=3)
        c.text(left_pad - 12, y + 4, item.get("symbol", ""), size=12, weight="bold", anchor="end")
        rsi_txt = ("RSI %.1f" % item["rsi_14"]) if item.get("rsi_14") is not None else "RSI N/A"
        summary = "Rs %s  (%+.2f%%)  [%s | %s]" % (
            _fmt_num(item.get("price")), pct, item.get("trend", "neutral"), rsi_txt
        )
        c.text(left_pad + chart_w + 12, y + 4, summary, size=11, fill=PALETTE["text"])

    _provenance_footer(c, scan_doc, note="Analytical spot indices only (not tradable cash equities)")
    return c.to_svg()


# ---------------------------------------------------------------- 3. Options Payoff + Greeks/IV

def render_options_payoff_svg(payload):
    """Render expiration payoff curve, breakevens, spot line, and Greeks/IV callouts."""
    if not isinstance(payload, dict):
        raise VisualError("Expected options payoff JSON dict")
    legs = payload.get("legs")
    if not legs and payload.get("strike") is not None:
        legs = [{
            "strike": payload["strike"],
            "option_type": payload.get("option_type") or "CE",
            "premium": payload.get("premium") or payload.get("ltp") or 0.0,
            "side": payload.get("side") or "buy",
            "qty": payload.get("qty") or payload.get("lot_size") or 1,
        }]
    if not legs:
        raise VisualError("Options payoff JSON requires 'legs' (or top-level strike/option_type/premium)")

    spot = payload.get("spot_price") or payload.get("underlying_price")
    if spot is None:
        raise VisualError("Options payoff requires real 'spot_price' / 'underlying_price'")
    spot = float(spot)

    strikes = [float(leg["strike"]) for leg in legs]
    span = max(spot * 0.08, (max(strikes) - min(strikes)) + spot * 0.04, 10.0)
    s_lo = max(0.01, min(strikes + [spot]) - span * 0.5)
    s_hi = max(strikes + [spot]) + span * 0.5
    steps = 80
    xs = [s_lo + (s_hi - s_lo) * i / float(steps) for i in range(steps + 1)]

    def leg_payoff(leg, s_exp):
        k = float(leg["strike"])
        prem = float(leg.get("premium") or 0.0)
        qty = float(leg.get("qty") or 1.0)
        sign = -1.0 if str(leg.get("side") or "buy").lower() == "sell" else 1.0
        ot = str(leg.get("option_type") or "CE").upper()
        intrinsic = max(0.0, s_exp - k) if ot in ("CE", "CALL", "C") else max(0.0, k - s_exp)
        return sign * (intrinsic - prem) * qty

    ys = [sum(leg_payoff(l, x) for l in legs) for x in xs]
    y_min = min(ys + [0.0])
    y_max = max(ys + [0.0])
    pad_y = (y_max - y_min) * 0.12 or 10.0
    y_lo, y_hi = y_min - pad_y, y_max + pad_y

    c = SvgCanvas(width=1060, height=580)
    left_pad, right_pad, top_pad, bottom_pad = 90, 260, 68, 80
    cw = c.width - left_pad - right_pad
    ch = c.height - top_pad - bottom_pad
    curr = _currency_prefix(payload)

    def px(sv):
        return left_pad + cw * (sv - s_lo) / (s_hi - s_lo)

    def py(pv):
        return top_pad + ch * (1.0 - (pv - y_lo) / (y_hi - y_lo))

    sym = payload.get("symbol") or payload.get("underlying") or "OPTIONS"
    c.text(30, 32, "%s Options Expiration Payoff (%s)" % (sym, payload.get("expiry") or ""), size=18, weight="bold")
    c.rect(left_pad, top_pad, cw, ch, fill=PALETTE["panel"], stroke=PALETTE["grid"])

    y0 = py(0.0)
    c.line(left_pad, y0, left_pad + cw, y0, stroke=PALETTE["axis"], width=1.2)
    c.line(px(spot), top_pad, px(spot), top_pad + ch, stroke=PALETTE["breakout"], width=1.4, dash="5,3")
    c.text(px(spot), top_pad - 6, "Spot %s%s" % (curr, _fmt_num(spot)), size=11, fill=PALETTE["breakout"], anchor="middle")

    pts = [(px(x), py(y)) for x, y in zip(xs, ys)]
    c.polyline(pts, stroke=PALETTE["ema1"], width=2.4)
    for k in range(5):
        v = y_lo + (y_hi - y_lo) * k / 4.0
        yp = py(v)
        c.line(left_pad, yp, left_pad + cw, yp, stroke=PALETTE["grid"], width=0.8, dash="2,3")
        c.text(left_pad - 8, yp + 4, curr + _fmt_num(v, 0), size=11, fill=PALETTE["muted"], anchor="end")
    for k in range(5):
        sv = s_lo + (s_hi - s_lo) * k / 4.0
        c.text(px(sv), top_pad + ch + 18, _fmt_num(sv, 0), size=11, fill=PALETTE["muted"], anchor="middle")

    # Greeks / IV card on right
    gx = left_pad + cw + 20
    c.rect(gx, top_pad, 220, 210, fill=PALETTE["panel"], stroke=PALETTE["grid"], rx=6)
    c.text(gx + 14, top_pad + 24, "Greeks & IV Summary", size=13, weight="bold", fill=PALETTE["accent"])
    greeks = payload.get("greeks") or payload
    rows = [
        ("Spot", "%s%s" % (curr, _fmt_num(spot))),
        ("IV", ("%s%%" % _fmt_num(greeks.get("iv"), 2)) if greeks.get("iv") is not None else "N/A"),
        ("Delta", _fmt_num(greeks.get("delta"), 4) if greeks.get("delta") is not None else "N/A"),
        ("Gamma", _fmt_num(greeks.get("gamma"), 4) if greeks.get("gamma") is not None else "N/A"),
        ("Theta", _fmt_num(greeks.get("theta"), 2) if greeks.get("theta") is not None else "N/A"),
        ("Vega", _fmt_num(greeks.get("vega"), 2) if greeks.get("vega") is not None else "N/A"),
        ("PCR", _fmt_num(payload.get("pcr"), 2) if payload.get("pcr") is not None else "N/A"),
    ]
    for r_i, (rk, rv) in enumerate(rows):
        ry = top_pad + 50 + r_i * 22
        c.text(gx + 14, ry, rk, size=11, fill=PALETTE["muted"])
        c.text(gx + 204, ry, rv, size=11, fill=PALETTE["text"], anchor="end")

    _provenance_footer(c, payload)
    return c.to_svg()


# ---------------------------------------------------------------- 4. Portfolio Allocation & P&L

def render_portfolio_svg(portfolio_doc, prices_doc=None):
    """Render holdings allocation and P&L breakdown directly from trader/portfolio.json."""
    if not isinstance(portfolio_doc, dict):
        raise VisualError("Expected portfolio JSON dict")
    positions = portfolio_doc.get("positions") or []
    cash = float(portfolio_doc.get("cash") or 0.0)
    realized = float(portfolio_doc.get("realized_pnl") or 0.0)
    quotes = (prices_doc or {}).get("quotes") or (prices_doc or {}).get("prices") or {}
    curr = _currency_prefix(portfolio_doc)

    rows = []
    total_unreal = 0.0
    for p in positions:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or p.get("ticker") or "").upper()
        qty = float(p.get("qty") or p.get("shares") or 0.0)
        avg = float(p.get("avg_price") or p.get("entry") or 0.0)
        q_item = quotes.get(sym) if isinstance(quotes.get(sym), dict) else None
        mark = float((q_item or {}).get("price") or (quotes.get(sym) if isinstance(quotes.get(sym), (int, float)) else avg) or 0.0)
        val = qty * mark
        pnl = qty * (mark - avg)
        total_unreal += pnl
        rows.append({"label": sym, "value": val, "pnl": pnl})

    total_eq = cash + sum(r["value"] for r in rows)
    if total_eq <= 0 and not rows:
        raise VisualError("Portfolio contains no cash or holdings to chart")

    all_bars = list(rows) + [{"label": "CASH", "value": cash, "pnl": None}]
    c = SvgCanvas(width=1020, height=max(420, 170 + len(all_bars) * 42))
    c.text(30, 32, "Portfolio Allocation & P&L", size=18, weight="bold")
    summary = "Total Value: %s%s  |  Cash: %s%s  |  Realized P&L: %s%s  |  Unrealized: %s%s" % (
        curr, _fmt_num(total_eq), curr, _fmt_num(cash),
        "+" if realized >= 0 else "-", curr + _fmt_num(abs(realized)),
        "+" if total_unreal >= 0 else "-", curr + _fmt_num(abs(total_unreal)),
    )
    c.text(30, 54, summary, size=12, fill=PALETTE["accent"])

    left_pad, right_pad, top_pad = 130, 220, 84
    cw = c.width - left_pad - right_pad
    max_v = max([r["value"] for r in all_bars] + [1.0])
    c.rect(left_pad, top_pad - 6, cw, len(all_bars) * 40 + 12, fill=PALETTE["panel"], stroke=PALETTE["grid"])
    for i, r in enumerate(all_bars):
        y = top_pad + i * 40 + 20
        bw = (r["value"] / max_v) * cw
        col = PALETTE["muted"] if r["label"] == "CASH" else (PALETTE["up"] if (r["pnl"] or 0) >= 0 else PALETTE["down"])
        c.rect(left_pad + 4, y - 10, max(2.0, bw - 8), 20, fill=col, rx=3)
        c.text(left_pad - 10, y + 4, r["label"], size=12, weight="bold", anchor="end")
        pct_w = (100.0 * r["value"] / total_eq) if total_eq > 0 else 0.0
        pnl_txt = "" if r["pnl"] is None else (" | P&L %s%s%s" % ("+" if r["pnl"] >= 0 else "-", curr, _fmt_num(abs(r["pnl"]))))
        c.text(left_pad + cw + 12, y + 4, "%s%s (%.1f%%)%s" % (curr, _fmt_num(r["value"]), pct_w, pnl_txt), size=11)

    meta = dict(prices_doc or portfolio_doc)
    meta.setdefault("provider", "local_portfolio")
    meta.setdefault("interval", "snapshot")
    meta.setdefault("data_tier", "DELAYED" if prices_doc else "HISTORICAL")
    _provenance_footer(c, meta)
    return c.to_svg()


# ---------------------------------------------------------------- 5. Risk Exposure & Drawdown

def render_risk_drawdown_svg(risk_doc=None, broker_doc=None, config_doc=None):
    """Render per-trade risk exposure vs limits and cumulative realized drawdown."""
    cfg = config_doc or {}
    rdoc = risk_doc or {}
    bdoc = broker_doc or {}
    curr = _currency_prefix(cfg or rdoc or bdoc)

    # Collect realized fill P&L sequence and checked order risks
    fills = [f for f in (bdoc.get("fills") or []) if isinstance(f, dict) and f.get("pnl") is not None]
    orders_checked = [r for r in (rdoc.get("orders") or rdoc.get("results") or []) if isinstance(r, dict)]
    if not fills and not orders_checked and not bdoc.get("daily"):
        raise VisualError("Need risk/broker records with checked orders or fill P&L to render risk-drawdown chart")

    c = SvgCanvas(width=1060, height=580)
    c.text(30, 32, "Risk Exposure & Daily Drawdown", size=18, weight="bold")
    max_loss_trade = cfg.get("max_loss_per_trade_inr") or cfg.get("max_loss_per_trade_usd")
    max_daily = cfg.get("max_daily_loss_inr") or cfg.get("max_daily_loss_usd")
    c.text(30, 54, "Max Loss/Trade: %s%s  |  Max Daily Loss Cap: %s%s" % (
        curr, _fmt_num(max_loss_trade) if max_loss_trade else "N/A",
        curr, _fmt_num(max_daily) if max_daily else "N/A",
    ), size=12, fill=PALETTE["accent"])

    # Plot cumulative realized P&L curve from fills (or daily summary)
    eq = [0.0]
    labels = ["Start"]
    for idx, f in enumerate(fills):
        eq.append(eq[-1] + float(f["pnl"]))
        labels.append(str(f.get("symbol") or ("#%d" % (idx + 1))))
    if len(eq) == 1 and isinstance(bdoc.get("daily"), dict):
        for d_k, d_v in sorted(bdoc["daily"].items()):
            pnl_v = d_v.get("realized_pnl") if isinstance(d_v, dict) else d_v
            if pnl_v is not None:
                eq.append(eq[-1] + float(pnl_v))
                labels.append(str(d_k))

    lp, rp, tp, bp = 85, 40, 82, 90
    cw = c.width - lp - rp
    ch = c.height - tp - bp
    c.rect(lp, tp, cw, ch, fill=PALETTE["panel"], stroke=PALETTE["grid"])

    cap_neg = -abs(float(max_daily)) if max_daily else None
    y_vals = list(eq) + ([cap_neg] if cap_neg is not None else [])
    y_lo = min(y_vals) - 10.0
    y_hi = max(y_vals + [10.0]) + 10.0

    def px(i):
        return lp + (cw * i / float(max(1, len(eq) - 1)))

    def py(v):
        return tp + ch * (1.0 - (v - y_lo) / (y_hi - y_lo))

    c.line(lp, py(0.0), lp + cw, py(0.0), stroke=PALETTE["axis"], width=1.1)
    if cap_neg is not None and y_lo <= cap_neg <= y_hi:
        c.line(lp, py(cap_neg), lp + cw, py(cap_neg), stroke=PALETTE["stop"], width=1.4, dash="5,3")
        c.text(lp + 8, py(cap_neg) - 6, "Daily Loss Limit (%s%s)" % (curr, _fmt_num(cap_neg)), size=11, fill=PALETTE["stop"])

    if len(eq) >= 2:
        pts = [(px(i), py(v)) for i, v in enumerate(eq)]
        c.polyline(pts, stroke=PALETTE["ema1"], width=2.4)
        for i, (xv, yv) in enumerate(pts):
            col = PALETTE["up"] if eq[i] >= 0 else PALETTE["down"]
            c.circle(xv, yv, r=4.0, fill=col)
            if i == 0 or i == len(pts) - 1 or len(pts) <= 8:
                c.text(xv, tp + ch + 18, labels[i], size=10, fill=PALETTE["muted"], anchor="middle")
    else:
        c.text(lp + cw / 2.0, tp + ch / 2.0, "No closed fills recorded yet", size=13, fill=PALETTE["muted"], anchor="middle")

    for k in range(5):
        yv = y_lo + (y_hi - y_lo) * k / 4.0
        yp = py(yv)
        c.line(lp, yp, lp + cw, yp, stroke=PALETTE["grid"], width=0.8, dash="2,3")
        c.text(lp - 8, yp + 4, curr + _fmt_num(yv, 0), size=11, fill=PALETTE["muted"], anchor="end")

    meta = dict(rdoc or bdoc)
    meta.setdefault("provider", "local_risk_ledger")
    meta.setdefault("interval", "daily")
    meta.setdefault("data_tier", "HISTORICAL")
    _provenance_footer(c, meta)
    return c.to_svg()


# ---------------------------------------------------------------- 6. Backtest Fan & Distribution

def render_backtest_svg(tm_doc):
    """Render Time Machine Monte Carlo bands and replay stats from trader/timemachine.json."""
    if not isinstance(tm_doc, dict) or not tm_doc.get("mc") or not tm_doc.get("plan"):
        raise VisualError("Backtest chart requires Time Machine JSON with 'mc' and 'plan'")
    plan = tm_doc["plan"]
    mc = tm_doc["mc"]
    bands = mc.get("bands") or []
    times = mc.get("band_times") or []
    if len(bands) < 2:
        raise VisualError("Time Machine JSON has insufficient band points")

    c = SvgCanvas(width=1080, height=580)
    curr = _currency_prefix(tm_doc)
    sym = plan.get("symbol") or ""
    c.text(30, 32, "%s Time Machine Replay (%s paths, seed %s)" % (
        sym, "{:,}".format(int(mc.get("band_paths") or mc.get("replays") or 10000)), mc.get("seed", "")
    ), size=18, weight="bold")
    c.text(30, 54, "Target First: %.1f%%  |  Stop First: %.1f%%  |  Mean R: %+.2f  |  Pool Sessions: %s" % (
        float(mc.get("target_pct") or 0.0),
        float(mc.get("stop_pct") or 0.0),
        float(mc.get("mean_r") or 0.0),
        mc.get("pool_sessions", "N/A"),
    ), size=12, fill=PALETTE["accent"])

    lp, rp, tp, bp = 85, 130, 80, 90
    cw = c.width - lp - rp
    ch = c.height - tp - bp
    c.rect(lp, tp, cw, ch, fill=PALETTE["panel"], stroke=PALETTE["grid"])

    all_p = [float(plan["stop"]), float(plan["entry"]), float(plan["target"])]
    for b in bands:
        all_p.extend([float(b[0]), float(b[4])])
    y_lo = min(all_p) - (max(all_p) - min(all_p)) * 0.05
    y_hi = max(all_p) + (max(all_p) - min(all_p)) * 0.05

    def px(i):
        return lp + cw * i / float(len(bands) - 1)

    def py(v):
        return tp + ch * (1.0 - (v - y_lo) / (y_hi - y_lo))

    # p5-p95 and p25-p75 shaded fans
    poly90 = [(px(i), py(b[4])) for i, b in enumerate(bands)] + [(px(i), py(b[0])) for i, b in reversed(list(enumerate(bands)))]
    poly50 = [(px(i), py(b[3])) for i, b in enumerate(bands)] + [(px(i), py(b[1])) for i, b in reversed(list(enumerate(bands)))]
    c.polygon(poly90, fill=PALETTE["ema1"], opacity=0.14)
    c.polygon(poly50, fill=PALETTE["ema1"], opacity=0.28)
    c.polyline([(px(i), py(b[2])) for i, b in enumerate(bands)], stroke=PALETTE["ema1"], width=2.3)

    for lbl, val, col in (
        ("Target %s%s" % (curr, _fmt_num(plan["target"])), float(plan["target"]), PALETTE["target"]),
        ("Entry %s%s" % (curr, _fmt_num(plan["entry"])), float(plan["entry"]), PALETTE["entry"]),
        ("Stop %s%s" % (curr, _fmt_num(plan["stop"])), float(plan["stop"]), PALETTE["stop"]),
    ):
        yp = py(val)
        c.line(lp, yp, lp + cw, yp, stroke=col, width=1.3, dash="5,3")
        c.text(lp + cw + 8, yp + 4, lbl, size=11, fill=col)

    for k in range(5):
        yv = y_lo + (y_hi - y_lo) * k / 4.0
        c.text(lp - 8, py(yv) + 4, _fmt_num(yv), size=11, fill=PALETTE["muted"], anchor="end")
    for idx in sorted({0, len(bands) // 2, len(bands) - 1}):
        lbl = times[idx] if idx < len(times) else str(idx)
        c.text(px(idx), tp + ch + 18, lbl, size=11, fill=PALETTE["muted"], anchor="middle")

    meta = dict(tm_doc)
    meta["interval"] = (tm_doc.get("data") or {}).get("interval") or "5m"
    meta.setdefault("data_tier", tm_doc.get("data_status") or "HISTORICAL")
    _provenance_footer(c, meta)
    return c.to_svg()


# ---------------------------------------------------------------- 7. Fundamentals Trend

def render_fundamentals_svg(facts_doc):
    """Render Revenue, Net Income & Net Margin trend directly from facts.py --json output."""
    if isinstance(facts_doc, list):
        if not facts_doc:
            raise VisualError("Facts JSON list is empty")
        facts_doc = facts_doc[0]
    if not isinstance(facts_doc, dict):
        raise VisualError("Expected facts JSON dict or list")
    ticker = facts_doc.get("ticker") or (facts_doc.get("result") or {}).get("ticker") or "COMPANY"
    res = facts_doc.get("result") or facts_doc
    periods = res.get("periods") or res.get("quarters") or res.get("annual") or []
    if not periods:
        raise VisualError("Facts JSON contains no 'periods'/'quarters'/'annual' entries")
    pts_data = list(reversed(periods)) if len(periods) >= 2 and str(periods[0].get("end") or periods[0].get("label") or "") > str(periods[-1].get("end") or periods[-1].get("label") or "") else list(periods)

    revs = [float(p.get("revenue") or p.get("total_revenue") or 0.0) for p in pts_data]
    ninc = [float(p.get("net_income") or p.get("net_profit") or 0.0) for p in pts_data]
    if not any(revs) and not any(ninc):
        raise VisualError("Fundamentals periods have no revenue or net income values")

    scale_unit = res.get("unit") or ("Rs Cr" if str(res.get("currency") or "").upper() == "INR" else "M")
    div = 1.0
    if max(revs + [0.0]) > 1e8 and scale_unit == "M":
        div = 1e6

    c = SvgCanvas(width=1060, height=580)
    c.text(30, 32, "%s Fundamental Trends (Revenue & Net Income, %s)" % (ticker, scale_unit), size=18, weight="bold")
    lp, rp, tp, bp = 88, 70, 74, 90
    cw = c.width - lp - rp
    ch = c.height - tp - bp
    c.rect(lp, tp, cw, ch, fill=PALETTE["panel"], stroke=PALETTE["grid"])

    y_vals = [v / div for v in revs + ninc] + [0.0]
    y_lo, y_hi = min(y_vals) * 1.1, max(y_vals) * 1.15 or 1.0
    slot_w = cw / float(len(pts_data))
    bw = max(8.0, min(28.0, slot_w * 0.32))

    def py(v):
        return tp + ch * (1.0 - (v - y_lo) / (y_hi - y_lo))

    y0 = py(0.0)
    c.line(lp, y0, lp + cw, y0, stroke=PALETTE["axis"], width=1.1)
    for i, p in enumerate(pts_data):
        cx = lp + (i + 0.5) * slot_w
        rv = revs[i] / div
        nv = ninc[i] / div
        c.rect(cx - bw - 2, py(max(0.0, rv)), bw, abs(py(rv) - y0), fill=PALETTE["ema1"], rx=2)
        ncol = PALETTE["up"] if nv >= 0 else PALETTE["down"]
        c.rect(cx + 2, py(nv) if nv >= 0 else y0, bw, abs(py(nv) - y0), fill=ncol, rx=2)
        lbl = str(p.get("label") or p.get("fy_quarter") or p.get("end") or ("P%d" % (i + 1)))[:10]
        c.text(cx, tp + ch + 18, lbl, size=10, fill=PALETTE["muted"], anchor="middle")

    for k in range(5):
        yv = y_lo + (y_hi - y_lo) * k / 4.0
        c.text(lp - 8, py(yv) + 4, _fmt_num(yv, 1), size=11, fill=PALETTE["muted"], anchor="end")

    c.circle(36, 52, r=4, fill=PALETTE["ema1"])
    c.text(46, 56, "Revenue", size=11, fill=PALETTE["ema1"])
    c.circle(130, 52, r=4, fill=PALETTE["up"])
    c.text(140, 56, "Net Income", size=11, fill=PALETTE["up"])

    meta = {
        "provider": res.get("source") or "sec_edgar/yahoo",
        "interval": "quarterly",
        "data_tier": "HISTORICAL",
        "last_data_timestamp": res.get("as_of") or str((pts_data[-1] or {}).get("end") or ""),
    }
    _provenance_footer(c, meta)
    return c.to_svg()


# ---------------------------------------------------------------- 8. News & Event Timeline

def render_news_timeline_svg(news_doc):
    """Render chronological news/catalyst timeline directly from news.py --json output."""
    if not isinstance(news_doc, dict):
        raise VisualError("Expected news JSON dict")
    items = news_doc.get("items") or news_doc.get("filings") or []
    if not items:
        raise VisualError("News JSON contains no items to chart")
    shown = items[:12]
    c = SvgCanvas(width=1080, height=max(440, 150 + len(shown) * 38))
    c.text(30, 32, "Catalyst & Filing Timeline (%d items)" % len(shown), size=18, weight="bold")

    cat_colors = {
        "corporate": "#22c55e",
        "business": "#38bdf8",
        "analyst": "#f59e0b",
        "market": "#c084fc",
        "low_relevance": "#94a3b8",
    }
    tx = 175
    top_y = 74
    c.line(tx, top_y, tx, top_y + (len(shown) - 1) * 38 + 12, stroke=PALETTE["grid"], width=2.0)

    for i, it in enumerate(shown):
        y = top_y + i * 38
        cat = str(it.get("category") or it.get("relevance") or "market").lower()
        col = cat_colors.get(cat, PALETTE["accent"])
        t_str = str(it.get("time") or it.get("published") or it.get("published_at") or it.get("filing_date") or "")[:16]
        tick = str(it.get("ticker") or it.get("symbol") or "").upper()
        title_str = str(it.get("title") or it.get("headline") or it.get("form") or "")[:82]
        c.text(tx - 12, y + 4, t_str, size=10, fill=PALETTE["muted"], anchor="end")
        c.circle(tx, y, r=5.0, fill=col)
        c.text(tx + 14, y + 4, "[%s] %s: %s" % (cat.upper(), tick, title_str), size=11, fill=PALETTE["text"])

    meta = {
        "provider": news_doc.get("source") or "rss/sec_edgar",
        "interval": "events",
        "data_tier": "DELAYED",
        "last_data_timestamp": news_doc.get("checked") or "",
    }
    _provenance_footer(c, meta)
    return c.to_svg()


# ---------------------------------------------------------------- File Writer & CLI

def write_svg(path, svg_text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg_text)
    return path


def _load_json(path):
    if not path or not os.path.isfile(path):
        raise VisualError("JSON file not found: %s" % path)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Opt-in SVG chart renderer from existing trader JSON payloads (stdlib only).")
    sub = ap.add_subparsers(dest="cmd")

    def add_common_io(p, need_input=True):
        if need_input:
            p.add_argument("--input", required=True, help="existing JSON payload path")
        p.add_argument("--out", required=True, help="output .svg path")

    p = sub.add_parser("candlestick", help="candlestick + EMA/VWAP/volume + support/resistance")
    add_common_io(p)
    p.add_argument("--ema", default="20,50", help="comma-separated EMA periods (default 20,50)")
    p.add_argument("--no-vwap", action="store_true")
    p.add_argument("--plan", help="optional trader/plan.json overlay")
    p.add_argument("--entry", help="optional trader/entry.json overlay")
    p.add_argument("--max-bars", type=int, default=120)

    p = sub.add_parser("entry-exit", help="entry, stop-loss, targets and exit chart")
    add_common_io(p)
    p.add_argument("--plan", help="trader/plan.json")
    p.add_argument("--entry", help="trader/entry.json")
    p.add_argument("--exit-price", type=float)
    p.add_argument("--ema", default="20,50")

    p = sub.add_parser("index-scan", help="comparative multi-index chart from scan.py indices JSON")
    add_common_io(p)

    p = sub.add_parser("options-payoff", help="options payoff + Greeks/IV chart")
    add_common_io(p)

    p = sub.add_parser("portfolio", help="portfolio allocation and P&L chart")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--prices", help="optional trader/prices.json")
    p.add_argument("--out", required=True)

    p = sub.add_parser("risk-drawdown", help="risk exposure and drawdown chart")
    p.add_argument("--risk", default="trader/state/risk.json")
    p.add_argument("--broker", default="trader/state/broker.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--out", required=True)

    p = sub.add_parser("backtest", help="Time Machine Monte Carlo & replay chart")
    add_common_io(p)

    p = sub.add_parser("fundamentals", help="fundamental revenue/profit/margin chart from facts.py --json")
    add_common_io(p)

    p = sub.add_parser("news-timeline", help="news/catalyst event timeline from news.py --json")
    add_common_io(p)

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        if a.cmd == "candlestick":
            emas = tuple(int(x) for x in a.ema.split(",") if x.strip()) if a.ema else ()
            svg = render_candlestick_svg(
                _load_json(a.input),
                ema_periods=emas,
                show_vwap=not a.no_vwap,
                plan=_load_json(a.plan) if a.plan else None,
                entry_doc=_load_json(a.entry) if a.entry else None,
                max_bars=a.max_bars,
            )
        elif a.cmd == "entry-exit":
            emas = tuple(int(x) for x in a.ema.split(",") if x.strip()) if a.ema else ()
            svg = render_entry_exit_svg(
                _load_json(a.input),
                plan=_load_json(a.plan) if a.plan else None,
                entry_doc=_load_json(a.entry) if a.entry else None,
                exit_price=a.exit_price,
                ema_periods=emas,
            )
        elif a.cmd == "index-scan":
            svg = render_index_comparison_svg(_load_json(a.input))
        elif a.cmd == "options-payoff":
            svg = render_options_payoff_svg(_load_json(a.input))
        elif a.cmd == "portfolio":
            svg = render_portfolio_svg(
                _load_json(a.portfolio),
                prices_doc=_load_json(a.prices) if a.prices and os.path.isfile(a.prices) else None,
            )
        elif a.cmd == "risk-drawdown":
            svg = render_risk_drawdown_svg(
                risk_doc=_load_json(a.risk) if os.path.isfile(a.risk) else None,
                broker_doc=_load_json(a.broker) if os.path.isfile(a.broker) else None,
                config_doc=_load_json(a.config) if os.path.isfile(a.config) else None,
            )
        elif a.cmd == "backtest":
            svg = render_backtest_svg(_load_json(a.input))
        elif a.cmd == "fundamentals":
            svg = render_fundamentals_svg(_load_json(a.input))
        elif a.cmd == "news-timeline":
            svg = render_news_timeline_svg(_load_json(a.input))
        else:
            return 1
        write_svg(a.out, svg)
        print("Wrote %s." % a.out)
        return 0
    except VisualError as e:
        print("visuals.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
