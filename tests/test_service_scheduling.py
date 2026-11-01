import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

import cn616a_service as service_module  # noqa: E402
from cn616a_service import CN616AService, CommandRequest, ServiceConfig  # noqa: E402


class CommandLifecycleTests(unittest.TestCase):
    def test_timeout_and_execution_start_are_race_safe(self):
        queued = CommandRequest({"id": "queued"})
        self.assertTrue(queued.cancel_if_queued())
        self.assertFalse(queued.mark_started())
        self.assertEqual(queued.timeout_response()["state"], "not_executed")

        running = CommandRequest({"id": "running"})
        self.assertTrue(running.mark_started())
        self.assertFalse(running.cancel_if_queued())
        self.assertEqual(running.timeout_response()["state"], "unknown")
        running.finish({"id": "running", "ok": True})
        self.assertEqual(running.timeout_response(), {"id": "running", "ok": True})


class ServiceSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.telemetry_started = threading.Event()
        self.telemetry_release = threading.Event()
        self.write_started = threading.Event()
        self.write_release = threading.Event()
        self.calls = []
        self.calls_lock = threading.Lock()
        self.active_operations = 0
        self.max_active_operations = 0

        test = self

        class FakeController:
            def __init__(self, *_args, **_kwargs):
                self._write_count = 0

            def _begin(self, name, *args):
                with test.calls_lock:
                    test.active_operations += 1
                    test.max_active_operations = max(
                        test.max_active_operations, test.active_operations
                    )
                    test.calls.append((name, args, "started"))

            def _end(self, name, *args):
                with test.calls_lock:
                    test.active_operations -= 1
                    test.calls.append((name, args, "completed"))

            def connect(self):
                return None

            def close(self):
                return None

            def read_config(self, zones):
                self._begin("read_config", tuple(zones))
                try:
                    return {"zones": {str(zone): {"sp_abs_c": 80.0} for zone in zones}}
                finally:
                    self._end("read_config", tuple(zones))

            def read_telemetry(self, zones):
                self._begin("read_telemetry", tuple(zones))
                test.telemetry_started.set()
                try:
                    if not test.telemetry_release.wait(3.0):
                        raise TimeoutError("test telemetry release was not signaled")
                    return {
                        "zones": {
                            str(zone): {"pv_c": 25.0, "sp_abs_c": 80.0}
                            for zone in zones
                        }
                    }
                finally:
                    self._end("read_telemetry", tuple(zones))

            def set_sp_abs(self, zone, value_c):
                self._begin("set_sp_abs", zone, value_c)
                self._write_count += 1
                test.write_started.set()
                try:
                    if self._write_count == 1 and not test.write_release.wait(3.0):
                        raise TimeoutError("test write release was not signaled")
                    return None
                finally:
                    self._end("set_sp_abs", zone, value_c)

            def read_rampsoak_all(self, zones):
                return {"zones": {str(zone): {} for zone in zones}}

        self.controller_patch = patch("cn616a_service.CN616A", new=FakeController)
        self.controller_patch.start()
        self.addCleanup(self.controller_patch.stop)

        port_probe = socket.socket()
        port_probe.bind(("127.0.0.1", 0))
        self.port = port_probe.getsockname()[1]
        port_probe.close()

        self.service = CN616AService(
            port="FAKE",
            unit=1,
            map_path=str(REPO_ROOT / "cn616a_register_map.json"),
            out_dir=Path(self.tmp.name),
            tcp_host="127.0.0.1",
            tcp_port=self.port,
            cfg=ServiceConfig(
                telemetry_hz=5.0,
                config_hz=0.0,
                rampsoak_hz=0.0,
                analysis_hz=0.0,
                flush_each_line=False,
            ),
        )
        self.service_thread = threading.Thread(
            target=self.service.run,
            kwargs={"poll_s": 0.2},
            daemon=True,
        )
        self.service_thread.start()
        self._wait_for_listener()
        self.addCleanup(self._stop_service)

    def _wait_for_listener(self):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.01)
        self.fail("service command listener did not start")

    def _stop_service(self):
        self.telemetry_release.set()
        self.write_release.set()
        self.service.stop_evt.set()
        self.service_thread.join(timeout=3.0)
        self.service._cmd_thread.join(timeout=1.0)

    def _send(self, message):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2.0) as conn:
            conn.settimeout(2.0)
            conn.sendall((json.dumps(message) + "\n").encode("utf-8"))
            response = bytearray()
            while b"\n" not in response:
                response.extend(conn.recv(4096))
        return json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))

    def _wait_until(self, predicate, timeout=1.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())

    def test_concurrent_clients_keep_serial_io_ordered_and_report_timings(self):
        self.assertTrue(self.telemetry_started.wait(1.0))
        self.assertEqual(self.service.cmd_q.maxsize, service_module.MAX_PENDING_COMMANDS)
        self.telemetry_release.set()

        barrier = threading.Barrier(5)
        responses = {}
        failures = []

        def client(index):
            request_id = f"parallel-{index}"
            try:
                barrier.wait(timeout=1.0)
                responses[request_id] = self._send({
                    "id": request_id,
                    "op": "set_sp_abs",
                    "zone": 1,
                    "value_c": 70.0 + index,
                })
            except Exception as exc:
                failures.append(exc)

        workers = [threading.Thread(target=client, args=(i,)) for i in range(4)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=1.0)
        self.assertTrue(self.write_started.wait(1.0))
        self.assertTrue(self._wait_until(lambda: self.service.cmd_q.qsize() >= 2))
        self.write_release.set()
        for worker in workers:
            worker.join(timeout=2.0)

        self.assertFalse(failures)
        self.assertEqual(set(responses), {f"parallel-{i}" for i in range(4)})
        for request_id, response in responses.items():
            self.assertEqual(response["id"], request_id)
            self.assertTrue(response["ok"])
            self.assertGreaterEqual(response["timing"]["controller_io_ms"], 0.0)
            self.assertGreaterEqual(response["timing"]["end_to_end_ms"], response["timing"]["queue_wait_ms"])
        completed_writes = [
            call[1][1]
            for call in self.calls
            if call[0] == "set_sp_abs" and call[2] == "completed"
        ]
        self.assertEqual(Counter(completed_writes), Counter(70.0 + i for i in range(4)))
        self.assertEqual(self.max_active_operations, 1)
        self.assertTrue(self._wait_until(
            lambda: sum(
                call[0] == "read_telemetry" and call[2] == "completed"
                for call in self.calls
            ) >= 2,
            timeout=1.0,
        ))

    def test_timeout_while_queued_cancels_command_before_execution(self):
        self.assertTrue(self.telemetry_started.wait(1.0))
        response = self._send({
            "id": "queued-timeout",
            "op": "set_sp_abs",
            "zone": 1,
            "value_c": 99.0,
            "timeout_s": 0.05,
        })
        self.assertEqual(response["state"], "not_executed")
        self.telemetry_release.set()
        self.assertTrue(self._wait_until(lambda: self.service.cmd_q.empty()))
        self.assertFalse(any(call[0] == "set_sp_abs" for call in self.calls))

    def test_timeout_during_execution_reports_unknown_and_does_not_retry(self):
        self.telemetry_release.set()
        response = self._send({
            "id": "running-timeout",
            "op": "set_sp_abs",
            "zone": 1,
            "value_c": 99.0,
            "timeout_s": 0.05,
        })
        self.assertTrue(self.write_started.is_set())
        self.assertEqual(response["state"], "unknown")
        self.write_release.set()
        self.assertTrue(self._wait_until(
            lambda: sum(call[0] == "set_sp_abs" and call[2] == "completed" for call in self.calls) == 1
        ))

    def test_client_disconnect_before_execution_cancels_queued_command(self):
        self.assertTrue(self.telemetry_started.wait(1.0))
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=1.0)
        conn.sendall((json.dumps({
            "id": "disconnect-before-run",
            "op": "set_sp_abs",
            "zone": 1,
            "value_c": 99.0,
        }) + "\n").encode("utf-8"))
        self.assertTrue(self._wait_until(lambda: self.service.cmd_q.qsize() == 1))
        conn.close()
        time.sleep(0.1)
        self.telemetry_release.set()
        self.assertTrue(self._wait_until(lambda: self.service.cmd_q.empty()))
        self.assertFalse(any(call[0] == "set_sp_abs" for call in self.calls))

    def test_protocol_rejects_non_object_and_oversized_requests(self):
        self.telemetry_release.set()
        self.assertIn("JSON object", self._send(["not", "an", "object"])["error"])
        with socket.create_connection(("127.0.0.1", self.port), timeout=1.0) as conn:
            conn.settimeout(1.0)
            conn.sendall(b"x" * (service_module.MAX_COMMAND_BYTES + 1))
            response = bytearray()
            while b"\n" not in response:
                response.extend(conn.recv(4096))
        self.assertEqual(json.loads(response.split(b"\n", 1)[0])["state"], "rejected")

    def test_multiple_json_lines_on_one_connection_preserve_ids(self):
        self.telemetry_release.set()
        with socket.create_connection(("127.0.0.1", self.port), timeout=1.0) as conn:
            conn.settimeout(1.0)
            conn.sendall(b'{"id":"legacy-a","op":"ping"}\n{"id":"legacy-b","op":"ping"}\n')
            stream = conn.makefile("rb")
            first = json.loads(stream.readline().decode("utf-8"))
            second = json.loads(stream.readline().decode("utf-8"))
        self.assertEqual([first["id"], second["id"]], ["legacy-a", "legacy-b"])
        self.assertTrue(first["ok"] and second["ok"])

    def test_shutdown_command_returns_confirmed_response_before_service_exits(self):
        self.telemetry_release.set()
        response = self._send({"id": "shutdown", "op": "shutdown"})
        self.assertEqual(response["id"], "shutdown")
        self.assertTrue(response["ok"])
        self.service_thread.join(timeout=1.0)
        self.assertFalse(self.service_thread.is_alive())

    def test_active_client_limit_rejects_extra_connection(self):
        self.telemetry_release.set()
        clients = []
        try:
            for _ in range(service_module.MAX_ACTIVE_CLIENTS):
                conn = socket.create_connection(("127.0.0.1", self.port), timeout=1.0)
                conn.settimeout(1.0)
                clients.append(conn)
            time.sleep(0.1)
            with socket.create_connection(("127.0.0.1", self.port), timeout=1.0) as overflow:
                overflow.settimeout(1.0)
                response = bytearray()
                while b"\n" not in response:
                    response.extend(overflow.recv(4096))
            self.assertEqual(json.loads(response.split(b"\n", 1)[0])["state"], "rejected")
        finally:
            for conn in clients:
                conn.close()

    def test_scheduled_run_stop_is_checked_at_command_boundaries(self):
        run_dir = Path(self.tmp.name) / "scheduled-run"
        run_dir.mkdir()
        self.service._run_active = True
        self.service._run_dir = run_dir
        self.service._run_started_monotonic = time.monotonic()
        self.service._run_schedule_enabled = True
        self.service._run_schedule_stop_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        self.service._check_scheduled_run_stop()

        self.assertFalse(self.service._run_active)
        self.assertTrue((run_dir / "cn616a_run_info.json").exists())
        payload = json.loads((run_dir / "cn616a_run_info.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["reason"], "scheduled")


if __name__ == "__main__":
    unittest.main()
