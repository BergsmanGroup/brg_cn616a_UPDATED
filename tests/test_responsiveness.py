import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import baseline_reference as reference
import gui_responsiveness_baseline as baseline
import responsiveness


class ReplaySerial:
    """Return pinned response bytes in partial reads, without hardware."""

    def __init__(self):
        raw, _ = reference.load_reference()
        transactions = raw["static_transactions"] + raw["samples"][0]["transactions"]
        self.responses = {t["request_hex"]: bytes.fromhex(t["response_hex"]) for t in transactions}
        self.pending = b""
        self.requests = []

    def write(self, data):
        self.requests.append(data)
        self.pending = self.responses[data.hex()]
        return len(data)

    def read(self, size):
        count = min(size, 4)
        result, self.pending = self.pending[:count], self.pending[count:]
        return result


class ResponsivenessTests(unittest.TestCase):
    def test_presets_forward_defaults_and_propagate_failure(self):
        for mode, scope, duration in (("quick", "--quick", "30"), ("full", "--matrix", "60")):
            with self.subTest(mode=mode), patch.object(baseline, "main", return_value=1) as run:
                self.assertEqual(responsiveness.main([mode]), 1)
                flags = run.call_args.args[0]
                self.assertIn(scope, flags)
                self.assertEqual(flags[flags.index("--duration-seconds") + 1], duration)
                self.assertEqual(flags[flags.index("--feedback-samples") + 1], "20")

    def test_full_covers_every_chart_history_and_service_combination(self):
        with patch.object(baseline, "_run_scenario", return_value={}) as scenario:
            report = baseline.run(baseline.build_argument_parser().parse_args(["--matrix", "--duration-seconds", "60"]))
        self.assertEqual(report["scenario_count"], 24)
        combinations = {(c.args[0].chart, c.args[0].log_size, c.args[0].service_mode) for c in scenario.call_args_list}
        self.assertEqual(len(combinations), 24)
        self.assertEqual({c[2] for c in combinations}, {"normal", "delayed", "disconnected", "error"})
        self.assertTrue(all(c.kwargs["duration_seconds"] == 60 for c in scenario.call_args_list))

    def test_tools_are_python_commands_and_test_presets_never_capture(self):
        with patch.object(reference, "capture_reference") as capture, patch.object(baseline, "main", return_value=0) as runner:
            responsiveness.main(["quick"])
            responsiveness.main(["full"])
            responsiveness.main(["verify-data"])
            self.assertEqual(runner.call_args.args[0], ["--verify-data"])
            responsiveness.main(["summarize", "report.json"])
            self.assertEqual(runner.call_args.args[0], ["--summarize", "report.json"])
            capture.assert_not_called()

    def test_alternative_reference_is_used_in_report_and_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "new-reference"
            reference.build_reference(directory)
            manifest_path = directory / reference.MANIFEST_FILE
            manifest = json.loads(manifest_path.read_text())
            manifest["provenance"] = "test corpus marker"
            manifest_path.write_text(json.dumps(manifest))
            args = baseline.build_argument_parser().parse_args(["--quick", "--reference-dir", str(directory)])
            with patch.object(baseline, "_run_scenario", return_value={}) as scenario:
                report = baseline.run(args)
            self.assertEqual(report["reference"]["provenance"], "test corpus marker")
            self.assertTrue(all(c.kwargs["reference_dir"] == directory for c in scenario.call_args_list))
            fixture = baseline.SyntheticLogWriter(
                Path(temporary) / "logs", sample_rate_hz=2, duration_seconds=1,
                service_port=12345, reference_dir=directory,
            )
            self.assertEqual(fixture.reference.manifest["provenance"], "test corpus marker")

    def test_capture_requires_acknowledgement_before_any_serial_access(self):
        with tempfile.TemporaryDirectory() as temporary, patch("serial.Serial") as serial:
            with self.assertRaisesRegex(ValueError, "controller-offline"):
                reference.capture_reference(Path(temporary) / "v2", port="COM_TEST", controller_offline=False)
            serial.assert_not_called()

    def test_existing_destination_and_invalid_parameters_fail_before_serial_access(self):
        with tempfile.TemporaryDirectory() as temporary, patch("serial.Serial") as serial:
            with self.assertRaises(FileExistsError):
                reference.capture_reference(Path(temporary), port="COM_TEST", controller_offline=True)
            for options in ({"duration_seconds": float("nan")}, {"timeout_seconds": 0}, {"baudrate": -1}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    reference.capture_reference(Path(temporary) / "v2", port="COM_TEST", controller_offline=True, **options)
            serial.assert_not_called()

    def test_raw_capture_saves_actual_response_bytes_and_can_be_replayed(self):
        stream = ReplaySerial()
        with tempfile.TemporaryDirectory() as temporary, patch("serial.Serial") as serial, contextlib.redirect_stdout(io.StringIO()):
            serial.return_value.__enter__.return_value = stream
            directory = Path(temporary) / "v2"
            self.assertEqual(responsiveness.main([
                "capture", "--port", "COM_TEST", "--controller-offline",
                "--duration-seconds", "0.01", "--output-dir", str(directory),
            ]), 0)
            raw, manifest = reference.load_reference(directory)
            self.assertIn("physical", manifest["provenance"])
            self.assertEqual(len(raw["samples"]), 1)
            for transaction in raw["static_transactions"] + raw["samples"][0]["transactions"]:
                self.assertEqual(bytes.fromhex(transaction["response_hex"]), stream.responses[transaction["request_hex"]])
            decoded = reference.adapt_reference(directory)
            self.assertEqual(decoded.telemetry[0]["zones"]["1"]["sp_abs"], 71)
            serial.assert_called_once_with(
                port="COM_TEST", baudrate=115200, bytesize=8, parity="N", stopbits=1,
                timeout=2.0, write_timeout=2.0, exclusive=True,
            )
            self.assertTrue(all(request[1] == 3 for request in stream.requests))

    def test_capture_rejects_write_requests_before_sending_any_bytes(self):
        stream = ReplaySerial()
        request = reference._with_crc(bytes.fromhex("010600010000"))
        with self.assertRaisesRegex(ValueError, "FC03"):
            reference.capture_transaction(stream, request.hex())
        self.assertEqual(stream.requests, [])

    def test_capture_timeout_and_corrupt_crc_are_explicit_and_leave_no_corpus(self):
        for corrupt in (False, True):
            stream = ReplaySerial()
            if corrupt:
                stream.responses = {key: value[:-1] + bytes([value[-1] ^ 1]) for key, value in stream.responses.items()}
            else:
                stream.responses = {key: b"" for key in stream.responses}
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temporary, patch("serial.Serial") as serial:
                serial.return_value.__enter__.return_value = stream
                directory = Path(temporary) / "v2"
                with self.assertRaises((TimeoutError, ValueError)):
                    reference.capture_reference(directory, port="COM_TEST", controller_offline=True)
                self.assertFalse(directory.exists())
                serial.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
