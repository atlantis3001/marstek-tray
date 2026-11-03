import ipaddress
import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from PyQt6.QtCore import QLockFile, QPointF, QRectF, Qt, QTimer
    from PyQt6.QtGui import QColor, QCursor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
    from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
                                 QPushButton, QSpinBox, QSystemTrayIcon)
except ImportError:  # Debian 11 only packages PyQt5
    from PyQt5.QtCore import QLockFile, QPointF, QRectF, Qt, QTimer
    from PyQt5.QtGui import QColor, QCursor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
    from PyQt5.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
                                 QPushButton, QSpinBox, QSystemTrayIcon)

from alerts import BatteryAlerts, MeterAlerts, ReachabilityAlerts
from daily_energy import DailyEnergy
from history import History
from history_chart import HistoryChart
from marstek import CUTOFF_SOC, DEFAULT_PORT, SMOOTHING_S, MarstekClient, hours_remaining, smoothed_power, state_from_power

CONFIG_PATH = Path.home() / ".config" / "marstek-tray.toml"
LOCK_PATH = Path.home() / ".cache" / "marstek-tray.lock"
ENERGY_PATH = Path.home() / ".local" / "state" / "marstek-tray.json"
HISTORY_PATH = Path.home() / ".local" / "state" / "marstek-tray-history.csv"
LOCK_WAIT_MS = 5_000
POLL_INTERVAL_MS = 60_000
# One poll out of METER_EVERY reads the CT meter instead of the battery, one out of METER_EVERY_WHEN_FULL
# when it is full: its power no longer changes, and what matters then is the export to the grid.
METER_EVERY = 5
METER_EVERY_WHEN_FULL = 2
GRID_IDLE_W = 20
MIN_MANUAL_REFRESH_S = 10
OFFLINE_AFTER_FAILURES = 2
NOTIFICATION_MS = 10_000
MAX_CUTOFF = 50

GREEN = QColor("#2e9e44")
ORANGE = QColor("#e08a00")
RED = QColor("#d32f2f")
GREY = QColor("#8a8a8a")
SUN = QColor("#f5b400")
WHITE = QColor("white")

MESSAGE_ICONS = {
    "info": QSystemTrayIcon.MessageIcon.Information,
    "warning": QSystemTrayIcon.MessageIcon.Warning,
    "critical": QSystemTrayIcon.MessageIcon.Critical,
}

RIGHT_ARROW = [(8, 45), (36, 45), (36, 38), (56, 50), (36, 62), (36, 55), (8, 55)]
# While charging, the arrow moves aside to make room for the sun: the battery charges from the solar surplus.
CHARGE_ARROW = [(26, 45), (44, 45), (44, 38), (62, 50), (44, 62), (44, 55), (26, 55)]


# The TOML written by save_config is parsed by hand: tomllib only exists from Python 3.11 on.
def load_config():
    config = {}
    for line in CONFIG_PATH.read_text().splitlines():
        key, sep, value = line.split("#", 1)[0].partition("=")
        if sep:
            config[key.strip()] = value.strip().strip('"')
    cutoff = int(config.get("cutoff", CUTOFF_SOC))
    if not 0 <= cutoff <= MAX_CUTOFF:
        raise ValueError(f"cutoff outside 0..{MAX_CUTOFF}: {cutoff}")
    return config["host"], int(config.get("port", DEFAULT_PORT)), cutoff


def save_config(host, port, cutoff):
    CONFIG_PATH.write_text(f'host = "{host}"\nport = {port}\ncutoff = {cutoff}\n')


def device_text(result):
    details = [str(result.get("device") or "Marstek device")]
    if result.get("ver") is not None:
        details.append(f"firmware {result['ver']}")
    if result.get("wifi_name"):
        details.append(f"Wi-Fi \"{result['wifi_name']}\"")
    return " · ".join(details)


