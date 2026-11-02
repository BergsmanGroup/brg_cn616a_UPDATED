"""Shared Pacific-time conversion and display helpers for run scheduling."""

from __future__ import annotations

import logging
from datetime import datetime, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger("cn616a.gui")
DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def pacific_timezone() -> tzinfo | None:
    """Return Pacific Time, falling back to the local timezone if tzdata is unavailable."""
    try:
        return ZoneInfo("America/Los_Angeles")
    except Exception:
        LOGGER.warning(
            "America/Los_Angeles tzdata unavailable (install the 'tzdata' package); "
            "falling back to the local machine timezone for run scheduling"
        )
        return datetime.now().astimezone().tzinfo


def parse_pacific_schedule(text: str) -> Optional[datetime]:
    for date_format in DATETIME_FORMATS:
        try:
            wall_time = datetime.strptime(text.strip(), date_format)
        except ValueError:
            continue
        return wall_time.replace(tzinfo=pacific_timezone())
    return None


def format_elapsed(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"


def format_schedule_display(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(pacific_timezone())
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError):
        return iso_str


def format_last_data_display(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(pacific_timezone())
        return dt.strftime("%Y-%m-%d %I:%M:%S %p %Z")
    except (TypeError, ValueError):
        return iso_str
