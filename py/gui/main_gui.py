"""
main_gui.py

Main GUI application for CN616A display (Phase 1: read-only telemetry/config).

Usage:
    python main_gui.py [--logs-dir LOGS_DIR] [--refresh-interval SECONDS]

Examples:
    python main_gui.py
    python main_gui.py --logs-dir ../logs --refresh-interval 3.0
"""

import tkinter as tk
from tkinter import ttk
from pathlib import Path
import argparse
import sys
import os
import logging
from logging.handlers import RotatingFileHandler
import threading
import faulthandler
from datetime import datetime


def _default_logs_dir() -> Path:
    return Path(__file__).parent.parent.parent / "logs"


def _setup_bootstrap_logging(logs_dir: Path, debug: bool = False) -> logging.Logger:
    """Configure early crash logging before GUI initialization completes."""
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("cn616a.gui")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False

    has_file_handler = any(
        isinstance(h, RotatingFileHandler)
        and getattr(h, "baseFilename", "").endswith("cn616a_gui_error.log")
        for h in logger.handlers
    )
    if not has_file_handler:
        handler = RotatingFileHandler(
            logs_dir / "cn616a_gui_error.log",
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logger.addHandler(handler)

    return logger


try:
    if __package__ in (None, ""):
        py_root = Path(__file__).resolve().parents[1]
        if str(py_root) not in sys.path:
            sys.path.insert(0, str(py_root))
        from gui.display_panels import TelemetryPanel, ConfigPanel, RampSoakPanel
        from gui.state_reader import get_service_config_state
    else:
        from .display_panels import TelemetryPanel, ConfigPanel, RampSoakPanel
        from .state_reader import get_service_config_state
except Exception:
    _setup_bootstrap_logging(_default_logs_dir()).exception("Failed to import GUI modules")
    raise


class CN616AGUI:
    """Main GUI application for CN616A display."""
    
    def __init__(
        self,
        logs_dir: Path,
        refresh_interval: float = 2.0,
        debug: bool = False,
        allow_unsafe_chart: bool = False,
    ):
        self.logs_dir = Path(logs_dir)
        self.refresh_interval = refresh_interval
        self.debug = debug
        self.allow_unsafe_chart = bool(allow_unsafe_chart)

        self.logger = logging.getLogger("cn616a.gui")
        self._setup_runtime_logging()
        
        self.root = tk.Tk()
        self.root.title("CN616A Display")
        self.root.geometry("900x700")
        
        self.chart_panel = None  # lazy-loaded
        self.chart_panel_initialized = False
        self.chart_available = False
        self.chart_disabled_reason = self._chart_disabled_reason()
        
        self.panels = []
        self._create_ui()
        self._apply_initial_service_refresh_rate()
        self._start_refresh()

    def _setup_runtime_logging(self):
        """Configure persistent GUI error logging and fault handlers."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        log_level = logging.DEBUG if self.debug else logging.INFO
        self.logger.setLevel(log_level)
        self.logger.propagate = False

        has_file_handler = any(
            isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "").endswith("cn616a_gui_error.log")
            for h in self.logger.handlers
        )
        if not has_file_handler:
            log_path = self.logs_dir / "cn616a_gui_error.log"
            handler = RotatingFileHandler(
                log_path,
                maxBytes=1_000_000,
                backupCount=5,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
            self.logger.addHandler(handler)

        # Keep this file handle alive for process lifetime so low-level crashes are captured.
        # Use line buffering to minimize data loss if the process exits abruptly.
        self._fault_log_fh = open(
            self.logs_dir / "cn616a_gui_fault.log",
            "a",
            encoding="utf-8",
            buffering=1,
        )
        try:
            self._fault_log_fh.write(f"=== GUI fault log session start: {datetime.now().isoformat()} ===\n")
            self._fault_log_fh.flush()
            faulthandler.enable(file=self._fault_log_fh, all_threads=True)
        except Exception:
            self.logger.exception("Failed to enable faulthandler")

        def _log_uncaught(exc_type, exc_value, exc_tb):
            self.logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

        sys.excepthook = _log_uncaught

        def _thread_hook(args):
            self.logger.critical(
                "Unhandled thread exception in %s",
                getattr(args.thread, "name", "<unknown>"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )

        threading.excepthook = _thread_hook
        self.logger.info("GUI runtime logging initialized")

    def _report_callback_exception(self, exc, val, tb):
        """Capture Tk callback exceptions that may otherwise look like silent crashes."""
        self.logger.critical("Tk callback exception", exc_info=(exc, val, tb))
    
    def _create_ui(self):
        """Create notebook with tabs for different views."""
        # Top frame for title
        title_frame = ttk.Frame(self.root)
        title_frame.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(title_frame, text="CN616A Controller Display (Read-Only)", 
                 font=("Arial", 16, "bold")).pack(side=tk.LEFT)
        ttk.Label(title_frame, text="Phase 1", foreground="blue").pack(side=tk.LEFT, padx=10)
        
        # Notebook
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Telemetry tab
        telemetry_panel = TelemetryPanel(notebook, self.logs_dir, debug=self.debug)
        notebook.add(telemetry_panel, text="Telemetry")
        self.panels.append(telemetry_panel)
        
        # Config tab
        config_panel = ConfigPanel(notebook, self.logs_dir, debug=self.debug,
                                    on_viewer_config_changed=self._on_viewer_config_changed,
                                    on_service_config_changed=self._on_service_config_changed)
        notebook.add(config_panel, text="Configuration")
        self.panels.append(config_panel)
        
        # Ramp/Soak tab
        rampsoak_panel = RampSoakPanel(notebook, self.logs_dir, debug=self.debug)
        notebook.add(rampsoak_panel, text="Ramp/Soak")
        self.panels.append(rampsoak_panel)
        
        # Chart tab: placeholder frame (lazy-loaded on first click when supported)
        self.chart_panel_frame = ttk.Frame(notebook)
        notebook.add(self.chart_panel_frame, text="Chart")

        if self.chart_disabled_reason:
            self.chart_available = False
            ttk.Label(
                self.chart_panel_frame,
                text=self.chart_disabled_reason,
                foreground="darkred",
                justify="left",
                wraplength=700,
            ).pack(fill=tk.BOTH, expand=True, padx=16, pady=16)
            self.logger.warning(self.chart_disabled_reason)
        else:
            self.chart_available = True

        # Bind notebook tab change to lazy-load chart
        notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)
        
        # Status bar
        footer_frame = ttk.Frame(self.root)
        footer_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(footer_frame, text=f"Logs dir: {self.logs_dir}", 
                 foreground="gray", font=("Arial", 8)).pack(side=tk.LEFT)
        self.refresh_label = ttk.Label(footer_frame, text=f"Refresh: {self.refresh_interval:.3f}s", 
                           foreground="gray", font=("Arial", 8))
        self.refresh_label.pack(side=tk.RIGHT)

    def _apply_initial_service_refresh_rate(self):
        """Load gui_refresh_hz from service config at startup and apply if valid."""
        try:
            svc_state = get_service_config_state(self.logs_dir)
            cfg = svc_state.get("config", {}) if isinstance(svc_state, dict) else {}
            gui_refresh_hz = float(cfg.get("gui_refresh_hz", 0) or 0)
            if gui_refresh_hz > 0:
                self.refresh_interval = 1.0 / gui_refresh_hz
                if hasattr(self, "refresh_label"):
                    self.refresh_label.config(text=f"Refresh: {self.refresh_interval:.3f}s")
        except Exception:
            self.logger.exception("Failed to apply initial service refresh rate")
    
    def _start_refresh(self):
        """Start auto-refresh on all panels."""
        for panel in self.panels:
            if hasattr(panel, 'auto_refresh_interval'):
                panel.auto_refresh_interval = self.refresh_interval
            panel.start_auto_refresh()
            panel.refresh()  # Initial refresh
    
    def _on_notebook_tab_changed(self, event):
        """Lazy-load chart panel when Chart tab is selected."""
        if not self.chart_available:
            return
        if self.chart_panel_initialized:
            return  # Already loaded
        
        # Check which tab is now active
        selected_tab = event.widget.select()
        tab_text = event.widget.tab(selected_tab, "text")
        
        if tab_text == "Chart":
            self._initialize_chart_panel()
            self.chart_panel_initialized = True
    
    def _chart_disabled_reason(self):
        """Return a message when chart should be disabled for runtime stability."""
        allow_env = str(os.getenv("CN616A_GUI_ALLOW_UNSAFE_CHART", "")).strip().lower() in {"1", "true", "yes", "on"}
        if sys.version_info >= (3, 13) and not (self.allow_unsafe_chart or allow_env):
            return (
                "Chart tab disabled by default on Python 3.13+ due a known TkAgg fatal crash in "
                "Matplotlib. To force-enable (unsafe), start GUI with --allow-unsafe-chart or set "
                "CN616A_GUI_ALLOW_UNSAFE_CHART=1. Recommended stable path: run GUI on Python 3.12."
            )
        return None

    def _get_chart_panel_cls(self):
        """Import chart panel lazily so startup failures are still logged to GUI error log."""
        if __package__ in (None, ""):
            from gui.chart_panel import ChartPanel as _ChartPanel
        else:
            from .chart_panel import ChartPanel as _ChartPanel
        return _ChartPanel

    def _initialize_chart_panel(self):
        """Initialize the chart panel inside the pre-created frame."""
        try:
            chart_panel_cls = self._get_chart_panel_cls()
        except Exception:
            self.logger.exception("Failed importing chart panel")
            return

        self.chart_panel = chart_panel_cls(
            self.chart_panel_frame,
            self.logs_dir,
            refresh_interval=self.refresh_interval,
            debug=False  # avoid debug spam from chart
        )
        self.chart_panel.pack(fill=tk.BOTH, expand=True)
        self.panels.append(self.chart_panel)
    
    def _on_viewer_config_changed(self, cfg):
        """Callback when viewer config is saved. Update chart if it's loaded."""
        if self.chart_panel is not None:
            self.chart_panel.apply_viewer_config(cfg)

    def _on_service_config_changed(self, cfg):
        """Apply GUI refresh rate changes from service config to running panels."""
        if self.chart_panel is not None and hasattr(self.chart_panel, "apply_service_config"):
            self.chart_panel.apply_service_config(cfg)

        try:
            gui_refresh_hz = float(cfg.get("gui_refresh_hz", 0) or 0)
        except Exception:
            self.logger.exception("Invalid gui_refresh_hz in service config: %r", cfg)
            return
        if gui_refresh_hz <= 0:
            return

        new_interval_s = 1.0 / gui_refresh_hz
        self.refresh_interval = new_interval_s

        # Apply immediately by updating intervals and restarting active refresh loops.
        for panel in self.panels:
            if hasattr(panel, "auto_refresh_interval"):
                panel.auto_refresh_interval = new_interval_s
            if hasattr(panel, "refresh_interval"):
                panel.refresh_interval = new_interval_s
            if hasattr(panel, "stop_auto_refresh") and hasattr(panel, "start_auto_refresh"):
                panel.stop_auto_refresh()
                panel.start_auto_refresh()

        if hasattr(self, "refresh_label"):
            self.refresh_label.config(text=f"Refresh: {new_interval_s:.3f}s")
    
    def _on_closing(self):
        """Clean up when window closes."""
        for panel in self.panels:
            panel.stop_auto_refresh()

        try:
            if hasattr(self, "_fault_log_fh") and self._fault_log_fh:
                self._fault_log_fh.flush()
                self._fault_log_fh.close()
        except Exception:
            self.logger.exception("Failed to close fault log handle")
        self.root.destroy()
    
    def run(self):
        """Start the GUI."""
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.report_callback_exception = self._report_callback_exception
        try:
            self.root.mainloop()
        except Exception:
            self.logger.exception("Tk mainloop terminated with exception")
            raise


def main():
    parser = argparse.ArgumentParser(description="CN616A GUI Display")
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=Path(__file__).parent.parent.parent / "logs",
        help="Path to logs directory (default: ../../../logs relative to this script)"
    )
    parser.add_argument(
        "--refresh-interval",
        type=float,
        default=2.0,
        help="Refresh interval in seconds (default: 2.0)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging to console"
    )
    parser.add_argument(
        "--allow-unsafe-chart",
        action="store_true",
        help="Enable chart tab on runtimes known to be unstable (e.g., Python 3.13 + TkAgg)"
    )
    
    args = parser.parse_args()
    bootstrap_logger = _setup_bootstrap_logging(args.logs_dir, debug=args.debug)
    
    if not args.logs_dir.exists():
        print(f"Error: Logs directory not found: {args.logs_dir}")
        sys.exit(1)
    
    try:
        gui = CN616AGUI(
            logs_dir=args.logs_dir,
            refresh_interval=args.refresh_interval,
            debug=args.debug,
            allow_unsafe_chart=args.allow_unsafe_chart,
        )
        gui.run()
    except Exception:
        bootstrap_logger.exception("GUI terminated with unhandled startup/runtime exception")
        raise


if __name__ == "__main__":
    main()
