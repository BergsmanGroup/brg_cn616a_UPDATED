"""Toolkit-independent configuration value parsing shared by Tk and Qt."""

from __future__ import annotations

import math
import re
from typing import Any


def parse_retention_seconds(value: Any) -> float | None:
    """Parse a duration such as '1 week', '7d', or '168h'."""
    text = str(value).strip().lower()
    if not text:
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([a-z]*)", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2) or "h"
    multipliers = {
        "s": 1.0,
        "sec": 1.0,
        "secs": 1.0,
        "second": 1.0,
        "seconds": 1.0,
        "m": 60.0,
        "min": 60.0,
        "mins": 60.0,
        "minute": 60.0,
        "minutes": 60.0,
        "h": 3600.0,
        "hr": 3600.0,
        "hrs": 3600.0,
        "hour": 3600.0,
        "hours": 3600.0,
        "d": 86_400.0,
        "day": 86_400.0,
        "days": 86_400.0,
        "w": 604_800.0,
        "wk": 604_800.0,
        "week": 604_800.0,
        "weeks": 604_800.0,
    }
    multiplier = multipliers.get(unit)
    seconds = amount * multiplier if multiplier is not None else None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds > 0 else None