class ConfigDialog(QDialog):
    def __init__(self, host, port, cutoff):
        super().__init__()
        self.setWindowTitle("Marstek Tray — settings")
        self.setMinimumWidth(460)
        self.host_edit = QLineEdit(host)
        self.host_edit.setPlaceholderText("192.168.1.100")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(port)
        self.cutoff_spin = QSpinBox()
        self.cutoff_spin.setRange(0, MAX_CUTOFF)
        self.cutoff_spin.setSuffix(" %")
        self.cutoff_spin.setValue(cutoff)

        self.probe = MarstekClient(host, port, parent=self)
        self.probe.device.connect(self.on_device)
        self.probe.failed.connect(self.on_probe_failed)
        self.probe.discovered.connect(self.on_discovered)
        self.test_button = QPushButton("Test")
        self.test_button.clicked.connect(self.test)
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.search)
        self.test_label = QLabel()
        self.test_label.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.host_edit.textChanged.connect(self.on_edited)
        self.port_spin.valueChanged.connect(self.on_edited)

        layout = QFormLayout(self)
        layout.addRow("Battery IP address:", self.host_edit)
        layout.addRow("Local API UDP port:", self.port_spin)
        layout.addRow("Battery cutoff:", self.cutoff_spin)
        probe_buttons = QHBoxLayout()
        probe_buttons.addWidget(self.test_button)
        probe_buttons.addWidget(self.search_button)
        layout.addRow("", probe_buttons)
        layout.addRow(self.test_label)
        layout.addRow(buttons)
        self.validate()

    def is_valid(self):
        try:
            ipaddress.IPv4Address(self.host_edit.text().strip())
            return True
        except ValueError:
            return False

    def validate(self):
        valid = self.is_valid()
        self.ok_button.setEnabled(valid)
        self.test_button.setEnabled(valid and not self.probe.pending)
        self.search_button.setEnabled(not self.probe.pending)

    def on_edited(self):
        self.probe.set_target(*self.target())
        self.show_result("")
        self.validate()

    def test(self):
        self.probe.set_target(*self.target())
        self.probe.get_device()
        self.show_result("Testing…")
        self.validate()

    def search(self):
        self.probe.set_target(*self.target())
        self.probe.discover()
        self.show_result("Searching…")
        self.validate()

    def on_device(self, result):
        self.show_result("✓ " + device_text(result))
        self.validate()

    def on_discovered(self, devices):
        if not devices:
            self.show_result(f"✗ No Marstek device answered on port {self.port_spin.value()}. "
                             "Try again: the battery sometimes ignores a request.", RED)
        elif len(devices) == 1:
            self.choose(devices[0])
        else:
            menu = QMenu(self)
            for device in devices:
                menu.addAction(f"{device['ip']} — {device_text(device)}", lambda device=device: self.choose(device))
            self.show_result(f"{len(devices)} devices found: pick the battery.")
            menu.exec(self.search_button.mapToGlobal(self.search_button.rect().bottomLeft()))
        self.validate()

    def choose(self, device):
        self.host_edit.setText(device["ip"])
        self.show_result("✓ " + device_text(device))

    def on_probe_failed(self):
        self.show_result("✗ No answer: check the IP, the port, and the local API in the Marstek app.", RED)
        self.validate()

    def show_result(self, text, color=None):
        self.test_label.setText(text)
        self.test_label.setStyleSheet("" if color is None else f"color: {color.name()}")
        self.adjustSize()

    def target(self):
        return self.host_edit.text().strip(), self.port_spin.value()

    def values(self):
        return *self.target(), self.cutoff_spin.value()


def level_color(soc):
    if soc < 30:
        return RED
    if soc < 70:
        return ORANGE
    return GREEN


def gauge_fraction(soc, cutoff=CUTOFF_SOC):
    return max(0.0, min(1.0, (soc - cutoff) / (100 - cutoff)))


