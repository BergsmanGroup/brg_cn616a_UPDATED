"""Alias-aware accessors for the service's supported snapshot and driver shapes."""

import math
from typing import Any, Dict, Optional


_ZONE_KEYS = {
    key
    for zone_id in range(1, 7)
    for key in (str(zone_id), zone_id, f"z{zone_id}", f"Z{zone_id}", f"zone{zone_id}", f"ZONE{zone_id}")
}


def get_zones(payload: Any, section: Optional[str] = None) -> Dict[Any, Any]:
    container = payload.get(section, payload) if section and isinstance(payload, dict) else payload
    if not isinstance(container, dict):
        return {}
    nested = container.get("zones")
    if isinstance(nested, dict):
        return nested
    return container if any(key in _ZONE_KEYS for key in container) else {}


def get_zone(zones: Any, zone_id: Any) -> Dict[str, Any]:
    if not isinstance(zones, dict):
        return {}
    text = str(zone_id)
    for key in (text, int(text) if text.isdigit() else text, f"z{text}", f"Z{text}", f"zone{text}", f"ZONE{text}"):
        value = zones.get(key)
        if isinstance(value, dict):
            return value
    return {}


def get_field(data: Any, *aliases: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in aliases:
        value = data.get(key)
        if value is not None:
            return value
    return None


def get_numeric(data: Any, *aliases: str) -> Optional[float]:
    if not isinstance(data, dict):
        return None
    for key in aliases:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def get_named_zone_section(payload: Any, section: str) -> Dict[Any, Any]:
    container = payload.get(section, payload) if isinstance(payload, dict) else payload
    if not isinstance(container, dict):
        return {}
    return get_zones(container)
