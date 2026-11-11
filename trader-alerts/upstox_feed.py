#!/usr/bin/env python3
"""Upstox API v3 WebSocket Market Data Feed client and pure-stdlib Protobuf decoder.

Strictly read-only streaming helper for Upstox Analytics Token / API v3 market data
feeds. Implemented using only Python 3.8+ standard library modules (zero runtime
dependencies on protobuf or websocket-client). Never stores, logs, prints, or emits
bearer tokens or authorized WSS signatures in errors or state.
"""

import base64
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import uuid


UPSTOX_WS_AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

VALID_MODES = ("ltpc", "option_greeks", "full", "full_d30")
VALID_TIERS = ("standard", "plus")
DEFAULT_ACCOUNT_TIER = "standard"

# Official Upstox V3 WebSocket tier-aware limits.
# Standard users: 2 concurrent connections; full_d30 is Upstox Plus exclusive.
# Individual limits apply when subscribing exclusively to one mode.
# Combined limits apply when mixing multiple modes across one or more connections.
TIER_LIMITS = {
    "standard": {
        "max_connections": 2,
        "modes": {
            "ltpc": {"individual": 5000, "combined": 2000, "per_connection": 5000},
            "option_greeks": {"individual": 3000, "combined": 2000, "per_connection": 3000},
            "full": {"individual": 2000, "combined": 1500, "per_connection": 2000},
            "full_d30": None,
        },
    },
    "plus": {
        "max_connections": 5,
        "modes": {
            "ltpc": {"individual": 5000, "combined": 2000, "per_connection": 5000},
            "option_greeks": {"individual": 3000, "combined": 2000, "per_connection": 3000},
            "full": {"individual": 2000, "combined": 1500, "per_connection": 2000},
            "full_d30": {"individual": 50, "combined": 1500, "per_connection": 50},
        },
    },
}

MODE_LIMITS = {
    "full_d30": 50,
    "full": 2000,
    "option_greeks": 3000,
    "ltpc": 5000,
}
COMBINED_MODE_LIMITS = {
    "full_d30": 1500,
    "full": 1500,
    "option_greeks": 2000,
    "ltpc": 2000,
}
MAX_TOTAL_SUBSCRIPTIONS = 5000
MAX_CONCURRENT_CONNECTIONS = TIER_LIMITS[DEFAULT_ACCOUNT_TIER]["max_connections"]


def normalize_account_tier(tier: Optional[str] = None) -> str:
    """Normalize configured Upstox account tier ('standard' default or 'plus')."""
    raw = str(tier or "").strip().lower()
    if not raw or raw == "standard":
        return "standard"
    if raw in ("plus", "upstox_plus", "upstox-plus", "upstox plus"):
        return "plus"
    raise ValueError(
        "Invalid Upstox account_tier '%s'. Supported values: standard, plus" % tier
    )


def get_tier_limits(tier: Optional[str] = None) -> Dict[str, Any]:
    """Return a copy of the Upstox V3 WebSocket limits for the requested tier."""
    canon = normalize_account_tier(tier)
    spec = TIER_LIMITS[canon]
    modes_copy = {}
    for m, cfg in spec["modes"].items():
        modes_copy[m] = dict(cfg) if isinstance(cfg, dict) else None
    return {
        "account_tier": canon,
        "max_connections": spec["max_connections"],
        "modes": modes_copy,
    }

FEED_TYPE_NAMES = {
    0: "initial_feed",
    1: "live_feed",
    2: "market_info",
}

REQUEST_MODE_NAMES = {
    0: "ltpc",
    1: "full_d5",
    2: "option_greeks",
    3: "full_d30",
}

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-+/=]+", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)?\b")
_QUERY_TOKEN_RE = re.compile(r"([?&](?:token|access_token|analytics_token|signature|Authorization)=)[^&\s]+", re.IGNORECASE)


def redact_secret(text: Any, secret: Optional[str] = None) -> str:
    """Scrub bearer tokens, JWTs, query auth params, and explicit secrets from text."""
    out = str(text or "")
    if secret and len(str(secret)) >= 4:
        out = out.replace(str(secret), "[REDACTED]")
    out = _BEARER_RE.sub(r"\1[REDACTED]", out)
    out = _QUERY_TOKEN_RE.sub(r"\1[REDACTED]", out)
    out = _JWT_RE.sub("[REDACTED_TOKEN]", out)
    return out


class UpstoxStreamError(RuntimeError):
    """Base error for Upstox WebSocket streaming lifecycle failures."""


class UpstoxStreamLimitError(UpstoxStreamError):
    """Raised when a subscription exceeds Upstox V3 mode or connection limits."""


class UpstoxStreamAuthError(UpstoxStreamError):
    """Raised when WebSocket authorization is rejected due to missing/invalid token."""


# ---------------------------------------------------------------- Pure-Stdlib Protobuf Decoder


