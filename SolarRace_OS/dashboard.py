"""
dashboard.py — Industrial Dark-Theme EV Telemetry Dashboard
============================================================
PySide6 main window that owns the layout, all display widgets,
and the CANWorker thread lifecycle.

All GUI updates happen exclusively in the main thread via Qt's
automatic queued-connection mechanism (signal emitted from QThread →
slot called in GUI thread).  No manual thread synchronisation needed.

Session data (RPM, Power, Voltage, SOC, Controller Temp) is accumulated
into plain Python lists from the moment START CAN is pressed.  The lists
grow unboundedly so the user can pan back through the entire session.
Auto-scrolling is disabled whenever the user manually drags/pans the
graph; a "↩ LIVE" button re-enables it.
"""

from datetime import datetime
from typing import Optional

import os
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

import pyqtgraph as pg
from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from can_worker import CANWorker

# =========================================================================== #
#  Qt Style Sheet — dark industrial theme                                      #
# =========================================================================== #
DARK_QSS = """
/* ── Globals ─────────────────────────────────────────────────────────── */
QMainWindow, QWidget {
    background-color: #0d1117;
    color: #c9d1d9;
    font-family: 'Segoe UI', 'Consolas', monospace;
    font-size: 13px;
}

/* ── Gauge card panel ─────────────────────────────────────────────────── */
QFrame#gaugeCard {
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
}

/* ── Alert bar ────────────────────────────────────────────────────────── */
QFrame#alertBar {
    background-color: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    min-height: 36px;
    max-height: 36px;
}

/* ── Gauge typography ─────────────────────────────────────────────────── */
QLabel#gaugeTitle {
    color: #8b949e;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 2px;
}
QLabel#gaugeValue {
    color: #58a6ff;
    font-size: 34px;
    font-weight: bold;
    font-family: 'Consolas', 'Courier New', monospace;
}
QLabel#gaugeUnit {
    color: #8b949e;
    font-size: 12px;
}

/* ── App title & status ───────────────────────────────────────────────── */
QLabel#appTitle {
    color: #e6edf3;
    font-size: 17px;
    font-weight: bold;
    letter-spacing: 1px;
}
QLabel#statusLabel {
    color: #8b949e;
    font-size: 12px;
    font-weight: bold;
    font-family: 'Consolas', monospace;
}

/* ── Section header ───────────────────────────────────────────────────── */
QLabel#sectionHeader {
    color: #8b949e;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 3px;
}

/* ── Large RPM numeric display ────────────────────────────────────────── */
QLabel#rpmDisplay {
    color: #e6edf3;
    font-size: 46px;
    font-weight: bold;
    font-family: 'Consolas', 'Courier New', monospace;
}

/* ── SOC progress bar — base; chunk colour is set dynamically ─────────── */
QProgressBar {
    border: 1px solid #30363d;
    border-radius: 8px;
    background-color: #0d1117;
    text-align: center;
    color: #e6edf3;
    font-size: 14px;
    font-weight: bold;
    min-height: 28px;
}

/* ── Control buttons ──────────────────────────────────────────────────── */
QPushButton#startBtn {
    background-color: #238636;
    color: #ffffff;
    border: 1px solid #2ea043;
    border-radius: 6px;
    padding: 8px 28px;
    font-size: 13px;
    font-weight: bold;
    min-width: 140px;
}
QPushButton#startBtn:hover   { background-color: #2ea043; }
QPushButton#startBtn:pressed  { background-color: #1a6929; }
QPushButton#startBtn:disabled {
    background-color: #21262d;
    color: #6e7681;
    border-color: #30363d;
}

QPushButton#stopBtn {
    background-color: #b62324;
    color: #ffffff;
    border: 1px solid #f85149;
    border-radius: 6px;
    padding: 8px 28px;
    font-size: 13px;
    font-weight: bold;
    min-width: 140px;
}
QPushButton#stopBtn:hover   { background-color: #da3633; }
QPushButton#stopBtn:pressed  { background-color: #8e1a1a; }
QPushButton#stopBtn:disabled {
    background-color: #21262d;
    color: #6e7681;
    border-color: #30363d;
}

QPushButton#exportBtn {
    background-color: #21262d;
    color: #58a6ff;
    border: 1px solid #58a6ff;
    border-radius: 6px;
    padding: 8px 28px;
    font-size: 13px;
    font-weight: bold;
    min-width: 140px;
}
QPushButton#exportBtn:hover   { background-color: #1f3d5c; }
QPushButton#exportBtn:pressed  { background-color: #161b22; }
QPushButton#exportBtn:disabled {
    background-color: #21262d;
    color: #6e7681;
    border-color: #30363d;
}

/* ── Live / scroll-back button ────────────────────────────────────────── */
QPushButton#liveBtn {
    background-color: #21262d;
    color: #58a6ff;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: bold;
}
QPushButton#liveBtn:hover   { background-color: #30363d; }
QPushButton#liveBtn:pressed  { background-color: #161b22; }
QPushButton#liveBtn:checked {
    background-color: #1f3d5c;
    border-color: #58a6ff;
    color: #58a6ff;
}
"""

