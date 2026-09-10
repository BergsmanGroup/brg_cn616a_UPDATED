"""
chart_render_pool.py

Owns a single-worker process pool that runs chart_render.render_zone_chart().

Rendering happens in a separate OS process so a native crash inside matplotlib's
Agg renderer (see cn616a_gui_fault.log for confirmed access-violation crashes during
Tk's idle_draw -> matplotlib draw path) terminates only the worker process. The GUI
process detects the broken pool, logs it, restarts a fresh worker, and drops at most
one rendered frame per affected zone.
"""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import ProcessPoolExecutor, CancelledError
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Dict, List, Tuple

from .chart_render import render_zone_chart

LOGGER = logging.getLogger("cn616a.gui")


class ChartRenderPool:
    """Shared render worker pool used by all zone chart panels."""

    def __init__(self):
        self._executor: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()
        self._pending: Dict[int, Any] = {}  # zone_id -> in-flight Future
        self._results: "queue.Queue[Tuple[int, dict]]" = queue.Queue()
        self._broken = False
        self._start_executor()

    def _start_executor(self) -> None:
        with self._lock:
            if self._executor is not None:
                try:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    LOGGER.exception("Failed shutting down previous chart render pool")
            try:
                self._executor = ProcessPoolExecutor(max_workers=1)
                self._broken = False
            except Exception:
                LOGGER.exception("Failed starting chart render worker process")
                self._executor = None
                self._broken = True
            self._pending = {}

    def submit(self, zone_id: int, request: Dict[str, Any]) -> None:
        """Submit a render request for a zone. Drops the request if one is already in flight."""
        with self._lock:
            executor = self._executor
            if executor is None:
                return
            prev = self._pending.get(zone_id)
            if prev is not None and not prev.done():
                return  # a render for this zone is already running; next tick will retry

            try:
                future = executor.submit(render_zone_chart, request)
            except Exception:
                LOGGER.exception("Failed submitting chart render job for zone %s", zone_id)
                return
            self._pending[zone_id] = future

        def _on_done(fut, zone_id=zone_id):
            try:
                result = fut.result()
            except CancelledError:
                return
            except BrokenProcessPool as exc:
                LOGGER.error("Chart render worker process crashed: %s", exc)
                self._results.put((zone_id, {"error": f"worker crashed: {exc}"}))
                self._broken = True
                return
            except Exception as exc:
                LOGGER.exception("Chart render worker raised for zone %s", zone_id)
                self._results.put((zone_id, {"error": f"{type(exc).__name__}: {exc}"}))
                return
            self._results.put((zone_id, result))

        future.add_done_callback(_on_done)

    def poll_results(self) -> List[Tuple[int, Dict[str, Any]]]:
        """Drain completed render results. Call only from the Tk main thread."""
        out: List[Tuple[int, Dict[str, Any]]] = []
        while True:
            try:
                out.append(self._results.get_nowait())
            except queue.Empty:
                break
        return out

    def is_broken(self) -> bool:
        return self._broken

    def restart(self) -> None:
        LOGGER.warning("Restarting chart render worker pool after a worker crash")
        self._start_executor()

    def shutdown(self) -> None:
        with self._lock:
            if self._executor is not None:
                try:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    LOGGER.exception("Failed shutting down chart render pool")
                self._executor = None
            self._pending = {}