def _read_varint(buf: bytes, pos: int, end: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while pos < end:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            if result & (1 << 63):
                # Sign-extend 64-bit two's complement int64 values encoded as unsigned varints
                if result >= (1 << 63) and shift >= 63:
                    result -= (1 << 64)
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("Malformed protobuf varint")
    raise ValueError("Truncated protobuf varint")


def _iter_fields(buf: bytes, start: int = 0, end: Optional[int] = None) -> List[Tuple[int, int, Any]]:
    """Parse raw Protobuf wire bytes into [(field_number, wire_type, value), ...]."""
    if end is None:
        end = len(buf)
    pos = start
    fields = []
    while pos < end:
        tag, pos = _read_varint(buf, pos, end)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num == 0:
            raise ValueError("Invalid protobuf field number 0")
        if wire_type == 0:
            val, pos = _read_varint(buf, pos, end)
        elif wire_type == 1:
            if pos + 8 > end:
                raise ValueError("Truncated 64-bit protobuf field")
            val = buf[pos:pos + 8]
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos, end)
            if length < 0 or pos + length > end:
                raise ValueError("Truncated length-delimited protobuf field")
            val = buf[pos:pos + length]
            pos += length
        elif wire_type == 5:
            if pos + 4 > end:
                raise ValueError("Truncated 32-bit protobuf field")
            val = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError("Unsupported protobuf wire type: %d" % wire_type)
        fields.append((field_num, wire_type, val))
    return fields


def _as_double(val: Any, wire_type: int) -> Optional[float]:
    if wire_type == 1 and isinstance(val, (bytes, bytearray)) and len(val) == 8:
        return struct.unpack("<d", val)[0]
    if wire_type == 5 and isinstance(val, (bytes, bytearray)) and len(val) == 4:
        return float(struct.unpack("<f", val)[0])
    if wire_type == 0:
        return float(val)
    return None


def _as_int64(val: Any, wire_type: int) -> Optional[int]:
    if wire_type == 0:
        return int(val)
    if wire_type == 1 and isinstance(val, (bytes, bytearray)) and len(val) == 8:
        return struct.unpack("<q", val)[0]
    if wire_type == 5 and isinstance(val, (bytes, bytearray)) and len(val) == 4:
        return struct.unpack("<i", val)[0]
    return None


def _decode_str(val: bytes) -> str:
    return bytes(val).decode("utf-8", errors="replace")


def _decode_ltpc(buf: bytes) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1:
            out["ltp"] = _as_double(val, wtype)
        elif fnum == 2:
            out["ltt"] = _as_int64(val, wtype)
        elif fnum == 3:
            out["ltq"] = _as_int64(val, wtype)
        elif fnum == 4:
            out["cp"] = _as_double(val, wtype)
    return out


def _decode_quote(buf: bytes) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1:
            out["bidQ"] = _as_int64(val, wtype)
        elif fnum == 2:
            out["bidP"] = _as_double(val, wtype)
        elif fnum == 3:
            out["askQ"] = _as_int64(val, wtype)
        elif fnum == 4:
            out["askP"] = _as_double(val, wtype)
    return out


def _decode_market_level(buf: bytes) -> Dict[str, Any]:
    quotes = []
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1 and wtype == 2:
            quotes.append(_decode_quote(val))
    return {"bidAskQuote": quotes}


def _decode_option_greeks(buf: bytes) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1:
            out["delta"] = _as_double(val, wtype)
        elif fnum == 2:
            out["theta"] = _as_double(val, wtype)
        elif fnum == 3:
            out["gamma"] = _as_double(val, wtype)
        elif fnum == 4:
            out["vega"] = _as_double(val, wtype)
        elif fnum == 5:
            out["rho"] = _as_double(val, wtype)
    return out


def _decode_ohlc(buf: bytes) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1 and wtype == 2:
            out["interval"] = _decode_str(val)
        elif fnum == 2:
            out["open"] = _as_double(val, wtype)
        elif fnum == 3:
            out["high"] = _as_double(val, wtype)
        elif fnum == 4:
            out["low"] = _as_double(val, wtype)
        elif fnum == 5:
            out["close"] = _as_double(val, wtype)
        elif fnum == 6:
            out["vol"] = _as_int64(val, wtype)
        elif fnum == 7:
            out["ts"] = _as_int64(val, wtype)
    return out


def _decode_market_ohlc(buf: bytes) -> Dict[str, Any]:
    entries = []
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1 and wtype == 2:
            entries.append(_decode_ohlc(val))
    return {"ohlc": entries}


def _decode_market_full_feed(buf: bytes) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1 and wtype == 2:
            out["ltpc"] = _decode_ltpc(val)
        elif fnum == 2 and wtype == 2:
            out["marketLevel"] = _decode_market_level(val)
        elif fnum == 3 and wtype == 2:
            out["optionGreeks"] = _decode_option_greeks(val)
        elif fnum == 4 and wtype == 2:
            out["marketOHLC"] = _decode_market_ohlc(val)
        elif fnum == 5:
            out["atp"] = _as_double(val, wtype)
        elif fnum == 6:
            out["vtt"] = _as_int64(val, wtype)
        elif fnum == 7:
            out["oi"] = _as_double(val, wtype)
        elif fnum == 8:
            out["iv"] = _as_double(val, wtype)
        elif fnum == 9:
            out["tbq"] = _as_double(val, wtype)
        elif fnum == 10:
            out["tsq"] = _as_double(val, wtype)
    return out


def _decode_index_full_feed(buf: bytes) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1 and wtype == 2:
            out["ltpc"] = _decode_ltpc(val)
        elif fnum == 2 and wtype == 2:
            out["marketOHLC"] = _decode_market_ohlc(val)
    return out


def _decode_full_feed(buf: bytes) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1 and wtype == 2:
            out["marketFF"] = _decode_market_full_feed(val)
        elif fnum == 2 and wtype == 2:
            out["indexFF"] = _decode_index_full_feed(val)
    return out


