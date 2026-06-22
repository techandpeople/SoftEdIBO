"""Tests for StreamRecorder — JSONL capture of gateway messages."""

import json

from src.data.stream_recorder import StreamRecorder


class _FakeGateway:
    """Minimal gateway: stores one callback, can fire messages, can remove it."""

    def __init__(self):
        self.callback = None

    def on_message(self, cb):
        self.callback = cb

    def remove_message_callback(self, cb):
        if self.callback == cb:
            self.callback = None

    def fire(self, msg):
        if self.callback is not None:
            self.callback(msg)


def _read_lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_writes_header_and_one_line_per_message(tmp_path):
    gw = _FakeGateway()
    path = tmp_path / "rec" / "S001.jsonl"
    rec = StreamRecorder(path, session_id="S001", gateway=gw)
    rec.start()

    gw.fire({"source": "AA", "type": "magnet", "act": [0]})
    gw.fire({"source": "AA", "type": "organ", "resistance_ohm": 952.4})
    rec.stop()

    lines = _read_lines(path)
    assert lines[0]["schema"] == 1
    assert lines[0]["session_id"] == "S001"
    assert "started" in lines[0]
    assert len(lines) == 3                       # header + 2 messages
    assert lines[1]["msg"]["type"] == "magnet"
    assert "t" in lines[1]
    assert lines[2]["msg"]["resistance_ohm"] == 952.4
    assert rec.message_count == 2


def test_creates_parent_directories(tmp_path):
    gw = _FakeGateway()
    path = tmp_path / "deep" / "nested" / "S002.jsonl"
    rec = StreamRecorder(path, gateway=gw)
    rec.start()
    rec.stop()
    assert path.exists()


def test_stop_unsubscribes_and_ignores_late_messages(tmp_path):
    gw = _FakeGateway()
    path = tmp_path / "S003.jsonl"
    rec = StreamRecorder(path, session_id="S003", gateway=gw)
    rec.start()
    gw.fire({"type": "magnet"})
    rec.stop()

    assert gw.callback is None                   # unsubscribed
    rec.handle_message({"type": "organ"})        # late message after stop
    assert _read_lines(path)[-1]["msg"]["type"] == "magnet"
    assert rec.message_count == 1


class _FakeMagnet:
    """Minimal simulated magnet sensor: on_magnet + fire."""

    def __init__(self):
        self._cbs = []

    def on_magnet(self, cb):
        self._cbs.append(cb)

    def fire_magnet(self, data):
        for cb in self._cbs:
            cb(data)


def test_records_simulated_magnet_without_gateway(tmp_path):
    # Simulation: no gateway, touches come from a SimulatedMagnetSensor.
    path = tmp_path / "S005.jsonl"
    rec = StreamRecorder(path, session_id="S005", gateway=None)
    rec.start()
    sensor = _FakeMagnet()
    rec.attach_magnet(sensor)

    sensor.fire_magnet({"type": "magnet", "source": "SIM", "act": [0], "mag": [1.0]})
    sensor.fire_magnet({"type": "magnet", "source": "SIM", "act": [], "mag": [0.0]})
    rec.stop()

    lines = _read_lines(path)
    assert len(lines) == 3                       # header + 2 magnet events
    assert lines[1]["msg"]["type"] == "magnet"
    assert rec.message_count == 2


def test_is_recording_flag(tmp_path):
    gw = _FakeGateway()
    rec = StreamRecorder(tmp_path / "S004.jsonl", gateway=gw)
    assert not rec.is_recording
    rec.start()
    assert rec.is_recording
    rec.stop()
    assert not rec.is_recording
