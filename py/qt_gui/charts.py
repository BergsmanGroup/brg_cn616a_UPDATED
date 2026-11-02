"""Embedded and floating Qt history charts backed by adapter-owned snapshots."""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .adapter import AdapterHistory, AdapterSnapshot, ControllerIdentity, ZoneKey


_MAX_PLOT_POINTS = 20_000
_WINDOW_OPTIONS = (
    (1.0 / 12.0, "5 min"),
    (0.25, "15 min"),
    (1.0, "1 hour"),
    (4.0, "4 hours"),
    (24.0, "24 hours"),
)
def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _zone_data(values: object, zone_id: int) -> Mapping[str, object]:
    mapping = _mapping(values)
    return _mapping(mapping.get(zone_id, mapping.get(str(zone_id))))


def _valid_hours(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    return hours if math.isfinite(hours) and hours > 0 else None


def _valid_width(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        width = float(value)
    except (TypeError, ValueError):
        return None
    return width if math.isfinite(width) and 0 < width <= 10 else None


def _number_text(value: object, decimals: int, unit: str = "") -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return f"{numeric:.{decimals}f}{unit}"


def _duration_text(hours: float) -> str:
    seconds = max(1, int(round(hours * 3600)))
    if seconds < 60:
        return f"{seconds} sec"
    if seconds < 3600:
        return f"{seconds / 60:g} min"
    return f"{hours:g} hr"


def _color(value: object, default: str) -> str:
    candidate = str(value) if value is not None else default
    return candidate if QColor(candidate).isValid() else default


def _viewer_preferences(snapshot: AdapterSnapshot) -> dict[str, object]:
    data = _mapping(snapshot.data)
    config = _mapping(data.get("config", data))
    viewer = _mapping(config.get("viewer"))
    if viewer:
        return dict(viewer)
    return {
        "history_hours": config.get("viewer_history_hours", 1.0),
        "line_width": config.get("viewer_line_width", 2.5),
        "pv_color": config.get("viewer_pv_color", "blue"),
        "sp_color": config.get("viewer_sp_color", "red"),
        "sp_autotune_color": config.get("viewer_sp_autotune_color", "purple"),
        "show_sp_abs": config.get("viewer_show_sp_abs", True),
        "show_sp_autotune": config.get("viewer_show_sp_autotune", True),
        "show_mae": config.get("viewer_show_mae", True),
    }


def _series_arrays(
    times: Sequence[datetime],
    values: Sequence[object],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    count = min(len(times), len(values))
    if count > _MAX_PLOT_POINTS:
        selected = np.linspace(0, count - 1, _MAX_PLOT_POINTS, dtype=np.int64)
        times = [times[int(index)] for index in selected]
        values = [values[int(index)] for index in selected]
        count = _MAX_PLOT_POINTS

    x_values: list[float] = []
    y_values: list[float] = []
    for timestamp, value in zip(times[:count], values[:count]):
        x_values.append(timestamp.timestamp())
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            y_values.append(float("nan"))
        else:
            numeric = float(value)
            y_values.append(numeric if math.isfinite(numeric) else float("nan"))

    connections = np.ones(count, dtype=bool)
    if count > 1:
        positive_deltas = [
            x_values[index] - x_values[index - 1]
            for index in range(1, count)
            if x_values[index] > x_values[index - 1]
        ]
        gap_threshold = (
            max(5.0, 2.5 * float(statistics.median_low(positive_deltas)))
            if positive_deltas
            else float("inf")
        )
        for index in range(1, count):
            delta = x_values[index] - x_values[index - 1]
            if (
                delta > gap_threshold
                or not math.isfinite(y_values[index - 1])
                or not math.isfinite(y_values[index])
            ):
                connections[index - 1] = False

    return (
        np.asarray(x_values, dtype=float),
        np.asarray(y_values, dtype=float),
        connections,
    )


def _prepare_chart_series(
    history_data: Mapping[str, object],
    zone_id: int,
) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    telemetry = _zone_data(history_data.get("telemetry"), zone_id)
    analysis = _zone_data(history_data.get("analysis"), zone_id)
    telemetry_times = telemetry.get("times")
    analysis_times = analysis.get("times")
    times = telemetry_times if isinstance(telemetry_times, Sequence) else ()
    mae_times = analysis_times if isinstance(analysis_times, Sequence) else ()
    series: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for name, values in (
        ("pv", telemetry.get("pv", ())),
        ("sp", telemetry.get("sp", ())),
        ("autotune", telemetry.get("sp_autotune", ())),
    ):
        numeric_values = values if isinstance(values, Sequence) else ()
        series[name] = _series_arrays(times, numeric_values)
    mae_values = analysis.get("mae", ())
    series["mae"] = _series_arrays(
        mae_times,
        mae_values if isinstance(mae_values, Sequence) else (),
    )
    return series


class ChartWidget(QWidget):
    history_window_requested = Signal(float)
    float_requested = Signal()

    def __init__(self, *, allow_float: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._allow_float = allow_float
        self._history: AdapterHistory | None = None
        self._selection: tuple[int, ZoneKey] | None = None
        self._analysis_snapshot: AdapterSnapshot | None = None
        self._prepared_series: Mapping[str, tuple[np.ndarray, np.ndarray, np.ndarray]] | None = None
        self._transform_error: str | None = None
        self._service_config: AdapterSnapshot | None = None
        self._applied_service_config: AdapterSnapshot | None = None
        self._zone_names = {zone_id: f"Zone {zone_id}" for zone_id in range(1, 7)}
        self._history_hours = 1.0
        self._preferences_warning: str | None = None
        self._line_width = 2.5
        self._colors = {
            "pv": "blue",
            "sp": "red",
            "autotune": "purple",
        }

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        controls = QHBoxLayout()
        self.zone_label = QLabel("Select a zone in Overview")
        self.zone_label.setObjectName("chartZone")
        controls.addWidget(self.zone_label, 1)
        controls.addWidget(QLabel("Window"))
        self.window_combo = QComboBox()
        self.window_combo.setObjectName("chartWindow")
        for hours, label in _WINDOW_OPTIONS:
            self.window_combo.addItem(label, hours)
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        controls.addWidget(self.window_combo)
        self.show_sp = QCheckBox("SP")
        self.show_autotune = QCheckBox("Autotune SP")
        self.show_mae = QCheckBox("MAE")
        for checkbox in (self.show_sp, self.show_autotune, self.show_mae):
            checkbox.setChecked(True)
            checkbox.toggled.connect(self._apply_visibility)
            controls.addWidget(checkbox)
        self.clear_view_button = QPushButton("Clear view")
        self.clear_view_button.setToolTip("Reset the visible time range; stored history is unchanged.")
        self.clear_view_button.clicked.connect(self.reset_view)
        controls.addWidget(self.clear_view_button)
        self.home_button = QPushButton("Home")
        self.home_button.setToolTip("Fit all currently loaded points.")
        self.home_button.clicked.connect(self.home_view)
        controls.addWidget(self.home_button)
        self._home_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Home), self)
        self._home_shortcut.activated.connect(self.home_view)
        self.float_button: QPushButton | None = None
        if allow_float:
            self.float_button = QPushButton("Float")
            self.float_button.clicked.connect(self.float_requested)
            controls.addWidget(self.float_button)
        layout.addLayout(controls)

        self.plot_widget = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self.plot_widget.setObjectName("historyPlot")
        self.plot_widget.setBackground("w")
        plot = self.plot_widget.getPlotItem()
        plot.setLabel("left", "Temperature / error", units="°C")
        plot.setLabel("bottom", "Time (local)")
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.addLegend(offset=(8, 8))
        self.pv_curve = plot.plot(name="PV", pen=pg.mkPen(self._colors["pv"], width=self._line_width))
        self.sp_curve = plot.plot(name="Absolute SP", pen=pg.mkPen(self._colors["sp"], width=self._line_width))
        self.autotune_curve = plot.plot(
            name="Autotune SP",
            pen=pg.mkPen(self._colors["autotune"], width=self._line_width, style=Qt.PenStyle.DashLine),
        )
        self.mae_curve = plot.plot(
            name="MAE",
            pen=pg.mkPen("#16837b", width=self._line_width, style=Qt.PenStyle.DotLine),
        )
        layout.addWidget(self.plot_widget, 1)
        self.metrics_label = QLabel("Current analysis metrics: N/A")
        self.metrics_label.setObjectName("chartMetrics")
        layout.addWidget(self.metrics_label)
        self.status_label = QLabel("Waiting for history")
        self.status_label.setObjectName("chartStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.setMinimumHeight(235)

    def set_selection(self, zone_id: int | None, key: ZoneKey | None) -> None:
        self._update_selection(zone_id, key)
        self._prepare_current_history()
        self._render_history()

    def set_chart_state(
        self,
        history: AdapterHistory | None,
        service_config: AdapterSnapshot | None,
        analysis_snapshot: AdapterSnapshot | None,
        selection: tuple[int, ZoneKey] | None,
        prepared_series: Mapping[str, tuple[np.ndarray, np.ndarray, np.ndarray]] | None,
        transform_error: str | None = None,
    ) -> None:
        self._history = history
        self._service_config = service_config
        self._analysis_snapshot = analysis_snapshot
        self._prepared_series = prepared_series
        self._transform_error = transform_error
        self._update_selection(
            selection[0] if selection is not None else None,
            selection[1] if selection is not None else None,
        )
        self._apply_current_service_config()
        self._render_history()

    def _update_selection(self, zone_id: int | None, key: ZoneKey | None) -> None:
        if zone_id is None or key is None:
            self._selection = None
            self.zone_label.setText("Select a zone in Overview")
        elif not 1 <= zone_id <= 6 or key.zone_id != zone_id:
            self._selection = None
            self.zone_label.setText("Zone selection is unavailable")
        else:
            self._selection = (zone_id, key)
            self.zone_label.setText(
                f"Zone {zone_id} · {self._zone_names.get(zone_id, f'Zone {zone_id}')}"
            )

    def set_history(self, history: AdapterHistory | None) -> None:
        self._history = history
        self._prepare_current_history()
        self._apply_current_service_config()
        self._render_history()

    def set_service_config(self, snapshot: AdapterSnapshot) -> None:
        self._service_config = snapshot
        self._apply_current_service_config()
        self._render_history()

    def set_history_hours(self, hours: float) -> None:
        self._history_hours = hours
        self._set_window_combo(hours)

    def set_preferences(self, preferences: Mapping[str, object]) -> tuple[float | None, str | None]:
        warnings: list[str] = []
        hours = _valid_hours(preferences.get("history_hours", self._history_hours))
        if hours is None:
            warnings.append("invalid history window")
        else:
            self.set_history_hours(hours)
        width = _valid_width(preferences.get("line_width", self._line_width))
        if width is None:
            warnings.append("invalid line width")
        else:
            self._line_width = width
        for key, preference in (
            ("pv", "pv_color"),
            ("sp", "sp_color"),
            ("autotune", "sp_autotune_color"),
        ):
            color = _color(preferences.get(preference, self._colors[key]), self._colors[key])
            if preferences.get(preference) is not None and color != str(preferences[preference]):
                warnings.append(f"invalid {preference}")
            self._colors[key] = color

        for checkbox, key in (
            (self.show_sp, "show_sp_abs"),
            (self.show_autotune, "show_sp_autotune"),
            (self.show_mae, "show_mae"),
        ):
            if key in preferences:
                checkbox.setChecked(bool(preferences[key]))
        self._apply_pens()
        self._apply_visibility()
        self._render_history()
        return hours, "; ".join(dict.fromkeys(warnings)) or None

    def set_zone_names(self, names: Mapping[int, str]) -> None:
        self._zone_names.update(names)
        if self._selection is not None:
            zone_id, _key = self._selection
            self.zone_label.setText(f"Zone {zone_id} · {self._zone_names[zone_id]}")

    def _apply_current_service_config(self) -> None:
        snapshot = self._service_config
        history = self._history
        if (
            snapshot is None
            or snapshot.data is None
            or snapshot.identity is None
            or history is None
            or history.identity is None
            or snapshot.identity != history.identity
        ):
            return
        if snapshot is self._applied_service_config:
            return
        self._applied_service_config = snapshot
        data = _mapping(snapshot.data)
        config = _mapping(data.get("config", data))
        raw_names = _mapping(config.get("zone_names"))
        names: dict[int, str] = {}
        for zone_id in range(1, 7):
            value = raw_names.get(zone_id, raw_names.get(str(zone_id)))
            if isinstance(value, str) and value.strip():
                names[zone_id] = value.strip()
        if names:
            self.set_zone_names(names)
        _hours, self._preferences_warning = self.set_preferences(_viewer_preferences(snapshot))

    def _prepare_current_history(self) -> None:
        self._prepared_series = None
        self._transform_error = None
        if (
            self._history is not None
            and self._history.data is not None
            and self._selection is not None
            and self._history.identity is not None
        ):
            zone_id, key = self._selection
            identity = self._history.identity
            if (key.bus_id, key.controller_id) == (
                identity.key.bus_id,
                identity.key.controller_id,
            ):
                self._prepared_series = _prepare_chart_series(self._history.data, zone_id)

    def _set_window_combo(self, hours: float) -> None:
        for index in range(self.window_combo.count()):
            if math.isclose(float(self.window_combo.itemData(index)), hours, rel_tol=1e-9):
                self.window_combo.setCurrentIndex(index)
                return
        label = _duration_text(hours)
        index = self.window_combo.findData(hours)
        if index < 0:
            self.window_combo.addItem(label, hours)
            index = self.window_combo.count() - 1
        self.window_combo.setCurrentIndex(index)

    def _window_changed(self, index: int) -> None:
        value = _valid_hours(self.window_combo.itemData(index))
        if value is not None and not math.isclose(value, self._history_hours, rel_tol=1e-9):
            self._history_hours = value
            self.history_window_requested.emit(value)
            self._render_history()

    def _apply_pens(self) -> None:
        self.pv_curve.setPen(pg.mkPen(self._colors["pv"], width=self._line_width))
        self.sp_curve.setPen(pg.mkPen(self._colors["sp"], width=self._line_width))
        self.autotune_curve.setPen(
            pg.mkPen(self._colors["autotune"], width=self._line_width, style=Qt.PenStyle.DashLine)
        )
        self.mae_curve.setPen(pg.mkPen("#16837b", width=self._line_width, style=Qt.PenStyle.DotLine))

    def _apply_visibility(self, _checked: bool | None = None) -> None:
        self.sp_curve.setVisible(self.show_sp.isChecked())
        self.autotune_curve.setVisible(self.show_autotune.isChecked())
        self.mae_curve.setVisible(self.show_mae.isChecked())

    def _render_history(self) -> None:
        for curve in (self.pv_curve, self.sp_curve, self.autotune_curve, self.mae_curve):
            curve.setData([], [])
        if self._selection is None:
            self.metrics_label.setText("Current analysis metrics: N/A")
            self.status_label.setText("Select a verified zone in Overview to view history.")
            return
        if self._history is None:
            self.metrics_label.setText("Current analysis metrics: N/A")
            self.status_label.setText("Waiting for the shared history reader.")
            return
        history = self._history
        if history.data is None or history.identity is None:
            self.metrics_label.setText("Current analysis metrics: N/A")
            self.status_label.setText(history.error or "History is unavailable or withheld.")
            return

        zone_id, key = self._selection
        identity = history.identity
        if (key.bus_id, key.controller_id) != (
            identity.key.bus_id,
            identity.key.controller_id,
        ):
            self.metrics_label.setText("Current analysis metrics: N/A")
            self.status_label.setText("Controller changed; select a zone again.")
            return

        self.metrics_label.setText(self._analysis_metrics(zone_id, history.identity))
        root = _mapping(history.data)
        telemetry = _zone_data(root.get("telemetry"), zone_id)
        analysis = _zone_data(root.get("analysis"), zone_id)
        telem_times = telemetry.get("times")
        mae_times = analysis.get("times")
        times = telem_times if isinstance(telem_times, Sequence) else ()
        analysis_times = mae_times if isinstance(mae_times, Sequence) else ()
        if self._transform_error is not None:
            self.status_label.setText(f"History transform failed: {self._transform_error}")
            return
        if self._prepared_series is None:
            self.status_label.setText("Preparing bounded chart data off-thread.")
            return
        self._set_curve(self.pv_curve, self._prepared_series["pv"])
        self._set_curve(self.sp_curve, self._prepared_series["sp"])
        self._set_curve(self.autotune_curve, self._prepared_series["autotune"])
        self._set_curve(self.mae_curve, self._prepared_series["mae"])
        self._apply_visibility()

        valid_times = [value for value in times if isinstance(value, datetime)]
        if valid_times:
            latest = max(value.timestamp() for value in valid_times)
            width = self._history_hours * 3600.0
            self.plot_widget.setXRange(latest - width, latest, padding=0)
            self.plot_widget.enableAutoRange(axis="y", enable=True)
        errors = root.get("errors", ())
        if isinstance(errors, Sequence) and errors:
            message = f"History loaded with warnings: {'; '.join(str(item) for item in errors[:2])}"
        else:
            message = (
                f"{len(times)} telemetry timestamps · {len(analysis_times)} analysis timestamps · "
                f"{_duration_text(self._history_hours)} window"
            )
        if self._preferences_warning:
            message += f" · Viewer preference warning: {self._preferences_warning}"
        self.status_label.setText(message)
        if not self._allow_float:
            self.window().setWindowTitle(
                f"CN616A chart — Zone {zone_id} · {self._zone_names[zone_id]}"
            )

    @staticmethod
    def _set_curve(
        curve: pg.PlotDataItem,
        data: tuple[np.ndarray, np.ndarray, np.ndarray],
    ) -> None:
        x_values, y_values, connections = data
        curve.setData(x=x_values, y=y_values, connect=connections)

    def reset_view(self) -> None:
        if self._history is None or self._history.data is None or self._selection is None:
            return
        zone_id, _key = self._selection
        telemetry = _zone_data(_mapping(self._history.data).get("telemetry"), zone_id)
        times = telemetry.get("times")
        if not isinstance(times, Sequence):
            return
        valid_times = [value for value in times if isinstance(value, datetime)]
        if not valid_times:
            return
        latest = max(value.timestamp() for value in valid_times)
        self.plot_widget.setXRange(latest - self._history_hours * 3600.0, latest, padding=0)
        self.plot_widget.enableAutoRange(axis="y", enable=True)
        self.status_label.setText("View reset; cached and service histories are unchanged.")

    def home_view(self) -> None:
        if self._history is None or self._history.data is None or self._selection is None:
            return
        self.plot_widget.getPlotItem().autoRange(padding=0.02)

    def _analysis_metrics(self, zone_id: int, identity: ControllerIdentity) -> str:
        snapshot = self._analysis_snapshot
        if (
            snapshot is None
            or snapshot.data is None
            or snapshot.identity != identity
        ):
            return "Current analysis metrics: N/A"
        values = snapshot.zone_values.get(zone_id, {})
        metrics: list[str] = []
        for label, key, decimals, unit in (
            ("MAE", "avg_abs_error_c", 3, " °C"),
            ("threshold", "threshold_c", 2, " °C"),
            ("samples", "n_points", 0, ""),
        ):
            value = _number_text(values.get(key), decimals, unit)
            if value is not None:
                metrics.append(f"{label} {value}")
        return (
            "Current analysis metrics: " + " · ".join(metrics)
            if metrics
            else "Current analysis metrics: N/A"
        )

class FloatingChartWindow(QMainWindow):
    closing = Signal(object)

    def __init__(self, chart: ChartWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("CN616A chart")
        self.resize(1000, 560)
        self.setCentralWidget(chart)

    def closeEvent(self, event) -> None:
        self.closing.emit(self)
        super().closeEvent(event)


class ChartsPanel(QWidget):
    """Fan out one adapter history stream to embedded and temporary floating views."""

    transform_completed = Signal(object)

    def __init__(
        self,
        history_requester: Callable[[float], int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._history_requester = history_requester
        self._history: AdapterHistory | None = None
        self._selection: tuple[int, ZoneKey] | None = None
        self._service_config: AdapterSnapshot | None = None
        self._analysis_snapshot: AdapterSnapshot | None = None
        self._requested_config: AdapterSnapshot | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cn616a-qt-chart-transform")
        self._transform_future: Future[dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]] | None = None
        self._running_transform_key: tuple[int, int, int] | None = None
        self._desired_transform_key: tuple[int, int, int] | None = None
        self._prepared_transform_key: tuple[int, int, int] | None = None
        self._prepared_series: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] | None = None
        self._transform_error: str | None = None
        self._closing = False
        self._floating_windows: set[FloatingChartWindow] = set()
        self._floating_charts: dict[FloatingChartWindow, ChartWidget] = {}
        self._history_hours = 1.0
        self._pending_update = False
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(35)
        self._render_timer.timeout.connect(self._render_views)
        self.transform_completed.connect(self._on_transform_completed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.chart = ChartWidget(parent=self)
        self.chart.history_window_requested.connect(self._request_history_window)
        self.chart.float_requested.connect(self.open_floating_window)
        layout.addWidget(self.chart)

    def set_selection(self, zone_id: int | None, key: ZoneKey | None) -> None:
        self._selection = (zone_id, key) if zone_id is not None and key is not None else None
        self._queue_update()

    def set_history(self, history: AdapterHistory | None) -> None:
        self._history = history
        self._queue_update()

    def set_service_config(self, snapshot: AdapterSnapshot) -> None:
        self._service_config = snapshot
        self._queue_update()

    def set_analysis_snapshot(self, snapshot: AdapterSnapshot) -> None:
        self._analysis_snapshot = snapshot
        self._queue_update()

    def _queue_update(self) -> None:
        if not self._closing and not self._pending_update:
            self._pending_update = True
            self._render_timer.start()

    def _render_views(self) -> None:
        self._pending_update = False
        config = self._configuration_for_history()
        if config is not None and config is not self._requested_config:
            self._requested_config = config
            preferences = _viewer_preferences(config)
            hours = _valid_hours(preferences.get("history_hours"))
            if hours is not None and not math.isclose(hours, self._history_hours, rel_tol=1e-9):
                self._request_history_window(hours)
        prepared_series, transform_error = self._ensure_prepared_series()
        self.chart.set_chart_state(
            self._history,
            self._service_config,
            self._analysis_snapshot,
            self._selection,
            prepared_series,
            transform_error,
        )
        for floating_chart in tuple(self._floating_charts.values()):
            floating_chart.set_chart_state(
                self._history,
                self._service_config,
                self._analysis_snapshot,
                self._selection,
                prepared_series,
                transform_error,
            )

    def _configuration_for_history(self) -> AdapterSnapshot | None:
        config = self._service_config
        history = self._history
        if (
            config is not None
            and config.identity is not None
            and history is not None
            and history.identity is not None
            and config.identity == history.identity
        ):
            return config
        return None

    def _ensure_prepared_series(
        self,
    ) -> tuple[
        dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] | None,
        str | None,
    ]:
        history = self._history
        selection = self._selection
        key: tuple[int, int, int] | None = None
        if (
            history is not None
            and history.data is not None
            and history.identity is not None
            and selection is not None
            and (selection[1].bus_id, selection[1].controller_id)
            == (history.identity.key.bus_id, history.identity.key.controller_id)
        ):
            key = (history.generation, id(history), selection[0])

        if key != self._desired_transform_key:
            self._desired_transform_key = key
            self._prepared_transform_key = None
            self._prepared_series = None
            self._transform_error = None
        if key is None:
            return None, None
        if self._prepared_transform_key == key:
            return self._prepared_series, self._transform_error
        if self._transform_future is None:
            if history is not None and selection is not None:
                self._start_transform(key, history, selection[0])
        return None, None

    def _start_transform(
        self,
        key: tuple[int, int, int],
        history: AdapterHistory,
        zone_id: int,
    ) -> None:
        if history.data is None:
            return
        future = self._executor.submit(_prepare_chart_series, history.data, zone_id)
        self._transform_future = future
        self._running_transform_key = key

        def publish(result_future: Future[dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]]) -> None:
            try:
                result = result_future.result()
            except Exception as exc:
                self.transform_completed.emit((key, None, f"{type(exc).__name__}: {exc}"))
            else:
                self.transform_completed.emit((key, result, None))

        future.add_done_callback(publish)

    def _on_transform_completed(self, result: object) -> None:
        if self._closing or not isinstance(result, tuple) or len(result) != 3:
            return
        key, series, error = result
        if key != self._running_transform_key:
            return
        self._transform_future = None
        self._running_transform_key = None
        if key == self._desired_transform_key:
            self._prepared_transform_key = key
            if isinstance(series, dict):
                self._prepared_series = series
                self._transform_error = error
            else:
                self._prepared_series = None
                self._transform_error = error or "worker returned an invalid result"
        self._queue_update()

    def _request_history_window(self, hours: float) -> None:
        self._history_hours = hours
        if self._history_requester is not None:
            try:
                self._history_requester(hours)
            except (RuntimeError, ValueError) as exc:
                self.chart.status_label.setText(f"History window could not be changed: {exc}")
                return
        else:
            self.chart.status_label.setText("No history reader is available in disconnected mode.")
        self.chart.set_history_hours(hours)
        for floating_chart in self._floating_charts.values():
            floating_chart.set_history_hours(hours)

    def open_floating_window(self) -> FloatingChartWindow:
        floating_chart = ChartWidget(allow_float=False)
        floating_chart.set_history_hours(self._history_hours)
        selection = self._selection
        prepared_series, transform_error = self._ensure_prepared_series()
        floating_chart.set_chart_state(
            self._history,
            self._service_config,
            self._analysis_snapshot,
            selection,
            prepared_series,
            transform_error,
        )
        floating_chart.history_window_requested.connect(self._request_history_window)
        window = FloatingChartWindow(floating_chart)
        window.closing.connect(self._remove_floating_window)
        self._floating_windows.add(window)
        self._floating_charts[window] = floating_chart
        window.show()
        self._queue_update()
        return window

    def _remove_floating_window(self, window: FloatingChartWindow) -> None:
        self._floating_windows.discard(window)
        self._floating_charts.pop(window, None)

    def shutdown(self) -> None:
        self._closing = True
        self._render_timer.stop()
        self._pending_update = False
        self._executor.shutdown(wait=True, cancel_futures=True)
        for window in tuple(self._floating_windows):
            window.close()
        self._floating_windows.clear()
        self._floating_charts.clear()
