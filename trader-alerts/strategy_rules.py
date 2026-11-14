"""Shared deterministic rules for historical daily strategies."""

import math


def prior_20d_close_high(completed_prior_closes):
    """Highest close among the 20 completed sessions immediately before a candidate.

    The input contains reference sessions only; the candidate/current session
    must be excluded by the caller. Return None when history is insufficient
    or contains an invalid price.
    """
    if not isinstance(completed_prior_closes, (list, tuple)) or len(completed_prior_closes) < 20:
        return None
    values = completed_prior_closes[-20:]
    try:
        closes = [float(value) for value in values]
    except (TypeError, ValueError, OverflowError):
        return None
    if any(not math.isfinite(value) or value <= 0 for value in closes):
        return None
    return max(closes)
