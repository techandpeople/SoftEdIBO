"""Tests for the Thymio robot module."""

from src.robots.thymio.thymio_robot import ThymioRobot
from src.robots.thymio.thymio_link import ThymioLink
from src.robots.thymio.thymio_dongle import ThymioDongle


def test_thymio_initial_state():
    thymio = ThymioRobot("thymio-1")
    assert thymio.status.value == "disconnected"
    assert thymio.robot_id == "thymio-1"


def test_thymio_connect():
    thymio = ThymioRobot("thymio-1")
    assert thymio.connect() is True
    assert thymio.status.value == "connected"


def test_thymio_disconnect():
    thymio = ThymioRobot("thymio-1")
    thymio.connect()
    thymio.disconnect()
    assert thymio.status.value == "disconnected"


# --- wheeled base via an injected link -------------------------------------

class _FakeLink:
    """Stand-in for ThymioLink: records calls, never touches a TDM."""

    def __init__(self, connect_result: bool = True):
        self.connected = False
        self.connect_result = connect_result
        self.calls: list[tuple] = []

    def connect(self, timeout: float = 5.0) -> bool:
        self.connected = self.connect_result
        return self.connect_result

    def close(self) -> None:
        self.connected = False

    def set_motors(self, left: int, right: int) -> bool:
        self.calls.append(("motors", left, right))
        return True

    def set_leds(self, r: int, g: int, b: int) -> bool:
        self.calls.append(("leds", r, g, b))
        return True

    def play_sound(self, system=None, freq=None, duration_ms=500) -> bool:
        self.calls.append(("sound", system, freq, duration_ms))
        return True


def test_thymio_movement_delegates_to_link():
    link = _FakeLink()
    thymio = ThymioRobot("t1", link=link)
    assert thymio.connect() is True
    assert link.connected

    assert thymio.set_motors(100, -100) is True
    assert ("motors", 100, -100) in link.calls

    assert thymio.send_command("motors", left=50, right=50) is True
    assert ("motors", 50, 50) in link.calls

    assert thymio.send_command("leds", r=0, g=32, b=0) is True
    assert ("leds", 0, 32, 0) in link.calls

    thymio.disconnect()
    assert not link.connected


def test_thymio_connect_fails_when_link_fails():
    thymio = ThymioRobot("t1", link=_FakeLink(connect_result=False))
    assert thymio.connect() is False
    assert thymio.status.value == "error"


def test_thymio_send_command_requires_connected():
    thymio = ThymioRobot("t1", link=_FakeLink())
    assert thymio.send_command("motors", left=10, right=10) is False


def test_thymio_sound_delegates_to_link():
    link = _FakeLink()
    thymio = ThymioRobot("t1", link=link)
    thymio.connect()
    assert thymio.play_sound(system=2) is True
    assert ("sound", 2, None, 500) in link.calls
    assert thymio.play_sound(freq=700, duration_ms=400) is True
    assert ("sound", None, 700, 400) in link.calls


def test_thymio_no_link_movement_is_noop():
    thymio = ThymioRobot("t1")
    thymio.connect()
    assert thymio.set_motors(100, 100) is True
    assert thymio.send_command("motors", left=1, right=2) is True
    assert thymio.play_sound(system=2) is True   # no link → no-op, still True


# --- the link itself, without the dongle / thymiodirect --------------------

def test_thymio_link_construct_without_dongle():
    link = ThymioLink()          # must not import thymiodirect or raise
    assert link.connected is False
    assert link.set_motors(10, 20) is False  # not connected → no-op
    assert link.set_leds(1, 2, 3) is False
    link.close()                 # safe even if never connected


def test_thymio_dongle_construct_without_hardware():
    d = ThymioDongle()           # must not import thymiodirect or raise
    assert d.connected is False
    assert d.nodes == []
    assert d.write(1, {"motor.left.target": 100}) is False  # not connected → no-op
    d.close()                    # safe even if never connected


# --- one dongle, several Thymios by node id (no hardware) ------------------

class _FakeDongle:
    """Stand-in for ThymioDongle: records writes, never touches serial/thymiodirect."""

    def __init__(self, nodes=(1, 2, 3)):
        self._nodes = list(nodes)
        self.writes: list[tuple] = []
        self.connected = False
        self.closed = 0

    def connect(self, timeout: float = 6.0) -> bool:
        self.connected = True
        return True

    @property
    def nodes(self) -> list:
        return list(self._nodes) if self.connected else []

    def write(self, node_id, variables) -> bool:
        self.writes.append((node_id, variables))
        return True

    def close(self) -> None:
        self.connected = False
        self.closed += 1


def test_link_binds_configured_node_and_delegates():
    d = _FakeDongle(nodes=(10, 20, 30))
    link = ThymioLink(dongle=d, node_id=20)
    assert link.connect() is True
    assert link.connected
    assert link.set_motors(100, -100) is True
    assert d.writes[-1] == (20, {"motor.left.target": 100, "motor.right.target": -100})
    assert link.set_leds(0, 32, 0) is True
    assert d.writes[-1] == (20, {"leds.top": [0, 32, 0]})


