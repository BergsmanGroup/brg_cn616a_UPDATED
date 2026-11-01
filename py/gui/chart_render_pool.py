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
from concurrent.futures import Future, ProcessPoolExecutor, CancelledError
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Dict, List, Optional, Tuple

from .chart_render import render_zone_chart

LOGGER = logging.getLogger("cn616a.gui")


class ChartRenderPool:
    """Shared render worker pool used by all zone chart panels."""

    def __init__(self):
        self._executor: ProcessPoolExecutor | None = None
        self._lock = threading.RLock()
        self._inflight: Optional[Tuple[int, Any, Future]] = None
        self._pending: Optional[Tuple[int, Any, Dict[str, Any]]] = None
        self._results: "queue.Queue[Tuple[int, dict]]" = queue.Queue(maxsize=1)
        self._broken = False
        self._closed = False
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
                return
            self._broken = False
            self._submit_pending_locked()

    def submit(self, zone_id: int, request: Dict[str, Any], *, request_id: Any = None) -> None:
        """Keep at most one render active and replace pending work with the newest request."""
        with self._lock:
            if self._closed:
                return
            self._pending = (zone_id, request_id, dict(request))
            self._submit_pending_locked()

    def _submit_pending_locked(self) -> None:
        if self._closed or self._broken or self._executor is None or self._inflight is not None:
            return
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        zone_id, request_id, request = pending
        try:
            future = self._executor.submit(render_zone_chart, request)
        except Exception as exc:
            LOGGER.exception("Failed submitting chart render job for zone %s", zone_id)
            self._broken = True
            self._publish(
                zone_id,
                {"error": f"unable to submit render: {type(exc).__name__}: {exc}", "request_id": request_id},
            )
            self._pending = pending
            return
        self._inflight = (zone_id, request_id, future)
        future.add_done_callback(
            lambda completed, z=zone_id, rid=request_id: self._on_done(completed, z, rid)
        )

    def _on_done(self, future: Future, zone_id: int, request_id: Any) -> None:
        result: Dict[str, Any]
        try:
            result = future.result()
        except CancelledError:
            result = {"error": "render canceled"}
        except BrokenProcessPool as exc:
            LOGGER.error("Chart render worker process crashed: %s", exc)
            result = {"error": f"worker crashed: {exc}"}
            with self._lock:
                self._broken = True
        except Exception as exc:
            LOGGER.exception("Chart render worker raised for zone %s", zone_id)
            result = {"error": f"{type(exc).__name__}: {exc}"}
        result["request_id"] = request_id
        with self._lock:
            if self._closed:
                return
            if self._inflight is not None and self._inflight[2] is future:
                self._inflight = None
            self._publish(zone_id, result)
            self._submit_pending_locked()

    def _publish(self, zone_id: int, result: Dict[str, Any]) -> None:
        try:
            self._results.put_nowait((zone_id, result))
        except queue.Full:
            try:
                self._results.get_nowait()
            except queue.Empty:
                pass
            try:
                self._results.put_nowait((zone_id, result))
            except queue.Full:
                LOGGER.warning("Dropping an obsolete chart render result")

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
        if not self._broken:
            return
        LOGGER.warning("Restarting chart render worker pool after a worker crash")
        self._start_executor()

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            self._pending = None
            if self._executor is not None:
                try:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    LOGGER.exception("Failed shutting down chart render pool")
                self._executor = None
            self._inflight = None
