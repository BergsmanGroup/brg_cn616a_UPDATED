import copy
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from gui_responsiveness_baseline import (  # noqa: E402
    LogReadInstrumentation,
    LoopbackJsonlService,
    RunMetrics,
    SyntheticLogWriter,
    _peak_working_set_bytes,
    build_argument_parser,
    format_performance_summary,
    main,
    run,
    summarize,
    verify_data,
)
import baseline_reference as reference


class GuiResponsivenessBaselineTests(unittest.TestCase):
    def test_summary_uses_nearest_rank_percentiles(self):
        summary = summarize([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(summary["sample_count"], 10)
        self.assertEqual(summary["p50"], 5.0)
        self.assertEqual(summary["p95"], 10.0)
        self.assertEqual(summary["p99"], 10.0)
        self.assertEqual(summary["max"], 10.0)

    def test_empty_summary_has_no_fabricated_values(self):
        summary = summarize([])
        self.assertEqual(summary["sample_count"], 0)
        self.assertIsNone(summary["p50"])
        self.assertIsNone(summary["p95"])

    def test_peak_working_set_measurement_reports_platform_status(self):
        memory_bytes, error = _peak_working_set_bytes()
        if os.name == "nt":
            self.assertIsNone(error)
            self.assertGreater(memory_bytes, 0)
        else:
            self.assertIsNone(memory_bytes)
            self.assertIn("unavailable", error)

    def test_fixture_generates_six_zone_state_and_rotated_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir) / "isolated"
            fixture = SyntheticLogWriter(
                logs_dir,
                sample_rate_hz=2.0,
                duration_seconds=1800.0,
                service_port=12345,
            )
            state = json.loads(
                (logs_dir / "cn616a_telemetry_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["zones"], [1, 2, 3, 4, 5, 6])
            self.assertEqual(set(state["telemetry"]["zones"]), {"1", "2", "3", "4", "5", "6"})
            self.assertEqual(fixture.generated_records, 3600)
            self.assertGreaterEqual(fixture.file_inventory()["rotated_files"], 1)
            config = json.loads(
                (logs_dir / "cn616a_service_config_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(config["config"]["last_tcp_host"], "127.0.0.1")
            self.assertEqual(config["config"]["last_tcp_port"], 12345)

    def test_fake_service_echoes_request_id_and_never_uses_non_loopback(self):
        service = LoopbackJsonlService("normal")
        service.start()
        try:
            self.assertEqual(service.host, "127.0.0.1")
            with socket.create_connection((service.host, service.port), timeout=1.0) as client:
                client.sendall(b'{"id":"test-id","op":"get_run_status"}\n')
                response = json.loads(client.makefile("rb").readline().decode("utf-8"))
            self.assertEqual(response["id"], "test-id")
            self.assertTrue(response["ok"])
            self.assertFalse(response["run_active"])
            self.assertEqual(service.snapshot()["operations"], ["get_run_status"])
        finally:
            service.close()

    def test_fake_service_delay_and_error_modes_are_synthetic(self):
        service = LoopbackJsonlService("delayed", delay_seconds=0.03)
        service.start()
        try:
            started = time.perf_counter()
            with socket.create_connection((service.host, service.port), timeout=1.0) as client:
                client.sendall(b'{"id":"delay-id","op":"ping"}\n')
                response = json.loads(client.makefile("rb").readline().decode("utf-8"))
            self.assertTrue(response["ok"])
            self.assertGreaterEqual(time.perf_counter() - started, 0.02)
        finally:
            service.close()

        service = LoopbackJsonlService("error")
        service.start()
        try:
            with socket.create_connection((service.host, service.port), timeout=1.0) as client:
                client.sendall(b'{"id":"error-id","op":"ping"}\n')
                response = json.loads(client.makefile("rb").readline().decode("utf-8"))
            self.assertFalse(response["ok"])
            self.assertIn("Synthetic", response["error"])
        finally:
            service.close()

    def test_copying_logs_preserves_source_and_uses_a_separate_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            destination = Path(temp_dir) / "destination"
            source.mkdir()
            source_file = source / "cn616a_telemetry_log.jsonl"
            source_contents = '{"telemetry":{}}\n'
            source_file.write_text(source_contents, encoding="utf-8")

            fixture = SyntheticLogWriter(
                destination,
                sample_rate_hz=1.0,
                duration_seconds=2.0,
                service_port=12345,
                copy_logs_from=source,
            )
            copied = list(destination.glob("*source_copy*.jsonl"))
            self.assertEqual(len(copied), 1)
            self.assertEqual(copied[0].read_text(encoding="utf-8"), source_contents)
            self.assertEqual(source_file.read_text(encoding="utf-8"), source_contents)
            self.assertEqual(fixture.copied_files, ["cn616a_telemetry_log.jsonl"])

    def test_log_read_instrumentation_counts_real_reads_under_fixture_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir) / "logs"
            logs_dir.mkdir()
            log_path = logs_dir / "cn616a_telemetry_log.jsonl"
            content = '{"ts":"one"}\n{"ts":"two"}\n'
            log_path.write_text(content, encoding="utf-8")
            outside_path = Path(temp_dir) / "outside.jsonl"
            outside_path.write_text(content, encoding="utf-8")
            metrics = RunMetrics()
            callback_context = threading.local()
            callback_context.name = "unit-test"

            with LogReadInstrumentation(logs_dir, metrics, callback_context):
                with log_path.open("r", encoding="utf-8") as stream:
                    self.assertEqual(stream.readlines(), content.splitlines(keepends=True))
                outside_path.read_text(encoding="utf-8")

            self.assertEqual(
                metrics.log_reads,
                {"unit-test": {"bytes": len(content.encode("utf-8")), "lines": 2}},
            )


class RawBaselineReferenceTests(unittest.TestCase):
    def test_v1_is_small_hash_pinned_and_contains_wire_frames_not_gui_json(self):
        raw, manifest = reference.load_reference()
        self.assertEqual(
            manifest["content_sha256"],
            "5343599678e20d26e2b330a5288222fc183d7e2b3dd7016e1c882852d647c5eb",
        )
        self.assertEqual(manifest["archive_bytes"], 3823)
        self.assertEqual(manifest["uncompressed_bytes"], 33272)
        self.assertEqual(len(raw["samples"]), 16)
        self.assertEqual(raw["samples"][-1]["elapsed_ms"], 7500)
        self.assertIn("not captured", raw["provenance"])
        self.assertIn("register_map", raw)
        transaction = raw["samples"][0]["transactions"][0]
        self.assertEqual(set(transaction), {"request_hex", "response_hex"})
        registers = reference.transaction_registers(transaction)
        self.assertEqual(len(registers), 12)
        self.assertEqual(registers[0x0100], 0)
        self.assertEqual(registers[0x0101], 0)

    def test_reference_regeneration_is_deterministic_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            first_manifest = reference.build_reference(first)
            second_manifest = reference.build_reference(second)
            self.assertEqual(first_manifest, second_manifest)
            self.assertEqual(first_manifest["content_sha256"], reference.load_reference()[1]["content_sha256"])
            self.assertEqual(
                (first / reference.REFERENCE_FILE).read_bytes(),
                (second / reference.REFERENCE_FILE).read_bytes(),
            )
            with self.assertRaises(FileExistsError):
                reference.build_reference(first)

    def test_corrupted_archive_or_content_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "reference"
            shutil.copytree(reference.REFERENCE_DIR, directory)
            archive = directory / reference.REFERENCE_FILE
            original = archive.read_bytes()
            archive.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            with self.assertRaisesRegex(ValueError, "Compressed reference"):
                reference.load_reference(directory)
            archive.write_bytes(original)
            manifest_path = directory / reference.MANIFEST_FILE
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["content_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "content size or SHA"):
                reference.load_reference(directory)

    def test_corrupted_crc_and_invalid_frame_lengths_are_rejected(self):
        raw, _ = reference.load_reference()
        transaction = copy.deepcopy(raw["samples"][0]["transactions"][0])
        response = bytearray.fromhex(transaction["response_hex"])
        response[3] ^= 1
        transaction["response_hex"] = response.hex()
        with self.assertRaisesRegex(ValueError, "CRC"):
            reference.transaction_registers(transaction)
        transaction["request_hex"] = "0103"
        with self.assertRaisesRegex(ValueError, "frame length"):
            reference.transaction_registers(transaction)

    def test_offline_driver_adapter_never_constructs_a_serial_client(self):
        sys.path.insert(0, str(REPO_ROOT / "py"))
        with patch("cn616a.ModbusSerialClient", side_effect=AssertionError("Serial access forbidden")) as serial_client:
            decoded = reference.adapt_reference()
        serial_client.assert_not_called()
        zone = decoded.telemetry[0]["zones"]["1"]
        self.assertEqual(zone["pv_c"], 0.0)
        self.assertEqual(zone["sp_abs"], 71.0)
        self.assertNotIn("sp_abs_c", zone)
        self.assertEqual(set(decoded.config), {"system", "zones"})
        self.assertEqual(set(decoded.config["zones"]), {"1", "2", "3", "4", "5", "6"})
        segments = decoded.rampsoak["zones"]["1"]["segments"]
        self.assertEqual(len(segments), 20)
        self.assertEqual(segments["1"]["sp_c"], 71.0)

    def test_new_driver_register_reads_fail_even_if_driver_swallows_read_error(self):
        sys.path.insert(0, str(REPO_ROOT / "py"))
        from cn616a import CN616A

        original = CN616A.read_telemetry

        def changed_read(controller, zones):
            try:
                controller.read_u16_addr(0xFFFF)
            except ValueError:
                pass
            return original(controller, zones)

        with patch.object(CN616A, "read_telemetry", changed_read):
            with self.assertRaisesRegex(ValueError, "uncaptured registers"):
                reference.adapt_reference()

    def test_fixed_epoch_workload_is_repeatable_and_uses_driver_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixtures = [
                SyntheticLogWriter(
                    Path(temporary) / name, sample_rate_hz=2.0,
                    duration_seconds=2.0, service_port=12345,
                    end_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                )
                for name in ("first", "second")
            ]
            for path in fixtures[0].logs_dir.glob("*.jsonl"):
                self.assertEqual(path.read_bytes(), (fixtures[1].logs_dir / path.name).read_bytes())
            config = json.loads((fixtures[0].logs_dir / "cn616a_config_state.json").read_text())
            self.assertIn("system", config["config"])
            ramp = json.loads((fixtures[0].logs_dir / "cn616a_rampsoak_state.json").read_text())
            self.assertEqual(len(ramp["rampsoak"]["zones"]["6"]["segments"]), 20)

    def test_full_workload_quantity_and_compression_without_gui_or_network(self):
        with patch("tkinter.Tk", side_effect=AssertionError("Tk root forbidden")), patch(
            "socket.socket", side_effect=AssertionError("Network forbidden")
        ):
            report = verify_data()
        inventory = report["expanded_jsonl"]
        self.assertEqual(inventory["generated_telemetry_records"], 7200)
        self.assertEqual(inventory["generated_analysis_records"], 7200)
        self.assertEqual(inventory["lines"], 14400)
        self.assertEqual(inventory["files"], 8)
        self.assertEqual(inventory["rotated_files"], 6)
        self.assertLess(inventory["bytes"], 25_000_000)
        self.assertLess(report["expanded_jsonl_gzip_bytes"], 1_000_000)
        self.assertGreater(report["compression_ratio"], 20)
        self.assertFalse(report["gui_started"])
        self.assertFalse(report["network_started"])


class BaselinePerformanceCliTests(unittest.TestCase):
    @staticmethod
    def report():
        return {"results": [{
            "scenario": {"chart": "open", "log_size": "large", "service_mode": "normal"},
            "event_loop_heartbeat_delay_ms": summarize([10.0] * 120),
            "command_feedback_latency_ms": summarize([12.0] * 20),
            "gui_actions": {"chart_initialized": True},
            "scheduled_callback_duration_ms": {"top_slowest_by_p95": [
                {"callback": "_run_gui_scenario.finish", "p95": 1000.0},
                {"callback": "ConfigPanel._refresh_tick", "p95": 30.0},
            ]},
            "jsonl_reads": {"total_bytes_read": 2000},
            "log_inventory_initial": {"bytes": 100},
            "startup_ms": 200,
            "process_cpu_seconds": 1.5,
            "peak_working_set_bytes": 1048576,
            "errors": [],
        }]}

    def test_refined_metrics_highlight_targets_callbacks_and_read_amplification(self):
        output = format_performance_summary(self.report())
        self.assertIn("WITHIN TARGET", output)
        self.assertIn("p95 10.0 ms", output)
        self.assertIn("n=20", output)
        self.assertIn("20.0x fixture-size amplification", output)
        self.assertIn("ConfigPanel._refresh_tick p95 30.0 ms", output)
        self.assertNotIn("_run_gui_scenario.finish", output)

    def test_slow_feedback_is_marked_as_a_missed_target(self):
        report = self.report()
        report["results"][0]["command_feedback_latency_ms"] = summarize([151.0] * 20)
        output = format_performance_summary(report)
        self.assertIn("TARGET MISSED", output)
        self.assertIn("target exceeded for button feedback", output)
        self.assertNotIn("WITHIN TARGET", output)

    def test_missing_chart_or_metrics_and_errors_never_appear_successful(self):
        for mutation in (
            {"gui_actions": {"chart_initialized": False}},
            {"event_loop_heartbeat_delay_ms": summarize([])},
            {"command_feedback_latency_ms": summarize([])},
            {"errors": ["Synthetic failure"]},
        ):
            with self.subTest(mutation=mutation):
                report = self.report()
                report["results"][0].update(mutation)
                output = format_performance_summary(report)
                self.assertNotIn("WITHIN TARGET", output)
                self.assertTrue("INCOMPLETE" in output or "ERROR" in output)

    def test_legacy_three_click_report_is_explicitly_limited(self):
        report = self.report()
        result = report["results"][0]
        del result["command_feedback_latency_ms"]
        result["gui_actions"]["command_feedback_samples_ms"] = [5, 10, 15]
        output = format_performance_summary(report)
        self.assertIn("LIMITED SAMPLES", output)
        self.assertIn("n=3", output)
        self.assertIn("Legacy report", output)

    def test_quick_profile_wires_duration_samples_and_scenarios_without_gui(self):
        arguments = build_argument_parser().parse_args(["--quick"])
        with patch("gui_responsiveness_baseline._run_scenario", return_value={}) as scenario:
            report = run(arguments)
        self.assertEqual(report["scenario_count"], 2)
        self.assertEqual([call.args[0].service_mode for call in scenario.call_args_list], ["normal", "delayed"])
        for call in scenario.call_args_list:
            self.assertEqual(call.args[0].chart, "open")
            self.assertEqual(call.kwargs["duration_seconds"], 30.0)
            self.assertEqual(call.kwargs["feedback_samples"], 20)
        self.assertEqual(report["reference"]["archive_bytes"], 3823)

    def test_cli_summary_is_dependency_free_and_does_not_create_tk(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(self.report()), encoding="utf-8")
            command = subprocess.run(
                [sys.executable, "-S", str(REPO_ROOT / "tests" / "gui_responsiveness_baseline.py"),
                 "--summarize", str(path)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(command.returncode, 0, command.stderr)
            self.assertIn("20.0x fixture-size amplification", command.stdout)

    def test_data_cli_writes_report_without_gui_or_network(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()) as output:
            path = Path(temporary) / "data.json"
            with patch("tkinter.Tk", side_effect=AssertionError("Tk forbidden")), patch(
                "socket.socket", side_effect=AssertionError("Network forbidden")
            ):
                self.assertEqual(main(["--verify-data", "--hours", "0.001", "--output", str(path)]), 0)
            report = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("no GUI or network", output.getvalue())
        self.assertEqual(report["expanded_jsonl"]["generated_telemetry_records"], 8)

    def test_invalid_numeric_options_fail_before_starting_scenario(self):
        for flags in (["--duration-seconds", "2"], ["--feedback-samples", "0"], ["--hours", "nan"]):
            with self.subTest(flags=flags), patch("gui_responsiveness_baseline._run_scenario") as scenario:
                with self.assertRaises(ValueError):
                    run(build_argument_parser().parse_args(flags))
                scenario.assert_not_called()


if __name__ == "__main__":
    unittest.main()