def _decode_first_level_with_greeks(buf: bytes) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1 and wtype == 2:
            out["ltpc"] = _decode_ltpc(val)
        elif fnum == 2 and wtype == 2:
            out["firstDepth"] = _decode_quote(val)
        elif fnum == 3 and wtype == 2:
            out["optionGreeks"] = _decode_option_greeks(val)
        elif fnum == 4:
            out["vtt"] = _as_int64(val, wtype)
        elif fnum == 5:
            out["oi"] = _as_double(val, wtype)
        elif fnum == 6:
            out["iv"] = _as_double(val, wtype)
    return out


def _decode_feed(buf: bytes) -> Dict[str, Any]:
    out = {}  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1 and wtype == 2:
            out["ltpc"] = _decode_ltpc(val)
        elif fnum == 2 and wtype == 2:
            out["fullFeed"] = _decode_full_feed(val)
        elif fnum == 3 and wtype == 2:
            out["firstLevelWithGreeks"] = _decode_first_level_with_greeks(val)
        elif fnum == 4:
            rm_val = _as_int64(val, wtype)
            out["requestMode"] = REQUEST_MODE_NAMES.get(rm_val, str(rm_val))
    return out


def _decode_map_entry_str_feed(buf: bytes) -> Tuple[str, Dict[str, Any]]:
    key = ""
    value = {}  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1 and wtype == 2:
            key = _decode_str(val)
        elif fnum == 2 and wtype == 2:
            value = _decode_feed(val)
    return key, value


def _decode_market_info(buf: bytes) -> Dict[str, Any]:
    # Official MarketDataFeedV3.proto enum MarketStatus numbering:
    # PRE_OPEN_START = 0, PRE_OPEN_END = 1, NORMAL_OPEN = 2,
    # NORMAL_CLOSE = 3, CLOSING_START = 4, CLOSING_END = 5.
    status_names = {
        0: "PRE_OPEN_START",
        1: "PRE_OPEN_END",
        2: "NORMAL_OPEN",
        3: "NORMAL_CLOSE",
        4: "CLOSING_START",
        5: "CLOSING_END",
    }
    seg_status = {}  # type: Dict[str, str]
    for fnum, wtype, val in _iter_fields(buf):
        if fnum == 1 and wtype == 2:
            k = ""
            v = ""
            for mf, mw, mval in _iter_fields(val):
                if mf == 1 and mw == 2:
                    k = _decode_str(mval)
                elif mf == 2:
                    if mw == 0:
                        v = status_names.get(int(mval), str(mval))
                    elif mw == 2:
                        v = _decode_str(mval)
            if k:
                seg_status[k] = v
    return {"segmentStatus": seg_status}


def decode_protobuf_feed_response(raw_bytes: bytes) -> Dict[str, Any]:
    """Decode a binary Upstox MarketDataFeedV3.proto FeedResponse packet into a dict."""
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise ValueError("Expected bytes for Protobuf FeedResponse")
    out = {
        "type": "live_feed",
        "feeds": {},
    }  # type: Dict[str, Any]
    for fnum, wtype, val in _iter_fields(bytes(raw_bytes)):
        if fnum == 1:
            t_code = _as_int64(val, wtype)
            out["type"] = FEED_TYPE_NAMES.get(t_code, str(t_code))
        elif fnum == 2 and wtype == 2:
            k, feed_val = _decode_map_entry_str_feed(val)
            if k:
                out["feeds"][k] = feed_val
        elif fnum == 3:
            out["currentTs"] = str(_as_int64(val, wtype))
        elif fnum == 4 and wtype == 2:
            out["marketInfo"] = _decode_market_info(val)
    return out


def normalize_feed_message(raw_message: Any) -> Dict[str, Any]:
    """Normalize an incoming WebSocket message (Protobuf bytes, JSON text, or dict)."""
    if isinstance(raw_message, dict):
        decoded = dict(raw_message)
    elif isinstance(raw_message, (bytes, bytearray)):
        raw_b = bytes(raw_message)
        stripped = raw_b.lstrip()
        if stripped.startswith(b"{"):
            decoded = json.loads(raw_b.decode("utf-8"))
        else:
            decoded = decode_protobuf_feed_response(raw_b)
    elif isinstance(raw_message, str):
        decoded = json.loads(raw_message)
    else:
        raise ValueError("Unsupported WebSocket frame payload type: %s" % type(raw_message).__name__)

    feeds_out = {}  # type: Dict[str, Dict[str, Any]]
    raw_feeds = decoded.get("feeds") or {}
    for ikey, entry in raw_feeds.items():
        if not isinstance(entry, dict):
            continue
        flat = {"instrument_key": ikey}  # type: Dict[str, Any]
        ltpc = entry.get("ltpc")
        full = entry.get("fullFeed")
        flwg = entry.get("firstLevelWithGreeks")
        if not ltpc and isinstance(full, dict) and full:
            mff = full.get("marketFF") or {}
            iff = full.get("indexFF") or {}
            ltpc = mff.get("ltpc") or iff.get("ltpc")
            if mff:
                if mff.get("atp") is not None:
                    flat["atp"] = mff["atp"]
                if mff.get("vtt") is not None:
                    flat["volume"] = int(mff["vtt"])
                if mff.get("oi") is not None:
                    flat["oi"] = float(mff["oi"])
                if mff.get("iv") is not None:
                    flat["iv"] = float(mff["iv"])
                if mff.get("tbq") is not None:
                    flat["total_buy_quantity"] = float(mff["tbq"])
                if mff.get("tsq") is not None:
                    flat["total_sell_quantity"] = float(mff["tsq"])
                if mff.get("optionGreeks"):
                    flat["greeks"] = mff["optionGreeks"]
                if mff.get("marketLevel"):
                    quotes = (mff["marketLevel"] or {}).get("bidAskQuote") or []
                    flat["depth"] = quotes
                    if quotes:
                        flat["bid"] = quotes[0].get("bidP")
                        flat["ask"] = quotes[0].get("askP")
                if mff.get("marketOHLC"):
                    flat["ohlc"] = (mff["marketOHLC"] or {}).get("ohlc") or []
            elif iff and iff.get("marketOHLC"):
                flat["ohlc"] = (iff["marketOHLC"] or {}).get("ohlc") or []
        elif not ltpc and isinstance(flwg, dict) and flwg:
            ltpc = flwg.get("ltpc")
            if flwg.get("firstDepth"):
                flat["depth"] = [flwg["firstDepth"]]
                flat["bid"] = flwg["firstDepth"].get("bidP")
                flat["ask"] = flwg["firstDepth"].get("askP")
            if flwg.get("optionGreeks"):
                flat["greeks"] = flwg["optionGreeks"]
            if flwg.get("vtt") is not None:
                flat["volume"] = int(flwg["vtt"])
            if flwg.get("oi") is not None:
                flat["oi"] = float(flwg["oi"])
            if flwg.get("iv") is not None:
                flat["iv"] = float(flwg["iv"])

        if isinstance(ltpc, dict):
            if ltpc.get("ltp") is not None:
                flat["last_price"] = float(ltpc["ltp"])
            if ltpc.get("cp") is not None:
                flat["prev_close"] = float(ltpc["cp"])
            if ltpc.get("ltq") is not None:
                flat["ltq"] = int(ltpc["ltq"])
            if ltpc.get("ltt") is not None:
                flat["ltt"] = int(ltpc["ltt"])
        feeds_out[ikey] = flat

    return {
        "type": decoded.get("type", "live_feed"),
        "currentTs": decoded.get("currentTs"),
        "feeds": feeds_out,
        "marketInfo": decoded.get("marketInfo"),
        "raw": decoded,
    }


