import math
from datetime import timedelta

try:
    from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
    from PyQt6.QtGui import QColor, QPainter, QPalette, QPen
    from PyQt6.QtWidgets import QWidget
except ImportError:  # Debian 11 only packages PyQt5
    from PyQt5.QtCore import QLineF, QPointF, QRectF, Qt
    from PyQt5.QtGui import QColor, QPainter, QPalette, QPen
    from PyQt5.QtWidgets import QWidget

from history import HISTORY_SPAN

GREEN = QColor("#2e9e44")
RED = QColor("#d32f2f")
BLUE = QColor("#3d7fd6")
# Beyond this, two readings are not joined: the computer was asleep or the battery did not answer.
MAX_GAP = timedelta(minutes=3)
POWER_STEP_W = 500
MARGIN_LEFT = 64
MARGIN = 12
AXIS_HEIGHT = 22


class HistoryChart(QWidget):
    def __init__(self, history, now, cutoff):
        super().__init__()
        self.history = history
        self.now = now
        self.cutoff = cutoff
        self.setWindowTitle("Marstek Tray — last 24 h")
        self.resize(760, 460)
        self.setMinimumSize(420, 300)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Window))
        end = self.now()
        start = end - HISTORY_SPAN
        width = self.width() - MARGIN_LEFT - MARGIN
        height = (self.height() - 2 * MARGIN - AXIS_HEIGHT) / 2 - MARGIN
        soc_area = QRectF(MARGIN_LEFT, MARGIN, width, height)
        power_area = QRectF(MARGIN_LEFT, soc_area.bottom() + 2 * MARGIN, width, height)

        def x(moment):
            return soc_area.left() + (moment - start) / HISTORY_SPAN * width

        samples = [sample for sample in self.history.samples if sample[0] >= start]
        self.draw_time_axis(painter, start, x, soc_area, power_area)
        self.draw_soc(painter, samples, x, soc_area)
        self.draw_power(painter, samples, x, power_area)
        painter.end()

    def text_color(self, alpha=255):
        color = QColor(self.palette().color(QPalette.ColorRole.WindowText))
        color.setAlpha(alpha)
        return color

    def draw_label(self, painter, y, text):
        painter.setPen(self.text_color())
        painter.drawText(QRectF(0, y - 10, MARGIN_LEFT - 8, 20),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)

    def draw_title(self, painter, area, text):
        painter.setPen(self.text_color(160))
        painter.drawText(area.adjusted(6, 2, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, text)

    def draw_time_axis(self, painter, start, x, soc_area, power_area):
        hour = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        while hour <= start + HISTORY_SPAN:
            if hour.hour % 3 == 0:
                painter.setPen(QPen(self.text_color(40), 1))
                for area in (soc_area, power_area):
                    painter.drawLine(QLineF(x(hour), area.top(), x(hour), area.bottom()))
                painter.setPen(self.text_color())
                painter.drawText(QRectF(x(hour) - 30, power_area.bottom() + 4, 60, AXIS_HEIGHT - 4),
                                 Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, f"{hour:%H}:00")
            hour += timedelta(hours=1)

    def draw_soc(self, painter, samples, x, area):
        def y(soc):
            return area.bottom() - soc / 100 * area.height()

        for soc in (0, 50, 100):
            painter.setPen(QPen(self.text_color(40), 1))
            painter.drawLine(QLineF(area.left(), y(soc), area.right(), y(soc)))
            self.draw_label(painter, y(soc), f"{soc} %")
        painter.setPen(QPen(RED, 1, Qt.PenStyle.DashLine))
        painter.drawLine(QLineF(area.left(), y(self.cutoff), area.right(), y(self.cutoff)))

        self.draw_title(painter, area, f"Level (cutoff at {self.cutoff} %)")
        painter.setPen(QPen(BLUE, 2))
        previous = None
        for moment, soc, _ in samples:
            point = QPointF(x(moment), y(soc))
            if previous is not None and moment - previous[0] <= MAX_GAP:
                painter.drawLine(QLineF(previous[1], point))
            previous = (moment, point)

    def draw_power(self, painter, samples, x, area):
        powers = [power for _, _, power in samples if power is not None]
        scale = max(POWER_STEP_W, math.ceil(max(map(abs, powers), default=0) / POWER_STEP_W) * POWER_STEP_W)

        def y(power):
            return area.center().y() - power / scale * area.height() / 2

        for power in (scale, 0, -scale):
            painter.setPen(QPen(self.text_color(120 if power == 0 else 40), 1))
            painter.drawLine(QLineF(area.left(), y(power), area.right(), y(power)))
            self.draw_label(painter, y(power), f"{power:+.0f} W" if power else "0 W")

        previous = None
        for moment, _, power in samples:
            # Each bar covers the interval since the previous reading, so that each CT meter reading,
            # which does not give the battery power, does not leave an empty stripe.
            since = previous if previous is not None and moment - previous <= MAX_GAP else moment - timedelta(minutes=1)
            previous = moment
            if power is not None:
                left = x(since)
                painter.fillRect(QRectF(left, min(y(0), y(power)), max(1.0, x(moment) - left), abs(y(power) - y(0))),
                                 GREEN if power > 0 else RED)
        self.draw_title(painter, area, "Power: charging in green, discharging in red")