def draw_sun(painter, center, radius):
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(SUN)
    painter.drawEllipse(center, radius, radius)
    pen = QPen(SUN, radius / 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    for i in range(8):
        angle = i * math.pi / 4
        direction = QPointF(math.cos(angle), math.sin(angle))
        painter.drawLine(center + direction * 1.45 * radius, center + direction * 2 * radius)


def draw_full_badge(painter):
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(GREEN)
    painter.drawEllipse(QPointF(32, 50), 12, 12)
    pen = QPen(WHITE, 4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    check = QPainterPath(QPointF(26, 50))
    check.lineTo(QPointF(30.5, 54.5))
    check.lineTo(QPointF(38, 45))
    painter.drawPath(check)


def draw_icon(soc, power, cutoff=CUTOFF_SOC):
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    state = state_from_power(power)
    full = soc is not None and soc >= 100 and state not in ("charging", "discharging")
    if soc is None:
        outline = GREY
    elif full:
        outline = GREEN
    else:
        outline = QApplication.palette().color(QPalette.ColorRole.WindowText)
    painter.setPen(QPen(outline, 5 if full else 4))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    body = QRectF(4, 6, 50, 28)
    painter.drawRoundedRect(body, 4, 4)
    painter.fillRect(QRectF(56, 14, 5, 12), outline)

    if soc is None:
        painter.setFont(QFont("sans-serif", 18, QFont.Weight.Bold))
        painter.drawText(body, Qt.AlignmentFlag.AlignCenter, "?")
    else:
        inner = body.adjusted(5, 5, -5, -5)
        inner.setWidth(inner.width() * gauge_fraction(soc, cutoff))
        painter.fillRect(inner, level_color(soc))

        if state in ("charging", "discharging"):
            points = CHARGE_ARROW if state == "charging" else [(64 - x, y) for x, y in RIGHT_ARROW]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(GREEN if state == "charging" else RED)
            painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
            if state == "charging":
                draw_sun(painter, QPointF(12, 50), 5)
        elif full:
            draw_full_badge(painter)

    painter.end()
    return QIcon(pixmap)


def power_text(power):
    state = state_from_power(power)
    if state == "charging":
        return f"Charging: {power:.0f} W"
    if state == "discharging":
        return f"Discharging: {-power:.0f} W"
    if state == "idle":
        return "Idle"
    return "Power unavailable"


def remaining_text(soc, power, capacity, now, cutoff=CUTOFF_SOC):
    hours = hours_remaining(soc, power, capacity, cutoff)
    if hours is None:
        return None
    target = "Full" if state_from_power(power) == "charging" else "Empty"
    minutes = round(hours * 60)
    if minutes >= 24 * 60:
        return f"{target} in more than 24 h"
    duration = f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d}"
    return f"{target} at {now + timedelta(minutes=minutes):%H:%M} (in {duration})"


def energy_text(energy):
    if energy.counters is None:
        return None
    period = "Today" if energy.since is None else f"Since {energy.since:%H:%M}"
    return f"{period}: {energy.charged / 1000:.1f} kWh charged, {energy.discharged / 1000:.1f} kWh discharged"


# Measured with a full battery in full sun: the meter reports a negative power while exporting.
def grid_text(power, at):
    if power < -GRID_IDLE_W:
        return f"Grid: {-power:.0f} W exported ({at:%H:%M})"
    if power > GRID_IDLE_W:
        return f"Grid: {power:.0f} W imported ({at:%H:%M})"
    return f"Grid: balanced ({at:%H:%M})"


def level_text(soc, capacity):
    if capacity is None:
        return f"Level: {soc:.0f} %"
    return f"Level: {soc:.0f} % ({capacity * soc / 100 / 1000:.1f} kWh)"


class BatteryTray:
    def __init__(self, host, port, cutoff):
        self.cutoff = cutoff
        self.client = MarstekClient(host, port)
        self.client.updated.connect(self.on_updated)
        self.client.failed.connect(self.on_failed)
        self.client.meter.connect(self.on_meter)
        self.soc = None
        self.power = None
        self.capacity = None
        self.measured_at = None
        self.samples = []
        self.failures = 0
        self.last_refresh = 0.0
        self.alerts = BatteryAlerts()
        self.reachability = ReachabilityAlerts()
        self.meter_alerts = MeterAlerts()
        self.grid_power = None
        self.grid_at = None
        self.polls = 0
        self.energy = DailyEnergy(ENERGY_PATH)
        self.history = History(HISTORY_PATH, datetime.now())
        self.chart = None

        self.menu = QMenu()
        self.level_action = self.menu.addAction("")
        self.power_action = self.menu.addAction("")
        self.grid_action = self.menu.addAction("")
        self.remaining_action = self.menu.addAction("")
        self.energy_action = self.menu.addAction("")
        self.time_action = self.menu.addAction("")
        self.meter_action = self.menu.addAction("")
        for action in (self.level_action, self.power_action, self.grid_action, self.remaining_action, self.energy_action,
                       self.time_action, self.meter_action):
            action.setEnabled(False)
        self.menu.addSeparator()
        self.refresh_action = self.menu.addAction("Refresh now", self.manual_refresh)
        self.menu.addAction("History…", self.show_history)
        self.menu.addAction("Settings…", self.configure)
        self.menu.addAction("Quit", QApplication.quit)
        self.menu.aboutToShow.connect(self.update_refresh_action)

        self.tray = QSystemTrayIcon()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.on_activated)

        self.timer = QTimer(interval=POLL_INTERVAL_MS)
        self.timer.timeout.connect(self.poll)

        self.render()
        self.tray.show()
        self.timer.start()
        self.refresh()

    def refresh(self):
        self.last_refresh = time.monotonic()
        self.client.refresh()

    def poll(self):
        self.polls += 1
        full = self.soc is not None and self.soc >= 100
        if self.polls % (METER_EVERY_WHEN_FULL if full else METER_EVERY) == 0:
            self.client.refresh_meter()
        else:
            self.refresh()

    def can_refresh(self):
        return not self.client.pending and time.monotonic() - self.last_refresh >= MIN_MANUAL_REFRESH_S

    def update_refresh_action(self):
        self.refresh_action.setEnabled(self.can_refresh())

    def manual_refresh(self):
        if self.can_refresh():
            self.refresh()

    def configure(self):
        dialog = ConfigDialog(self.client.host, self.client.port, self.cutoff)
        if not dialog.exec():
            return
        host, port, self.cutoff = dialog.values()
        save_config(host, port, self.cutoff)
        if self.chart is not None:
            self.chart.cutoff = self.cutoff
            self.chart.update()
        self.client.set_target(host, port)
        self.soc = None
        self.power = None
        self.samples = []
        self.failures = 0
        self.alerts = BatteryAlerts()
        self.reachability = ReachabilityAlerts()
        self.meter_alerts = MeterAlerts()
        self.grid_power = None
        self.render()
        self.refresh()

    def show_history(self):
        if self.chart is None:
            self.chart = HistoryChart(self.history, datetime.now, self.cutoff)
        self.chart.show()
        self.chart.raise_()
        self.chart.activateWindow()

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.menu.popup(QCursor.pos())

    def on_updated(self, soc, power, capacity, counters):
        self.soc = soc
        self.power = power
        self.capacity = capacity
        self.measured_at = datetime.now()
        self.samples = [(moment, p) for moment, p in self.samples
                        if (self.measured_at - moment).total_seconds() < SMOOTHING_S] + [(self.measured_at, power)]
        self.energy.update(self.measured_at, counters)
        self.history.add(self.measured_at, soc, power)
        if self.chart is not None:
            self.chart.update()
        self.failures = 0
        self.notify()
        self.render()

    def notify(self):
        alerts = self.reachability.succeeded() + self.alerts.update(self.soc, self.power)
        for alert in alerts:
            lines = [level_text(self.soc, self.capacity), power_text(self.power), self.remaining()]
            message = "\n".join(line for line in lines if line)
            self.tray.showMessage(f"Marstek — {alert.title}", message, MESSAGE_ICONS[alert.level], NOTIFICATION_MS)

    def on_meter(self, connected, grid_power):
        if connected is None:
            return
        self.grid_power = grid_power if connected else None
        self.grid_at = datetime.now()
        alerts = self.meter_alerts.succeeded() if connected else self.meter_alerts.failed(datetime.now())
        self.render()
        for alert in alerts:
            if alert.level == "warning":
                message = "In Auto mode, the battery no longer follows household consumption. Check that the meter is powered and on Wi-Fi."
            else:
                message = "The battery follows household consumption again."
            self.tray.showMessage(f"Marstek — {alert.title}", message, MESSAGE_ICONS[alert.level], NOTIFICATION_MS)

    def remaining(self):
        return remaining_text(self.soc, smoothed_power(self.samples), self.capacity, self.measured_at, self.cutoff)

    def on_failed(self):
        self.failures += 1
        alerts = self.reachability.failed(datetime.now())
        if self.failures >= OFFLINE_AFTER_FAILURES:
            self.soc = None
            self.power = None
        self.render()
        for alert in alerts:
            message = ("Check that the local API is enabled in the Marstek app and that the battery is on Wi-Fi, "
                       f"or wait for it to restart.\n{self.client.host}:{self.client.port}")
            self.tray.showMessage(f"Marstek — {alert.title}", message, MESSAGE_ICONS[alert.level], NOTIFICATION_MS)

    def render(self):
        self.tray.setIcon(draw_icon(self.soc, self.power, self.cutoff))
        if self.soc is None:
            waiting = self.failures < OFFLINE_AFTER_FAILURES
            status = "Waiting for a reading…" if waiting else f"Battery unreachable since {self.reachability.down_since:%H:%M} ({self.client.host}:{self.client.port})"
            self.tray.setToolTip(f"Marstek\n{status}")
            self.level_action.setText(status)
            self.power_action.setVisible(False)
            self.grid_action.setVisible(False)
            self.remaining_action.setVisible(False)
            self.energy_action.setVisible(False)
            self.time_action.setVisible(False)
            self.meter_action.setVisible(False)
            return
        remaining = self.remaining()
        grid = None if self.grid_power is None else grid_text(self.grid_power, self.grid_at)
        tooltip = [f"Marstek — {self.soc:.0f} %", power_text(self.power), grid, remaining]
        self.tray.setToolTip("\n".join(line for line in tooltip if line))
        self.level_action.setText(level_text(self.soc, self.capacity))
        self.power_action.setText(power_text(self.power))
        self.remaining_action.setText(remaining or "")
        self.time_action.setText(f"Measured at {self.measured_at:%H:%M}")
        self.power_action.setVisible(True)
        self.grid_action.setText(grid or "")
        self.grid_action.setVisible(grid is not None)
        self.remaining_action.setVisible(remaining is not None)
        energy = energy_text(self.energy)
        self.energy_action.setText(energy or "")
        self.energy_action.setVisible(energy is not None)
        self.time_action.setVisible(True)
        meter_down = self.meter_alerts.down_since
        if meter_down is not None:
            self.meter_action.setText(f"CT meter disconnected since {meter_down:%H:%M}")
        self.meter_action.setVisible(meter_down is not None)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Marstek Tray")
    app.setDesktopFileName("marstek-tray")
    app.setQuitOnLastWindowClosed(False)

    # A second instance would double the requests, which the battery handles badly. The wait gives an
    # instance that was just stopped (restart by install.sh) time to release the lock.
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(LOCK_PATH))
    if not lock.tryLock(LOCK_WAIT_MS):
        return 0

    try:
        host, port, cutoff = load_config()
    except (OSError, KeyError, ValueError):
        dialog = ConfigDialog("", DEFAULT_PORT, CUTOFF_SOC)
        if not dialog.exec():
            return 0
        host, port, cutoff = dialog.values()
        save_config(host, port, cutoff)

    tray = BatteryTray(host, port, cutoff)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
