import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from gui.service_client import ServiceClient  # noqa: E402


class FakeJsonLinesService:
    def __init__(self, handlers):
        self.handlers = list(handlers)
        self.requests = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.host, self.port = self.listener.getsockname()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        index = 0
        while index < len(self.handlers):
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1.0)
                stream = connection.makefile("rb")
                line = stream.readline()
                request = json.loads(line.decode("utf-8"))
                self.requests.append(request)
                response = self.handlers[index](request)
                index += 1
                if response is None:
                    continue
                if isinstance(response, bytes):
                    connection.sendall(response)
                elif isinstance(response, list):
                    for chunk in response:
                        connection.sendall(chunk)
                        time.sleep(0.01)
                else:
                    connection.sendall(json.dumps(response).encode("utf-8") + b"\n")

    def close(self):
        self.listener.close()
        self.thread.join(timeout=1.5)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class ServiceClientTests(unittest.TestCase):
    @staticmethod
    def _poll_until(client, completed, timeout=1.0):
        deadline = time.monotonic() + timeout
        while not completed.is_set() and time.monotonic() < deadline:
            client.poll_results()
            completed.wait(0.01)
        client.poll_results()
        return completed.is_set()

    def _submit_and_collect(self, service, *, messages=None, timeout_s=0.5):
        client = ServiceClient(timeout_s=timeout_s)
        completed = threading.Event()
        results = []
        callback_thread = []

        def callback(result):
            results.append(result)
            callback_thread.append(threading.get_ident())
            completed.set()

        try:
            started = time.perf_counter()
            status = client.submit(
                service.host,
                service.port,
                messages or [{"id": "request-1", "op": "ping"}],
                callback,
            )
            submit_elapsed = time.perf_counter() - started
            self.assertEqual(status, "submitted")
            return client, completed, results, callback_thread, submit_elapsed
        except Exception:
            client.shutdown()
            raise

    def test_partial_json_lines_response_is_delivered_on_calling_thread(self):
        with FakeJsonLinesService([
            lambda request: [
                b'{"id":"',
                request["id"].encode("ascii"),
                b'","ok":true}\n',
            ]
        ]) as service:
            client, completed, results, callback_threads, submit_elapsed = self._submit_and_collect(service)
            try:
                self.assertLess(submit_elapsed, 0.1)
                self.assertTrue(self._poll_until(client, completed))
                caller_thread = threading.get_ident()
                self.assertEqual(callback_threads, [caller_thread])
                self.assertEqual(results[0]["outcomes"][0]["state"], "confirmed_success")
            finally:
                client.shutdown()

    def test_sequence_stops_on_confirmed_failure_and_reports_unattempted_commands(self):
        def response(ok, error=None):
            def make(request):
                result = {"id": request["id"], "ok": ok}
                if error:
                    result["error"] = error
                return result
            return make

        with FakeJsonLinesService([response(True), response(False, "rejected")]) as service:
            client, completed, results, _, _ = self._submit_and_collect(
                service,
                messages=[
                    {"id": "one", "op": "first"},
                    {"id": "two", "op": "second"},
                    {"id": "three", "op": "third"},
                ],
            )
            try:
                self.assertTrue(self._poll_until(client, completed))
                self.assertEqual([request["op"] for request in service.requests], ["first", "second"])
                self.assertEqual(
                    [item["state"] for item in results[0]["outcomes"]],
                    ["confirmed_success", "confirmed_failure", "not_sent"],
                )
            finally:
                client.shutdown()

    def test_timeout_after_send_is_unknown_and_is_not_retried(self):
        with FakeJsonLinesService([lambda _request: time.sleep(0.15)]) as service:
            client, completed, results, _, submit_elapsed = self._submit_and_collect(
                service, timeout_s=0.05
            )
            try:
                self.assertLess(submit_elapsed, 0.1)
                self.assertTrue(self._poll_until(client, completed))
                self.assertEqual(results[0]["outcomes"][0]["state"], "unknown")
                self.assertEqual(len(service.requests), 1)
            finally:
                client.shutdown()

    def test_malformed_and_mismatched_responses_have_unknown_outcomes(self):
        with self.subTest(response="malformed"), FakeJsonLinesService([
            lambda _request: b'{bad json\n'
        ]) as service:
            client, completed, results, _, _ = self._submit_and_collect(service)
            try:
                self.assertTrue(self._poll_until(client, completed))
                self.assertEqual(results[0]["outcomes"][0]["state"], "unknown")
            finally:
                client.shutdown()

        with self.subTest(response="mismatched ID"), FakeJsonLinesService([
            lambda _request: {"id": "another-request", "ok": True}
        ]) as service:
            client, completed, results, _, _ = self._submit_and_collect(service)
            try:
                self.assertTrue(self._poll_until(client, completed))
                self.assertEqual(results[0]["outcomes"][0]["state"], "unknown")
            finally:
                client.shutdown()

    def test_send_error_after_socket_connect_is_unknown(self):
        class BrokenConnection:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def settimeout(self, _timeout):
                return None

            def sendall(self, _data):
                raise OSError("connection failed during send")

        client = ServiceClient(timeout_s=0.1)
        try:
            with patch("gui.service_client.socket.create_connection", return_value=BrokenConnection()):
                outcome = client._send_one("127.0.0.1", 8765, {"id": "request", "op": "mutate"})
            self.assertEqual(outcome["state"], "unknown")
        finally:
            client.shutdown()


if __name__ == "__main__":
    unittest.main()