# ---------------------------------------------------------------- RFC 6455 Stdlib WebSocket


class StdlibWebSocketClient:
    """Minimal RFC 6455 TLS WebSocket client using only Python stdlib socket+ssl."""

    def __init__(self, url: str, timeout: float = 15.0, headers: Optional[Dict[str, str]] = None):
        self.url = url
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._sock = None  # type: Optional[Any]
        self._buf = bytearray()
        self.closed = True

    def connect(self, max_redirects: int = 5) -> "StdlibWebSocketClient":
        current_url = self.url
        for hop in range(max_redirects + 1):
            parsed = urlparse(current_url)
            scheme = (parsed.scheme or "wss").lower()
            if scheme == "https":
                scheme = "wss"
            elif scheme == "http":
                scheme = "ws"
            if scheme not in ("wss", "ws"):
                raise UpstoxStreamError("Unsupported WebSocket scheme: %s" % scheme)
            host = parsed.hostname
            if not host:
                raise UpstoxStreamError("WebSocket URL has no hostname")
            port = parsed.port or (443 if scheme == "wss" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path = "%s?%s" % (path, parsed.query)

            self._buf.clear()
            raw_sock = socket.create_connection((host, port), timeout=self.timeout)
            if scheme == "wss":
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(raw_sock, server_hostname=host)
            else:
                sock = raw_sock
            sock.settimeout(self.timeout)
            self._sock = sock

            ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_hdr = host if port in (80, 443) else "%s:%d" % (host, port)
            req_headers = {
                "Host": host_hdr,
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Key": ws_key,
                "Sec-WebSocket-Version": "13",
                "User-Agent": "day-trader-skill/1.0",
            }
            orig_host = urlparse(self.url).hostname
            if hop == 0 or (orig_host and host == orig_host):
                for k, v in self.headers.items():
                    req_headers[k] = v
            lines = ["GET %s HTTP/1.1" % path]
            for k, v in req_headers.items():
                lines.append("%s: %s" % (k, v))
            lines.append("")
            lines.append("")
            sock.sendall("\r\n".join(lines).encode("ascii"))

            resp_bytes = self._read_until(b"\r\n\r\n")
            resp_text = resp_bytes.decode("iso-8859-1", errors="replace")
            resp_lines = resp_text.split("\r\n")
            status_line = resp_lines[0] if resp_lines else ""
            status_parts = status_line.split(" ", 2)
            try:
                status_code = int(status_parts[1]) if len(status_parts) >= 2 else 0
            except ValueError:
                status_code = 0

            hdrs = {}
            for line in resp_lines[1:]:
                if ":" in line:
                    hk, hv = line.split(":", 1)
                    hdrs[hk.strip().lower()] = hv.strip()

            if status_code in (301, 302, 303, 307, 308):
                self._close_raw_socket()
                location = hdrs.get("location")
                if not location:
                    raise UpstoxStreamError(
                        "WebSocket handshake redirect (%d) missing Location header" % status_code
                    )
                current_url = urljoin(current_url, location)
                if hop >= max_redirects:
                    raise UpstoxStreamError(
                        "WebSocket handshake exceeded max redirects (%d)" % max_redirects
                    )
                continue

            if status_code in (401, 403):
                self.close()
                raise UpstoxStreamAuthError(
                    "WebSocket handshake unauthorized (%d): %s"
                    % (status_code, redact_secret(status_line))
                )
            if status_code != 101:
                self.close()
                raise UpstoxStreamError(
                    "WebSocket handshake rejected: %s" % redact_secret(status_line)
                )

            expected_accept = base64.b64encode(
                hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            if hdrs.get("sec-websocket-accept") != expected_accept:
                self.close()
                raise UpstoxStreamError("Invalid Sec-WebSocket-Accept response from server")

            self.url = current_url
            self.closed = False
            return self
        raise UpstoxStreamError("WebSocket handshake redirect loop aborted")

    def _close_raw_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        self.closed = True

    def _read_until(self, marker: bytes) -> bytes:
        while True:
            idx = self._buf.find(marker)
            if idx != -1:
                end = idx + len(marker)
                out = bytes(self._buf[:end])
                del self._buf[:end]
                return out
            chunk = self._sock.recv(4096)
            if not chunk:
                raise UpstoxStreamError("Connection closed during WebSocket handshake")
            self._buf.extend(chunk)

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise UpstoxStreamError("WebSocket connection closed unexpectedly")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed or self._sock is None:
            raise UpstoxStreamError("WebSocket is not connected")
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def send(self, data: Any) -> None:
        if isinstance(data, (bytes, bytearray)):
            self._send_frame(0x02, bytes(data))
        else:
            self._send_frame(0x01, str(data).encode("utf-8"))

    def recv(self, timeout: Optional[float] = None) -> Optional[Any]:
        if self.closed or self._sock is None:
            raise UpstoxStreamError("WebSocket is not connected")
        prev_timeout = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            chunks = bytearray()
            msg_opcode = None
            while True:
                b0, b1 = self._read_exact(2)
                fin = bool(b0 & 0x80)
                opcode = b0 & 0x0F
                masked = bool(b1 & 0x80)
                length = b1 & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._read_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._read_exact(8))[0]
                mask_key = self._read_exact(4) if masked else None
                payload = self._read_exact(length)
                if mask_key:
                    payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

                if opcode == 0x08:  # Close frame
                    self.close()
                    return None
                if opcode == 0x09:  # Ping -> auto Pong
                    self._send_frame(0x0A, payload)
                    continue
                if opcode == 0x0A:  # Pong
                    continue

                if msg_opcode is None and opcode != 0x00:
                    msg_opcode = opcode
                chunks.extend(payload)
                if fin:
                    if msg_opcode == 0x01:
                        return bytes(chunks).decode("utf-8", errors="replace")
                    return bytes(chunks)
        except socket.timeout:
            return None
        except (ConnectionError, EOFError, OSError):
            self._close_raw_socket()
            return None
        except UpstoxStreamError as e:
            if "closed unexpectedly" in str(e).lower():
                self._close_raw_socket()
                return None
            raise
        finally:
            if self._sock is not None:
                try:
                    self._sock.settimeout(prev_timeout)
                except OSError:
                    pass

    def close(self) -> None:
        if self._sock is not None:
            if not self.closed:
                try:
                    self._send_frame(0x08, b"")
                except Exception:
                    pass
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        self.closed = True


# ---------------------------------------------------------------- WebSocket Streamer Lifecycle


class UpstoxStreamQuota:
    """Shared user-level Upstox V3 WebSocket connection and mode-limit coordinator."""

    def __init__(
        self,
        account_tier: str = DEFAULT_ACCOUNT_TIER,
        max_connections: Optional[int] = None,
    ):
        self.account_tier = normalize_account_tier(account_tier)
        self.tier_spec = get_tier_limits(self.account_tier)
        self.max_connections = (
            int(max_connections)
            if max_connections is not None
            else int(self.tier_spec["max_connections"])
        )
        self._lock = threading.RLock()
        self._streamers = []  # type: List["UpstoxMarketDataStreamer"]

    def register(self, streamer: "UpstoxMarketDataStreamer") -> None:
        with self._lock:
            if streamer not in self._streamers:
                self._streamers.append(streamer)

    def connected_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._streamers if getattr(s, "_connection_counted", False))

    def validate_connect(
        self,
        streamer: "UpstoxMarketDataStreamer",
        candidate_by_mode: Optional[Dict[str, set]] = None,
    ) -> None:
        with self._lock:
            self.register(streamer)
            active_conns = self.connected_count()
            already_counted = getattr(streamer, "_connection_counted", False)
            if not already_counted and active_conns >= self.max_connections:
                raise UpstoxStreamLimitError(
                    "Upstox %s tier concurrent WebSocket limit exceeded (max %d connections)"
                    % (self.account_tier, self.max_connections)
                )
            self.validate_subscriptions(
                streamer,
                candidate_by_mode if candidate_by_mode is not None else streamer._subscriptions,
                include_unconnected_target=True,
            )

    def validate_subscriptions(
        self,
        target_streamer: "UpstoxMarketDataStreamer",
        candidate_by_mode: Dict[str, set],
        include_unconnected_target: bool = False,
    ) -> None:
        with self._lock:
            self.register(target_streamer)
            totals_by_mode = {m: 0 for m in VALID_MODES}
            mode_limits_override = getattr(target_streamer, "_custom_mode_limits", None) or {}
            per_conn_overrides = target_streamer.mode_limits
            for s in self._streamers:
                if s is target_streamer:
                    sub_map = candidate_by_mode
                    if not (include_unconnected_target or s.is_connected or any(sub_map.values())):
                        continue
                else:
                    if not s.is_connected:
                        continue
                    sub_map = s._subscriptions
                for m, keys in sub_map.items():
                    if keys:
                        spec = self.tier_spec["modes"].get(m)
                        if spec is None:
                            raise UpstoxStreamLimitError(
                                "Upstox V3 mode '%s' is only supported on Upstox Plus (account_tier='plus'); "
                                "current account_tier='%s'" % (m, self.account_tier)
                            )
                        per_conn_cap = spec["per_connection"]
                        if m in mode_limits_override:
                            per_conn_cap = min(per_conn_cap, int(mode_limits_override[m]))
                        elif m in per_conn_overrides and per_conn_overrides[m] is not None:
                            per_conn_cap = min(per_conn_cap, int(per_conn_overrides[m]))
                        if len(keys) > per_conn_cap:
                            raise UpstoxStreamLimitError(
                                "Upstox V3 mode '%s' per-connection limit exceeded on '%s' tier: "
                                "%d requested (max %d)"
                                % (m, self.account_tier, len(keys), per_conn_cap)
                            )
                        totals_by_mode[m] += len(keys)

            active_modes = [m for m in VALID_MODES if totals_by_mode[m] > 0]
            if not active_modes:
                return

            if len(active_modes) == 1:
                only_mode = active_modes[0]
                spec = self.tier_spec["modes"][only_mode]
                cap = spec["individual"]
                if only_mode in mode_limits_override:
                    cap = min(cap, int(mode_limits_override[only_mode]))
                if totals_by_mode[only_mode] > cap:
                    raise UpstoxStreamLimitError(
                        "Upstox V3 '%s' tier individual limit exceeded for mode '%s': %d requested (max %d)"
                        % (self.account_tier, only_mode, totals_by_mode[only_mode], cap)
                    )
                return

            # Combined limit across >= 2 concurrently subscribed modes.
            combined_caps = []
            for m in active_modes:
                spec = self.tier_spec["modes"][m]
                cap = spec["combined"]
                if m in mode_limits_override:
                    cap = min(cap, int(mode_limits_override[m]))
                if totals_by_mode[m] > cap:
                    raise UpstoxStreamLimitError(
                        "Upstox V3 '%s' tier combined limit exceeded for mode '%s': %d requested (max %d)"
                        % (self.account_tier, m, totals_by_mode[m], cap)
                    )
                combined_caps.append(cap)

            total_across_modes = sum(totals_by_mode[m] for m in active_modes)
            effective_combined_cap = min(combined_caps)
            if total_across_modes > effective_combined_cap:
                raise UpstoxStreamLimitError(
                    "Upstox V3 '%s' tier combined subscription limit exceeded across modes (%s): "
                    "%d requested (max %d)"
                    % (
                        self.account_tier,
                        ", ".join(active_modes),
                        total_across_modes,
                        effective_combined_cap,
                    )
                )