# SOC progress-bar chunk colours — applied as a partial stylesheet override
_SOC_CHUNK_GREEN  = "QProgressBar::chunk { background-color: #3fb950; border-radius: 7px; }"
_SOC_CHUNK_YELLOW = "QProgressBar::chunk { background-color: #d29922; border-radius: 7px; }"
_SOC_CHUNK_RED    = "QProgressBar::chunk { background-color: #f85149; border-radius: 7px; }"

# Number of samples shown in the rolling view when auto-scrolling
_VIEW_WINDOW = 250


# =========================================================================== #
#  GaugeCard — reusable single-value display panel                            #
# =========================================================================== #
class GaugeCard(QFrame):
    """
    A dark framed card that shows:
      • a small ALL-CAPS title
      • a large coloured value (updates via set_value())
      • a small unit label below the value
    """

    def __init__(self, title: str, unit: str, parent=None):
        super().__init__(parent)
        self.setObjectName("gaugeCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(170, 115)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 10)
        layout.setSpacing(2)

        self._title_lbl = QLabel(title.upper())
        self._title_lbl.setObjectName("gaugeTitle")

        self._value_lbl = QLabel("---")
        self._value_lbl.setObjectName("gaugeValue")

        self._unit_lbl = QLabel(unit)
        self._unit_lbl.setObjectName("gaugeUnit")

        layout.addWidget(self._title_lbl)
        layout.addWidget(self._value_lbl)
        layout.addWidget(self._unit_lbl)
        layout.addStretch()

    def set_value(self, text: str) -> None:
        """Update the displayed value.  Call from any slot in the GUI thread."""
        self._value_lbl.setText(text)


# =========================================================================== #
#  MainWindow                                                                  #
# =========================================================================== #
class MainWindow(QMainWindow):
    """
    Top-level application window.

    Layout (top → bottom):
      1. Header bar   — app title + live status label
      2. Alert bar    — up to 3 active error/limit alerts (or SYSTEM OK)
      3. Gauge grid   — voltage, SOC bar, instant power, controller temp,
                        motor temp, RPM display
      4. Plot panel   — dual-curve (RPM + Power) pyqtgraph time-series
                        with full session history and manual pan support
      5. Control bar  — START / STOP buttons
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EV CAN Telemetry  |  SiliXcon LYNX")
        self.setMinimumSize(1020, 720)

        self._worker: Optional[CANWorker] = None

        # ── Log entries accumulated during an active session ─────────── #
        self._log_entries: list[str] = []

        # ── Persistent session data ──────────────────────────────────── #
        # Populated from START to STOP; cleared on each new START press.
        # Keys match the signals that feed them.
        self._session_data: dict[str, list] = {
            "rpm":       [],
            "power":     [],
            "voltage":   [],
            "soc":       [],
            "ctrl_temp": [],
        }

        # ── Auto-scroll state ────────────────────────────────────────── #
        # True  → graph viewport tracks the latest samples automatically.
        # False → user has panned; we leave the viewport alone until they
        #         click the "↩ LIVE" button to re-engage auto-scroll.
        self._auto_scroll: bool = True

        # ── Zero-sync guard ──────────────────────────────────────────── #
        # Prevents _sync_zero_baselines from being called re-entrantly
        # when setYRange() on the power ViewBox itself triggers a range
        # change event on the primary ViewBox (can happen on some
        # pyqtgraph builds where linked axes propagate back).
        self._syncing_axes: bool = False

        self._build_ui()

    # ====================================================================== #
    #  UI construction helpers                                                #
    # ====================================================================== #
    def _build_ui(self) -> None:
        """Assemble the full widget tree."""
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        root.addLayout(self._build_header())
        root.addWidget(self._build_alert_bar())
        root.addLayout(self._build_gauge_grid())
        root.addWidget(self._build_plot_panel(), stretch=1)
        root.addLayout(self._build_controls())

    # ── Header ─────────────────────────────────────────────────────────── #
    def _build_header(self) -> QHBoxLayout:
        layout = QHBoxLayout()

        title = QLabel("⚡  EV TELEMETRY DASHBOARD")
        title.setObjectName("appTitle")
        layout.addWidget(title)
        layout.addStretch()

        self._status_lbl = QLabel("● IDLE  |  Press START CAN to connect")
        self._status_lbl.setObjectName("statusLabel")
        layout.addWidget(self._status_lbl)

        return layout

    # ── Alert bar ───────────────────────────────────────────────────────── #
    def _build_alert_bar(self) -> QFrame:
        """
        Horizontal strip that shows up to 3 active alerts.
        Errors appear in red, limits in orange, idle state in green.
        """
        frame = QFrame()
        frame.setObjectName("alertBar")

        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(20)

        prefix = QLabel("ALERTS:")
        prefix.setStyleSheet(
            "color: #8b949e; font-size: 10px; font-weight: bold; "
            "letter-spacing: 2px; font-family: 'Consolas', monospace;"
        )
        layout.addWidget(prefix)

        # Three slots — hidden when no alert occupies them
        self._alert_labels: list[QLabel] = []
        for _ in range(3):
            lbl = QLabel()
            lbl.setStyleSheet(
                "font-size: 12px; font-weight: bold; "
                "font-family: 'Consolas', monospace;"
            )
            lbl.setVisible(False)
            layout.addWidget(lbl)
            self._alert_labels.append(lbl)

        # "SYSTEM OK" shown when the alert list is empty
        self._ok_label = QLabel("●  SYSTEM OK")
        self._ok_label.setStyleSheet(
            "color: #3fb950; font-size: 12px; font-weight: bold; "
            "font-family: 'Consolas', monospace;"
        )
        layout.addWidget(self._ok_label)

        layout.addStretch()
        return frame

    # ── Gauge grid ──────────────────────────────────────────────────────── #
    def _build_gauge_grid(self) -> QGridLayout:
        """
        Two-row, four-column grid of telemetry cards:
          Row 0: [Battery Voltage] [Battery SOC bar ──── (×2 cols)] [Instant Power]
          Row 1: [Controller Temp] [Motor Thermistor]    [Motor RPM (numeric) ×2 cols]
        """
        grid = QGridLayout()
        grid.setSpacing(10)

        # ── Row 0, Col 0: Battery Voltage ───────────────────────────── #
        self._voltage_card = GaugeCard("Battery Voltage", "V")
        grid.addWidget(self._voltage_card, 0, 0)

        # ── Row 0, Col 1-2: Battery SOC progress bar ─────────────────── #
        soc_panel = QFrame()
        soc_panel.setObjectName("gaugeCard")
        soc_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        soc_panel.setMinimumSize(170, 115)
        soc_inner = QVBoxLayout(soc_panel)
        soc_inner.setContentsMargins(16, 12, 16, 10)
        soc_inner.setSpacing(8)

        soc_title = QLabel("BATTERY SOC")
        soc_title.setObjectName("gaugeTitle")
        soc_inner.addWidget(soc_title)

        self._soc_bar = QProgressBar()
        self._soc_bar.setRange(0, 100)
        self._soc_bar.setValue(0)
        self._soc_bar.setFormat("%p %")
        self._soc_bar.setMinimumHeight(32)
        self._soc_bar.setStyleSheet(_SOC_CHUNK_GREEN)
        soc_inner.addWidget(self._soc_bar)
        soc_inner.addStretch()

        grid.addWidget(soc_panel, 0, 1, 1, 2)  # span 2 columns

        # ── Row 0, Col 3: Instant Power ──────────────────────────────── #
        self._power_card = GaugeCard("Instant Power", "W")
        grid.addWidget(self._power_card, 0, 3)

        # ── Row 1, Col 0: Controller Temperature ─────────────────────── #
        self._ctrl_temp_card = GaugeCard("Controller Temp", "°C")
        grid.addWidget(self._ctrl_temp_card, 1, 0)

        # ── Row 1, Col 1: Motor Thermistor ───────────────────────────── #
        self._motor_temp_card = GaugeCard("Motor Thermistor", "ADC / Ω  (raw)")
        grid.addWidget(self._motor_temp_card, 1, 1)

        # ── Row 1, Col 2-3: Motor RPM (large numeric) ────────────────── #
        rpm_panel = QFrame()
        rpm_panel.setObjectName("gaugeCard")
        rpm_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        rpm_panel.setMinimumSize(170, 115)
        rpm_inner = QVBoxLayout(rpm_panel)
        rpm_inner.setContentsMargins(16, 12, 16, 10)
        rpm_inner.setSpacing(2)

        rpm_title = QLabel("MOTOR RPM")
        rpm_title.setObjectName("gaugeTitle")
        rpm_inner.addWidget(rpm_title)

        self._rpm_display = QLabel("0")
        self._rpm_display.setObjectName("rpmDisplay")
        rpm_inner.addWidget(self._rpm_display)
        rpm_inner.addStretch()

        grid.addWidget(rpm_panel, 1, 2, 1, 2)  # span 2 columns

        return grid

    # ── Dual-curve plot panel ───────────────────────────────────────────── #
    def _build_plot_panel(self) -> QFrame:
        """
        A framed panel containing a pyqtgraph PlotWidget with:
          • RPM curve  — blue fill-under line (left Y axis)
          • Power curve — solid red line      (right Y axis, secondary ViewBox)
          • Legend distinguishing both series
          • Manual pan detection: disables auto-scroll
          • "↩ LIVE" button: re-engages auto-scroll
        """
        frame = QFrame()
        frame.setObjectName("gaugeCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        # ── Header row with Live button ─────────────────────────────── #
        header_row = QHBoxLayout()

        header = QLabel("RPM & POWER  —  SESSION TREND")
        header.setObjectName("sectionHeader")
        header_row.addWidget(header)
        header_row.addStretch()

        self._live_btn = QPushButton("↩  LIVE")
        self._live_btn.setObjectName("liveBtn")
        self._live_btn.setToolTip("Re-engage auto-scroll to the latest samples")
        self._live_btn.setFixedHeight(24)
        self._live_btn.clicked.connect(self._resume_auto_scroll)
        header_row.addWidget(self._live_btn)

        layout.addLayout(header_row)

        # ── PlotWidget ──────────────────────────────────────────────── #
        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setBackground("#161b22")
        self._plot_widget.setMinimumHeight(190)

        plot_item = self._plot_widget.getPlotItem()
        axis_pen      = pg.mkPen(color="#30363d", width=1)
        axis_text_pen = pg.mkPen(color="#8b949e")

        for axis_name in ("left", "bottom"):
            axis = plot_item.getAxis(axis_name)
            axis.setPen(axis_pen)
            axis.setTextPen(axis_text_pen)

        self._plot_widget.setLabel("left",   "RPM",      color="#58a6ff", size="10pt")
        self._plot_widget.setLabel("bottom", "Samples",  color="#8b949e", size="10pt")
        self._plot_widget.showGrid(x=True, y=True, alpha=0.12)

        # ── Secondary ViewBox for Power (right Y axis) ──────────────── #
        self._power_vb = pg.ViewBox()
        plot_item.showAxis("right")
        plot_item.scene().addItem(self._power_vb)
        plot_item.getAxis("right").linkToView(self._power_vb)
        self._power_vb.setXLink(plot_item)

        # Disable all mouse interaction on the secondary ViewBox.
        # The user interacts only with the primary (RPM) ViewBox; the
        # Power axis follows via _sync_zero_baselines so zeros stay locked.
        self._power_vb.setMouseEnabled(x=False, y=False)
        # We manage the Y range manually — prevent auto-range from fighting us.
        self._power_vb.enableAutoRange(y=False)

        right_axis = plot_item.getAxis("right")
        right_axis.setPen(axis_pen)
        right_axis.setTextPen(pg.mkPen(color="#f85149"))
        self._plot_widget.setLabel("right", "Power (W)", color="#f85149", size="10pt")

        # Keep secondary ViewBox geometry in sync when the plot is resized
        plot_item.vb.sigResized.connect(self._sync_power_vb_geometry)

        # Zero-baseline synchronisation: whenever the primary ViewBox range
        # changes (user drag, zoom, or programmatic setXRange), recompute
        # the Power Y-range so both zeros sit at the same screen height.
        plot_item.vb.sigRangeChanged.connect(self._sync_zero_baselines)

        # ── Legend ──────────────────────────────────────────────────── #
        legend = plot_item.addLegend(
            offset=(10, 10),
            brush=pg.mkBrush(22, 27, 34, 200),
            pen=pg.mkPen(color="#30363d"),
        )

        # ── RPM curve (main ViewBox, left axis) ─────────────────────── #
        # addLegend() was called first so the name= parameter auto-registers
        # this curve in the legend.
        self._rpm_curve = self._plot_widget.plot(
            pen=pg.mkPen(color="#58a6ff", width=2),
            fillLevel=0,
            brush=pg.mkBrush(88, 166, 255, 25),
            name="RPM",
        )

        # ── Power curve (secondary ViewBox, right axis) ──────────────── #
        self._power_curve = pg.PlotDataItem(
            pen=pg.mkPen(color="#f85149", width=2),
        )
        self._power_vb.addItem(self._power_curve)
        legend.addItem(self._power_curve, "Power (W)")

        # ── Manual pan detection ─────────────────────────────────────── #
        # sigRangeChangedManually fires only for mouse interactions,
        # not for programmatic setXRange() calls — no feedback loop risk.
        plot_item.vb.sigRangeChangedManually.connect(self._on_manual_pan)

        layout.addWidget(self._plot_widget)

        # Initial geometry sync (widget not yet shown, but safe to call)
        self._sync_power_vb_geometry()

        return frame

    # ── Control buttons ─────────────────────────────────────────────────── #
    def _build_controls(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.setSpacing(10)

        self._start_btn = QPushButton("▶  START CAN")
        self._start_btn.setObjectName("startBtn")
        self._start_btn.setMinimumHeight(40)
        self._start_btn.clicked.connect(self._start_can)

        self._stop_btn = QPushButton("■  STOP CAN")
        self._stop_btn.setObjectName("stopBtn")
        self._stop_btn.setMinimumHeight(40)
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_can)

        self._export_btn = QPushButton("💾  EXPORT LOG")
        self._export_btn.setObjectName("exportBtn")
        self._export_btn.setMinimumHeight(40)
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_log)

        layout.addWidget(self._start_btn)
        layout.addWidget(self._stop_btn)
        layout.addWidget(self._export_btn)
        layout.addStretch()

        return layout

    # ====================================================================== #
    #  Plot helpers                                                            #
    # ====================================================================== #
    def _sync_power_vb_geometry(self) -> None:
        """Keep the secondary (Power) ViewBox geometry aligned with the main plot."""
        pi = self._plot_widget.getPlotItem()
        self._power_vb.setGeometry(pi.vb.sceneBoundingRect())
        self._power_vb.linkedViewChanged(pi.vb, self._power_vb.XAxis)

    @Slot(object, object)
    def _sync_zero_baselines(self, _, ranges) -> None:
        """
        Lock both Y-axis zeros to the same vertical screen position.

        Called every time the primary (RPM) ViewBox range changes —
        whether by user interaction, auto-scroll, or zooming.

        Algorithm
        ---------
        Given the primary Y-range  [rpm_min, rpm_max], the fraction of the
        viewport height that sits *below* Y = 0 is:

            zero_frac = (0 - rpm_min) / (rpm_max - rpm_min)

        We choose a Power Y-range [pwr_min, pwr_max] that satisfies the same
        fraction, while still fitting all power samples visible in the current
        X window:

            pwr_min = -zero_frac  * pwr_span
            pwr_max = (1-zero_frac) * pwr_span

        where  pwr_span = pwr_data_max / (1 - zero_frac)  ensures the
        highest power value just reaches the top of the viewport.

        A 10 % headroom margin is added so the curve never hugs the border.
        """
        if self._syncing_axes:
            return
        self._syncing_axes = True
        try:
            # ranges = [[x_min, x_max], [y_min, y_max]]
            [[x_min, x_max], [rpm_min, rpm_max]] = ranges

            rpm_span = rpm_max - rpm_min
            if rpm_span <= 0:
                return

            # Fraction of viewport height below Y = 0  (can be <0 or >1 when
            # zero is scrolled off-screen, which is intentional — the sync
            # formula still produces a valid, consistent power range).
            zero_frac = (0.0 - rpm_min) / rpm_span

            # ── Determine max power value in the currently visible X window ── #
            power_data = self._session_data["power"]
            if power_data:
                i_lo = max(0, int(x_min))
                i_hi = min(len(power_data), int(x_max) + 2)
                visible_power = power_data[i_lo:i_hi]
                pwr_data_max = float(max(visible_power)) if visible_power else 0.0
            else:
                pwr_data_max = 0.0

            # Guarantee at least 1 W of range so we never divide by zero
            pwr_data_max = max(pwr_data_max, 1.0)

            # Power is uint16 (always ≥ 0), so pwr_data_min is always 0.
            # ── Apply 10 % headroom then compute zero-locked range ─────── #
            pwr_data_max *= 1.10

            if zero_frac >= 1.0:
                # Zero is above the viewport top — show data in negative region
                pwr_min = -pwr_data_max
                pwr_max = 0.0
            elif zero_frac <= 0.0:
                # Zero is below the viewport bottom — show all positive data
                pwr_min = 0.0
                pwr_max = pwr_data_max
            else:
                # Normal case: zero is inside (or near) the viewport.
                # Scale so pwr_max = (1 - zero_frac) * pwr_span = pwr_data_max
                pwr_span = pwr_data_max / (1.0 - zero_frac)
                pwr_min  = -zero_frac * pwr_span
                pwr_max  = pwr_data_max          # = (1-zero_frac)*pwr_span

            self._power_vb.setYRange(pwr_min, pwr_max, padding=0)
        finally:
            self._syncing_axes = False

    def _update_plot_curves(self) -> None:
        """
        Redraw both curves from session data.
        If auto-scroll is active, advance the X viewport to the latest window.
        """
        rpm_data   = self._session_data["rpm"]
        power_data = self._session_data["power"]

        n_rpm   = len(rpm_data)
        n_power = len(power_data)

        if n_rpm:
            self._rpm_curve.setData(rpm_data)
        if n_power:
            self._power_curve.setData(power_data)

        if self._auto_scroll:
            n = max(n_rpm, n_power, 1)
            x_max = n
            x_min = max(0, n - _VIEW_WINDOW)
            self._plot_widget.setXRange(x_min, x_max, padding=0)

    @Slot()
    def _on_manual_pan(self) -> None:
        """Called when the user drags or scrolls the graph manually."""
        if self._auto_scroll:
            self._auto_scroll = False
            self._live_btn.setStyleSheet(
                "background-color: #3d2209; color: #d29922; "
                "border: 1px solid #d29922; border-radius: 4px; "
                "padding: 3px 10px; font-size: 11px; font-weight: bold;"
            )

    @Slot()
    def _resume_auto_scroll(self) -> None:
        """Re-engage auto-scrolling and snap the view to the latest data."""
        self._auto_scroll = True
        self._live_btn.setStyleSheet("")   # revert to QSS default
        self._update_plot_curves()

    # ====================================================================== #
    #  CAN worker lifecycle                                                   #
    # ====================================================================== #
    def _start_can(self) -> None:
        """Instantiate a fresh CANWorker, wire its signals, and start it."""
        if self._worker and self._worker.isRunning():
            return

        # Reset session storage and log for the new capture session
        for key in self._session_data:
            self._session_data[key].clear()
        self._log_entries.clear()

        # Re-engage auto-scroll for the fresh session
        self._auto_scroll = True
        self._live_btn.setStyleSheet("")

        self._worker = CANWorker(parent=self)

        self._worker.rpm_updated.connect(self._on_rpm)
        self._worker.voltage_updated.connect(self._on_voltage)
        self._worker.soc_updated.connect(self._on_soc)
        self._worker.controller_temp_updated.connect(self._on_ctrl_temp)
        self._worker.motor_temp_updated.connect(self._on_motor_temp)
        self._worker.power_updated.connect(self._on_power)
        self._worker.alerts_updated.connect(self._on_alerts)
        self._worker.connection_error.connect(self._on_error)
        self._worker.status_updated.connect(self._on_status)

        self._worker.start()

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._export_btn.setEnabled(True)

    def _stop_can(self) -> None:
        """Gracefully stop the worker thread and reset the UI."""
        if self._worker:
            self._worker.stop()
            self._worker = None

        self._log_entries.clear()

        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._export_btn.setEnabled(False)

    # ====================================================================== #
    #  Logging                                                                #
    # ====================================================================== #
    def _log(self, line: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._log_entries.append(f"{ts}  {line}")

    def _export_log(self) -> None:
        if not self._log_entries:
            QMessageBox.information(self, "Export Log", "No log data to export yet.")
            return

        default_name = f"can_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CAN Log", default_name, "Text Files (*.txt);;All Files (*)"
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self._log_entries))
            QMessageBox.information(
                self, "Export Log", f"Saved {len(self._log_entries)} entries to:\n{path}"
            )
        except OSError as e:
            QMessageBox.critical(self, "Export Log", f"Failed to save file:\n{e}")

    # ====================================================================== #
    #  Slots — all run in the GUI thread via Qt's queued connection           #
    # ====================================================================== #
    @Slot(int)
    def _on_rpm(self, rpm: int) -> None:
        self._rpm_display.setText(f"{rpm:+d}")
        self._session_data["rpm"].append(rpm)
        self._log(f"RPM={rpm}")
        self._update_plot_curves()

    @Slot(int)
    def _on_power(self, watts: int) -> None:
        self._power_card.set_value(str(watts))
        self._session_data["power"].append(watts)
        self._log(f"POWER={watts} W")
        # Plot is already redrawn by _on_rpm (same CAN frame); no extra call needed.

    @Slot(float)
    def _on_voltage(self, volts: float) -> None:
        self._voltage_card.set_value(f"{volts:.2f}")
        self._session_data["voltage"].append(volts)
        self._log(f"VOLTAGE={volts:.2f} V")

    @Slot(int)
    def _on_soc(self, pct: int) -> None:
        self._soc_bar.setValue(pct)
        self._session_data["soc"].append(pct)
        self._log(f"SOC={pct}%")

        if pct > 50:
            self._soc_bar.setStyleSheet(_SOC_CHUNK_GREEN)
        elif pct > 20:
            self._soc_bar.setStyleSheet(_SOC_CHUNK_YELLOW)
        else:
            self._soc_bar.setStyleSheet(_SOC_CHUNK_RED)

    @Slot(int)
    def _on_ctrl_temp(self, deg_c: int) -> None:
        self._ctrl_temp_card.set_value(str(deg_c))
        self._session_data["ctrl_temp"].append(deg_c)
        self._log(f"CTRL_TEMP={deg_c} C")

    @Slot(int)
    def _on_motor_temp(self, raw: int) -> None:
        self._motor_temp_card.set_value(str(raw))
        self._log(f"MOTOR_TEMP={raw} (raw ADC)")

    @Slot(list)
    def _on_alerts(self, alerts: list) -> None:
        """
        Update the alert bar.

        *alerts* is a list of (label: str, severity: str) tuples where
        severity is either 'error' (red) or 'limit' (orange).
        At most 3 items; an empty list means all-clear.
        """
        _ERROR_COLOR = "color: #f85149;"
        _LIMIT_COLOR = "color: #d29922;"

        if not alerts:
            self._ok_label.setVisible(True)
            for lbl in self._alert_labels:
                lbl.setVisible(False)
            return

        alert_str = ", ".join(f"{t}({s})" for t, s in alerts)
        self._log(f"ALERTS={alert_str}")
        self._ok_label.setVisible(False)
        for idx, lbl in enumerate(self._alert_labels):
            if idx < len(alerts):
                text, severity = alerts[idx]
                lbl.setText(f"▲  {text}")
                color = _ERROR_COLOR if severity == "error" else _LIMIT_COLOR
                lbl.setStyleSheet(
                    f"{color} font-size: 12px; font-weight: bold; "
                    "font-family: 'Consolas', monospace;"
                )
                lbl.setVisible(True)
            else:
                lbl.setVisible(False)

    @Slot(str)
    def _on_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)
        if "CONNECTED" in msg and "DIS" not in msg:
            self._status_lbl.setStyleSheet(
                "color: #3fb950; font-size: 12px; font-weight: bold;"
            )
        else:
            self._status_lbl.setStyleSheet(
                "color: #8b949e; font-size: 12px; font-weight: bold;"
            )

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        """Display a modal error dialog and reset the controls."""
        self._status_lbl.setText("● ERROR")
        self._status_lbl.setStyleSheet(
            "color: #f85149; font-size: 12px; font-weight: bold;"
        )
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        QMessageBox.critical(self, "CAN Bus Error", msg)

    # ====================================================================== #
    #  Clean shutdown                                                         #
    # ====================================================================== #
    def closeEvent(self, event) -> None:
        """
        Called when the user closes the window.
        Always stop the worker thread before letting Qt destroy the widgets.
        Failing to do so risks a use-after-free crash inside the worker.
        """
        self._stop_can()
        super().closeEvent(event)


if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
