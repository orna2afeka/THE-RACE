"""
driver_dash_v2.py — Sports Car Style Racing HUD Dashboard
=========================================================
800×480 Raspberry Pi 4 optimized, but scales up cleanly to any fullscreen
resolution (see resizeEvent). Custom QPainter speed gauge, neon HUD
aesthetics, threshold flash alerts.
"""

import html
import math
import os
import sys
from typing import Optional

# Drivetrain constants live at the repo root, shared with the pit dashboard so
# the two can never disagree about how fast the car is going. Bootstrap the path
# here as well as in main.py, so this module still runs standalone.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import drivetrain  # noqa: E402  (path set up immediately above)

from PySide6.QtCore import (
    Qt, QEasingCurve, QPropertyAnimation, QRectF, QTimer, Slot,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QKeySequence, QPainter, QPen,
    QRadialGradient, QShortcut,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QApplication,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from can_worker import CANWorker
from modules import pt1000

# ── Palette ────────────────────────────────────────────────────────────────── #
_BG     = "#0e060a"
_PANEL  = "#0c1624"
_BORDER = "#18293d"
# Explicit "off" colour for the status indicators and the map badge.
# NOT _DIM: despite the name, _DIM is #ffffff (pure white) — an unlit indicator
# painted with it would be the brightest thing on the row and read as ON, which
# is the one mistake a boolean warning light must never make.
_OFF = "#40546b"
_CYAN   = "#00e5ff"
_LIME   = "#aaff00"
_ORANGE = "#ff6500"
_RED    = "#ff2020"
_WHITE  = "#eef4ff"
_DIM    = "#ffffff"
_FLASH  = "#3a0606"

# Muted slate for the small caption inside the message strips ("STRATEGY",
# "TURN IN", "MAX"). A caption is a signpost, not information: it must be
# legible without competing with the value it labels, which is why it is neither
# the neon accent nor the same white as the value itself.
_CAPTION = "#7d95ad"


def _tint(hex_colour: str, alpha: float) -> str:
    """`rgba(...)` string from a #rrggbb accent — a wash of the accent colour.

    The message strips are washed in their own accent instead of being filled
    with it. A solid fill turns a 44 px strip into the brightest object on a
    dark HUD, which is why the old banners read as loud rather than urgent: at
    night the driver's eye went to a text box instead of the speed. A 6–20 %
    wash keeps the strip clearly present and still lets the speedometer stay
    the brightest thing on the screen.
    """
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"

C_CYAN   = QColor(_CYAN)
C_LIME   = QColor(_LIME)
C_ORANGE = QColor(_ORANGE)
C_RED    = QColor(_RED)
C_WHITE  = QColor(_WHITE)
C_DIM    = QColor(_DIM)
C_BORDER = QColor(_BORDER)

# Gauge arc geometry: 225° start (SW), sweeping CW 270° to 315° (SE).
# In Qt: start=225, spanAngle=-270 (negative = clockwise).
_ARC_START = 225
_ARC_SPAN  = -270

SPEED_MAX = 140

# Temperature thresholds are shared with the pit (limits.py at the repo root),
# so a gauge that is red in the car is red on the pit wall too.
from limits import (                # noqa: E402  (repo root added to sys.path above)
    MOTOR_TEMP_WARN, MOTOR_TEMP_CRIT,
    CTRL_TEMP_WARN, CTRL_TEMP_CRIT,
    CELL_TEMP_WARN, CELL_TEMP_CRIT,
)


def _fit_font(text: str, cap_pt: float, budget_px: float,
              bold: bool = True) -> QFont:
    """A monospace font at `cap_pt`, stepped down until `text` fits `budget_px`.

    MEASURED with QFontMetricsF rather than estimated from the point size. The
    car runs Linux, where Consolas does not exist and Qt substitutes DejaVu Sans
    Mono — noticeably wider per character. Any ratio hard-coded from what this
    renders on Windows would fit here and push the longer gauge titles straight
    through the arc they sit inside on the actual dashboard.

    Four steps is plenty: font width is near-linear in point size, so the first
    correction lands within a point of the answer.
    """
    pt = max(6, int(cap_pt))
    font = QFont("Consolas", pt)
    font.setBold(bold)
    if not text:
        return font
    for _ in range(4):
        width = QFontMetricsF(font).horizontalAdvance(text)
        if width <= budget_px or pt <= 6:
            break
        pt = max(6, min(pt - 1, int(pt * budget_px / width)))
        font = QFont("Consolas", pt)
        font.setBold(bold)
    return font


def _rpm_to_speed(rpm: int) -> float:
    """Convert motor RPM to km/h.

    The formula that used to live here was `(rpm / 5.0) * 60 * (2π × 0.279) /
    1000`: gear ratio 5.0 where the rest of the system used 5.09, and a 0.279 m
    wheel RADIUS where the rest used a 1.727 m CIRCUMFERENCE. It made the
    driver's speedometer read 3.3 % higher than the pit's for the same frame.

    It carried a "do NOT change this formula" comment, which is why it survived
    so long — but the instruction it encoded (keep the HUD and pit in step) is
    exactly what it was violating. It now defers to drivetrain.py, the single
    definition both dashboards, the odometer and the exporter share.
    """
    return drivetrain.speed_kmh(rpm)


def _pt1000_to_celsius(r_measured: float) -> float | None:
    """Convert PT1000 resistance (Ω) to °C.

    Delegates to modules/pt1000.py, which interpolates the sensor's datasheet
    table. This used to solve the Callendar-Van Dusen equation inline; that
    quadratic form is only valid at and above 0 °C, and it was a second,
    disagreeing copy of a conversion the pit also performs. One table, one
    answer, on both screens.
    """
    return pt1000.celsius_from_ohms(r_measured)


# ── Global stylesheet ──────────────────────────────────────────────────────── #
# Note: alertBar height and button font-size are NOT fixed here — resizeEvent
# scales them so the HUD looks right at fullscreen as well as at 800×480.
RACING_QSS = f"""
QMainWindow, QWidget {{
    background-color: {_BG};
    color: {_WHITE};
    font-family: 'Consolas', 'Courier New', monospace;
}}
QFrame#panel {{
    background-color: {_PANEL};
    border: 1px solid {_BORDER};
    border-radius: 8px;
}}
QFrame#alertBar {{
    background-color: {_PANEL};
    border: 1px solid {_BORDER};
    border-radius: 5px;
}}
/* Page-navigation arrows. Deliberately the loudest control on the bar:
   large glyph, high contrast, generous padding — a gloved driver has to hit
   these while moving. :pressed gives an obvious confirmation on a touchscreen,
   where there is no hover state to show the press landed. */
QPushButton#navBtn {{
    background-color: #10212b;
    color: {_CYAN};
    border: 2px solid {_CYAN};
    border-radius: 8px;
    font-size: 30px;
    font-weight: bold;
}}
QPushButton#navBtn:pressed {{
    background-color: {_CYAN};
    color: #06121a;
}}
QPushButton#startBtn {{
    background-color: #0a3518;
    color: {_LIME};
    border: 1px solid {_LIME};
    border-radius: 5px;
    padding: 5px 20px;
    font-weight: bold;
    letter-spacing: 1px;
}}
QPushButton#startBtn:hover {{ background-color: #0e4a22; }}
QPushButton#startBtn:disabled {{
    background-color: #0d0d0d;
    color: {_DIM};
    border-color: {_DIM};
}}
QPushButton#stopBtn {{
    background-color: #380808;
    color: {_RED};
    border: 1px solid {_RED};
    border-radius: 5px;
    padding: 5px 20px;
    font-weight: bold;
    letter-spacing: 1px;
}}
QPushButton#stopBtn:hover {{ background-color: #521212; }}
QPushButton#stopBtn:disabled {{
    background-color: #0d0d0d;
    color: {_DIM};
    border-color: {_DIM};
}}
"""


# ─────────────────────────────────────────────────────────────────────────────
#  TachometerWidget — large semi-circular speed gauge (0–140 km/h)
# ─────────────────────────────────────────────────────────────────────────────
class TachometerWidget(QWidget):
    """
    Big, bold digital speed readout painted with QPainter.
    Shows the km/h number large and clear (no dial/needle) so it's readable
    at a glance. Background flashes red when speed exceeds 120 km/h.
    """

    _SPEED_ALERT = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rpm: int = 0
        self._speed: float = 0.0
        self._flash_on: bool = False
        self._alert: bool = False

        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(500)
        self._flash_timer.timeout.connect(self._toggle_flash)

        # Small minimum, Expanding policy: this widget PAINTS itself from its
        # actual size (see paintEvent), so it does not need floor space to look
        # right — it needs permission to shrink. A 280×230 floor here, plus the
        # gauges' own floors, made the window's minimum 890×642: larger than the
        # 800×480 panel the car runs, so Qt clipped the bottom row of controls
        # instead of letting the instruments give up a few pixels.
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_rpm(self, rpm: int) -> None:
        self._rpm = max(0, rpm)
        self._speed = abs(_rpm_to_speed(rpm))
        alert = self._speed > self._SPEED_ALERT
        if alert and not self._alert:
            self._flash_timer.start()
        elif not alert and self._alert:
            self._flash_timer.stop()
            self._flash_on = False
        self._alert = alert
        self.update()

    def _toggle_flash(self) -> None:
        self._flash_on = not self._flash_on
        self.update()

    def paintEvent(self, _) -> None:  # noqa: ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()

        if self._flash_on:
            p.fillRect(0, 0, w, h, QColor(_FLASH))

        # Number turns red past the alert threshold, otherwise crisp white.
        num_color = C_RED if self._alert else C_WHITE

        # ── Big bold speed number ──────────────────────────────────────── #
        # Font scales with the panel; capped against width so 3 digits ("140")
        # always fit, and against height so it never overflows the unit label.
        num_fs = max(40, int(min(w * 0.42, h * 0.58)))
        nf = QFont("Consolas", num_fs)
        nf.setBold(True)
        p.setFont(nf)
        p.setPen(QPen(num_color))
        p.drawText(
            QRectF(0, h * 0.06, w, h * 0.66),
            Qt.AlignCenter,
            f"{self._speed:.0f}",
        )

        # ── km/h unit label ────────────────────────────────────────────── #
        uf = QFont("Consolas", max(12, int(num_fs * 0.24)))
        uf.setBold(True)
        p.setFont(uf)
        p.setPen(QPen(C_DIM))
        p.drawText(
            QRectF(0, h * 0.72, w, h * 0.22),
            Qt.AlignCenter,
            "km/h",
        )

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
#  MiniGauge — small circular gauge for side panel telemetry
# ─────────────────────────────────────────────────────────────────────────────
class MiniGauge(QWidget):
    """
    Compact circular arc gauge with three severity levels.

        normal    the gauge's own colour
        warning   amber value text, NO flashing — "keep an eye on this"
        critical  red and flashing at 500 ms — "act now"

    The two tiers exist because a single threshold made the flashing useless.
    The controller gauge was set to alert at 30 °C, which any powered controller
    exceeds immediately, so the HUD flashed red permanently and the driver
    learned to ignore it. Flashing has to be rare to mean anything, so it is now
    reserved for the critical tier alone.

    `warn_threshold` defaults to infinity, so a gauge that passes only
    `alert_threshold` behaves exactly as before.
    """

    def __init__(
        self,
        title: str,
        unit: str,
        max_val: float,
        color: QColor,
        alert_threshold: float = float("inf"),
        decimals: int = 0,
        parent=None,
        warn_threshold: float = float("inf"),
    ):
        super().__init__(parent)
        self._title = title
        self._unit = unit
        self._max_val = max_val
        self._color = color
        self._threshold = alert_threshold
        self._warn_threshold = warn_threshold
        self._warning: bool = False
        self._decimals = decimals
        self._value: float = 0.0
        self._flash_on: bool = False
        self._alert: bool = False

        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(500)
        self._flash_timer.timeout.connect(self._toggle_flash)

        # See TachometerWidget's minimum for why this is small: three of these
        # stacked in the right panel were setting a 472 px floor on a 480 px
        # screen all by themselves.
        self.setMinimumSize(70, 56)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_value(self, value: float) -> None:
        self._value = value
        alert = value > self._threshold
        # Warning is everything between the two thresholds. Critical wins, so a
        # gauge is never "warning" and "critical" at once.
        self._warning = (not alert) and (value > self._warn_threshold)
        if alert and not self._alert:
            self._flash_timer.start()
        elif not alert and self._alert:
            self._flash_timer.stop()
            self._flash_on = False
        self._alert = alert
        self.update()

    def _toggle_flash(self) -> None:
        self._flash_on = not self._flash_on
        self.update()

    def paintEvent(self, _) -> None:  # noqa: ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()

        if self._flash_on:
            p.fillRect(0, 0, w, h, QColor(_FLASH))

        # ── Circle geometry — MUST fit inside the widget ──────────────────── #
        # The old maths centred the circle at 0.36×height while allowing a radius
        # of up to 0.476×height, so the top of every gauge was drawn ABOVE y=0.
        # Qt clips painting to the widget, so what reached the screen was an arc
        # sliced off flat against the panel border — the arcs appeared to collide
        # with the status bar above them. Measured at 800×480 the side gauges
        # overflowed their own top edge by 17–20 px and the DS002 gauges by 61.
        #
        # Centring at h/2 is what makes a fitting circle possible: the title no
        # longer sits in a band below the arc (it moves into the arc's own bottom
        # gap, see below), so the full height is available to the circle.
        pen_half = 5.5                      # half of the widest stroke, the glow
        inset = pen_half + max(4.0, min(w, h) * 0.06)      # + breathing room
        radius = max(6.0, min(w / 2.0 - inset, h / 2.0 - inset))
        cx, cy = w / 2.0, h / 2.0

        rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        # Three tiers: red at critical, amber at warning, the gauge's own colour
        # when all is well. Only the red tier also flashes (see set_value).
        if self._alert:
            color = C_RED
        elif self._warning:
            color = C_ORANGE
        else:
            color = self._color

        # Track arc
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(C_BORDER, 7, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, _ARC_START * 16, _ARC_SPAN * 16)

        # Value arc
        frac = min(1.0, self._value / self._max_val) if self._max_val > 0 else 0.0
        val_span = int(_ARC_SPAN * frac)

        if val_span != 0:
            glow_pen = QPen(
                QColor(color.red(), color.green(), color.blue(), 65),
                11, Qt.SolidLine, Qt.RoundCap,
            )
            p.setPen(glow_pen)
            p.drawArc(rect, _ARC_START * 16, val_span * 16)

            p.setPen(QPen(color, 6, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, _ARC_START * 16, val_span * 16)

            p.setPen(QPen(C_WHITE, 1.5, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, _ARC_START * 16, val_span * 16)

        # ── Text, all of it inside the circle ─────────────────────────────── #
        # Value, unit and title stack down the middle. The value and the title
        # are sized to their own length as well as to the radius: "95" and "1828"
        # are the same gauge, and "SOC" sits beside "MOTOR CURRENT". Sized on the
        # radius alone, the long ones run straight through the arc around them.
        value_txt = f"{self._value:.{self._decimals}f}"

        # 1.75 r, not the full 1.9 r of the rect: the widest point of the circle
        # is level with the value, so text taken right out to the rect edge
        # touches the stroke on both sides.
        vf = _fit_font(value_txt, radius * 0.60, radius * 1.75)
        p.setFont(vf)
        p.setPen(QPen(C_RED if self._alert
                      else (C_ORANGE if self._warning else C_WHITE)))
        p.drawText(
            QRectF(cx - radius * 0.95, cy - radius * 0.58, radius * 1.9, radius * 0.68),
            Qt.AlignCenter,
            value_txt,
        )

        # Unit label
        uf = _fit_font(self._unit, radius * 0.24, radius * 1.5, bold=False)
        p.setFont(uf)
        p.setPen(QPen(C_DIM))
        p.drawText(
            QRectF(cx - radius * 0.9, cy + radius * 0.10, radius * 1.8, radius * 0.30),
            Qt.AlignCenter,
            self._unit,
        )

        # Title — in the 90° gap at the bottom of the arc, which is otherwise
        # dead space. Putting it there rather than in a band underneath is what
        # lets the circle keep its size now that it has to fit inside the widget.
        #
        # It starts BELOW the arc's lowest ink (the ends sit at 0.707 r, plus the
        # stroke) and is measured against the WIDGET's width, not the circle's.
        # Both matter: a title placed higher runs across the arc's lower flanks —
        # "MOTOR POWER" came out as "OTOR POWE" with its first and last letters
        # buried in the stroke — and below the arc the widget is far wider than
        # the circle, so there is room for the long titles at a legible size.
        # 0.7071 r is where the arc's ends sit (it stops 45° short of the bottom
        # on each side); + the stroke + 1 px clears its ink exactly.
        title_top = cy + 0.7071 * radius + pen_half + 1.0
        title_h = max(8.0, h - 2.0 - title_top)
        # Capped by the band's height as well as the radius: point size renders
        # about 1.6× as many pixels tall, so 0.6 × the band is what fits in it.
        tf = _fit_font(self._title, min(radius * 0.22, title_h * 0.60), w - 10.0)
        p.setFont(tf)
        p.setPen(QPen(color))
        p.drawText(
            QRectF(5.0, title_top, w - 10.0, title_h),
            Qt.AlignCenter,
            self._title,
        )

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
#  RacingDashboard — main window (800×480 design size, scales to fullscreen)
# ─────────────────────────────────────────────────────────────────────────────
class RacingDashboard(QMainWindow):
    """
    Sports Car HUD main window.

    Layout (800 × 480 design px; scales proportionally at fullscreen):
      ┌─────────────────────────────────────────────────────────────────┐
      │  Alert / status bar                                             │
      ├────────────┬────────────────────────────────────┬───────────────┤
      │ Left panel │       Speedometer (expands)         │  Right panel  │
      │  SOC %     │    speed arc + digital km/h         │  Current A    │
      │  Motor °C  │                                     │  Ctrl Temp °C │
      ├────────────┴────────────────────────────────────┴───────────────┤
      │  Controls bar   START / STOP  ·  version label                  │
      └─────────────────────────────────────────────────────────────────┘

    Battery Current is derived as Power (W) / Voltage (V) because the
    SiliXcon LYNX protocol does not broadcast current directly.
    """

    _SIDE_W = 155   # px width of left / right panels at the 800×480 design size

    # Page-navigation touch targets. 64×90 px on the 800×480 panel is roughly
    # 13×18 mm on a 7" screen — comfortably above the ~10 mm that a gloved
    # fingertip needs to hit reliably in a moving car.
    _NAV_BTN_H = 64
    _NAV_BTN_W = 90

    _SCREEN_NAMES = ("DS001", "DS002")

    # Height the pit-message strip takes WHEN a message is showing. It is hidden
    # the rest of the time — an empty box permanently occupying the screen is
    # clutter on instruments the driver reads at speed.
    _PIT_BANNER_H = 46

    # Target speed is a permanent readout — it always has a value to show, so it
    # keeps its space. The turn warning, like the pit message, is an EVENT: it
    # is hidden between corners rather than leaving an empty strip on screen.
    _TARGET_H = 40
    _TURN_ALERT_H = 44

    # Within this much of target counts as on-pace (green). Wide enough that a
    # driver holding a steady line is not nagged by a permanently amber readout.
    _TARGET_TOLERANCE_KMH = 5.0

    def __init__(self):
        super().__init__()
        self.setWindowTitle("EV Racing HUD  |  SiliXcon LYNX")
        self.resize(800, 480)

        self._worker: Optional[CANWorker] = None
        self._last_voltage: float = 1.0   # kept > 0 to avoid divide-by-zero
        # Until the BMS sends a real battery current, the HUD falls back to
        # I = P / V. Latches True on the first genuine reading and never goes
        # back, so an estimate can't overwrite a measurement.
        self._have_real_current: bool = False
        # Newest indicator snapshot, so a resize repaints them as they were.
        self._last_flags_shown: dict = {}
        # Target-speed state. None = no profile loaded, shown as a grey dash
        # rather than a target of zero the driver would try to match.
        self._target_kmh = None
        self._target_strategy: str = ""
        self._last_speed_kmh: float = 0.0
        self._turn_active: bool = False

        # UI scale factor (1.0 at the 800×480 design size, grows on fullscreen
        # displays) plus the current text colors, so resizeEvent can re-apply
        # font sizes without losing the live status/alert coloring.
        self._sc: float = 1.0
        self._status_color: str = _DIM
        self._alert_color: str = _LIME
        self._pit_color: str = _CYAN   # pit-to-driver message banner color

        self._build_ui()
        self._install_shortcuts()
        self.setCursor(Qt.BlankCursor)

    # ── UI construction ──────────────────────────────────────────────────── #
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(5, 5, 5, 5)
        vbox.setSpacing(4)

        vbox.addWidget(self._build_alert_bar())

        # Pit-to-driver banner sits OUTSIDE the page stack: both DS001 and DS002
        # must show it, and a message the driver paged away from is a message
        # they didn't get. Same reasoning puts faults in the shared alert bar.
        # Turn warning above the pit banner: both are shared chrome, visible on
        # DS001 and DS002 alike. A corner does not stop existing because the
        # driver paged to the electrical screen.
        vbox.addWidget(self._build_turn_alert())
        vbox.addWidget(self._build_pit_banner())

        # ── Paged screens ────────────────────────────────────────────────── #
        self._screens = QStackedWidget()
        self._screens.addWidget(self._build_screen_ds001())
        self._screens.addWidget(self._build_screen_ds002())
        vbox.addWidget(self._screens, stretch=1)

        vbox.addWidget(self._build_controls_bar())
        self._update_page_indicator()

    def _build_alert_bar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("alertBar")
        self._alert_bar = frame

        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(0)

        self._status_lbl = QLabel("● IDLE  —  Press START CAN")
        self._status_lbl.setStyleSheet(
            f"color: {_DIM}; font-size: 11px; font-weight: bold; letter-spacing: 1px;"
        )
        layout.addWidget(self._status_lbl)
        layout.addStretch()

        # Active power map — in the shared bar so it is visible on BOTH screens.
        # The driver must be able to confirm which map is live at a glance
        # without paging, so it never moves and never scrolls away.
        self._map_lbl = QLabel("MAP —")
        self._map_lbl.setAlignment(Qt.AlignCenter)
        self._apply_map_style(active=False)
        layout.addWidget(self._map_lbl)
        layout.addSpacing(12)

        self._alert_lbl = QLabel("")
        self._alert_lbl.setStyleSheet(
            f"color: {_LIME}; font-size: 11px; font-weight: bold;"
        )
        layout.addWidget(self._alert_lbl)

        return frame

    def _build_left_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("panel")
        frame.setFixedWidth(self._SIDE_W)
        self._left_panel = frame

        vbox = QVBoxLayout(frame)
        # Margins and spacing wide enough that a gauge never sits flush against
        # the panel border or its neighbour — the gauges draw right to their own
        # edges, so all the separation between them comes from here.
        vbox.setContentsMargins(6, 8, 6, 8)
        vbox.setSpacing(8)

        self._soc_gauge = MiniGauge("SOC", "%", 100.0, C_LIME, decimals=0)
        self._motor_temp_gauge = MiniGauge(
            "MOTOR TEMP", "°C", 120.0, C_CYAN, decimals=0,
            warn_threshold=MOTOR_TEMP_WARN, alert_threshold=MOTOR_TEMP_CRIT,
        )

        # ── VALIDATION READOUT (temporary) ────────────────────────────────── #
        # Raw sensor resistance beside the temperature it converts to, so the
        # table can be checked against a reference thermometer while the car is
        # on the bench. Both come from ONE signal carrying ONE frame, so what is
        # shown here is always a matched pair, never two different samples.
        # Delete this label and its updates in _on_motor_temp once the
        # conversion is signed off — the gauge above is the race-day display.
        self._motor_raw_lbl = QLabel("Ω —   |   °C —")
        self._motor_raw_lbl.setAlignment(Qt.AlignCenter)
        # 10px, not 13: at 13 this line was wider than the 155 px side panel and
        # lost its "Ω" off the left edge and its last digit off the right — the
        # one readout whose whole purpose is being read exactly.
        self._motor_raw_lbl.setStyleSheet(
            f"color: {_DIM}; font-size: 10px; font-family: 'Consolas', monospace;"
            f"border: 1px solid {_BORDER}; border-radius: 3px; padding: 2px;"
        )

        vbox.addWidget(self._soc_gauge, stretch=1)
        vbox.addWidget(self._motor_temp_gauge, stretch=1)
        vbox.addWidget(self._motor_raw_lbl)

        return frame

    def _build_tacho_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("panel")

        vbox = QVBoxLayout(frame)
        vbox.setContentsMargins(0, 0, 0, 0)

        self._tacho = TachometerWidget()
        vbox.addWidget(self._tacho)

        # TARGET SPEED — right under the actual speed, because the only useful
        # way to read a target is against what you are currently doing.
        # Text comes from _apply_target_style() below — it renders "TARGET —"
        # until a profile is loaded, so there is nothing to seed here.
        self._target_lbl = QLabel("")
        self._target_lbl.setAlignment(Qt.AlignCenter)
        self._target_lbl.setFixedHeight(self._TARGET_H)
        self._target_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._target_lbl.setMinimumWidth(1)
        self._apply_target_style()
        vbox.addWidget(self._target_lbl)

        # NOTE: the pit-message banner used to be created here. It now lives in
        # _build_pit_banner(), above the page stack, so it is visible on both
        # screens. A stray `self._pit_lbl = QLabel("")` was left behind here by
        # that move, and because this panel is built AFTER _build_pit_banner()
        # it silently rebound self._pit_lbl to a label that was never added to
        # any layout — Qt then showed it as a floating top-level window. Do not
        # reintroduce a _pit_lbl assignment in this method.
        return frame

    # ── Shared chrome (visible on every screen) ──────────────────────────── #
    def _build_pit_banner(self) -> QLabel:
        """Pit-to-driver message strip — shown ONLY while a message is live.

        Lives above the page stack rather than inside a screen: a message the
        driver has paged away from is a message they never received.

        Hidden when there is nothing to say. The strip is the only element on
        the HUD that appears and disappears; everything else (target speed, turn
        warning, indicators) holds its space permanently. That is deliberate —
        a pit message is an event, not a reading, and reserving a permanently
        empty box for it costs screen height that the speed number can use.
        """
        self._pit_lbl = QLabel("")
        self._pit_lbl.setAlignment(Qt.AlignCenter)
        self._pit_lbl.setWordWrap(True)
        # Fixed height reserved up-front. Two lines' worth at the design scale,
        # so a longer message wraps inside the box instead of growing it.
        self._pit_lbl.setFixedHeight(self._PIT_BANNER_H)
        # Ignored (not Expanding) horizontally: a word-wrapped label reports a
        # size hint as wide as its text, and with a long pit message that hint
        # widened the whole window — which then rescaled every widget on screen.
        # Ignored makes the label take whatever width it is given and never ask
        # for more, so the message content cannot move anything.
        self._pit_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        # An explicit minimum width is also required: a word-wrapped QLabel still
        # reports a minimumSizeHint based on its text, and the size POLICY does
        # not override that. Without this, a long message pushed the window
        # wider, which rescaled the whole HUD — the layout shift we are removing.
        self._pit_lbl.setMinimumWidth(1)
        self._pit_has_message = False
        # Kept so the strip can be re-rendered at a new scale on resize: the
        # caption/value type sizes are baked into the rich text, not just the
        # stylesheet, so the text has to be rebuilt rather than restyled.
        self._pit_cat = ""
        self._pit_val = ""
        self._pit_lbl.setVisible(False)
        return self._pit_lbl

    def _build_turn_alert(self) -> QLabel:
        """Upcoming-corner warning — shown only while a corner is coming up.

        It is an in-layout strip, never a floating window: an earlier bug made
        the pit banner a top-level window sitting over the instruments, and that
        must not be repeated here. Between corners it is simply hidden, so an
        idle lap shows gauges rather than an empty black band.
        """
        self._turn_lbl = QLabel("")
        self._turn_lbl.setAlignment(Qt.AlignCenter)
        self._turn_lbl.setFixedHeight(self._TURN_ALERT_H)
        self._turn_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._turn_lbl.setMinimumWidth(1)
        self._turn_active = False
        # Live corner, kept for the same reason as _pit_cat/_pit_val above.
        self._turn_dist_m = 0.0
        self._turn_max_kmh = 0.0
        self._turn_severity = "none"
        self._turn_lbl.setVisible(False)
        return self._turn_lbl

    # ── Message-strip presentation ───────────────────────────────────────── #
    # Both strips follow one rule: a small muted CAPTION says what the number
    # is, a large bright VALUE says what it is. That hierarchy is what lets a
    # driver read either strip in the fraction of a second they can spare —
    # a single line of same-sized shouting capitals has to be read word by word.
    @staticmethod
    def _strip_html(pairs, caption_px: int, value_px: int,
                    value_colour: str, nowrap: bool = True) -> str:
        """Rich text for a strip: [(caption, value), ...] laid out in one line.

        A caption may be "" (a message with no category is just a message).
        Everything is HTML-escaped: pit text arrives from Firebase, and a stray
        '<' in a hand-typed message must not silently eat the rest of it.

        `nowrap` turns the spaces INSIDE a caption or value into non-breaking
        ones. A fixed-height strip has no second line to wrap onto, so a break
        there does not wrap, it deletes: "FAST 189S" silently rendered as "FAST"
        with the rest laid out below the visible area. The pit strip passes
        False, because a long instruction genuinely may use its second line.
        """
        def esc(text: str) -> str:
            out = html.escape(text)
            return out.replace(" ", "&nbsp;") if nowrap else out

        chunks = []
        for caption, value in pairs:
            bits = []
            if caption:
                bits.append(
                    f'<span style="font-size:{caption_px}px; color:{_CAPTION};">'
                    f'{esc(caption.upper())}</span>')
            if value:
                bits.append(
                    f'<span style="font-size:{value_px}px; color:{value_colour};'
                    f' font-weight:600;">{esc(str(value))}</span>')
            if bits:
                # Two spaces between a caption and its value, not one: at these
                # sizes a single space lets "STRATEGY" and "HOLD PACE" collide
                # into one word-shape, which is what made the strip look
                # crowded even when the type sizes were right.
                chunks.append("&nbsp;&nbsp;".join(bits))
        # A dot separates the groups — quieter than a dash, and it does not read
        # as a minus sign next to a number. Set at the VALUE size so it is not a
        # speck between two large numbers, but in the caption colour so it stays
        # punctuation rather than content.
        sep = (f'&nbsp;&nbsp;&nbsp;<span style="font-size:{value_px}px;'
               f' color:{_CAPTION};">·</span>&nbsp;&nbsp;&nbsp;')
        return sep.join(chunks)

    def _apply_strip_style(self, label: QLabel, accent: str,
                           wash: float) -> None:
        """Common look: an accent wash, a hairline of the same accent, rounded.

        Deliberately NOT a filled block. See _tint() for why.
        """
        radius = max(4, int(7 * self._sc))
        pad_v = max(2, int(4 * self._sc))
        pad_h = max(10, int(18 * self._sc))
        # The outline carries most of the definition and the fill stays light:
        # a heavy fill over this near-black background muddies the accent colour
        # (red at 20 % reads as brown), while a crisp edge round a light wash
        # keeps it recognisably red.
        label.setStyleSheet(
            f"background-color: {_tint(accent, wash)};"
            f"border: {max(1, int(self._sc))}px solid "
            f"{_tint(accent, min(0.85, wash * 4.5))};"
            f"border-radius: {radius}px; padding: {pad_v}px {pad_h}px;"
            f"letter-spacing: 1px;"
        )

    def _fade_in(self, widget) -> None:
        """Fade a strip in over 180 ms instead of snapping it on.

        A strip that appears instantly registers as something flickering at the
        edge of vision; the same strip fading in over a fifth of a second reads
        as arriving. It is the cheapest possible animation — one opacity effect,
        reused, never recreated — because this runs on a Pi driving a 7" panel.
        """
        anim = getattr(widget, "_fade_anim", None)
        if anim is None:
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", widget)
            anim.setDuration(180)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            widget._fade_anim = anim
        anim.stop()
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.start()

    def _apply_turn_style(self, severity: str = "none") -> None:
        """Hidden (no corner), amber (soft), or red (hard).

            ⚠  TURN IN  120 m  ·  MAX  34 km/h

        The distance and the limit are the two numbers a driver acts on, so they
        are the two large ones; the words around them are captions.
        """
        if severity == "none":
            self._turn_lbl.setVisible(False)
            return

        self._turn_lbl.setVisible(True)
        accent = _RED if severity == "hard" else _ORANGE
        caption_px = max(11, int(13 * self._sc))
        value_px = max(17, int(22 * self._sc))
        glyph = (f'<span style="font-size:{value_px}px; color:{accent};">'
                 f'⚠</span>&nbsp;&nbsp;')
        self._turn_lbl.setText(glyph + self._strip_html(
            [("turn in", f"{self._turn_dist_m:.0f} m"),
             ("max", f"{self._turn_max_kmh:.0f} km/h")],
            caption_px, value_px, _WHITE))
        # A hard corner gets a stronger wash, not a different shape: severity
        # should read as intensity at a glance, without re-reading the numbers.
        self._apply_strip_style(self._turn_lbl, accent,
                                0.15 if severity == "hard" else 0.10)

    # ── Screen DS001 — the race screen ───────────────────────────────────── #
    def _build_screen_ds001(self) -> QWidget:
        """Speed, SOC, power, temperatures, and the boolean indicator row."""
        page = QWidget()
        vbox = QVBoxLayout(page)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self._build_left_panel())
        row.addWidget(self._build_tacho_panel(), stretch=1)
        row.addWidget(self._build_right_panel())
        vbox.addLayout(row, stretch=1)

        vbox.addWidget(self._build_indicator_row())
        return page

    def _build_indicator_row(self) -> QFrame:
        """Boolean status lights: parking brake, lights, ECU, reverse.

        Big, flat, always in the same order and always in the same place — a
        driver checks these by position, not by reading them. Each stays visible
        when inactive (dimmed) rather than disappearing, so a dark REV light
        means "not in reverse" and never "the indicator is missing".
        """
        frame = QFrame()
        frame.setObjectName("panel")
        frame.setFixedHeight(46)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(6)

        # (key, short label, colour when active)
        self._INDICATORS = [
            ("ecu_on",        "ECU",   _LIME),
            ("parking_brake", "BRAKE", _RED),
            ("lights_on",     "LIGHTS", _CYAN),
            ("reverse",       "REV",   _ORANGE),
        ]
        # Start every indicator UNKNOWN. Until a frame or a GPIO read says
        # otherwise, we genuinely do not know any of these.
        self._indicator_lbls = {}
        for key, text, _colour in self._INDICATORS:
            lbl = QLabel(f"{text} ?")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self._indicator_lbls[key] = lbl
            layout.addWidget(lbl)
        # {} yields None for every key -> all four render as UNKNOWN.
        self._apply_indicator_styles({})
        return frame

    def _apply_indicator_styles(self, flags: dict) -> None:
        """Repaint the indicator row. Three states, not two.

        None means "no source" — the switch is not wired to the Pi yet, or the
        pin could not be read. That is shown as a dashed outline and a "?" and
        is deliberately NOT drawn the same as OFF: telling a driver the parking
        brake is released when nobody actually knows is the one mistake this
        row must not make.
        """
        for key, text, colour in self._INDICATORS:
            value = flags.get(key)
            lbl = self._indicator_lbls[key]

            if value is None:                       # unknown — no source
                lbl.setText(f"{text} ?")
                lbl.setStyleSheet(
                    f"color: {_OFF}; background: transparent;"
                    f"border: 2px dashed {_OFF}; border-radius: 4px;"
                    f"font-size: {int(13 * self._sc)}px; font-weight: bold;"
                    f"letter-spacing: 1px;"
                )
                continue

            on = bool(value)
            lbl.setText(text)
            lbl.setStyleSheet(
                f"color: {colour if on else _OFF};"
                f"background: {'rgba(255,255,255,0.10)' if on else 'transparent'};"
                f"border: 2px solid {colour if on else _OFF};"
                f"border-radius: 4px;"
                f"font-size: {int(13 * self._sc)}px; font-weight: bold;"
                f"letter-spacing: 1px;"
            )

    # ── Screen DS002 — the electrical screen ─────────────────────────────── #
    def _build_screen_ds002(self) -> QWidget:
        """Speed and SOC again (the driver always needs those), plus the pack
        and motor electrical picture: voltage, battery current, motor current,
        and both temperatures."""
        page = QWidget()
        vbox = QVBoxLayout(page)
        # Same reasoning as the side panels: the gap between two gauges is this
        # spacing and nothing else, because each gauge paints to its own edge.
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._ds2_speed = MiniGauge("SPEED", "km/h", float(SPEED_MAX), C_CYAN,
                                    decimals=0)
        self._ds2_soc = MiniGauge("SOC", "%", 100.0, C_LIME, decimals=0)
        self._ds2_voltage = MiniGauge("BATT VOLTS", "V", 120.0, C_LIME, decimals=1)
        for g in (self._ds2_speed, self._ds2_soc, self._ds2_voltage):
            top.addWidget(g, stretch=1)
        vbox.addLayout(top, stretch=1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self._ds2_batt_current = MiniGauge("BATT CURRENT", "A", 60.0, C_ORANGE,
                                           alert_threshold=40.0, decimals=1)
        self._ds2_motor_current = MiniGauge("MOTOR CURRENT", "A", 120.0, C_ORANGE,
                                            alert_threshold=90.0, decimals=1)
        self._ds2_motor_temp = MiniGauge(
            "MOTOR TEMP", "°C", 120.0, C_CYAN, decimals=0,
            warn_threshold=MOTOR_TEMP_WARN, alert_threshold=MOTOR_TEMP_CRIT)
        self._ds2_cell_temp = MiniGauge(
            "MAX CELL", "°C", 80.0, C_CYAN, decimals=0,
            warn_threshold=CELL_TEMP_WARN, alert_threshold=CELL_TEMP_CRIT)
        for g in (self._ds2_batt_current, self._ds2_motor_current,
                  self._ds2_motor_temp, self._ds2_cell_temp):
            bottom.addWidget(g, stretch=1)
        vbox.addLayout(bottom, stretch=1)

        return page

    # ── Touch pagination ─────────────────────────────────────────────────── #
    def _go_screen(self, index: int) -> None:
        """Switch pages, wrapping around so one button can cycle everything."""
        count = self._screens.count()
        self._screens.setCurrentIndex(index % count)
        self._update_page_indicator()

    def _next_screen(self) -> None:
        self._go_screen(self._screens.currentIndex() + 1)

    def _prev_screen(self) -> None:
        self._go_screen(self._screens.currentIndex() - 1)

    def _update_page_indicator(self) -> None:
        """Refresh 'DS001  ● ○  1/2' after a page change."""
        idx, count = self._screens.currentIndex(), self._screens.count()
        dots = "  ".join("●" if i == idx else "○" for i in range(count))
        name = self._SCREEN_NAMES[idx] if idx < len(self._SCREEN_NAMES) else "?"
        self._page_lbl.setText(f"{name}   {dots}   {idx + 1}/{count}")

    def _build_right_panel(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("panel")
        frame.setFixedWidth(self._SIDE_W)
        self._right_panel = frame

        vbox = QVBoxLayout(frame)
        # Matches the left panel — see the note there.
        vbox.setContentsMargins(6, 8, 6, 8)
        vbox.setSpacing(8)

        # DS001 asks for motor power, the ECU temperature and the hottest
        # battery cell. Battery current moved to DS002, where the full
        # electrical picture lives — it was the least glanceable of the four.
        self._power_gauge = MiniGauge(
            "MOTOR POWER", "W", 3000.0, C_CYAN, decimals=0
        )
        self._temp_gauge = MiniGauge(
            "CTRL TEMP", "°C", 100.0, C_CYAN, decimals=0,
            warn_threshold=CTRL_TEMP_WARN, alert_threshold=CTRL_TEMP_CRIT,
        )
        # Hottest cell in the pack — the battery-safety number. Its resting
        # colour is cyan like the others; red is reserved for the alert tier, so
        # a red gauge always means the same thing wherever it appears.
        self._cell_temp_gauge = MiniGauge(
            "MAX CELL", "°C", 80.0, C_CYAN, decimals=0,
            warn_threshold=CELL_TEMP_WARN, alert_threshold=CELL_TEMP_CRIT,
        )

        vbox.addWidget(self._power_gauge, stretch=1)
        vbox.addWidget(self._temp_gauge, stretch=1)
        vbox.addWidget(self._cell_temp_gauge, stretch=1)

        return frame

    def _build_controls_bar(self) -> QFrame:
        """Bottom bar: page navigation (large, gloved-hand targets) + CAN control.

        Navigation is deliberately the biggest thing here. _NAV_BTN_H is 64 px
        against the 34 px of the CAN buttons: a driver changes pages while
        moving and must not have to aim, whereas START/STOP is a pit-lane
        action. The two nav buttons also sit at opposite ends of the bar, so a
        mis-hit is a wrong page, never an accidental STOP CAN.
        """
        frame = QFrame()
        frame.setFixedHeight(self._NAV_BTN_H + 8)
        self._controls_bar = frame

        layout = QHBoxLayout(frame)
        layout.setContentsMargins(5, 3, 5, 3)
        layout.setSpacing(8)

        self._prev_btn = QPushButton("◀")
        self._next_btn = QPushButton("▶")
        for btn, slot in ((self._prev_btn, self._prev_screen),
                          (self._next_btn, self._next_screen)):
            btn.setObjectName("navBtn")
            btn.setFixedHeight(self._NAV_BTN_H)
            btn.setMinimumWidth(self._NAV_BTN_W)
            btn.clicked.connect(slot)

        # Which screen am I on — always between the two arrows, so the driver's
        # eye lands on it while reaching for either button.
        self._page_lbl = QLabel("")
        self._page_lbl.setAlignment(Qt.AlignCenter)
        self._page_lbl.setStyleSheet(
            f"color: {_WHITE}; font-size: 15px; font-weight: bold;"
            f"letter-spacing: 2px;"
        )

        layout.addWidget(self._prev_btn)
        layout.addStretch()
        layout.addWidget(self._page_lbl)
        layout.addStretch()
        layout.addWidget(self._next_btn)

        # NO START/STOP CAN BUTTONS — deliberately.
        #
        # START was never needed: main.py calls _start_can() itself at boot, so
        # the driver has never had to press it. STOP was worse than useless — it
        # was the one control on a touchscreen the driver operates while moving
        # that could kill the HUD, the pit telemetry feed and the odometer, and
        # it sat right next to the page-change buttons they DO use. One brush of
        # a glove and the car goes dark to the pit wall.
        #
        # Nothing is lost by removing them: the worker already recovers from bus
        # faults on its own (it reopens the interfaces and reports NOT CONNECTED
        # / SILENCE in the status bar), so the buttons were not a recovery path,
        # and the status bar still shows CAN state at a glance.
        #
        # The methods stay — main.py and closeEvent call them — and the bench
        # keyboard shortcuts below still give full manual control when a laptop
        # is plugged in. See _install_shortcuts().
        self._ver_lbl = QLabel("EV HUD v2.0  |  SiliXcon LYNX  @  1 Mbps")
        self._ver_lbl.setStyleSheet(f"color: {_DIM}; font-size: 9px;")
        layout.addWidget(self._ver_lbl)
        self._fit_controls_bar()

        return frame

    def _fit_controls_bar(self) -> None:
        """Drop the version string when the bar is too narrow to hold it.

        This label alone asked for 360 px, which made the controls bar demand
        880 px — wider than the 800 px panel in the car. A Qt layout does not
        scale down to fit; it clips, so the bottom of the HUD lost the right end
        of itself and the nav buttons were cut off mid-glyph.

        Of everything on this bar, the version string is the one thing a driver
        never needs: it is a bench aid, and the same text is printed to the log
        at every start. So it is what gives way. The buttons and the page
        indicator always keep their space.
        """
        needed = (self._prev_btn.minimumWidth() + self._next_btn.minimumWidth()
                  + self._page_lbl.minimumSizeHint().width()
                  + self._ver_lbl.minimumSizeHint().width()
                  + int(40 * self._sc))          # margins + the two spacings
        # Measured against the SCREEN, not our own width. Our width is partly a
        # RESULT of this decision — Qt grows a window to its layout's minimum —
        # so testing against it says "it fits" purely because the label already
        # forced the window wide enough to hold it.
        self._ver_lbl.setVisible(self._reference_size()[0] >= needed)

    def _reference_size(self) -> tuple:
        """(width, height) to size the UI against: the SCREEN where possible.

        Never our own geometry. Everything scaled from the window fed back into
        the window's minimum size and grew it again on the next event — see the
        note in resizeEvent. The screen cannot be pushed around by our layout,
        so it is the one stable input.
        """
        screen = self.screen()
        if screen is not None:
            avail = screen.availableSize()
            return avail.width(), avail.height()
        return self.width(), self.height()       # no screen (offscreen/headless)

    # ── CAN worker lifecycle ──────────────────────────────────────────────── #
    def _start_can(self) -> None:
        if self._worker and self._worker.isRunning():
            return

        self._last_voltage = 1.0
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
        self._worker.motor_map_updated.connect(self._on_motor_map)
        self._worker.motor_current_updated.connect(self._on_motor_current)
        self._worker.battery_current_updated.connect(self._on_battery_current)
        self._worker.cell_temp_updated.connect(self._on_cell_temp)
        self._worker.vehicle_flags_updated.connect(self._on_vehicle_flags)
        self._worker.target_speed_updated.connect(self._on_target_speed)
        self._worker.turn_alert_updated.connect(self._on_turn_alert)

        self._worker.start()

    def _stop_can(self) -> None:
        if self._worker:
            self._worker.stop()
            self._worker = None

    def _restart_can(self) -> None:
        """Bench helper (Ctrl+R): tear the worker down and bring it back up."""
        self._stop_can()
        self._start_can()

    def _install_shortcuts(self) -> None:
        """Keyboard-only CAN control, for bench work with a laptop attached.

        Deliberately keyboard rather than on-screen: the driver's touchscreen
        should not carry a control that can take the car off the air, but an
        engineer with a keyboard plugged in still wants one.
            Ctrl+R        restart the CAN worker
            Ctrl+T        stop it
            Ctrl+Shift+Q  quit the HUD for good
            Alt+F4        same — the key everyone reaches for first
            Ctrl+Shift+C  show/hide the mouse cursor

        Quit and cursor are the escape hatches. The HUD is fullscreen with a
        hidden cursor and the boot wrapper restarts it, so without them a
        plugged-in keyboard and mouse are useless for debugging on the car.

        Alt+F4 is bound here as well as being handled by closeEvent, because
        which of the two fires depends on the compositor: wayfire/labwc may
        swallow the chord and send the window a close request instead of
        delivering the key to Qt. Both routes now end the same way — exit 42,
        wrapper stays down. Ctrl+Shift+Q remains for the case where the
        compositor's Alt+F4 is disabled entirely.

        It is not a risk to the driver: the car's HUD has no keyboard attached
        during a race, and F4 is nowhere near a gloved hand on a touchscreen.
        """
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._restart_can)
        QShortcut(QKeySequence("Ctrl+T"), self, activated=self._stop_can)
        QShortcut(QKeySequence("Ctrl+Shift+Q"), self, activated=self._quit_for_good)
        QShortcut(QKeySequence("Alt+F4"), self, activated=self._quit_for_good)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self._toggle_cursor)

    def _quit_for_good(self) -> None:
        """Exit with code 42 — the wrapper's "stay down" signal.

        deploy/start_hud.sh treats every other exit code as a crash and relaunches
        the HUD. 42 is how an engineer says the exit was intentional.
        """
        print("[HUD] quit requested — exiting with code 42 (wrapper will stay down).")
        self._stop_can()
        QApplication.exit(42)

    def _toggle_cursor(self) -> None:
        """Show or hide the mouse pointer (hidden by default for the driver)."""
        hidden = self.cursor().shape() == Qt.BlankCursor
        self.setCursor(Qt.ArrowCursor if hidden else Qt.BlankCursor)

    # ── Signal slots ──────────────────────────────────────────────────────── #
    # Values shown on BOTH screens are pushed to both gauges here, rather than
    # refreshed when a page becomes visible: the hidden page then already holds
    # live data, so switching screens shows the current value immediately
    # instead of a stale one that corrects itself a moment later.
    @Slot(int)
    def _on_rpm(self, rpm: int) -> None:
        self._tacho.set_rpm(rpm)
        speed = _rpm_to_speed(rpm)
        self._ds2_speed.set_value(speed)
        # Remembered so the target readout can show the delta without waiting
        # for the next profile tick.
        self._last_speed_kmh = abs(speed)
        if self._target_kmh is not None:
            self._apply_target_style(self._last_speed_kmh - self._target_kmh)

    @Slot(float)
    def _on_voltage(self, volts: float) -> None:
        self._last_voltage = max(volts, 0.1)   # guard against division by zero
        self._ds2_voltage.set_value(volts)

    @Slot(int)
    def _on_soc(self, pct: int) -> None:
        self._soc_gauge.set_value(float(pct))
        self._ds2_soc.set_value(float(pct))

    @Slot(int)
    def _on_ctrl_temp(self, deg_c: int) -> None:
        self._temp_gauge.set_value(float(deg_c))

    @Slot(float, float, str)
    def _on_motor_temp(self, ohms: float, celsius: float, status: str) -> None:
        """Motor PT1000 reading: raw resistance + the °C it converts to.

        Both arrive in one signal from one CAN frame, so the validation readout
        below can never pair an Ω with a °C from a different sample.
        `celsius` is -1000.0 when the resistance could not be converted (see
        can_worker._decode_temp) — Qt signals cannot carry None.
        """
        converted = celsius > -999.0

        # The gauge is the driver's display: only ever show a real temperature.
        # On a bad or missing reading, hold it at 0 rather than inventing one.
        shown = celsius if converted else 0.0
        self._motor_temp_gauge.set_value(shown)
        self._ds2_motor_temp.set_value(shown)

        # The validation readout tells the whole truth, including WHY a
        # resistance did not convert — that is what makes a wiring fault
        # diagnosable on the bench instead of just looking like a cold motor.
        if status == pt1000.STATUS_NO_READING:
            self._motor_raw_lbl.setText("Ω —   |   °C —")
        elif converted:
            self._motor_raw_lbl.setText(f"Ω {ohms:.1f}   |   °C {celsius:.1f}")
        else:
            reason = {
                pt1000.STATUS_BELOW_RANGE: "below table",
                pt1000.STATUS_ABOVE_RANGE: "open circuit?",
            }.get(status, status or "unconvertible")
            self._motor_raw_lbl.setText(f"Ω {ohms:.1f}   |   {reason}")

    @Slot(str, int)
    def _on_motor_map(self, name: str, raw: int) -> None:
        """Active power map badge. raw < 0 means the bus went quiet."""
        if raw < 0 or not name:
            self._map_lbl.setText("MAP —")
            self._apply_map_style(active=False)
            return
        self._map_lbl.setText(name.upper())
        # Reverse gets the warning colour: it should never be live on track.
        self._apply_map_style(active=True, warn="Reverse" in name)

    def _apply_map_style(self, active: bool, warn: bool = False) -> None:
        colour = _ORANGE if warn else (_CYAN if active else _OFF)
        self._map_lbl.setStyleSheet(
            f"color: {colour}; font-size: {int(13 * self._sc)}px;"
            f"font-weight: bold; letter-spacing: 1px;"
            f"border: 2px solid {colour}; border-radius: 4px; padding: 1px 8px;"
        )

    def _apply_target_style(self, delta_kmh: float = None) -> None:
        """Render the target readout in the same caption/value form as the strips.

            TARGET 92  ·  Δ -3  ·  FAST 189S

        Green when within tolerance, amber when off, grey when there is no
        profile. Colour rather than a number the driver has to subtract: at
        90 km/h nobody is doing mental arithmetic.

        This used to be one line of same-sized bold text, and at the 800×480
        design size it was WIDER than the tacho panel — the driver saw
        "RGET 92 (-49) FAST_18" with both ends sliced off. Sizing the words down
        and the numbers up fixes the overflow and puts the emphasis where it
        belongs at the same time.
        """
        if delta_kmh is None:
            colour = _OFF
        elif abs(delta_kmh) <= self._TARGET_TOLERANCE_KMH:
            colour = _LIME
        else:
            colour = _ORANGE

        if self._target_kmh is None:
            pairs = [("target", "—")]
        else:
            pairs = [("target", f"{self._target_kmh:.0f}"),
                     ("Δ", "—" if delta_kmh is None else f"{delta_kmh:+.0f}")]
            if self._target_strategy:
                # Caption-only pair: which profile is live is context, not a
                # number to act on. Underscores become a NON-BREAKING space —
                # "FAST 189S" is a name a driver reads, "fast_189s" is a
                # filename, and an ordinary space let the line break there and
                # drop "189S" onto an invisible second row.
                pairs.append((self._target_strategy.replace("_", " "), ""))

        # Fit by MEASURING, not by counting characters: this strip is boxed in
        # by the two side panels, and how much fits depends on the UI scale, the
        # strategy name and the width of the digits all at once.
        #
        # If it will not fit even at the smallest size, the STRATEGY NAME is
        # dropped rather than the numbers shrunk further. "MED SLOW 220S" beside
        # a three-digit delta is the one combination that overflows, and of the
        # three things on this strip the profile's name is the one a driver can
        # look up elsewhere — the target and the delta are what they are
        # steering by.
        variants = [pairs]
        if len(pairs) > 2:
            variants.append(pairs[:2])
        for variant in variants:
            if self._fit_strip_text(
                    self._target_lbl,
                    lambda cap_px, val_px, v=variant: self._strip_html(
                        v, cap_px, val_px, colour),
                    (18, 16, 14), caption_ratio=12 / 18):
                break
        self._apply_strip_style(self._target_lbl, colour, 0.09)

    def _fit_strip_text(self, label: QLabel, build, value_sizes,
                        caption_ratio: float) -> bool:
        """Set the largest of `value_sizes` whose text still fits on one line.

        `build(caption_px, value_px)` returns the rich text to try. sizeHint()
        on a non-wrapping rich-text label is its ideal single-line width, so
        this is a real measurement of the rendered result rather than a guess
        from the character count — which is what a proportional caption beside
        monospace digits needs.

        Returns True if it fitted. Before the first layout the label has no
        meaningful width: the largest size is used, True is returned, and the
        next data update (or the resize) re-fits it against a real width.
        """
        avail = label.width()
        for value_px in value_sizes:
            cap_px = max(9, int(value_px * caption_ratio * self._sc))
            val_px = max(11, int(value_px * self._sc))
            label.setText(build(cap_px, val_px))
            if avail < 50 or label.sizeHint().width() <= avail:
                return True
        return False

    @Slot(float, str)
    def _on_target_speed(self, target_kmh: float, strategy: str) -> None:
        """Target speed for this point on the lap, from the active profile."""
        self._target_kmh = target_kmh
        self._target_strategy = strategy
        self._apply_target_style(self._last_speed_kmh - target_kmh)

    @Slot(float, float, float)
    def _on_turn_alert(self, distance_m: float, max_kmh: float,
                       drop_kmh: float) -> None:
        """Upcoming corner, or all-zeros to clear."""
        if distance_m <= 0.0:
            self._turn_active = False
            self._turn_severity = "none"
            self._turn_lbl.setText("")
            self._apply_turn_style("none")
            return

        was_active = self._turn_active
        self._turn_active = True
        self._turn_dist_m = distance_m
        self._turn_max_kmh = max_kmh
        # Red for a big drop: a 60 km/h scrub needs more warning than a 20.
        self._turn_severity = "hard" if drop_kmh >= 35 else "soft"
        self._apply_turn_style(self._turn_severity)
        # Fade in on ARRIVAL only. The distance updates several times a second
        # on the approach to a corner; fading on every update would flicker.
        if not was_active:
            self._fade_in(self._turn_lbl)

    @Slot(dict)
    def _on_vehicle_flags(self, flags: dict) -> None:
        # Remembered so resizeEvent can re-apply the styles at the new scale
        # without blanking the indicators back to "all off".
        self._last_flags_shown = dict(flags)
        self._apply_indicator_styles(flags)

    @Slot(float)
    def _on_motor_current(self, amps: float) -> None:
        self._ds2_motor_current.set_value(abs(amps))

    @Slot(float)
    def _on_battery_current(self, amps: float) -> None:
        """Real BMS current — preferred over the P/V estimate when available."""
        self._have_real_current = True
        self._ds2_batt_current.set_value(abs(amps))

    @Slot(float)
    def _on_cell_temp(self, deg_c: float) -> None:
        self._cell_temp_gauge.set_value(deg_c)
        self._ds2_cell_temp.set_value(deg_c)

    @Slot(int)
    def _on_power(self, watts: int) -> None:
        self._power_gauge.set_value(float(watts))
        # Fall back to I = P / V only until the BMS reports real current. The
        # derived value is badly wrong at low voltage, so a true reading wins
        # the moment one arrives.
        if not self._have_real_current:
            self._ds2_batt_current.set_value(abs(watts / self._last_voltage))

    @Slot(list)
    def _on_alerts(self, alerts: list) -> None:
        if not alerts:
            self._alert_lbl.setText("●  SYSTEM OK")
            self._alert_color = _LIME
        else:
            self._alert_color = _RED if alerts[0][1] == "error" else _ORANGE
            text = "  ▲  ".join(label for label, _ in alerts)
            self._alert_lbl.setText(f"▲  {text}")
        self._apply_alert_style()

    @Slot(str)
    def _on_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)
        m = msg.upper()
        # Order matters: "NOT CONNECTED" also contains "CONNECTED".
        if "NOT CONNECTED" in m or "ERROR" in m or "DISCONNECT" in m:
            self._status_color = _RED
        elif "SILEN" in m or "NO DATA" in m:
            self._status_color = _ORANGE
        elif "LIVE" in m or "CONNECTED" in m:
            self._status_color = _LIME
        else:
            self._status_color = _DIM
        self._apply_status_style()

    @Slot(object)
    def set_pit_message(self, payload) -> None:
        """Show a pit instruction above the speed. `payload` is a dict
        {category, value} (or None/empty to clear the banner, which hides the
        strip). Flashes bright on arrival, then settles to a calm cyan after a
        few seconds. Runs on the GUI thread (delivered via a queued signal from
        the Firebase listener)."""
        cat = val = ""
        if isinstance(payload, dict):
            cat = str(payload.get("category") or "").strip()
            val = str(payload.get("value") or "").strip()

        if not cat and not val:
            # Cleared — the strip goes away entirely rather than sitting there
            # empty, giving the space back to the instruments below it.
            self._pit_has_message = False
            self._pit_cat = self._pit_val = ""
            self._pit_lbl.setText("")
            self._pit_lbl.setVisible(False)
            return

        self._pit_has_message = True
        self._pit_cat, self._pit_val = cat, val
        self._apply_pit_style(flash=True)
        self._pit_lbl.setVisible(True)
        self._fade_in(self._pit_lbl)
        QTimer.singleShot(2500, lambda: self._apply_pit_style(flash=False))

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        """Report a CAN fault WITHOUT blocking the driver.

        This used to raise a modal QMessageBox. On a fullscreen HUD with no
        keyboard that is close to the worst possible response: the dialog covers
        the instruments the driver is using and waits for a click they may not
        be able to give while driving — turning a recoverable bus fault into a
        blank dashboard. The message now goes to the status bar and the alert
        row, which are already where the driver looks for exactly this.
        """
        self._status_lbl.setText(f"● CAN ERROR — {msg}")
        self._status_color = _RED
        self._apply_status_style()
        self._on_alerts([("CAN BUS ERROR", "error")])
        print(f"[HUD] CAN error: {msg}")   # full text still reaches the console

    # ── Responsive scaling (fullscreen) ───────────────────────────────────── #
    def _apply_status_style(self) -> None:
        px = max(9, int(11 * self._sc))
        self._status_lbl.setStyleSheet(
            f"color: {self._status_color}; font-size: {px}px; "
            "font-weight: bold; letter-spacing: 1px;"
        )

    def _apply_alert_style(self) -> None:
        px = max(9, int(11 * self._sc))
        self._alert_lbl.setStyleSheet(
            f"color: {self._alert_color}; font-size: {px}px; font-weight: bold;"
        )

    def _apply_pit_style(self, flash: bool = False) -> None:
        """Render the pit strip:

            STRATEGY  HOLD PACE

        The category is the caption, the instruction is the value. `flash` is
        the arrival state — the SAME strip in a stronger wash for 2.5 s, not a
        second design. Inverting to a filled block on arrival, as it used to,
        made the message change shape twice (arrive, then settle) for one event.

        With no message the strip is hidden, so there is nothing to style.
        """
        if not getattr(self, "_pit_has_message", False):
            return
        # A long instruction steps DOWN a type size rather than overflowing the
        # fixed-height strip. The pit types these by hand, so "BOX" and
        # "GOOD JOB — 2 LAPS TO GO, HOLD 88 ON THE BACK STRAIGHT" both have to
        # land well: 22 px on one line, 17 px on one, 14 px wrapped to two.
        # Measured against the 46 px strip — a 60-character message at 17 px
        # needs 50 px and gets its last row of pixels sliced off.
        # Past ~120 characters nothing is legible at a glance anyway, so it is
        # cut rather than shrunk further; the pit dashboard is the place to say
        # more than that.
        value = self._pit_val
        if len(value) > 120:
            value = value[:119].rstrip() + "…"
        length = len(self._pit_cat) + len(value)
        value_px = 22 if length <= 30 else (17 if length <= 55 else 14)
        self._pit_lbl.setText(self._strip_html(
            [(self._pit_cat, value)],
            max(11, int(13 * self._sc)),
            max(12, int(value_px * self._sc)), _WHITE, nowrap=False))
        self._apply_strip_style(self._pit_lbl, self._pit_color,
                                0.26 if flash else 0.12)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        # resize() can fire before the UI is built — ignore until it exists.
        if not hasattr(self, "_left_panel"):
            return

        # Scale everything relative to the 800×480 design size. Use the smaller
        # of the width/height ratios so nothing overflows on odd aspect ratios.
        # Scale from the SCREEN, not from our own window.
        #
        # Deriving it from the window created a feedback loop: everything below
        # sets FIXED heights from the scale, those raise the layout's minimum
        # height, the window grows to satisfy it, the larger window yields a
        # larger scale, and round it goes — the HUD inflated by a few pixels on
        # every single resize event until it filled the desktop. The screen size
        # cannot be pushed around by our own layout, so it is a stable input.
        #
        # At fullscreen (how the car runs) window and screen are the same, so
        # this changes nothing in production — it only stops the drift when the
        # HUD is run in a window, e.g. on a bench laptop or under test.
        ref_w, ref_h = self._reference_size()
        scale = max(1.0, min(ref_w / 800.0, ref_h / 480.0))

        # Width-driven, so it runs on EVERY resize, not only when the scale
        # changes — a window can get narrower without the scale moving at all.
        self._fit_controls_bar()

        # Re-applying an identical scale only churns widgets, so skip it.
        if abs(scale - getattr(self, "_applied_sc", -1.0)) < 0.01:
            return
        self._applied_sc = scale
        self._sc = scale
        s = self._sc

        # Side panels grow proportionally (were a hard-coded 155 px).
        side_w = int(self._SIDE_W * s)
        self._left_panel.setFixedWidth(side_w)
        self._right_panel.setFixedWidth(side_w)

        # Top alert bar + bottom controls bar heights. The controls bar is sized
        # from the NAV button, not a fixed 42 px — the nav targets are the
        # tallest thing on it, and hard-coding the old height would crush them
        # at fullscreen (the opposite of what a gloved driver needs).
        self._alert_bar.setFixedHeight(int(30 * s))
        # The message container scales with the screen but stays a FIXED height
        # at any given scale, so it still never shifts the gauges below it.
        self._pit_lbl.setFixedHeight(int(self._PIT_BANNER_H * s))
        self._turn_lbl.setFixedHeight(int(self._TURN_ALERT_H * s))
        self._target_lbl.setFixedHeight(int(self._TARGET_H * s))
        nav_h = int(self._NAV_BTN_H * s)
        self._controls_bar.setFixedHeight(nav_h + int(8 * s))

        for btn in (self._prev_btn, self._next_btn):
            btn.setFixedHeight(nav_h)
            btn.setMinimumWidth(int(self._NAV_BTN_W * s))

        self._page_lbl.setStyleSheet(
            f"color: {_WHITE}; font-size: {max(13, int(15 * s))}px;"
            f"font-weight: bold; letter-spacing: 2px;"
        )

        # Version label + live status/alert text.
        self._ver_lbl.setStyleSheet(f"color: {_DIM}; font-size: {max(9, int(9 * s))}px;")
        self._apply_status_style()
        self._apply_alert_style()
        self._apply_pit_style()
        # Re-apply the scale-dependent styles on the new-in-this-build widgets.
        self._apply_map_style(
            active=self._map_lbl.text() not in ("", "MAP —"),
            warn="REVERSE" in self._map_lbl.text().upper(),
        )
        self._apply_indicator_styles(self._last_flags_shown)
        self._apply_target_style(
            None if self._target_kmh is None
            else self._last_speed_kmh - self._target_kmh)
        # The LIVE severity, not a guess: re-applying "soft" here used to demote
        # a red corner warning to amber for as long as it stayed on screen.
        self._apply_turn_style(self._turn_severity)

    def closeEvent(self, event) -> None:  # noqa: ANN001
        """Shut down cleanly AND tell the wrapper this was intentional.

        The window can only be closed by a person — a compositor Alt+F4, a
        window-manager close, or the desktop's task switcher. Every one of those
        means "I want it gone", so the exit code must be 42; anything else and
        deploy/start_hud.sh reads it as a crash and relaunches the HUD a second
        later, which is exactly the "can't close it on the Pi" problem.
        """
        self._stop_can()
        # Close the pit-command Firebase stream if one was attached (main.py).
        reg = getattr(self, "_cmd_reg", None)
        if reg is not None:
            try:
                reg.close()
            except Exception:
                pass
        print("[HUD] window closed — exiting with code 42 (wrapper will stay down).")
        QApplication.exit(42)
        super().closeEvent(event)


if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setStyleSheet(RACING_QSS)
    win = RacingDashboard()
    win.show()
    sys.exit(app.exec())
