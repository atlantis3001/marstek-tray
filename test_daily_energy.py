import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from daily_energy import DailyEnergy


def at(hour, minute, day=30):
    return datetime(2026, 10, day, hour, minute)


class DailyEnergyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state" / "marstek-tray.json"
        self.energy = DailyEnergy(self.path)

    def test_since_first_measure_of_the_day(self):
        self.energy.update(at(8, 12), (543160, 405790))
        self.energy.update(at(14, 0), (549360, 408890))
        self.assertEqual(self.energy.since, at(8, 12))
        self.assertEqual((self.energy.charged, self.energy.discharged), (6200, 3100))

    def test_full_day_when_running_at_midnight(self):
        self.energy.update(at(23, 59, day=29), (540000, 405000))
        self.energy.update(at(0, 0), (540010, 405100))
        self.energy.update(at(9, 0), (541010, 406100))
        self.assertIsNone(self.energy.since)
        self.assertEqual((self.energy.charged, self.energy.discharged), (1000, 1000))

    def test_kept_across_restarts(self):
        self.energy.update(at(8, 12), (543160, 405790))
        restarted = DailyEnergy(self.path)
        restarted.update(at(14, 0), (549360, 408890))
        self.assertEqual(restarted.since, at(8, 12))
        self.assertEqual(restarted.charged, 6200)

    def test_restarts_when_counters_go_back(self):
        self.energy.update(at(8, 12), (543160, 405790))
        self.energy.update(at(10, 0), (100, 50))
        self.assertEqual(self.energy.since, at(10, 0))
        self.assertEqual((self.energy.charged, self.energy.discharged), (0, 0))

    def test_unreadable_file_ignored(self):
        self.path.parent.mkdir()
        self.path.write_text("{not json")
        energy = DailyEnergy(self.path)
        energy.update(at(8, 12), (543160, 405790))
        self.assertEqual(energy.since, at(8, 12))

    def test_nothing_without_counters(self):
        self.energy.update(at(8, 12), None)
        self.assertIsNone(self.energy.counters)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
