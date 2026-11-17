"""Hermetic market data for the trader-options test files.

Without this, any test that calls ``analyze_symbol_for_options("NIFTY")`` with no chart
silently pulls live Yahoo NIFTY bars, a live spot quote (which prices the simulated option
chain) and the live 5-index regime -- so results drift with the real market and the tests
flip pass/fail from day to day. This pins all three to the frozen 2026-11-13 NIFTY session
and blocks sockets so any new live call fails loudly instead of passing by luck. It also pins
the engine's wall clock to Sat 2026-11-14 10:30 IST (the weekend after the frozen session):
the fixture chain expires on a Tuesday, so a real clock made the tests fail every evening
after an expiry-day close.

Usage (in a test module, after the sys.path setup):
    from _options_offline import offline_options_market  # noqa: F401  (autouse fixture)
"""

import copy
import datetime
import json
import os
import socket

import pytest

import _load
import data_in
import entry
import options
import scan

FROZEN_NIFTY_CHART = os.path.join(_load.FIX, "trader-options", "nifty_5m_20261113.json")
FROZEN_NIFTY_SPOT = 25144.30  # 2026-11-13 close, last bar of the frozen chart
FROZEN_NOW_UTC = datetime.datetime(2026, 11, 14, 5, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def offline_options_market(monkeypatch):
    with open(FROZEN_NIFTY_CHART, encoding="utf-8") as f:
        chart = json.load(f)

    def _blocked(*args, **kwargs):
        raise RuntimeError("Network access is blocked in trader-options tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(options, "_utc_now", lambda: FROZEN_NOW_UTC)
    monkeypatch.setattr(entry, "get_chart", lambda *a, **k: copy.deepcopy(chart))
    monkeypatch.setattr(
        data_in.YahooMarketDataProvider,
        "get_quote",
        lambda self, sym, **k: {
            "price": FROZEN_NIFTY_SPOT,
            "data_status": "HISTORICAL",
            "tier": "HISTORICAL",
            "provider": "fixture",
        },
    )
    monkeypatch.setattr(scan, "get_broad_market_regime", lambda *a, **k: {"canonical_regime": "UNCERTAIN"})
    yield
