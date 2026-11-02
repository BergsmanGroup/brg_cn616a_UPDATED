"""Command-line entry point for the optional Qt GUI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CN616A Qt GUI (disconnected/read-only unless offline replay is selected)"
    )
    parser.add_argument(
        "--offline-replay",
        action="store_true",
        help="Generate a temporary repository-baseline replay; never contacts a service or controller",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from PySide6.QtWidgets import QApplication

        from .offline import OfflineReplaySession
        from .window import QtMainWindow
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
    session = OfflineReplaySession() if args.offline_replay else None
    window = QtMainWindow(session)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