def test_link_auto_binds_first_node_when_unset():
    d = _FakeDongle(nodes=(7, 8))
    link = ThymioLink(dongle=d, node_id=None)
    assert link.connect() is True
    link.set_motors(1, 2)
    assert d.writes[-1][0] == 7          # first node discovered


def test_link_fails_when_configured_node_absent():
    d = _FakeDongle(nodes=(1, 2))
    link = ThymioLink(dongle=d, node_id=99)
    assert link.connect(timeout=0.2) is False
    assert link.connected is False
    assert link.set_motors(1, 1) is False   # unbound → no-op


def test_shared_dongle_two_links_isolated_and_survive_link_close():
    d = _FakeDongle(nodes=(1, 2))
    a = ThymioLink(dongle=d, node_id=1)
    b = ThymioLink(dongle=d, node_id=2)
    assert a.connect() and b.connect()
    a.set_motors(50, 50)
    b.set_leds(0, 32, 0)
    assert (1, {"motor.left.target": 50, "motor.right.target": 50}) in d.writes
    assert (2, {"leds.top": [0, 32, 0]}) in d.writes

    a.close()                            # closing one link must NOT close the shared dongle
    assert d.closed == 0
    assert d.connected is True
    assert a.connected is False          # but this link reads as disconnected
    assert a.set_motors(0, 0) is False   # and stops writing
    assert b.connected is True           # the other link is untouched
    assert b.set_motors(0, 0) is True


# --- dongle-free: drive through the gateway's C6 (802.15.4) -----------------

class _FakeGateway:
    """Stand-in for Gateway: records sent commands, no serial."""

    def __init__(self, connected: bool = True):
        self.is_connected = connected
        self.sent: list[tuple] = []

    def send(self, target, command, **kwargs) -> bool:
        self.sent.append((target, command, kwargs))
        return True


def _gateway_link(gateway, **kwargs):
    from src.robots.thymio.thymio_gateway_link import ThymioGatewayLink
    return ThymioGatewayLink(gateway=gateway, **kwargs)


def test_gateway_link_connect_starts_c6_link():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=25)
    assert link.connect() is True
    assert link.connected
    assert ("thymio", "thymio_link", {"on": True, "ch": 25}) in gw.sent
    # address-less slot 0 rides the C6 default — no thymio_set is sent
    assert not any(cmd == "thymio_set" for _, cmd, _ in gw.sent)


def test_gateway_link_drives_and_leds():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=20)
    link.connect()
    assert link.set_motors(200, -200) is True
    assert ("thymio", "thymio_drive", {"idx": 0, "left": 200, "right": -200}) in gw.sent
    assert link.set_leds(32, 0, 0) is True
    assert ("thymio", "thymio_leds", {"idx": 0, "r": 32, "g": 0, "b": 0}) in gw.sent
    assert link.stop() is True
    assert ("thymio", "thymio_drive", {"idx": 0, "left": 0, "right": 0}) in gw.sent


def test_gateway_link_play_sound():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=25)
    link.connect()
    assert link.play_sound(system=2) is True
    assert ("thymio", "thymio_sound", {"idx": 0, "sys": 2}) in gw.sent
    # 500 ms tone → Thymio duration unit is 1/60 s → 30
    assert link.play_sound(freq=700, duration_ms=500) is True
    assert ("thymio", "thymio_sound", {"idx": 0, "freq": 700, "dur": 30}) in gw.sent


def test_gateway_link_play_sound_needs_connection():
    link = _gateway_link(_FakeGateway(connected=False))
    assert link.play_sound(system=2) is False


def test_gateway_link_registers_address_for_extra_thymios():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=25, index=1, address="7b31")
    assert link.connect() is True
    assert ("thymio", "thymio_set", {"idx": 1, "addr": "7b31"}) in gw.sent
    link.set_motors(150, 150)
    assert ("thymio", "thymio_drive", {"idx": 1, "left": 150, "right": 150}) in gw.sent


def test_gateway_link_close_zeros_this_robot():
    gw = _FakeGateway()
    link = _gateway_link(gw, index=2, address="8c42")
    link.connect()
    link.close()
    # close zeros THIS robot's motors (the shared poller stays up for the others)
    assert ("thymio", "thymio_drive", {"idx": 2, "left": 0, "right": 0}) in gw.sent
    assert link.connected is False
    assert link.set_motors(1, 1) is False    # not active → no-op


def test_gateway_link_fails_when_gateway_down():
    link = _gateway_link(_FakeGateway(connected=False))
    assert link.connect() is False
    assert link.connected is False
    assert link.set_motors(1, 1) is False
    assert _gateway_link(None).connect() is False   # no gateway at all


def test_gateway_link_drives_thymio_robot():
    gw = _FakeGateway()
    thymio = ThymioRobot("t1", link=_gateway_link(gw, channel=25))
    assert thymio.connect() is True
    assert thymio.set_motors(100, -100) is True
    assert ("thymio", "thymio_drive", {"idx": 0, "left": 100, "right": -100}) in gw.sent
    thymio.disconnect()
    assert ("thymio", "thymio_drive", {"idx": 0, "left": 0, "right": 0}) in gw.sent
