"""
state_reader.py

Safe JSON file reader for CN616A state files.
- Handles missing files gracefully
- Handles parse errors
- Returns None or default dicts on any error
- No external dependencies (only stdlib)
"""

import json
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime


def safe_read_json(file_path: Path) -> Optional[Dict[str, Any]]:
    """
    Safely read a JSON file.
    
    Returns:
        Parsed JSON dict on success, None if file doesn't exist or parse failed.
    """
    if not file_path.exists():
        return None
    
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_telemetry_state(logs_dir: Path) -> Dict[str, Any]:
    """
    Read telemetry state JSON.
    
    Returns:
        Dict with keys: ts, unit, port, zones, telemetry (or empty dict if not available)
    """
    path = logs_dir / "cn616a_telemetry_state.json"
    data = safe_read_json(path)
    
    if data is None:
        return {
            "ts": None,
            "unit": None,
            "port": None,
            "zones": [],
            "telemetry": {},
            "error": "File not found or parse error"
        }
    
    return data


def get_config_state(logs_dir: Path) -> Dict[str, Any]:
    """
    Read config state JSON.
    
    Returns:
        Dict with keys: ts, unit, port, zones, config (or empty dict if not available)
    """
    path = logs_dir / "cn616a_config_state.json"
    data = safe_read_json(path)
    
    if data is None:
        return {
            "ts": None,
            "unit": None,
            "port": None,
            "zones": [],
            "config": {},
            "error": "File not found or parse error"
        }
    
    return data


def get_rampsoak_state(logs_dir: Path) -> Dict[str, Any]:
    """
    Read ramp/soak state JSON.
    
    Returns:
        Dict with keys: ts, unit, port, zones, rampsoak (or empty dict if not available)
    """
    path = logs_dir / "cn616a_rampsoak_state.json"
    data = safe_read_json(path)
    
    if data is None:
        return {
            "ts": None,
            "unit": None,
            "port": None,
            "zones": [],
            "rampsoak": {},
            "error": "File not found or parse error"
        }
    
    return data


def get_analysis_state(logs_dir: Path) -> Dict[str, Any]:
    """
    Read analysis state JSON.
    
    Returns:
        Dict with keys: ts, unit, port, zones, analysis (or empty dict if not available)
    """
    path = logs_dir / "cn616a_analysis_state.json"
    data = safe_read_json(path)
    
    if data is None:
        return {
            "ts": None,
            "unit": None,
            "port": None,
            "zones": [],
            "analysis": {},
            "error": "File not found or parse error"
        }
    
    return data


def get_service_config_state(logs_dir: Path) -> Dict[str, Any]:
    """
    Read service config state JSON.
    
    Returns:
        Dict with service configuration or empty dict if not available
    """
    path = logs_dir / "cn616a_service_config_state.json"
    data = safe_read_json(path)
    
    if data is None:
        return {
            "error": "File not found or parse error"
        }
    
    return data


def format_timestamp(ts_str: Optional[str]) -> str:
    """Format ISO timestamp for display. Returns 'N/A' if None or invalid."""
    if not ts_str:
        return "N/A"
    try:
        # Parse ISO format and return readable string
        return ts_str
    except Exception:
        return ts_str if isinstance(ts_str, str) else "N/A"


def safe_get(obj: Any, *keys: str, default: Any = None) -> Any:
    """
    Safely navigate nested dicts.
    
    Args:
        obj: The object to navigate
        *keys: Variable number of string keys to traverse
        default: Value to return if any key is missing or type is wrong (use keyword arg!)
    
    Example: 
        safe_get(data, "telemetry", "zones", "1", "pv_c", default=None)
    
    IMPORTANT: Use keyword argument for default!
        CORRECT:   safe_get(data, "key1", "key2", default={})
        WRONG:     safe_get(data, "key1", "key2", {})  # {} treated as a key!
    """
    current = obj
    for key in keys:
        if not isinstance(key, str):
            return default
        
        if isinstance(current, dict):
            try:
                current = current.get(key)
            except TypeError:
                return default
        else:
            return default
    
    return current if current is not None else default