class UpstoxMarketDataStreamer:
    """Lifecycle-managed Upstox V3 WebSocket streaming client.

    Handles:
    - Single-use /v3/feed/market-data-feed/authorize URI acquisition
    - Tier-aware subscription & concurrent connection tracking (Standard default, Plus opt-in)
    - Pure-stdlib Protobuf FeedResponse decoding into normalized tick events
    - Reconnection with exponential backoff & fresh WSS re-authorization
    - Stale-feed / missing-tick watchdog detection (distinguishing heartbeats vs ticks)
    """

    def __init__(
        self,
        authorize_fn: Callable[[], str],
        ws_connect: Optional[Callable[[str], Any]] = None,
        on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_status: Optional[Callable[[Dict[str, Any]], None]] = None,
        stale_after_seconds: float = 30.0,
        max_total_keys: int = MAX_TOTAL_SUBSCRIPTIONS,
        mode_limits: Optional[Dict[str, int]] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        active_connections_counter: Optional[List[int]] = None,
        account_tier: str = DEFAULT_ACCOUNT_TIER,
        quota: Optional[UpstoxStreamQuota] = None,
        max_connections: Optional[int] = None,
    ):
        self._authorize_fn = authorize_fn
        self._ws_connect = ws_connect or (lambda url: StdlibWebSocketClient(url).connect())
        self.on_message = on_message
        self.on_status = on_status
        self.stale_after_seconds = float(stale_after_seconds)
        self.max_total_keys = int(max_total_keys)

        resolved_tier = quota.account_tier if quota is not None else normalize_account_tier(account_tier)
        self.account_tier = resolved_tier
        self.tier_spec = get_tier_limits(resolved_tier)
        self.max_connections = (
            int(max_connections)
            if max_connections is not None
            else (quota.max_connections if quota is not None else int(self.tier_spec["max_connections"]))
        )
        self._custom_mode_limits = {
            k: int(v) for k, v in (mode_limits or {}).items()
        }
        self.mode_limits = {}  # type: Dict[str, Optional[int]]
        for m in VALID_MODES:
            spec = self.tier_spec["modes"].get(m)
            if spec is None:
                self.mode_limits[m] = None
            else:
                cap = int(spec["individual"])
                if m in self._custom_mode_limits:
                    cap = min(cap, self._custom_mode_limits[m])
                self.mode_limits[m] = cap

        self._clock = clock or time.monotonic
        self._sleep = sleep_fn or time.sleep
        self._active_connections_counter = active_connections_counter
        self._quota = quota if quota is not None else UpstoxStreamQuota(
            account_tier=self.account_tier,
            max_connections=self.max_connections,
        )

        self._ws = None  # type: Optional[Any]
        self._connected = False
        self._connection_counted = False
        self._subscriptions = {m: set() for m in VALID_MODES}  # type: Dict[str, set]
        self.last_connected_at = None  # type: Optional[float]
        self.last_message_at = None  # type: Optional[float]
        self.last_tick_at = None  # type: Optional[float]
        self.last_feed_ts = None  # type: Optional[str]
        self.last_ticks = {}  # type: Dict[str, Dict[str, Any]]
        self.reconnect_count = 0
        self.stale_detected = False
        self._quota.register(self)

    def __repr__(self) -> str:
        total = len(self.active_keys())
        return "<UpstoxMarketDataStreamer tier=%s connected=%s keys=%d reconnects=%d>" % (
            self.account_tier, self._connected, total, self.reconnect_count
        )

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    def active_keys(self, mode: Optional[str] = None) -> List[str]:
        if mode is not None:
            self._validate_mode(mode)
            return sorted(self._subscriptions[mode])
        out = set()
        for keys in self._subscriptions.values():
            out.update(keys)
        return sorted(out)

    def _validate_mode(self, mode: str) -> str:
        m = str(mode or "").strip()
        if m not in VALID_MODES:
            raise ValueError("Invalid Upstox V3 stream mode '%s'. Valid: %s" % (mode, ", ".join(VALID_MODES)))
        if self.tier_spec["modes"].get(m) is None:
            raise UpstoxStreamLimitError(
                "Upstox V3 mode '%s' is only supported on Upstox Plus (account_tier='plus'); "
                "current account_tier='%s'" % (m, self.account_tier)
            )
        return m

    def _check_subscription_limits(self, candidate_by_mode: Dict[str, set]) -> None:
        all_keys = set()
        for m, keys in candidate_by_mode.items():
            if keys and self.tier_spec["modes"].get(m) is None:
                raise UpstoxStreamLimitError(
                    "Upstox V3 mode '%s' is only supported on Upstox Plus (account_tier='plus'); "
                    "current account_tier='%s'" % (m, self.account_tier)
                )
            all_keys.update(keys)
        if len(all_keys) > self.max_total_keys:
            raise UpstoxStreamLimitError(
                "Upstox V3 connection subscription limit exceeded: %d unique keys (max %d)"
                % (len(all_keys), self.max_total_keys)
            )
        self._quota.validate_subscriptions(self, candidate_by_mode)

    def authorize(self) -> str:
        """Obtain a fresh single-use authorized WSS URI from Upstox V3 authorize endpoint."""
        url = self._authorize_fn()
        if not url or not str(url).strip().startswith(("wss://", "ws://")):
            raise UpstoxStreamAuthError("Invalid authorized WebSocket redirect URI received")
        return str(url).strip()

    def connect(self) -> "UpstoxMarketDataStreamer":
        if self._connected:
            return self
        if self._active_connections_counter is not None:
            if self._active_connections_counter[0] >= self.max_connections:
                raise UpstoxStreamLimitError(
                    "Upstox %s tier concurrent WebSocket limit exceeded (max %d connections)"
                    % (self.account_tier, self.max_connections)
                )
        self._quota.validate_connect(self, self._subscriptions)
        wss_url = self.authorize()
        try:
            self._ws = self._ws_connect(wss_url)
        except Exception as e:
            raise UpstoxStreamError("WebSocket connect failed: %s" % redact_secret(e))
        self._connected = True
        self._connection_counted = True
        if self._active_connections_counter is not None:
            self._active_connections_counter[0] += 1
        now_t = self._clock()
        self.last_connected_at = now_t
        if self.last_message_at is None:
            self.last_message_at = now_t
        self.stale_detected = False
        self._emit_status("connected")
        return self

    def _send_control(self, method: str, data: Dict[str, Any]) -> None:
        if not self.is_connected:
            raise UpstoxStreamError("Cannot send '%s': WebSocket is not connected" % method)
        frame = {
            "guid": uuid.uuid4().hex,
            "method": method,
            "data": data,
        }
        payload = json.dumps(frame).encode("utf-8")
        self._ws.send(payload)

    def subscribe(self, instrument_keys: List[str], mode: str = "full") -> List[str]:
        m = self._validate_mode(mode)
        clean_keys = [str(k).strip() for k in instrument_keys if str(k).strip()]
        if not clean_keys:
            return []
        candidate = {k: set(v) for k, v in self._subscriptions.items()}
        for k_set_mode, k_set in candidate.items():
            if k_set_mode != m:
                k_set.difference_update(clean_keys)
        candidate[m].update(clean_keys)
        self._check_subscription_limits(candidate)

        if self.is_connected:
            self._send_control("sub", {"mode": m, "instrumentKeys": clean_keys})
        self._subscriptions = candidate
        return clean_keys

    def unsubscribe(self, instrument_keys: List[str]) -> List[str]:
        clean_keys = [str(k).strip() for k in instrument_keys if str(k).strip()]
        if not clean_keys:
            return []
        if self.is_connected:
            self._send_control("unsub", {"instrumentKeys": clean_keys})
        for k_set in self._subscriptions.values():
            k_set.difference_update(clean_keys)
        return clean_keys

    def change_mode(self, instrument_keys: List[str], mode: str) -> List[str]:
        m = self._validate_mode(mode)
        clean_keys = [str(k).strip() for k in instrument_keys if str(k).strip()]
        if not clean_keys:
            return []
        candidate = {k: set(v) for k, v in self._subscriptions.items()}
        for k_set_mode, k_set in candidate.items():
            if k_set_mode != m:
                k_set.difference_update(clean_keys)
        candidate[m].update(clean_keys)
        self._check_subscription_limits(candidate)

        if self.is_connected:
            self._send_control("change_mode", {"mode": m, "instrumentKeys": clean_keys})
        self._subscriptions = candidate
        return clean_keys

    def _emit_status(self, state: str, detail: Optional[str] = None) -> None:
        if self.on_status is not None:
            self.on_status({
                "state": state,
                "detail": redact_secret(detail) if detail else None,
                "timestamp": self._clock(),
                "reconnect_count": self.reconnect_count,
            })

    def handle_raw_message(self, raw_message: Any) -> Dict[str, Any]:
        event = normalize_feed_message(raw_message)
        now_t = self._clock()
        self.last_message_at = now_t
        if event.get("currentTs") is not None:
            self.last_feed_ts = str(event["currentTs"])
        feeds = event.get("feeds") or {}
        if feeds:
            self.last_tick_at = now_t
            self.stale_detected = False
        elif not self.active_keys():
            self.stale_detected = False
        for ikey, flat in feeds.items():
            merged = dict(self.last_ticks.get(ikey) or {})
            merged.update(flat)
            merged["received_at"] = now_t
            self.last_ticks[ikey] = merged
        if self.on_message is not None:
            self.on_message(event)
        return event

    def poll_once(self, timeout: Optional[float] = 1.0) -> Optional[Dict[str, Any]]:
        """Read and decode a single frame if connected; detects close, EOF, and stale feeds."""
        if not self.is_connected:
            raise UpstoxStreamError("Stream is not connected")
        try:
            raw = self._ws.recv(timeout=timeout)
        except (ConnectionError, EOFError, OSError) as e:
            self._close_socket_only()
            self._emit_status("disconnected", "Remote WebSocket closed: %s" % redact_secret(e))
            return None
        except UpstoxStreamError:
            self._close_socket_only()
            self._emit_status("disconnected", "WebSocket stream error")
            raise
        if raw is None:
            if getattr(self._ws, "closed", False):
                self._close_socket_only()
                self._emit_status("disconnected", "Remote WebSocket closed")
                return None
            self.check_stale()
            return None
        return self.handle_raw_message(raw)

    def consume(self, max_messages: int = 10, timeout: Optional[float] = 0.5) -> List[Dict[str, Any]]:
        events = []
        for _ in range(max_messages):
            ev = self.poll_once(timeout=timeout)
            if ev is None:
                break
            events.append(ev)
        return events

    def reconnect(
        self,
        max_attempts: int = 3,
        initial_backoff: float = 1.0,
        backoff_factor: float = 2.0,
        max_backoff: float = 30.0,
    ) -> "UpstoxMarketDataStreamer":
        """Reconnect with exponential backoff, fresh authorize() URI, and resubscribe."""
        self._close_socket_only()
        last_err = None  # type: Optional[Exception]
        for attempt in range(max_attempts):
            try:
                self.reconnect_count += 1
                self.connect()
                for m in VALID_MODES:
                    keys = sorted(self._subscriptions[m])
                    if keys:
                        self._send_control("sub", {"mode": m, "instrumentKeys": keys})
                self._emit_status("reconnected")
                return self
            except (UpstoxStreamAuthError, UpstoxStreamLimitError):
                self._close_socket_only()
                raise
            except Exception as e:
                last_err = e
                self._close_socket_only()
                if attempt + 1 < max_attempts:
                    delay = min(max_backoff, initial_backoff * (backoff_factor ** attempt))
                    self._sleep(delay)
        raise UpstoxStreamError(
            "Upstox WebSocket reconnect failed after %d attempts: %s"
            % (max_attempts, redact_secret(last_err))
        )

    def is_stale(self, now_ts: Optional[float] = None) -> bool:
        if not self._connected:
            return False
        ref = now_ts if now_ts is not None else self._clock()
        if self.active_keys():
            base = self.last_tick_at if self.last_tick_at is not None else self.last_connected_at
        else:
            base = self.last_tick_at or self.last_message_at or self.last_connected_at
        if base is None:
            return False
        return (ref - base) > self.stale_after_seconds

    def check_stale(self, now_ts: Optional[float] = None) -> bool:
        stale = self.is_stale(now_ts=now_ts)
        if stale and not self.stale_detected:
            self.stale_detected = True
            self._emit_status("stale", "No WebSocket tick within %.1fs" % self.stale_after_seconds)
        return stale

    def get_health(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        ref = now_ts if now_ts is not None else self._clock()
        msg_age = (ref - self.last_message_at) if self.last_message_at is not None else None
        tick_age = (ref - self.last_tick_at) if self.last_tick_at is not None else None
        return {
            "account_tier": self.account_tier,
            "max_connections": self.max_connections,
            "connected": self.is_connected,
            "stale": self.is_stale(now_ts=ref),
            "seconds_since_last_message": round(msg_age, 3) if msg_age is not None else None,
            "seconds_since_last_tick": round(tick_age, 3) if tick_age is not None else None,
            "stale_after_seconds": self.stale_after_seconds,
            "last_feed_ts": self.last_feed_ts,
            "reconnect_count": self.reconnect_count,
            "subscribed_count": len(self.active_keys()),
            "subscriptions_by_mode": {m: sorted(keys) for m, keys in self._subscriptions.items() if keys},
        }

    def _close_socket_only(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
        if self._connection_counted:
            if self._active_connections_counter is not None:
                self._active_connections_counter[0] = max(0, self._active_connections_counter[0] - 1)
            self._connection_counted = False
        self._connected = False

    def close(self) -> None:
        self._close_socket_only()
        self._emit_status("closed")
