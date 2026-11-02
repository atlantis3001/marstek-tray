import unittest
from datetime import datetime, timedelta

from alerts import BatteryAlerts, MeterAlerts, ReachabilityAlerts


def titles(alerts, *measures):
    return [[alert.title for alert in alerts.update(soc, power)] for soc, power in measures]


class FullAlertTest(unittest.TestCase):
    def test_once_when_reaching_full(self):
        self.assertEqual(titles(BatteryAlerts(), (97, 500), (100, 20), (100, 0)), [[], ["Battery full"], []])

    def test_not_when_already_full_at_startup(self):
        self.assertEqual(titles(BatteryAlerts(), (100, 0), (100, 0)), [[], []])

    def test_not_repeated_when_oscillating(self):
        measures = [(98, 500), (100, 0), (99, -50), (100, 30)]
        self.assertEqual(titles(BatteryAlerts(), *measures), [[], ["Battery full"], [], []])

    def test_again_after_dropping_by_margin(self):
        measures = [(98, 500), (100, 0), (95, -500), (100, 500)]
        self.assertEqual(titles(BatteryAlerts(), *measures), [[], ["Battery full"], [], ["Battery full"]])


class LowAlertTest(unittest.TestCase):
    def test_below_40_when_discharging(self):
        measures = [(42, -500), (39, -500), (38, -500)]
        self.assertEqual(titles(BatteryAlerts(), *measures), [[], ["Battery below 40 %"], []])

    def test_not_when_charging_or_at_rest(self):
        self.assertEqual(titles(BatteryAlerts(), (39, 500), (38, 0)), [[], []])

    def test_when_discharge_starts_below_threshold(self):
        self.assertEqual(titles(BatteryAlerts(), (38, 500), (38, -500)), [[], ["Battery below 40 %"]])

    def test_at_startup_when_already_low(self):
        self.assertEqual(titles(BatteryAlerts(), (35, -500)), [["Battery below 40 %"]])

    def test_not_repeated_when_oscillating(self):
        measures = [(39, -500), (42, 500), (39, -500)]
        self.assertEqual(titles(BatteryAlerts(), *measures), [["Battery below 40 %"], [], []])

    def test_again_after_rising_by_margin(self):
        measures = [(39, -500), (45, 500), (39, -500)]
        self.assertEqual(titles(BatteryAlerts(), *measures), [["Battery below 40 %"], [], ["Battery below 40 %"]])


    def test_below_30_after_40(self):
        measures = [(41, -500), (39, -500), (31, -500), (29, -500), (28, -500)]
        expected = [[], ["Battery below 40 %"], [], ["Battery below 30 %"], []]
        self.assertEqual(titles(BatteryAlerts(), *measures), expected)

    def test_30_is_a_warning(self):
        self.assertEqual([alert.level for alert in BatteryAlerts().update(29, -500)], ["warning"])

    def test_only_lowest_threshold_at_startup(self):
        self.assertEqual(titles(BatteryAlerts(), (25, -500)), [["Battery below 30 %"]])

    def test_rearmed_one_by_one_when_rising(self):
        # Back up to 36 %: 30 % is re-armed, 40 % not yet (that takes 45 %).
        measures = [(29, -500), (36, 500), (39, -500), (29, -500)]
        expected = [["Battery below 30 %"], [], [], ["Battery below 30 %"]]
        self.assertEqual(titles(BatteryAlerts(), *measures), expected)


    def test_below_20_is_critical(self):
        alerts = BatteryAlerts()
        self.assertEqual(titles(alerts, (35, -500), (25, -500)), [["Battery below 40 %"], ["Battery below 30 %"]])
        self.assertEqual([tuple(alert) for alert in alerts.update(19, -500)], [("critical", "Battery below 20 %")])

    def test_only_20_at_startup_when_very_low(self):
        self.assertEqual(titles(BatteryAlerts(), (12, -500)), [["Battery below 20 %"]])



def minute(n):
    return datetime(2026, 10, 29, 19, 0) + timedelta(minutes=n)


UNREACHABLE = ["Battery unreachable for 5 min"]
BACK = ["Battery reachable again"]


class ReachabilityAlertTest(unittest.TestCase):
    def failures(self, reachability, minutes):
        return [[alert.title for alert in reachability.failed(minute(n))] for n in minutes]

    def test_after_5_minutes_once(self):
        reachability = ReachabilityAlerts()
        self.assertEqual(self.failures(reachability, range(8)), [[]] * 5 + [UNREACHABLE, [], []])
        self.assertEqual(reachability.down_since, minute(0))

    def test_back_only_after_alert(self):
        reachability = ReachabilityAlerts()
        self.failures(reachability, range(6))
        self.assertEqual([alert.title for alert in reachability.succeeded()], BACK)
        self.assertIsNone(reachability.down_since)

    def test_short_outage_is_silent(self):
        reachability = ReachabilityAlerts()
        self.assertEqual(self.failures(reachability, range(3)), [[], [], []])
        self.assertEqual(reachability.succeeded(), [])

    def test_new_outage_counts_from_scratch(self):
        reachability = ReachabilityAlerts()
        self.failures(reachability, range(3))
        reachability.succeeded()
        self.assertEqual(self.failures(reachability, range(10, 16)), [[]] * 5 + [UNREACHABLE])



class MeterAlertTest(unittest.TestCase):
    def readings(self, meter, minutes):
        return [[alert.title for alert in meter.failed(minute(n))] for n in minutes]

    def test_on_second_reading_five_minutes_apart(self):
        meter = MeterAlerts()
        # The second reading may arrive slightly before exactly 5 min.
        moments = [minute(0), minute(5) - timedelta(seconds=1), minute(10)]
        self.assertEqual([[alert.title for alert in meter.failed(moment)] for moment in moments],
                         [[], ["CT meter disconnected"], []])

    def test_single_reading_is_silent(self):
        meter = MeterAlerts()
        self.assertEqual(self.readings(meter, [0]), [[]])
        self.assertEqual(meter.succeeded(), [])

    def test_back_after_alert(self):
        meter = MeterAlerts()
        self.readings(meter, [0, 5])
        self.assertEqual([alert.title for alert in meter.succeeded()], ["CT meter connected again"])


if __name__ == "__main__":
    unittest.main()
