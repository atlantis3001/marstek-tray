import json
from datetime import datetime

# Below this, the day counts as complete: the app was running at midnight.
FULL_DAY_START_MIN = 5


class DailyEnergy:
    """Energy charged and discharged since the start of the day, as the difference with the battery's
    cumulative counters read at the first reading of the day. That reading is kept in a file so that
    restarting the app during the day does not reset the totals."""

    def __init__(self, path):
        self.path = path
        self.start = None
        self.counters = None
        try:
            start = json.loads(path.read_text())
            self.start = (datetime.fromisoformat(start["since"]), float(start["charged"]), float(start["discharged"]))
        except (OSError, KeyError, TypeError, ValueError):
            pass

    def update(self, now, counters):
        self.counters = counters
        if counters is None:
            return
        if self.start is None or self.start[0].date() != now.date() or counters[0] < self.start[1] or counters[1] < self.start[2]:
            self.start = (now, *counters)
            # Without this file the totals merely restart from zero at the next launch: no reason to stop the app.
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"since": now.isoformat(timespec="seconds"),
                                                 "charged": counters[0], "discharged": counters[1]}))
            except OSError:
                pass

    @property
    def since(self):
        """Start of the totals, or None when they cover the whole day."""
        since = self.start[0]
        return None if since.hour == 0 and since.minute < FULL_DAY_START_MIN else since

    @property
    def charged(self):
        return self.counters[0] - self.start[1]

    @property
    def discharged(self):
        return self.counters[1] - self.start[2]
