"""Tests for the Thymio robot module."""

from src.robots.thymio.thymio_robot import ThymioRobot
from src.robots.thymio.thymio_link import ThymioLink


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


def test_thymio_no_link_movement_is_noop():
    thymio = ThymioRobot("t1")
    thymio.connect()
    assert thymio.set_motors(100, 100) is True
    assert thymio.send_command("motors", left=1, right=2) is True


# --- the link itself, without a TDM ----------------------------------------

def test_thymio_link_construct_without_tdm():
    link = ThymioLink()          # must not connect or raise
    assert link.connected is False
    assert link.set_motors(10, 20) is True   # queued, safe before connect
    assert link.set_leds(1, 2, 3) is True
    link.close()                 # safe even if never started
