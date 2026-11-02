import json
import socket
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

try:
    from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer
    from PyQt6.QtNetwork import QHostAddress
except ImportError:
    from PyQt5.QtCore import QCoreApplication, QEventLoop, QTimer
    from PyQt5.QtNetwork import QHostAddress

import marstek
from marstek import MarstekClient, hours_remaining, smoothed_power, state_from_power

app = QCoreApplication.instance() or QCoreApplication([])


class FakeBattery(threading.Thread):
    def __init__(self, replies, host="127.0.0.1", port=0):
        super().__init__(daemon=True)
        self.replies = replies
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]

    def run(self):
        try:
            while True:
                data, addr = self.sock.recvfrom(4096)
                request = json.loads(data)
                reply = self.replies.get(request["method"])
                if reply is not None:
                    reply = dict(reply, id=request["id"], src="VenusE-test")
                    self.sock.sendto(json.dumps(reply).encode(), addr)
        except (socket.timeout, OSError):
            pass


def refresh_and_wait(client):
    outcome = {}
    loop = QEventLoop()
    client.updated.connect(lambda soc, power, capacity, counters: (
        outcome.update(soc=soc, power=power, capacity=capacity, counters=counters), loop.quit()))
    client.failed.connect(lambda: (outcome.update(failed=True), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    client.refresh()
    loop.exec()
    return outcome


def get_device_and_wait(client):
    outcome = {}
    loop = QEventLoop()
    client.device.connect(lambda result: (outcome.update(device=result), loop.quit()))
    client.failed.connect(lambda: (outcome.update(failed=True), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    client.get_device()
    loop.exec()
    return outcome


def discover_and_wait(client):
    outcome = {}
    loop = QEventLoop()
    client.discovered.connect(lambda devices: (outcome.update(devices=devices), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    client.discover()
    loop.exec()
    return outcome


def refresh_meter_and_wait(client):
    outcome = {}
    loop = QEventLoop()
    client.meter.connect(lambda connected, grid_power: (outcome.update(connected=connected, grid_power=grid_power), loop.quit()))
    client.failed.connect(lambda: (outcome.update(failed=True), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    client.refresh_meter()
    loop.exec()
    return outcome


class StateFromPowerTest(unittest.TestCase):
    def test_states(self):
        self.assertEqual(state_from_power(450), "charging")
        self.assertEqual(state_from_power(-320), "discharging")
        self.assertEqual(state_from_power(5), "idle")
        self.assertEqual(state_from_power(-10), "idle")
        self.assertIsNone(state_from_power(None))


class HoursRemainingTest(unittest.TestCase):
    def test_until_full_when_charging(self):
        self.assertAlmostEqual(hours_remaining(80, 1024, 5120), 1.0)

    def test_until_cutoff_when_discharging(self):
        self.assertAlmostEqual(hours_remaining(62, -640, 5120), 4.0)

    def test_until_configured_cutoff(self):
        self.assertAlmostEqual(hours_remaining(62, -640, 5120, cutoff=22), 3.2)

    def test_zero_below_cutoff(self):
        self.assertEqual(hours_remaining(10, -640, 5120), 0)

    def test_unknown_at_rest_or_without_capacity(self):
        self.assertIsNone(hours_remaining(50, 5, 5120))
        self.assertIsNone(hours_remaining(50, None, 5120))
        self.assertIsNone(hours_remaining(50, -640, None))


def samples(*powers):
    start = datetime(2026, 10, 30, 14, 0)
    return [(start + timedelta(minutes=minute), power) for minute, power in enumerate(powers)]


class SmoothedPowerTest(unittest.TestCase):
    def test_average_of_last_measures(self):
        self.assertEqual(smoothed_power(samples(-400, -1600, -400)), -800)

    def test_only_measures_in_same_direction(self):
        self.assertEqual(smoothed_power(samples(-900, 600, 800)), 700)
        self.assertEqual(smoothed_power(samples(-900, 3, -300)), -300)

    def test_only_last_ten_minutes(self):
        self.assertEqual(smoothed_power(samples(-5000, *[-500] * 10)), -500)

    def test_unknown_when_last_power_unknown(self):
        self.assertIsNone(smoothed_power(samples(-500, None)))


class MarstekClientTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("MIN_GAP_S", 0), ("TIMEOUT_MS", 300), ("DISCOVERY_MS", 300)):
            patcher = mock.patch.object(marstek, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client_for(self, replies):
        battery = FakeBattery(replies)
        battery.start()
        self.addCleanup(battery.sock.close)
        return MarstekClient("127.0.0.1", battery.port)

    def test_power_from_bat_power(self):
        client = self.client_for({
            "ES.GetStatus": {"result": {"id": 0, "bat_soc": 78, "bat_power": -320, "ongrid_power": 999}},
        })
        self.assertEqual(refresh_and_wait(client), {"soc": 78.0, "power": -320.0, "capacity": None, "counters": None})

    def test_power_from_ongrid_power_on_firmware_150(self):
        client = self.client_for({
            "ES.GetStatus": {"result": {"id": 0, "bat_soc": 80, "bat_cap": 5120, "pv_power": 0,
                                        "ongrid_power": -1288, "offgrid_power": 0, "total_pv_energy": 0,
                                        "total_grid_output_energy": 405790, "total_grid_input_energy": 543160,
                                        "total_load_energy": 0}},
        })
        self.assertEqual(refresh_and_wait(client),
                         {"soc": 80.0, "power": 1288.0, "capacity": 5120.0, "counters": (543160.0, 405790.0)})

    def test_power_unknown_without_power_fields(self):
        client = self.client_for({"ES.GetStatus": {"result": {"id": 0, "bat_soc": 55}}})
        self.assertEqual(refresh_and_wait(client), {"soc": 55.0, "power": None, "capacity": None, "counters": None})

    def test_failed_on_error_reply(self):
        client = self.client_for({
            "ES.GetStatus": {"error": {"code": -32601, "message": "Method not found"}},
        })
        self.assertEqual(refresh_and_wait(client), {"failed": True})

    def test_set_target_restarts_on_new_battery(self):
        client = self.client_for({})
        client.refresh()
        battery = FakeBattery({"ES.GetStatus": {"result": {"id": 0, "bat_soc": 42}}})
        battery.start()
        self.addCleanup(battery.sock.close)
        client.set_target("127.0.0.1", battery.port)
        self.assertEqual(refresh_and_wait(client), {"soc": 42.0, "power": None, "capacity": None, "counters": None})

    def test_failed_when_battery_silent(self):
        client = self.client_for({})
        self.assertEqual(refresh_and_wait(client), {"failed": True})

    def test_pending_until_reply(self):
        client = self.client_for({"ES.GetStatus": {"result": {"id": 0, "bat_soc": 42}}})
        self.assertFalse(client.pending)
        client.refresh()
        self.assertTrue(client.pending)
        refresh_and_wait(client)
        self.assertFalse(client.pending)

    def test_meter_connected(self):
        client = self.client_for({"EM.GetStatus": {"result": {"id": 0, "ct_state": 1, "a_power": -3, "total_power": -3}}})
        self.assertEqual(refresh_meter_and_wait(client), {"connected": True, "grid_power": -3.0})

    def test_meter_disconnected(self):
        client = self.client_for({"EM.GetStatus": {"result": {"id": 0, "ct_state": 0, "a_power": 0, "total_power": 0}}})
        self.assertEqual(refresh_meter_and_wait(client), {"connected": False, "grid_power": 0.0})

    def test_meter_unknown_on_error_reply(self):
        client = self.client_for({"EM.GetStatus": {"error": {"code": -32601, "message": "Method not found"}}})
        self.assertEqual(refresh_meter_and_wait(client), {"connected": None, "grid_power": None})

    def test_meter_failed_when_battery_silent(self):
        client = self.client_for({})
        self.assertEqual(refresh_meter_and_wait(client), {"failed": True})

    def test_get_device(self):
        device = {"device": "VenusE 3.0", "ver": 150, "wifi_name": "MyWiFi", "ip": "192.168.1.100"}
        client = self.client_for({"Marstek.GetDevice": {"result": device}})
        self.assertEqual(get_device_and_wait(client), {"device": device})

    def test_get_device_failed_on_error_reply(self):
        client = self.client_for({"Marstek.GetDevice": {"error": {"code": -32602, "message": "Invalid params"}}})
        self.assertEqual(get_device_and_wait(client), {"failed": True})

    def test_discover_collects_every_answer(self):
        battery = FakeBattery({"Marstek.GetDevice": {"result": {"device": "VenusE 3.0", "ip": "192.168.1.100"}}})
        battery.start()
        self.addCleanup(battery.sock.close)
        meter = FakeBattery({"Marstek.GetDevice": {"result": {"device": "CT002"}}}, host="127.0.0.2", port=battery.port)
        meter.start()
        self.addCleanup(meter.sock.close)
        client = MarstekClient("", battery.port)
        addresses = [QHostAddress("127.0.0.1"), QHostAddress("127.0.0.2")]
        with mock.patch.object(marstek, "broadcast_addresses", return_value=addresses):
            devices = discover_and_wait(client)["devices"]
        self.assertCountEqual(devices, [{"device": "VenusE 3.0", "ip": "192.168.1.100"}, {"device": "CT002", "ip": "127.0.0.2"}])

    def test_discover_without_answer(self):
        client = self.client_for({})
        with mock.patch.object(marstek, "broadcast_addresses", return_value=[QHostAddress("127.0.0.1")]):
            self.assertEqual(discover_and_wait(client), {"devices": []})


class MinGapTest(unittest.TestCase):
    def test_requests_from_all_clients_spaced(self):
        battery = FakeBattery({
            "ES.GetStatus": {"result": {"id": 0, "bat_soc": 42}},
            "Marstek.GetDevice": {"result": {"device": "VenusE 3.0"}},
        })
        battery.start()
        self.addCleanup(battery.sock.close)
        with mock.patch.object(marstek, "MIN_GAP_S", 0.5):
            first = MarstekClient("127.0.0.1", battery.port)
            second = MarstekClient("127.0.0.1", battery.port)
            start = time.monotonic()
            first.refresh()
            self.assertEqual(get_device_and_wait(second), {"device": {"device": "VenusE 3.0"}})
            # Margin: a regular QTimer may fire up to 5 % early.
            self.assertGreater(time.monotonic() - start, 0.4)


if __name__ == "__main__":
    unittest.main()
