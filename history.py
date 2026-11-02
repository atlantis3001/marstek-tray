from datetime import datetime, timedelta

HISTORY_SPAN = timedelta(hours=24)
# The file is rewritten without the expired readings only once there are enough of them,
# rather than at every reading.
REWRITE_AFTER = 60


class History:
    """(moment, level, power) readings of the last 24 h, appended to a CSV file at every reading
    so they survive an app restart."""

    def __init__(self, path, now):
        self.path = path
        self.samples = []
        self.stale = 0
        try:
            lines = path.read_text().splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                moment, soc, power = line.split(",")
                self.samples.append((datetime.fromisoformat(moment), float(soc), float(power) if power else None))
            except ValueError:
                self.stale += 1
        self.prune(now)

    def add(self, moment, soc, power):
        self.samples.append((moment, soc, power))
        self.prune(moment)
        try:
            if self.stale >= REWRITE_AFTER:
                self.path.write_text("".join(self.line(*sample) for sample in self.samples))
                self.stale = 0
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as file:
                    file.write(self.line(moment, soc, power))
        except OSError:
            pass

    def prune(self, now):
        kept = [sample for sample in self.samples if now - sample[0] <= HISTORY_SPAN]
        self.stale += len(self.samples) - len(kept)
        self.samples = kept

    @staticmethod
    def line(moment, soc, power):
        return f"{moment.isoformat(timespec='seconds')},{soc:g},{'' if power is None else f'{power:g}'}\n"
