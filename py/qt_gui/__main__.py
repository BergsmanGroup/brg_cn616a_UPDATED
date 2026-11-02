"""Command-line entry point for the optional Qt GUI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CN616A Qt GUI (disconnected/read-only unless a mode is explicitly selected)"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--offline-replay",
        action="store_true",
        help="Generate a temporary repository-baseline replay; never contacts a service or controller",
    )
    modes.add_argument(
        "--service",
        action="store_true",
        help="Connect to the explicitly supplied existing service endpoint",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="In --service mode, allow live reads but disable and reject all writes",
    )
    parser.add_argument("--host", help="Service host; requires --service")
    parser.add_argument("--port", type=int, help="Service TCP port; requires --service")
    parser.add_argument("--logs-dir", type=Path, help="Service state/log directory; requires --service")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.service and (not args.host or args.port is None or args.logs_dir is None):
        parser.error("--service requires --host, --port, and --logs-dir")
    if args.read_only and not args.service:
        parser.error("--read-only requires --service")
    if args.service and not 1 <= args.port <= 65535:
        parser.error("--port must be in the range 1..65535")
    if not args.service and any(value is not None for value in (args.host, args.port, args.logs_dir)):
        parser.error("--host, --port, and --logs-dir require --service")
    try:
        from PySide6.QtWidgets import QApplication

        from .offline import OfflineReplaySession
        from .window import QtMainWindow
        if args.service:
            from .service_adapter import QtServiceAdapter
    except ModuleNotFoundError as exc:
        if exc.name != "PySide6":
            raise
        print(
            "Qt GUI dependencies are missing. Install them with "
            '".\\.venv\\Scripts\\python.exe -m pip install -r requirements-qt.txt".',
            file=sys.stderr,
        )
        return 2

    app = QApplication.instance() or QApplication(["cn616a-qt-gui"])
    app.setApplicationName("CN616A Qt GUI")
    if args.offline_replay:
        session = OfflineReplaySession()
    elif args.service:
        session = QtServiceAdapter(
            args.host,
            args.port,
            args.logs_dir,
            read_only=args.read_only,
        )
    else:
        session = None
    window = QtMainWindow(session)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
