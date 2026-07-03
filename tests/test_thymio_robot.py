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

    def play_sound(self, system=None, freq=None, duration_ms=500, track=None) -> bool:
        self.calls.append(("sound", system, freq, duration_ms, track))
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
    assert ("sound", 2, None, 500, None) in link.calls
    assert thymio.play_sound(freq=700, duration_ms=400) is True
    assert ("sound", None, 700, 400, None) in link.calls
    assert thymio.play_sound(track=3) is True
    assert ("sound", None, None, 500, 3) in link.calls


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
        self.callbacks: list = []

    def send(self, target, command, **kwargs) -> bool:
        self.sent.append((target, command, kwargs))
        return True

    def on_message(self, callback) -> None:
        self.callbacks.append(callback)

    def remove_message_callback(self, callback) -> None:
        self.callbacks = [cb for cb in self.callbacks if cb is not callback]


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


# --- wireless sensor read + impact detection (gateway/C6 path) --------------

def _sensor_frame(idx=0, acc=(0, 0, 20), mic=0, ground=(1000, 1000)):
    return {"source": "thymio", "type": "thymio_sensors",
            "idx": idx, "acc": list(acc), "mic": mic, "ground": list(ground)}


def test_gateway_link_streams_sensors():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=25)
    link.connect()
    got = []
    link.on_sensors(lambda acc, mic, ground: got.append((acc, mic, ground)))
    gw.callbacks[0](_sensor_frame(acc=(1, 2, 20), mic=18, ground=(900, 950)))
    assert got == [((1, 2, 20), 18, (900, 950))]
    assert link.last_sensors == ((1, 2, 20), 18, (900, 950))


def test_gateway_link_impact_edge_triggers_once_with_hysteresis():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=25, impact_threshold=20)
    link.connect()
    fires = []
    link.on_impact(lambda level: fires.append(level))
    feed = gw.callbacks[0]

    feed(_sensor_frame(acc=(0, 0, 20)))   # dev 0 → no impact
    feed(_sensor_frame(acc=(0, 0, 60)))   # dev 40 ≥ 20 → one impact (2× → knock)
    feed(_sensor_frame(acc=(0, 0, 58)))   # still high → no re-fire
    assert fires == [2]
    feed(_sensor_frame(acc=(0, 0, 25)))   # dev 5 < 12 (0.6×thr) → re-arm
    feed(_sensor_frame(acc=(0, 0, 45)))   # dev 25 ≥ 20 → second impact (1.25× → touch)
    assert fires == [2, 1]
    assert link.impact_count == 2


def test_gateway_link_classifies_touch_knock_slap():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=25, impact_threshold=20)
    # thresholds: touch ≥20, knock ≥40 (2×), slap ≥70 (3.5×)
    assert link.impact_level(10) == 0            # below touch
    assert link.impact_level(25) == 1            # touch
    assert link.impact_level(50) == 2            # knock
    assert link.impact_level(90) == 3            # slap


def test_gateway_link_threshold_live_tunable():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=25, impact_threshold=100)
    link.connect()
    fires = []
    link.on_impact(lambda level: fires.append(level))
    feed = gw.callbacks[0]
    feed(_sensor_frame(acc=(0, 0, 50)))   # dev 30 < 100 → nothing
    assert fires == []
    link.impact_threshold = 20            # more sensitive now
    feed(_sensor_frame(acc=(0, 0, 51)))   # dev 31 ≥ 20 → fires (touch)
    assert fires == [1]


def test_gateway_link_lifted_edge():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=25)   # DEFAULT_LIFT_THRESHOLD = 150
    link.connect()
    events = []
    link.on_lifted(lambda lifted: events.append(lifted))
    feed = gw.callbacks[0]
    feed(_sensor_frame(ground=(1000, 1000)))   # on table → nothing
    feed(_sensor_frame(ground=(20, 30)))       # both low → lifted
    feed(_sensor_frame(ground=(10, 40)))       # still lifted → no re-fire
    assert events == [True]
    assert link.is_lifted and link.lifted_count == 1
    feed(_sensor_frame(ground=(1000, 1000)))   # back on table → set down
    assert events == [True, False]
    assert not link.is_lifted


def test_gateway_link_ignores_foreign_sensor_frames():
    gw = _FakeGateway()
    link = _gateway_link(gw, index=0)
    link.connect()
    got = []
    link.on_sensors(lambda acc, mic, ground: got.append(1))
    feed = gw.callbacks[0]
    feed(_sensor_frame(idx=1))                          # another slot
    feed({"source": "AA:BB", "type": "status"})         # not the thymio route
    feed({"source": "thymio", "type": "pong"})          # not a sensor frame
    assert got == []


def test_robot_status_and_impact_via_gateway():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=25, impact_threshold=20)
    robot = ThymioRobot("t1", link=link)
    robot.connect()
    knocks = []
    robot.on_impact(lambda level: knocks.append(level))
    gw.callbacks[0](_sensor_frame(acc=(0, 0, 60), mic=30, ground=(800, 820)))
    assert knocks == [2]                       # dev 40 = 2× → knock
    sensors = robot.get_status_data()["sensors"]
    assert sensors["acc"] == [0, 0, 60]
    assert sensors["mic"] == 30
    assert sensors["ground"] == [800, 820]
    assert sensors["impact_count"] == 1
    assert sensors["last_impact_level"] == 2
    assert sensors["lifted"] is False


def test_gateway_link_plays_sd_track():
    gw = _FakeGateway()
    link = _gateway_link(gw, channel=25)
    link.connect()
    assert link.play_sound(track=4) is True
    assert ("thymio", "thymio_sound", {"idx": 0, "track": 4}) in gw.sent


def test_robot_without_link_has_empty_sensors_and_noop_impact():
    robot = ThymioRobot("t1")
    robot.connect()
    robot.on_impact(lambda: None)            # no link → silently ignored
    assert robot.get_status_data()["sensors"] == {}
    assert robot.impact_threshold == 0.0
