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

if __package__ in (None, ""):
    py_root = Path(__file__).resolve().parents[1]
    if str(py_root) not in sys.path:
        sys.path.insert(0, str(py_root))
    from gui.display_panels import TelemetryPanel, ConfigPanel, RampSoakPanel
    from gui.chart_panel import ChartPanel
    from gui.state_reader import get_service_config_state
else:
    from .display_panels import TelemetryPanel, ConfigPanel, RampSoakPanel
    from .chart_panel import ChartPanel
    from .state_reader import get_service_config_state


class CN616AGUI:
    """Main GUI application for CN616A display."""
    
    def __init__(self, logs_dir: Path, refresh_interval: float = 2.0, debug: bool = False):
        self.logs_dir = Path(logs_dir)
        self.refresh_interval = refresh_interval
        self.debug = debug
        
        self.root = tk.Tk()
        self.root.title("CN616A Display")
        self.root.geometry("900x700")
        
        self.chart_panel = None  # lazy-loaded
        self.chart_panel_initialized = False
        
        self.panels = []
        self._create_ui()
        self._apply_initial_service_refresh_rate()
        self._start_refresh()
    
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
        
        # Chart tab: placeholder frame (lazy-loaded on first click)
        self.chart_panel_frame = ttk.Frame(notebook)
        notebook.add(self.chart_panel_frame, text="Chart")
        
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
            pass
    
    def _start_refresh(self):
        """Start auto-refresh on all panels."""
        for panel in self.panels:
            if hasattr(panel, 'auto_refresh_interval'):
                panel.auto_refresh_interval = self.refresh_interval
            panel.start_auto_refresh()
            panel.refresh()  # Initial refresh
    
    def _on_notebook_tab_changed(self, event):
        """Lazy-load chart panel when Chart tab is selected."""
        if self.chart_panel_initialized:
            return  # Already loaded
        
        # Check which tab is now active
        selected_tab = event.widget.select()
        tab_text = event.widget.tab(selected_tab, "text")
        
        if tab_text == "Chart":
            self._initialize_chart_panel()
            self.chart_panel_initialized = True
    
    def _initialize_chart_panel(self):
        """Initialize the chart panel inside the pre-created frame."""
        self.chart_panel = ChartPanel(
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
        try:
            gui_refresh_hz = float(cfg.get("gui_refresh_hz", 0) or 0)
        except Exception:
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
        self.root.destroy()
    
    def run(self):
        """Start the GUI."""
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.mainloop()


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
    
    args = parser.parse_args()
    
    if not args.logs_dir.exists():
        print(f"Error: Logs directory not found: {args.logs_dir}")
        sys.exit(1)
    
    gui = CN616AGUI(logs_dir=args.logs_dir, refresh_interval=args.refresh_interval, debug=args.debug)
    gui.run()


if __name__ == "__main__":
    main()
