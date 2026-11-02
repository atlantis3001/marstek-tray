import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import marstek_tray


class GaugeFractionTest(unittest.TestCase):
    def test_empty_at_cutoff_and_full_at_100(self):
        self.assertEqual(marstek_tray.gauge_fraction(12), 0)
        self.assertEqual(marstek_tray.gauge_fraction(100), 1)
        self.assertAlmostEqual(marstek_tray.gauge_fraction(56), 0.5)

    def test_other_cutoff(self):
        self.assertEqual(marstek_tray.gauge_fraction(20, cutoff=20), 0)
        self.assertAlmostEqual(marstek_tray.gauge_fraction(60, cutoff=20), 0.5)

    def test_clamped_outside_range(self):
        self.assertEqual(marstek_tray.gauge_fraction(5), 0)
        self.assertEqual(marstek_tray.gauge_fraction(101), 1)


class RemainingTextTest(unittest.TestCase):
    now = datetime(2026, 10, 30, 23, 20)

    def test_discharging_until_cutoff(self):
        self.assertEqual(marstek_tray.remaining_text(62, -640, 5120, self.now), "Empty at 03:20 (in 4 h 00)")

    def test_charging_less_than_an_hour(self):
        self.assertEqual(marstek_tray.remaining_text(95, 512, 5120, self.now), "Full at 23:50 (in 30 min)")

    def test_beyond_a_day(self):
        self.assertEqual(marstek_tray.remaining_text(90, -20, 5120, self.now), "Empty in more than 24 h")

    def test_unknown_at_rest(self):
        self.assertIsNone(marstek_tray.remaining_text(50, 5, 5120, self.now))


class EnergyTextTest(unittest.TestCase):
    def energy(self, since, counters=(6200, 3100)):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        energy = marstek_tray.DailyEnergy(Path(directory.name) / "energy.json")
        energy.update(since, (0, 0))
        energy.update(since, counters)
        return energy

    def test_full_day(self):
        self.assertEqual(marstek_tray.energy_text(self.energy(datetime(2026, 10, 30, 0, 1))),
                         "Today: 6.2 kWh charged, 3.1 kWh discharged")

    def test_since_start(self):
        self.assertEqual(marstek_tray.energy_text(self.energy(datetime(2026, 10, 30, 8, 12))),
                         "Since 08:12: 6.2 kWh charged, 3.1 kWh discharged")

    def test_none_without_counters(self):
        self.assertIsNone(marstek_tray.energy_text(self.energy(datetime(2026, 10, 30, 8, 12), counters=None)))


class GridTextTest(unittest.TestCase):
    at = datetime(2026, 11, 1, 14, 21)

    def test_export_is_negative(self):
        self.assertEqual(marstek_tray.grid_text(-369, self.at), "Grid: 369 W exported (14:21)")

    def test_import(self):
        self.assertEqual(marstek_tray.grid_text(1200, self.at), "Grid: 1200 W imported (14:21)")

    def test_balanced(self):
        self.assertEqual(marstek_tray.grid_text(-3, self.at), "Grid: balanced (14:21)")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "marstek-tray.toml"
        patcher = mock.patch.object(marstek_tray, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def load(self, text):
        self.path.write_text(text)
        return marstek_tray.load_config()

    def test_written_by_save_config(self):
        marstek_tray.save_config("192.168.1.100", 30001, 20)
        self.assertEqual(marstek_tray.load_config(), ("192.168.1.100", 30001, 20))

    def test_comments_and_spaces(self):
        text = '# garage battery\n\n  host=  "10.0.0.5"   # static IP\nport =30002\n'
        self.assertEqual(self.load(text), ("10.0.0.5", 30002, marstek_tray.CUTOFF_SOC))

    def test_default_port_and_cutoff(self):
        self.assertEqual(self.load('host = "10.0.0.5"\n'), ("10.0.0.5", marstek_tray.DEFAULT_PORT, marstek_tray.CUTOFF_SOC))

    def test_invalid_cutoff(self):
        with self.assertRaises(ValueError):
            self.load('host = "10.0.0.5"\ncutoff = "twelve"\n')
        with self.assertRaises(ValueError):
            self.load('host = "10.0.0.5"\ncutoff = 100\n')

    def test_missing_host(self):
        with self.assertRaises(KeyError):
            self.load("port = 30000\n")

    def test_invalid_port(self):
        with self.assertRaises(ValueError):
            self.load('host = "10.0.0.5"\nport = "thirty thousand"\n')

    def test_missing_file(self):
        with self.assertRaises(OSError):
            marstek_tray.load_config()


if __name__ == "__main__":
    unittest.main()
