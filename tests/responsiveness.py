"""Public quick/full test presets and explicitly opt-in raw capture tools."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

import baseline_reference
import gui_responsiveness_baseline as baseline


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated CN616A responsiveness tests and reference-data tools.")
    commands = parser.add_subparsers(dest="command", required=True)
    for mode, description in (
        ("quick", "Two 30-second chart-open sessions: normal and delayed fake service."),
        ("full", "24 60-second sessions: all chart/history states and four fake-service modes (~25 minutes)."),
    ):
        command = commands.add_parser(mode, help=description, description=description)
        command.add_argument("--reference-dir", type=Path, default=baseline_reference.REFERENCE_DIR)
        command.add_argument("--output", type=Path, help="Save the JSON report here instead of OS temporary storage.")
    commands.add_parser("verify-data", help="Verify the bundled synthetic input and measure generated data sizes.")
    summary = commands.add_parser("summarize", help="Print metrics from a saved JSON report.")
    summary.add_argument("report", type=Path)
    capture = commands.add_parser(
        "capture",
        help="Read raw registers from an exclusively available controller (NOT a running service).",
        description="Requires a separate controller or a manually stopped service. Never stops a service or writes controller settings.",
    )
    capture.add_argument("--port", required=True, help="Explicit serial port; never inferred from live settings.")
    capture.add_argument("--output-dir", required=True, type=Path, help="New version directory; never overwrite v1.")
    capture.add_argument("--controller-offline", action="store_true", help="Confirm no service or other program owns this controller.")
    capture.add_argument("--duration-seconds", type=float, default=60.0)
    capture.add_argument("--baudrate", type=int, default=115200, help="Default 115200, 8N1; must match the controller.")
    capture.add_argument("--timeout-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    if args.command == "capture":
        try:
            manifest = baseline_reference.capture_reference(
                args.output_dir, port=args.port, controller_offline=args.controller_offline,
                duration_seconds=args.duration_seconds, baudrate=args.baudrate,
                timeout_seconds=args.timeout_seconds,
            )
        except (OSError, ValueError, RuntimeError, ImportError) as exc:
            parser.exit(1, f"Raw capture failed: {exc}\n")
        print(json.dumps(manifest, indent=2))
        print(f"Raw reference saved to {args.output_dir.resolve()}; live hardware timing is not replayed by the GUI test.")
        return 0
    if args.command == "verify-data":
        return baseline.main(["--verify-data"])
    if args.command == "summarize":
        return baseline.main(["--summarize", str(args.report)])
    options = [
        "--quick" if args.command == "quick" else "--matrix",
        "--duration-seconds", "30" if args.command == "quick" else "60",
        "--feedback-samples", "20", "--reference-dir", str(args.reference_dir),
    ]
    if args.output:
        options.extend(["--output", str(args.output)])
    return baseline.main(options)


if __name__ == "__main__":
    raise SystemExit(main())
