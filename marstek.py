import json
import time

try:
    from PyQt6.QtCore import QObject, QTimer, pyqtSignal
    from PyQt6.QtNetwork import QAbstractSocket, QHostAddress, QNetworkInterface, QUdpSocket
except ImportError:  # Debian 11 only packages PyQt5
    from PyQt5.QtCore import QObject, QTimer, pyqtSignal
    from PyQt5.QtNetwork import QAbstractSocket, QHostAddress, QNetworkInterface, QUdpSocket

DEFAULT_PORT = 30000
TIMEOUT_MS = 10_000
DISCOVERY_MS = 3_000
IDLE_THRESHOLD_W = 10
# By default the battery cuts off at 12 %: the default DOD is 88 % (Marstek API doc, § 3.8.1).
# Configurable for those who changed the DOD in the Marstek app.
CUTOFF_SOC = 12
# The remaining time follows the average of the last minutes: household consumption varies too much
# from one reading to the next (washing machine, oven) for an estimate based on the instant power alone.
SMOOTHING_S = 10 * 60
# The battery leaves a request unanswered when it follows the previous one too closely.
MIN_GAP_S = 10


def state_from_power(power):
    if power is None:
        return None
    if power > IDLE_THRESHOLD_W:
        return "charging"
    if power < -IDLE_THRESHOLD_W:
        return "discharging"
    return "idle"


def hours_remaining(soc, power, capacity, cutoff=CUTOFF_SOC):
    """Hours until the battery is full (charging) or cut off (discharging), at constant power."""
    state = state_from_power(power)
    if capacity is None or state not in ("charging", "discharging"):
        return None
    if state == "charging":
        return capacity * (100 - soc) / 100 / power
    return capacity * max(0, soc - cutoff) / 100 / -power


def smoothed_power(samples):
    """Average of the (moment, power) readings of the last SMOOTHING_S seconds, stopping at the first
    one that does not go in the same direction as the latest."""
    last_moment, last_power = samples[-1]
    state = state_from_power(last_power)
    if state is None:
        return None
    powers = []
    for moment, power in reversed(samples):
        if (last_moment - moment).total_seconds() >= SMOOTHING_S or state_from_power(power) != state:
            break
        powers.append(power)
    return sum(powers) / len(powers)


def battery_power(result):
    if result.get("bat_power") is not None:
        return float(result["bat_power"])
    # Venus E 3.0 firmware 150 does not report bat_power: ongrid_power is negative while the battery
    # charges from the grid, positive while it powers the house.
    if result.get("ongrid_power") is not None:
        return -float(result["ongrid_power"])
    return None


def energy_counters(result):
    """Energy (Wh) that went into and out of the battery since it was commissioned."""
    charged = result.get("total_grid_input_energy")
    discharged = result.get("total_grid_output_energy")
    if charged is None or discharged is None:
        return None
    return float(charged), float(discharged)


def broadcast_addresses():
    flags = QNetworkInterface.InterfaceFlag
    addresses = []
    for interface in QNetworkInterface.allInterfaces():
        if interface.flags() & flags.IsUp and interface.flags() & flags.CanBroadcast and not interface.flags() & flags.IsLoopBack:
            addresses += [entry.broadcast() for entry in interface.addressEntries()
                          if entry.ip().protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol and not entry.broadcast().isNull()]
    return addresses or [QHostAddress(QHostAddress.SpecialAddress.Broadcast)]


class MarstekClient(QObject):
    updated = pyqtSignal(float, object, object, object)
    device = pyqtSignal(object)
    discovered = pyqtSignal(object)
    meter = pyqtSignal(object, object)
    failed = pyqtSignal()

    # Shared by all clients: the settings window creates a second one for its tests.
    _next_slot = 0.0

    def __init__(self, host, port=DEFAULT_PORT, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self._socket = QUdpSocket(self)
        self._socket.bind(QHostAddress(QHostAddress.SpecialAddress.AnyIPv4), 0)
        self._socket.readyRead.connect(self._read)
        self._delay = QTimer(self, singleShot=True)
        self._delay.timeout.connect(self._write)
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self._on_timeout)
        self._request_id = 0
        self._pending = None
        self._params = None
        self._broadcast = False
        self._found = {}

    def set_target(self, host, port):
        self.host = host
        self.port = port
        self._delay.stop()
        self._timer.stop()
        self._pending = None

    @property
    def pending(self):
        return self._pending is not None

    # A single request per reading: the battery stops answering when several follow each other.
    def refresh(self):
        self._send("ES.GetStatus", {"id": 0})

    def refresh_meter(self):
        self._send("EM.GetStatus", {"id": 0})

    def get_device(self):
        self._send("Marstek.GetDevice", {"ble_mac": "0"})

    # Broadcast on the local network (Marstek API doc, § 2.2.2): every Marstek device listening on
    # this port answers, so we wait DISCOVERY_MS instead of stopping at the first reply.
    def discover(self):
        self._send("Marstek.GetDevice", {"ble_mac": "0"}, broadcast=True)

    def _send(self, method, params, broadcast=False):
        if self._pending:
            return
        self._pending = method
        self._params = params
        self._broadcast = broadcast
        self._found = {}
        self._request_id += 1
        now = time.monotonic()
        send_at = max(now, MarstekClient._next_slot)
        MarstekClient._next_slot = send_at + MIN_GAP_S
        self._delay.start(round((send_at - now) * 1000))

    def _write(self):
        request = json.dumps({"id": self._request_id, "method": self._pending, "params": self._params}).encode()
        if self._broadcast:
            for address in broadcast_addresses():
                self._socket.writeDatagram(request, address, self.port)
            self._timer.start(DISCOVERY_MS)
        else:
            self._socket.writeDatagram(request, QHostAddress(self.host), self.port)
            self._timer.start(TIMEOUT_MS)

    def _read(self):
        while self._socket.hasPendingDatagrams():
            datagram = self._socket.receiveDatagram()
            try:
                message = json.loads(bytes(datagram.data()))
            except ValueError:
                continue
            if not (self._pending and isinstance(message, dict) and message.get("id") == self._request_id):
                continue
            if self._broadcast:
                if message.get("result"):
                    ip = message["result"].get("ip") or datagram.senderAddress().toString()
                    self._found[ip] = dict(message["result"], ip=ip)
            else:
                self._timer.stop()
                method, self._pending = self._pending, None
                result = message.get("result") or {}
                if method == "Marstek.GetDevice":
                    self._handle_device(result)
                elif method == "EM.GetStatus":
                    self._handle_meter(result)
                else:
                    self._handle(result)

    def _on_timeout(self):
        self._pending = None
        if self._broadcast:
            self.discovered.emit(list(self._found.values()))
        else:
            self.failed.emit()

    def _handle(self, result):
        soc = result.get("bat_soc")
        if soc is None:
            self.failed.emit()
        else:
            capacity = result.get("bat_cap")
            self.updated.emit(float(soc), battery_power(result), None if capacity is None else float(capacity),
                              energy_counters(result))

    # Emits (connected, power exchanged with the grid). connected is None when the battery does not report
    # the meter state: it did answer, so this is not a failure.
    def _handle_meter(self, result):
        ct_state = result.get("ct_state")
        grid_power = result.get("total_power")
        self.meter.emit(None if ct_state is None else ct_state == 1, None if grid_power is None else float(grid_power))

    def _handle_device(self, result):
        if result:
            self.device.emit(result)
        else:
            self.failed.emit()
