"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os

# Isolate the unit-test suite from developer-local .env provider/token overrides
# so default-Yahoo and unconfigured-auth tests are deterministic. Env-loading
# behavior itself is tested explicitly in tests/test_env_and_clock.py.
for _k in (
    "INDIA_MARKET_DATA_PROVIDER",
    "BHARAT_DATA_PROVIDER",
    "UPSTOX_ANALYTICS_TOKEN",
    "INDIA_MARKET_DATA_ALLOW_YAHOO_FALLBACK",
    "INDIA_MARKET_DATA_FALLBACK_PROVIDER",
):
    os.environ[_k] = ""

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")


def load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
