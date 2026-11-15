"""
Hermetic test configuration and fixtures.
Ensures tests never depend on developer local files in the git-ignored trader/ directory.
"""
import json
import os
import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def ensure_hermetic_trader_state():
    """
    Ensure hermetic fallback portfolio and broker state exist if the git-ignored
    trader/ directory files are not present (e.g. in CI or a fresh clone).
    """
    trader_dir = os.path.join(BASE_DIR, "trader")
    state_dir = os.path.join(trader_dir, "state")
    port_file = os.path.join(trader_dir, "portfolio.json")
    broker_file = os.path.join(state_dir, "broker.json")

    created_port = False
    created_broker = False

    if not os.path.exists(port_file):
        os.makedirs(trader_dir, exist_ok=True)
        with open(port_file, "w", encoding="utf-8") as f:
            json.dump({"cash": 500000.0, "positions": []}, f)
        created_port = True

    if not os.path.exists(broker_file):
        os.makedirs(state_dir, exist_ok=True)
        with open(broker_file, "w", encoding="utf-8") as f:
            json.dump({"orders": [], "positions": [], "placed_today": []}, f)
        created_broker = True

    yield

    if created_port and os.path.exists(port_file):
        try:
            os.remove(port_file)
        except Exception:
            pass

    if created_broker and os.path.exists(broker_file):
        try:
            os.remove(broker_file)
        except Exception:
            pass

