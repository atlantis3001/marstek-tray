import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import history
from history import History

NOW = datetime(2026, 10, 31, 12, 0)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state" / "history.csv"

    def test_kept_across_restarts(self):
        History(self.path, NOW).add(NOW, 61, -200.0)
        History(self.path, NOW).add(NOW + timedelta(minutes=1), 61, None)
        self.assertEqual(History(self.path, NOW).samples,
                         [(NOW, 61, -200.0), (NOW + timedelta(minutes=1), 61, None)])

    def test_only_last_24_hours(self):
        old = History(self.path, NOW - timedelta(hours=25))
        old.add(NOW - timedelta(hours=25), 80, 500.0)
        old.add(NOW - timedelta(hours=23), 70, -300.0)
        self.assertEqual([soc for _, soc, _ in History(self.path, NOW).samples], [70])

    def test_file_rewritten_once_enough_stale_lines(self):
        start = NOW - timedelta(hours=24)
        recorder = History(self.path, start)
        for minute in range(24 * 60 + history.REWRITE_AFTER + 1):
            recorder.add(start + timedelta(minutes=minute), 50, 0.0)
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), len(recorder.samples))

    def test_damaged_line_skipped(self):
        self.path.parent.mkdir()
        self.path.write_text(f"{NOW.isoformat()},61,-200\n2026-10-31T12:01:00,6\n{NOW.isoformat()},62,\n")
        self.assertEqual(History(self.path, NOW).samples, [(NOW, 61, -200.0), (NOW, 62, None)])


if __name__ == "__main__":
    unittest.main()
