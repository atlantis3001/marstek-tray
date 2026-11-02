from collections import namedtuple

from marstek import state_from_power

FULL_SOC = 100
REARM_MARGIN = 5
# Low thresholds, only notified while discharging.
LOW_ALERTS = {40: "info", 30: "warning", 20: "critical"}

# level: "info", "warning" or "critical".
Alert = namedtuple("Alert", "level title")


class BatteryAlerts:
    """Decides on notifications from successive readings.

    A single notification per threshold crossing, and the level has to move REARM_MARGIN % away from
    the threshold before it can come back: a level hovering around a threshold does not repeat it.
    """

    def __init__(self):
        self.full_armed = None
        # Lowest threshold already notified, None if none.
        self.low_alerted = None

    def update(self, soc, power):
        alerts = []
        # No notification when the battery is already full when the app starts.
        if self.full_armed is None:
            self.full_armed = soc < FULL_SOC
        if soc >= FULL_SOC and self.full_armed:
            self.full_armed = False
            alerts.append(Alert("info", "Battery full"))
        elif soc <= FULL_SOC - REARM_MARGIN:
            self.full_armed = True
        alerts += self._low(soc, power)
        return alerts

    def _low(self, soc, power):
        # The level is rising: re-arm the thresholds it went past by the margin, lowest first.
        while self.low_alerted is not None and soc >= self.low_alerted + REARM_MARGIN:
            higher = [threshold for threshold in LOW_ALERTS if threshold > self.low_alerted]
            self.low_alerted = min(higher) if higher else None
        crossed = [threshold for threshold in LOW_ALERTS if soc < threshold]
        if not crossed or state_from_power(power) != "discharging":
            return []
        # Only the lowest crossed threshold is notified, for instance at startup with an already low battery.
        lowest = min(crossed)
        if self.low_alerted is not None and lowest >= self.low_alerted:
            return []
        self.low_alerted = lowest
        return [Alert(LOW_ALERTS[lowest], f"Battery below {lowest} %")]


UNREACHABLE_AFTER_S = 5 * 60
# The meter is only read every 5 min: the alert goes off at the second "disconnected" reading in a row.
METER_DOWN_AFTER_S = 4 * 60


class OutageAlerts:
    """One notification when an outage has lasted after_s, a single one per outage, then another when
    it ends. A shorter outage does not bother anyone."""

    def __init__(self, after_s, down_title, back_title):
        self.after_s = after_s
        self.down_title = down_title
        self.back_title = back_title
        # Time of the first failure of the current outage, None when all is well.
        self.down_since = None
        self.alerted = False

    def failed(self, now):
        if self.down_since is None:
            self.down_since = now
        if self.alerted or (now - self.down_since).total_seconds() < self.after_s:
            return []
        self.alerted = True
        return [Alert("warning", self.down_title)]

    def succeeded(self):
        alerted = self.alerted
        self.down_since = None
        self.alerted = False
        return [Alert("info", self.back_title)] if alerted else []


class ReachabilityAlerts(OutageAlerts):
    """Battery no longer answering. A single missed reading does not notify: the grey icon is enough."""

    def __init__(self):
        super().__init__(UNREACHABLE_AFTER_S, f"Battery unreachable for {UNREACHABLE_AFTER_S // 60} min",
                         "Battery reachable again")


class MeterAlerts(OutageAlerts):
    """CT meter disconnected: in Auto mode, the battery then no longer follows household consumption."""

    def __init__(self):
        super().__init__(METER_DOWN_AFTER_S, "CT meter disconnected", "CT meter connected again")
