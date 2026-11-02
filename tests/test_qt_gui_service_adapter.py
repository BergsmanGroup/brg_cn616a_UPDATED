from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from py.qt_gui.adapter import CommandOutcome, ZoneKey  # noqa: E402
from py.qt_gui.service_adapter import QtServiceAdapter  # noqa: E402
from py.qt_gui.window import QtMainWindow  # noqa: E402


class FakeJsonLinesEndpoint:
    """Test-owned loopback endpoint using an OS-assigned ephemeral port."""

    def __init__(self, handlers: dict[str, Any] | None = None):
        self.handlers = handlers or {}
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.05)
        self.host, self.port = self._listener.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(1.0)
                    line = connection.makefile("rb").readline()
                    request = json.loads(line.decode("utf-8"))
                    with self._lock:
                        self.requests.append(request)
                    handler = self.handlers.get(str(request.get("op")))
                    response = handler(request) if callable(handler) else handler
                    if response is None:
                        continue
                    if isinstance(response, bytes):
                        connection.sendall(response)
                    elif isinstance(response, list):
                        for chunk in response:
                            connection.sendall(chunk)
                    else:
                        response = dict(response)
                        response.setdefault("id", request.get("id"))
                        connection.sendall(json.dumps(response).encode("utf-8") + b"\n")
                except (OSError, ValueError, json.JSONDecodeError):
                    continue

    def request_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.requests)

    def close(self) -> None:
        self._stopping.set()
        self._listener.close()
        self._thread.join(timeout=1.5)

    def __enter__(self) -> FakeJsonLinesEndpoint:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class QtServiceAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication(["qt-service-adapter-tests"])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.temporary.name)
        self.adapters: list[QtServiceAdapter] = []
        self.endpoints: list[FakeJsonLinesEndpoint] = []

    def tearDown(self) -> None:
        for adapter in reversed(self.adapters):
            adapter.shutdown()
        for endpoint in reversed(self.endpoints):
            endpoint.close()
        self.temporary.cleanup()

    def _endpoint(self, handlers: dict[str, Any] | None = None) -> FakeJsonLinesEndpoint:
        endpoint = FakeJsonLinesEndpoint(handlers)
        self.endpoints.append(endpoint)
        return endpoint

    def _adapter(
        self,
        endpoint: FakeJsonLinesEndpoint,
        *,
        logs_dir: Path | None = None,
        **options: Any,
    ) -> QtServiceAdapter:
        adapter = QtServiceAdapter(
            endpoint.host,
            endpoint.port,
            logs_dir or self.logs_dir,
            timeout_s=options.pop("timeout_s", 0.3),
            capacity=options.pop("capacity", 8),
            poll_interval_ms=10,
            history_hours=0.1,
            **options,
        )
        self.adapters.append(adapter)
        return adapter

    def _wait_for(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.002)
        self.app.processEvents()
        self.assertTrue(predicate(), "Qt adapter did not reach the expected state")

    def _write_snapshots(
        self,
        *,
        port: str | None = "COM_QT_TEST",
        unit: int | None = 3,
        timestamp: str | None = None,
    ) -> None:
        timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        metadata: dict[str, object] = {"ts": timestamp, "zones": [1]}
        if port is not None:
            metadata["port"] = port
        if unit is not None:
            metadata["unit"] = unit
        states = {
            "cn616a_telemetry_state.json": {
                **metadata,
                "telemetry": {
                    "zones": {
                        "z1": {
                            "PV": 0.0,
                            "SP": 0.0,
                            "SP_autotune": 12.5,
                            "output_pct": 0,
                        }
                    }
                },
            },
            "cn616a_config_state.json": {
                **metadata,
                "config": {"zones": {"Z1": {"sensor_type": "K"}}},
            },
            "cn616a_rampsoak_state.json": {
                **metadata,
                "rampsoak": {"zones": {"zone1": {"segments": {"1": {"sp_c": 0}}}}},
            },
            "cn616a_analysis_state.json": {
                **metadata,
                "analysis": {"1": {"avg_abs_error_c": 0.0, "equilibrium": False, "point_count": 4}},
            },
            "cn616a_service_config_state.json": {
                "ts": timestamp,
                "config": {"telemetry_poll_hz": 2.0},
            },
        }
        for filename, state in states.items():
            (self.logs_dir / filename).write_text(json.dumps(state), encoding="utf-8")

    def _write_history(self, port: str, unit: int) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        telemetry = {
            "ts": timestamp,
            "port": port,
            "unit": unit,
            "telemetry": {"zones": {"1": {"pv_c": 0, "sp_abs_c": 70}}},
        }
        analysis = {
            "ts": timestamp,
            "port": port,
            "unit": unit,
            "analysis": {"1": {"avg_abs_error_c": 0.0}},
        }
        (self.logs_dir / "cn616a_telemetry_log.jsonl").write_text(
            json.dumps(telemetry) + "\n",
            encoding="utf-8",
        )
        (self.logs_dir / "cn616a_analysis_log.jsonl").write_text(
            json.dumps(analysis) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _status_response(request: dict[str, Any]) -> dict[str, object]:
        return {"id": request["id"], "ok": True, "connected": True}

    def test_five_categories_aliases_zero_missing_time_staleness_and_target_keys(self) -> None:
        self._write_snapshots()
        self._write_history("COM_QT_TEST", 3)
        endpoint = self._endpoint({"get_status": self._status_response})
        adapter = self._adapter(endpoint)
        self._wait_for(
            lambda: len(adapter.snapshots()) == 5
            and all(snapshot.data is not None for snapshot in adapter.snapshots().values())
            and not adapter.status.read_only
        )
        views = adapter.snapshots()

        self.assertEqual(set(views), {"telemetry", "config", "rampsoak", "analysis", "service_config"})
        telemetry = views["telemetry"]
        self.assertFalse(telemetry.stale)
        self.assertIsNone(telemetry.error)
        self.assertIsNotNone(telemetry.source_time)
        self.assertEqual(telemetry.zone_values[1]["pv_c"], 0.0)
        self.assertEqual(telemetry.zone_values[1]["sp_abs_c"], 0.0)
        self.assertEqual(telemetry.zone_values[1]["autotune_sp_c"], 12.5)
        self.assertIsNone(telemetry.zone_values[1]["p_gain"])
        self.assertEqual(
            telemetry.keyed_values[ZoneKey("COM_QT_TEST", 3, 1)]["pv_c"],
            0.0,
        )
        self.assertEqual(views["config"].zone_values[1]["sensor_type"], "K")
        self.assertEqual(views["rampsoak"].zone_values[1]["segments"]["1"]["sp_c"], 0)
        self.assertEqual(views["analysis"].zone_values[1]["avg_abs_error_c"], 0.0)
        self.assertEqual(views["service_config"].data["config"]["telemetry_poll_hz"], 2.0)
        self.assertEqual(adapter.status.target.key.bus_id, "COM_QT_TEST")
        self.assertEqual(adapter.status.target.modbus_address, 3)

    def test_stale_telemetry_remains_readable_but_disables_writes(self) -> None:
        old_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self._write_snapshots(timestamp=old_timestamp)
        endpoint = self._endpoint({"get_status": self._status_response})
        adapter = self._adapter(endpoint, stale_after_s=0.1)
        self._wait_for(
            lambda: "telemetry" in adapter.snapshots()
            and adapter.snapshots()["telemetry"].data is not None
            and adapter.snapshots()["telemetry"].stale
        )

        telemetry = adapter.snapshots()["telemetry"]
        self.assertTrue(adapter.status.read_only)
        self.assertIsNotNone(telemetry.identity)
        self.assertIsNotNone(telemetry.age_s)
        self.assertGreater(telemetry.age_s, 0.1)
        self.assertEqual(telemetry.zone_values[1]["pv_c"], 0.0)
        self.assertEqual(
            adapter.submit(
                [{"op": "set_sp_abs", "zone": 1, "value_c": 70.0}],
                target=ZoneKey("COM_QT_TEST", 3, 1),
            ),
            "rejected",
        )
        self.assertEqual([request["op"] for request in endpoint.request_snapshot()], ["get_status"])
        self.assertEqual(adapter.outcomes()[-1].result, "not_sent")

    def test_stale_timestamp_pacific_alias_disables_writes_with_fresh_file_mtime(self) -> None:
        self._write_snapshots()
        telemetry_path = self.logs_dir / "cn616a_telemetry_state.json"
        telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
        telemetry.pop("ts")
        telemetry["timestamp_pacific"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        telemetry_path.write_text(json.dumps(telemetry), encoding="utf-8")
        endpoint = self._endpoint({"get_status": self._status_response})
        adapter = self._adapter(endpoint)
        self._wait_for(
            lambda: adapter.status.connected
            and adapter.snapshots().get("telemetry") is not None
            and adapter.snapshots()["telemetry"].data is not None
        )

        snapshot = adapter.snapshots()["telemetry"]
        self.assertIsNotNone(snapshot.source_time)
        self.assertTrue(snapshot.stale)
        self.assertTrue(adapter.status.read_only)
        result = adapter.submit(
            [{"op": "set_sp_abs", "zone": 1, "value_c": 1.0}],
            target=ZoneKey("COM_QT_TEST", 3, 1),
        )
        self.assertEqual(result, "rejected")
        self.assertEqual([request["op"] for request in endpoint.request_snapshot()], ["get_status"])

    def test_unknown_identity_allows_reads_but_rejects_writes(self) -> None:
        self._write_snapshots(port=None, unit=None)
        endpoint = self._endpoint({"get_status": self._status_response})
        adapter = self._adapter(endpoint)
        self._wait_for(lambda: adapter.status.connected and "telemetry" in adapter.snapshots())

        self.assertTrue(adapter.status.read_only)
        self.assertIsNone(adapter.snapshots()["telemetry"].identity)
        self.assertTrue(adapter.snapshots()["telemetry"].zone_values[1])
        self.assertFalse(adapter.snapshots()["telemetry"].keyed_values)
        self.assertEqual(adapter.submit([{"op": "get_status"}]), "submitted")
        self.assertEqual(
            adapter.submit([{"op": "set_sp_abs", "zone": 1, "value_c": 70.0}]),
            "rejected",
        )
        self._wait_for(lambda: len(endpoint.request_snapshot()) >= 2)
        self.assertEqual(
            [request["op"] for request in endpoint.request_snapshot()],
            ["get_status", "get_status"],
        )
        self.assertEqual(adapter.outcomes()[-1].result, "not_sent")

    def test_missing_telemetry_identity_blocks_writes_despite_identified_config(self) -> None:
        self._write_snapshots()
        telemetry_path = self.logs_dir / "cn616a_telemetry_state.json"
        telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
        telemetry.pop("port")
        telemetry.pop("unit")
        telemetry_path.write_text(json.dumps(telemetry), encoding="utf-8")
        endpoint = self._endpoint({"get_status": self._status_response})
        adapter = self._adapter(endpoint)
        self._wait_for(
            lambda: adapter.status.connected
            and len(adapter.snapshots()) == 5
            and all(snapshot.data is not None for snapshot in adapter.snapshots().values())
        )

        self.assertIsNone(adapter.snapshots()["telemetry"].identity)
        self.assertIsNotNone(adapter.snapshots()["config"].identity)
        self.assertTrue(adapter.status.read_only)
        self.assertEqual(
            adapter.submit(
                [{"op": "set_sp_abs", "zone": 1, "value_c": 70.0}],
                target=ZoneKey("COM_QT_TEST", 3, 1),
            ),
            "rejected",
        )
        self.assertEqual([request["op"] for request in endpoint.request_snapshot()], ["get_status"])

    def test_foreign_controller_unit_field_and_zone_payloads_are_rejected(self) -> None:
        self._write_snapshots()
        endpoint = self._endpoint({"get_status": self._status_response})
        adapter = self._adapter(endpoint)
        self._wait_for(lambda: not adapter.status.read_only)

        foreign = ZoneKey("COM_OTHER", 3, 1)
        selected = ZoneKey("COM_QT_TEST", 3, 1)
        self.assertEqual(
            adapter.submit([{"op": "set_sp_abs", "zone": 1, "value_c": 71}], target=foreign),
            "rejected",
        )
        self.assertEqual(
            adapter.submit(
                [{"op": "set_sp_abs", "zone": 2, "value_c": 71}],
                target=selected,
            ),
            "rejected",
        )
        self.assertEqual(
            adapter.submit(
                [{"op": "set_sp_abs", "zone": 1, "unit": 3, "value_c": 71}],
                target=selected,
            ),
            "rejected",
        )
        self.assertEqual([request["op"] for request in endpoint.request_snapshot()], ["get_status"])
        self.assertEqual([item.result for item in adapter.outcomes()], ["not_sent"] * 3)

    def test_ordered_sequence_preserves_confirmed_failure_and_not_sent(self) -> None:
        self._write_snapshots()
        endpoint = self._endpoint(
            {
                "get_status": self._status_response,
                "set_sp_abs": lambda request: {"id": request["id"], "ok": True},
                "set_control_method": lambda request: {
                    "id": request["id"],
                    "ok": False,
                    "error": "rejected by fake",
                },
            }
        )
        adapter = self._adapter(endpoint)
        self._wait_for(lambda: not adapter.status.read_only)
        target = ZoneKey("COM_QT_TEST", 3, 1)

        status = adapter.submit(
            [
                {"op": "set_sp_abs", "zone": 1, "value_c": 70.0},
                {"op": "set_control_method", "zone": 1, "method": "PID_CONTROL"},
                {"op": "set_control_mode", "zone": 1, "mode": "STANDARD_CONTROL"},
            ],
            target=target,
        )
        self.assertEqual(status, "submitted")
        self._wait_for(lambda: len(adapter.outcomes()) == 3)
        outcomes = adapter.outcomes()
        self.assertEqual(
            [outcome.result for outcome in outcomes],
            ["confirmed_success", "confirmed_failure", "not_sent"],
        )
        self.assertIn("readback has not been verified", outcomes[0].message)
        requests = endpoint.request_snapshot()
        self.assertEqual(
            [request["op"] for request in requests],
            ["get_status", "set_sp_abs", "set_control_method"],
        )
        self.assertEqual(requests[1]["value_c"], 70.0)
        self.assertEqual(requests[2]["method"], "PID_CONTROL")
        self.assertTrue(all(isinstance(request["id"], str) for request in requests))
        self.assertTrue(all("unit" not in request for request in requests))

    def test_delayed_endpoint_keeps_event_loop_and_tabs_responsive(self) -> None:
        self._write_snapshots()
        request_seen = threading.Event()
        release_response = threading.Event()

        def delayed_status(request: dict[str, Any]) -> dict[str, object]:
            request_seen.set()
            if not release_response.wait(timeout=3.0):
                return {"id": request["id"], "ok": False, "error": "test response was not released"}
            return self._status_response(request)

        endpoint = self._endpoint({"get_status": delayed_status})
        adapter = self._adapter(endpoint, timeout_s=4.0)
        window = QtMainWindow(adapter)
        heartbeats = []
        heartbeat = QTimer()
        heartbeat.timeout.connect(lambda: heartbeats.append(time.monotonic()))
        heartbeat.start(5)
        QTimer.singleShot(10, lambda: window.tabs.setCurrentIndex(2))
        window.show()
        try:
            self._wait_for(
                lambda: request_seen.is_set()
                and len(heartbeats) > 10
                and window.tabs.currentIndex() == 2
            )
            self.assertFalse(adapter.status.connected)
            release_response.set()
            self._wait_for(lambda: adapter.status.connected and not adapter.status.read_only)
            self.assertIn("SERVICE AND CONTROLLER CONNECTED", window.mode_banner.text())
            self.assertNotIn("READ-ONLY", window.mode_banner.text())
        finally:
            release_response.set()
            heartbeat.stop()
            window.close()

    def test_connection_failure_is_explicit_and_close_is_bounded(self) -> None:
        endpoint = self._endpoint()
        port = endpoint.port
        endpoint.close()
        adapter = QtServiceAdapter(
            "127.0.0.1",
            port,
            self.logs_dir,
            timeout_s=0.1,
            poll_interval_ms=10,
        )
        self.adapters.append(adapter)
        window = QtMainWindow(adapter)
        self._wait_for(lambda: "SERVICE UNAVAILABLE" in adapter.status.message)
        self.assertTrue(adapter.status.read_only)
        self.assertIn("SERVICE UNAVAILABLE", window.mode_banner.text())
        started = time.monotonic()
        window.close()
        self.assertLess(time.monotonic() - started, 2.0)

    def test_negative_status_response_is_visible_and_read_only(self) -> None:
        endpoint = self._endpoint(
            {"get_status": lambda request: {"id": request["id"], "ok": False, "error": "status denied"}}
        )
        adapter = self._adapter(endpoint)
        self._wait_for(lambda: "status denied" in adapter.status.message)

        self.assertTrue(adapter.status.connected)
        self.assertTrue(adapter.status.read_only)
        self.assertIn("status unavailable", adapter.status.message.lower())

    def test_explicit_endpoint_is_used_instead_of_stored_service_config(self) -> None:
        self._write_snapshots()
        service_config_path = self.logs_dir / "cn616a_service_config_state.json"
        stored_config = json.loads(service_config_path.read_text(encoding="utf-8"))
        stored_config["config"].update(
            {"last_tcp_host": "stored.invalid", "last_tcp_port": 8765}
        )
        service_config_path.write_text(json.dumps(stored_config), encoding="utf-8")
        endpoint = self._endpoint({"get_status": self._status_response})
        create_connection = socket.create_connection

        def selected_endpoint_only(address, *args, **kwargs):
            self.assertEqual(address, (endpoint.host, endpoint.port))
            return create_connection(address, *args, **kwargs)

        with patch(
            "py.gui.service_client.socket.create_connection",
            side_effect=selected_endpoint_only,
        ):
            adapter = self._adapter(endpoint)
            self._wait_for(lambda: adapter.status.connected)
            self.assertEqual(adapter.endpoint, f"{endpoint.host}:{endpoint.port}")

    def test_post_send_disconnect_is_unknown_and_never_retried(self) -> None:
        self._write_snapshots()
        endpoint = self._endpoint(
            {
                "get_status": self._status_response,
                "set_sp_abs": lambda _request: None,
            }
        )
        adapter = self._adapter(endpoint, timeout_s=0.2)
        self._wait_for(lambda: not adapter.status.read_only)

        result = adapter.submit(
            [{"op": "set_sp_abs", "zone": 1, "value_c": 72.0}],
            target=ZoneKey("COM_QT_TEST", 3, 1),
        )
        self.assertEqual(result, "submitted")
        self._wait_for(lambda: len(adapter.outcomes()) == 1)
        self.assertEqual(adapter.outcomes()[0].result, "unknown")
        self.assertEqual(
            [request["op"] for request in endpoint.request_snapshot()],
            ["get_status", "set_sp_abs"],
        )

    def test_queue_saturation_is_visible_and_does_not_duplicate_writes(self) -> None:
        self._write_snapshots()

        def slow_write(_request: dict[str, Any]) -> dict[str, object]:
            time.sleep(0.25)
            return {"ok": True}

        endpoint = self._endpoint({"get_status": self._status_response, "set_sp_abs": slow_write})
        adapter = self._adapter(endpoint, timeout_s=0.05, capacity=1)
        self._wait_for(lambda: not adapter.status.read_only)
        target = ZoneKey("COM_QT_TEST", 3, 1)

        self.assertEqual(
            adapter.submit([{"op": "set_sp_abs", "zone": 1, "value_c": 72.0}], target=target),
            "submitted",
        )
        self._wait_for(
            lambda: any(request["op"] == "set_sp_abs" for request in endpoint.request_snapshot())
        )
        self.assertEqual(
            adapter.submit([{"op": "set_sp_abs", "zone": 1, "value_c": 73.0}], target=target),
            "full",
        )
        self.assertIn("queue is full", adapter.outcomes()[-1].message)
        self._wait_for(lambda: len(adapter.outcomes()) >= 2)
        writes = [request for request in endpoint.request_snapshot() if request["op"] == "set_sp_abs"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(adapter.outcomes()[0].result, "not_sent")
        self.assertTrue(any(outcome.result == "unknown" for outcome in adapter.outcomes()))

    def test_coalesced_request_has_a_visible_not_sent_outcome(self) -> None:
        self._write_snapshots()
        request_seen = threading.Event()

        def delayed_write(request: dict[str, Any]) -> dict[str, object]:
            request_seen.set()
            time.sleep(0.15)
            return {"id": request["id"], "ok": True}

        endpoint = self._endpoint({"get_status": self._status_response, "set_sp_abs": delayed_write})
        adapter = self._adapter(endpoint)
        self._wait_for(lambda: not adapter.status.read_only)
        target = ZoneKey("COM_QT_TEST", 3, 1)
        message = [{"op": "set_sp_abs", "zone": 1, "value_c": 76.0}]
        self.assertEqual(adapter.submit(message, target=target, coalesce_key="same-write"), "submitted")
        self.assertTrue(request_seen.wait(1.0))
        self.assertEqual(adapter.submit(message, target=target, coalesce_key="same-write"), "coalesced")
        self.assertTrue(
            any(
                outcome.result == "not_sent" and "coalesced" in outcome.message
                for outcome in adapter.outcomes()
            )
        )

    def test_shutdown_retains_inflight_unknown_and_queued_not_sent_outcomes(self) -> None:
        self._write_snapshots()
        request_seen = threading.Event()

        def hanging_write(_request: dict[str, Any]) -> None:
            request_seen.set()
            time.sleep(0.3)
            return None

        endpoint = self._endpoint({"get_status": self._status_response, "set_sp_abs": hanging_write})
        adapter = self._adapter(endpoint, timeout_s=0.05, capacity=4)
        self._wait_for(lambda: not adapter.status.read_only)
        target = ZoneKey("COM_QT_TEST", 3, 1)
        self.assertEqual(
            adapter.submit([{"op": "set_sp_abs", "zone": 1, "value_c": 74.0}], target=target),
            "submitted",
        )
        self.assertTrue(request_seen.wait(1.0))
        self.assertEqual(
            adapter.submit([{"op": "set_sp_abs", "zone": 1, "value_c": 75.0}], target=target),
            "submitted",
        )

        started = time.monotonic()
        adapter.shutdown()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(
            [outcome.result for outcome in adapter.outcomes()],
            ["unknown", "not_sent"],
        )
        self.assertFalse(adapter.is_current_generation(adapter.generation))
        self.assertEqual(
            [request["op"] for request in endpoint.request_snapshot()],
            ["get_status", "set_sp_abs"],
        )

    def test_history_identity_mismatch_is_withheld_and_generations_do_not_cross(self) -> None:
        self._write_snapshots(port="COM_ACTIVE", unit=3)
        self._write_history("COM_OLD", 3)
        endpoint = self._endpoint({"get_status": self._status_response})
        old_adapter = self._adapter(endpoint)
        self._wait_for(lambda: old_adapter.history is not None)
        self.assertIsNone(old_adapter.history.data)
        self.assertIn("does not match", old_adapter.history.error)

        old_generation = old_adapter.generation
        self._write_history("COM_ACTIVE", 3)
        new_adapter = self._adapter(endpoint)
        self._wait_for(lambda: new_adapter.history is not None and new_adapter.status.connected)
        self.assertNotEqual(old_generation, new_adapter.generation)
        self.assertFalse(new_adapter.is_current_generation(old_generation))
        self.assertEqual(new_adapter.history.generation, new_adapter.generation)

        window = QtMainWindow(new_adapter)
        stale_snapshot = old_adapter.snapshots()["telemetry"]
        current_snapshot = window.snapshot_models.get("telemetry")
        window._on_snapshot_updated(stale_snapshot)
        self.assertIs(window.snapshot_models.get("telemetry"), current_snapshot)
        window._on_outcome_received(
            CommandOutcome(
                operation="set_sp_abs",
                result="confirmed_success",
                message="stale",
                generation=old_generation,
            )
        )
        self.assertEqual(window.command_outcomes, [])
        window.close()

    def test_current_history_is_withheld_when_snapshot_target_changes(self) -> None:
        self._write_snapshots(port="COM_ACTIVE", unit=3)
        self._write_history("COM_ACTIVE", 3)
        endpoint = self._endpoint({"get_status": self._status_response})
        adapter = self._adapter(endpoint)
        self._wait_for(lambda: adapter.history is not None and adapter.history.data is not None)

        self._write_snapshots(port="COM_NEW", unit=4)
        self._wait_for(
            lambda: adapter.status.target is not None
            and adapter.status.target.key.bus_id == "COM_NEW"
            and adapter.status.target.key.controller_id == 4
        )
        self._wait_for(
            lambda: adapter.history is not None
            and adapter.history.data is None
            and adapter.history.error is not None
            and "does not match" in adapter.history.error
        )

    def test_history_with_missing_record_identity_is_withheld(self) -> None:
        self._write_snapshots(port="COM_ACTIVE", unit=3)
        self._write_history("COM_ACTIVE", 3)
        analysis_path = self.logs_dir / "cn616a_analysis_log.jsonl"
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        analysis.pop("port")
        analysis.pop("unit")
        analysis_path.write_text(json.dumps(analysis) + "\n", encoding="utf-8")
        endpoint = self._endpoint({"get_status": self._status_response})
        adapter = self._adapter(endpoint)
        self._wait_for(
            lambda: adapter.history is not None
            and adapter.history.data is None
            and adapter.history.error is not None
            and "unknown controller identity" in adapter.history.error
        )

    def test_restart_creates_new_generation_and_never_replays_commands(self) -> None:
        self._write_snapshots()
        endpoint = self._endpoint(
            {
                "get_status": self._status_response,
                "set_sp_abs": lambda request: {"id": request["id"], "ok": True},
            }
        )
        first = self._adapter(endpoint)
        self._wait_for(lambda: not first.status.read_only)
        first_generation = first.generation
        self.assertEqual(
            first.submit(
                [{"op": "set_sp_abs", "zone": 1, "value_c": 77.0}],
                target=ZoneKey("COM_QT_TEST", 3, 1),
            ),
            "submitted",
        )
        self._wait_for(lambda: len(first.outcomes()) == 1)
        first.shutdown()
        self.assertEqual(first.outcomes()[0].result, "confirmed_success")

        second = self._adapter(endpoint)
        self._wait_for(lambda: second.status.connected)
        self.assertNotEqual(first_generation, second.generation)
        self.assertFalse(second.is_current_generation(first_generation))
        self.assertEqual(
            [request["op"] for request in endpoint.request_snapshot()],
            ["get_status", "set_sp_abs", "get_status"],
        )
        self.assertEqual(second.outcomes(), ())

    def test_callbacks_and_snapshot_model_updates_run_on_qt_thread(self) -> None:
        self._write_snapshots()
        endpoint = self._endpoint({"get_status": self._status_response})
        adapter = self._adapter(endpoint)
        callback_threads: list[int] = []
        adapter.snapshot_updated.connect(lambda _item: callback_threads.append(threading.get_ident()))
        adapter.outcome_received.connect(lambda _item: callback_threads.append(threading.get_ident()))
        target = ZoneKey("COM_QT_TEST", 3, 1)
        self._wait_for(lambda: not adapter.status.read_only)
        adapter.submit(
            [{"op": "set_sp_abs", "zone": 1, "value_c": 76.0}],
            target=target,
        )
        self._wait_for(lambda: len(adapter.outcomes()) == 1)
        self.assertTrue(callback_threads)
        self.assertTrue(all(thread_id == threading.get_ident() for thread_id in callback_threads))

    def test_submit_from_non_qt_thread_is_rejected(self) -> None:
        endpoint = self._endpoint({"get_status": self._status_response})
        adapter = self._adapter(endpoint)
        errors: list[Exception] = []

        def submit_from_worker() -> None:
            try:
                adapter.submit([{"op": "get_status"}])
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=submit_from_worker)
        worker.start()
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)


if __name__ == "__main__":
    unittest.main()
