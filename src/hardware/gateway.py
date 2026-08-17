"""Serial communication with the SoftEdIBO gateway ESP32.

The gateway ESP32 is connected to the PC via USB/serial and relays
commands to/from remote ESP32 nodes using the ESP-NOW protocol.

Protocol format (JSON over serial):
  PC -> Gateway:  {"target": "AA:BB:CC:DD:EE:01", "cmd": "inflate", "chamber": 0, "value": 255}
  Gateway -> PC:  {"source": "AA:BB:CC:DD:EE:01", "type": "status", "chamber": 0, "pressure": 128}
"""

import json
import logging
import threading
import time
import weakref
from typing import Any, Callable

import serial

logger = logging.getLogger(__name__)

# Seconds to wait for the peer on the serial port to prove it is a gateway
# before giving up and reporting the connection as failed.
VERIFY_TIMEOUT_S = 2.0

# Seconds a serial write may block before giving up. Without this, a write to a
# vanished gateway (e.g. just after flashing, while the USB CDC device resets and
# re-enumerates) blocks the calling thread forever. A short cap turns that into a
# SerialTimeoutException the senders already treat as a failed write.
WRITE_TIMEOUT_S = 1.0


class Gateway:
    """Manages serial communication with the SoftEdIBO gateway."""

    def __init__(self, port: str, baud_rate: int = 115200):
        self._port = port
        self._baud_rate = baud_rate
        self._serial: serial.Serial | None = None
        self._running = False
        self._read_thread: threading.Thread | None = None
        # Serializes serial writes so frames from concurrent senders can't
        # interleave mid-line and corrupt the JSON. Senders are multi-threaded:
        # the GUI/actuation thread, the OTA worker, and (per-node, at session
        # start) the confirmed-limit push threads all call send() on this one
        # port. The read thread never takes it, so an ack can still be parsed
        # while a write is in progress.
        self._write_lock = threading.Lock()
        # WeakMethod refs so old controllers are GC'd after robot reconfiguration.
        self._callbacks: list[weakref.ref] = []
        # Strong refs to raw serial taps (e.g. the serial monitor). Held strongly
        # because the consumer deregisters explicitly when it closes.
        self._raw_callbacks: list[Callable[[str, str], None]] = []
        self._logged_disconnected = False
        self._known_macs: set[str] = set()
        # mac -> time.monotonic() of its last message, so callers can tell a
        # node that is alive NOW from one merely seen since connect (the
        # session setup's online/offline dots scan + read this).
        self._last_seen: dict[str, float] = {}
        # time.monotonic() captured when scan() last broadcast its ping. A node
        # is "online" if it answered since then; because scan() moves this
        # forward, a node that stops responding (powered off) drops offline on
        # the next scan — unlike known_macs, which only ever grows. None until
        # the first scan (reachability unknown).
        self._scan_ref: float | None = None
        # RGBW LED-ring variant self-reported by each node in its ready/pong frame
        # (mac -> bool). Lets the OTA picker auto-select the right firmware bin
        # instead of asking the user. A node absent here hasn't reported it yet
        # (offline, or running firmware that predates the field).
        self._node_rgbw: dict[str, bool] = {}
        # Strong refs to "the link dropped on its own" listeners (e.g. the panel
        # repainting itself as disconnected). Fired only on an *unexpected* loss
        # — the gateway unplugged or reset (e.g. just after a flash) — not on a
        # caller-initiated :meth:`disconnect`.
        self._disconnect_callbacks: list[Callable[[], None]] = []

    @property
    def known_macs(self) -> frozenset[str]:
        """MAC addresses of nodes that have sent at least one message."""
        return frozenset(self._known_macs)

    def node_last_seen(self, mac: str) -> float | None:
        """``time.monotonic()`` of the node's last message, or None (never).

        Compare against a reference taken before a :meth:`scan` to know which
        nodes answered THAT scan (i.e. are reachable right now) rather than at
        any point since connect."""
        return self._last_seen.get(mac)

    def is_online(self, mac: str) -> bool:
        """True if ``mac`` answered the most recent :meth:`scan` — reachable now.

        Unlike :attr:`known_macs` (which only ever grows), this drops back to
        False for a node that stops responding, because ``scan`` moves the
        reference forward and a powered-off node no longer refreshes its
        last-seen time. Callers show the online set by scanning, waiting for the
        reply window (~2 s), then reading this. False before the first scan."""
        if self._scan_ref is None:
            return False
        ts = self._last_seen.get(mac)
        return ts is not None and ts >= self._scan_ref

    @property
    def online_macs(self) -> frozenset[str]:
        """MACs that answered the most recent :meth:`scan` (reachable now)."""
        if self._scan_ref is None:
            return frozenset()
        ref = self._scan_ref
        return frozenset(m for m, ts in self._last_seen.items() if ts >= ref)

    def node_rgbw(self, mac: str) -> bool | None:
        """RGBW LED-ring variant a node reported (True/False), or None if unknown.

        Populated from the ``rgbw`` field in a node's ready/pong frame (a scan or
        boot refreshes it). None means the node hasn't reported it yet — offline,
        or running firmware from before the field existed.
        """
        return self._node_rgbw.get(mac)

    @property
    def is_connected(self) -> bool:
        """Check if gateway is connected."""
        return self._serial is not None and self._serial.is_open

    def connect(self, verify: bool = True) -> bool:
        """Open serial connection to the gateway.

        Opening a serial port succeeds for *any* device that enumerates on it
        — a node flashed over USB, an unrelated dev board, etc. ``verify``
        (default) therefore runs a short handshake and only reports success if
        the peer actually identifies itself as a SoftEdIBO gateway, so we never
        mistake some other serial device for the gateway. Pass ``verify=False``
        to skip the check (e.g. tests with a fake port).
        """
        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baud_rate,
                timeout=1,
                write_timeout=WRITE_TIMEOUT_S,
            )
        except serial.SerialException as e:
            logger.warning("Failed to connect to gateway on %s: %s", self._port, e)
            return False

        if verify and not self._verify_is_gateway():
            logger.warning(
                "Device on %s did not identify as a SoftEdIBO gateway — "
                "not connecting", self._port,
            )
            self._serial.close()
            self._serial = None
            return False

        self._running = True
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()
        logger.info("Connected to SoftEdIBO gateway on %s", self._port)
        return True

    def _is_gateway_line(self, raw: bytes | bytearray) -> bool:
        """True if ``raw`` is a line only a gateway would emit.

        A gateway announces ``gateway_ready`` once at boot, always answers the
        gateway-local ``get_ap`` probe (``ap_config`` on the SoftAP build,
        ``ap_not_supported`` otherwise), and is the only thing that tags
        forwarded node messages with ``source``. A node's USB serial emits
        none of these, so any one of them confirms a real gateway.
        """
        try:
            data = json.loads(raw.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        if data.get("status") == "gateway_ready":
            return True
        if data.get("type") in ("ap_config", "ap_set"):
            return True
        if data.get("type") == "error" and data.get("reason") == "ap_not_supported":
            return True
        return "source" in data

    def _send_probe(self) -> None:
        """Poke the gateway so it answers even if it booted long ago.

        The IDF gateways (C6/S3) always reply to the gateway-local ``get_ap``;
        the old Arduino gateway ignores it but still streams ``gateway_ready``
        and forwarded node messages, which :meth:`_is_gateway_line` accepts.
        """
        ser = self._serial
        if ser is None:
            return
        try:
            ser.write(b'{"cmd":"get_ap"}\n')
        except serial.SerialException:
            pass

    def _scan_lines(self, buf: bytearray) -> bool:
        """Consume complete newline-terminated lines from ``buf`` in place,
        returning True as soon as one of them proves a gateway."""
        while b"\n" in buf:
            raw, _, rest = buf.partition(b"\n")
            buf[:] = rest
            if self._is_gateway_line(raw):
                return True
        return False

    def _verify_is_gateway(self) -> bool:
        """Block until the peer proves it is a gateway, or the timeout elapses.

        Runs before the background read thread starts, so it owns the serial
        port and can read synchronously. Partial/garbled lines simply fail the
        JSON parse in :meth:`_is_gateway_line` and are ignored.
        """
        if self._serial is None:
            return False
        prev_timeout = self._serial.timeout
        self._serial.timeout = 0.1
        buf = bytearray()
        try:
            self._serial.reset_input_buffer()
            self._send_probe()
            last_probe = time.monotonic()
            deadline = last_probe + VERIFY_TIMEOUT_S
            while time.monotonic() < deadline:
                try:
                    buf.extend(self._serial.read(self._serial.in_waiting or 1))
                except serial.SerialException:
                    return False
                if self._scan_lines(buf):
                    return True
                now = time.monotonic()
                if now - last_probe > 0.6:  # re-prod in case the first was dropped
                    last_probe = now
                    self._send_probe()
            return False
        finally:
            if self._serial is not None:
                self._serial.timeout = prev_timeout

    def disconnect(self) -> None:
        """Close serial connection."""
        self._running = False
        if self._read_thread is not None:
            self._read_thread.join(timeout=2)
        if self._serial is not None:
            self._serial.close()
            self._serial = None
        self._known_macs.clear()
        self._node_rgbw.clear()
        self._last_seen.clear()
        self._scan_ref = None
        logger.info("Disconnected from SoftEdIBO gateway")

    def send(self, target_mac: str, command: str, repeat: int = 1,
             **kwargs: Any) -> bool:
        """Send a command to a remote ESP32 node via the gateway.

        ``repeat`` writes the same frame more than once. ESP-NOW delivery is
        best-effort and the node's radio competes with its own telemetry, so an
        occasional command frame is dropped — the "didn't go on the first try"
        symptom. Sending an *idempotent* command a few times makes it very likely
        one lands without the node acting on it twice: setting limits is
        idempotent, inflate/deflate target a level (the firmware no-ops once it is
        reached), and a valve/pump toggle just re-asserts a state. Do NOT use
        repeat for commands that accumulate per frame.
        """
        if not self.is_connected:
            if not self._logged_disconnected:
                logger.debug("Gateway not connected — commands will be dropped")
                self._logged_disconnected = True
            return False
        self._logged_disconnected = False

        ser = self._serial
        if ser is None:              # lost between the check and the write
            return False
        message = {"target": target_mac, "cmd": command, **kwargs}
        try:
            line = json.dumps(message)
            payload = (line + "\n").encode("utf-8")
            with self._write_lock:
                for _ in range(max(1, repeat)):
                    ser.write(payload)
            logger.debug("Sent to %s: %s%s", target_mac, command,
                         f" (x{repeat})" if repeat > 1 else "")
            self._emit_raw("tx", line)
            return True
        except serial.SerialException:
            logger.exception("Failed to send command to %s", target_mac)
            return False

    def send_gateway(self, command: str, **kwargs: Any) -> bool:
        """Send a command to the gateway itself (not relayed to a node).

        Gateway-local commands carry no ``target`` field, so the firmware
        handles them on the spot instead of forwarding over ESP-NOW (e.g.
        ``get_ap`` / ``set_ap`` for the SoftAP build).
        """
        if not self.is_connected:
            return False
        ser = self._serial
        if ser is None:              # lost between the check and the write
            return False
        message = {"cmd": command, **kwargs}
        try:
            line = json.dumps(message)
            with self._write_lock:
                ser.write((line + "\n").encode("utf-8"))
            self._emit_raw("tx", line)
            return True
        except serial.SerialException:
            logger.exception("Failed to send gateway command %s", command)
            return False

    def send_raw(self, text: str) -> bool:
        """Write a raw line to the gateway serial port.

        Used by the serial monitor to send arbitrary commands the structured
        :meth:`send` API can't express. A trailing newline is added if missing.
        """
        if not self.is_connected:
            return False
        ser = self._serial
        if ser is None:              # lost between the check and the write
            return False
        line = text if text.endswith("\n") else text + "\n"
        try:
            with self._write_lock:
                ser.write(line.encode("utf-8"))
            self._emit_raw("tx", line.rstrip("\n"))
            return True
        except serial.SerialException:
            logger.exception("Failed to send raw serial line")
            return False

    def scan(self) -> None:
        """Broadcast a ping to all nodes.

        Responders appear in :attr:`known_macs` and refresh their last-seen
        time. The reference captured here is what makes :meth:`is_online` /
        :attr:`online_macs` report reachability as of *this* scan, so a node
        powered off since the previous scan reads offline rather than staying
        stuck online. Set before the ping so a reply can only land after it."""
        self._scan_ref = time.monotonic()
        self.send("FF:FF:FF:FF:FF:FF", "ping")

    def on_message(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback for incoming messages from ESP32 nodes."""
        self._callbacks.append(weakref.WeakMethod(callback))

    def remove_message_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Deregister a callback previously passed to :meth:`on_message`.

        Used by short-lived consumers (e.g. the OTA updater) that need to stop
        receiving messages deterministically rather than waiting for GC.
        """
        self._callbacks = [
            wr for wr in self._callbacks if wr() is not None and wr() != callback
        ]

    def on_raw(self, callback: Callable[[str, str], None]) -> None:
        """Register a tap for raw serial traffic.

        ``callback(direction, text)`` is invoked for every complete line, where
        ``direction`` is ``"rx"`` (from the gateway) or ``"tx"`` (sent by us).
        RX callbacks fire on the serial read thread, so consumers that touch the
        GUI must marshal to the GUI thread (e.g. via a Qt signal).
        """
        self._raw_callbacks.append(callback)

    def remove_raw_callback(self, callback: Callable[[str, str], None]) -> None:
        """Deregister a tap previously passed to :meth:`on_raw`."""
        if callback in self._raw_callbacks:
            self._raw_callbacks.remove(callback)

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        """Register a listener for an *unexpected* loss of the gateway link.

        Fired from the serial read thread when the port dies on its own (the
        gateway unplugged or reset — e.g. right after flashing). Not fired on a
        caller-initiated :meth:`disconnect`. Consumers that touch the GUI must
        marshal to the GUI thread (e.g. via a Qt signal).
        """
        self._disconnect_callbacks.append(callback)

    def _notify_disconnected(self) -> None:
        for cb in list(self._disconnect_callbacks):
            try:
                cb()
            except Exception:
                logger.exception("Disconnect callback failed")

    def _emit_raw(self, direction: str, text: str) -> None:
        for cb in list(self._raw_callbacks):
            try:
                cb(direction, text)
            except Exception:
                logger.exception("Raw serial tap callback failed")

    def _dispatch_line(self, raw: bytes | bytearray) -> None:
        """Parse one complete line and fan it out to registered callbacks."""
        if not raw.strip():
            return
        self._emit_raw("rx", raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        try:
            data = json.loads(raw.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Invalid JSON from gateway: %s", raw)
            return
        source = data.get("source")
        # "thymio" is the 802.15.4/C6 route tag, not an ESP-NOW node — keep it out of the
        # node list so it doesn't show up in Discover Nodes / the Add Node picker.
        if source == "thymio":
            # The C6 return path (sensor replies, discovery, link acks) is otherwise
            # invisible in the log — surface every C6 frame so a missing thymio_sensors
            # stream is diagnosable from softedibo.log.
            logger.debug("From C6 (thymio): %s", data)
        if source and source != "thymio":
            self._known_macs.add(source)
            self._last_seen[source] = time.monotonic()
            # Nodes self-report their LED-ring build in ready/pong so the OTA
            # picker can pick the matching bin without asking (see node_rgbw).
            if "rgbw" in data:
                self._node_rgbw[source] = bool(data["rgbw"])
        # Surface gateway-reported errors (e.g. a command the gateway couldn't
        # parse because the serial link dropped/garbled bytes) so a swallowed
        # command is visible in the console instead of failing silently.
        if data.get("type") == "error":
            logger.warning("Gateway error: %s", data)
        dead: list[weakref.ref] = []
        for wr in self._callbacks:
            cb = wr()
            if cb is None:
                dead.append(wr)
            else:
                cb(data)
        for d in dead:
            self._callbacks.remove(d)

    def _read_loop(self) -> None:
        """Background thread that reads incoming serial data.

        Accumulates raw bytes and only dispatches complete newline-terminated
        lines. This avoids parsing partial messages: ``readline()`` returns
        whatever is buffered when the serial timeout fires, which can split a
        message mid-line and produce spurious "Invalid JSON" warnings.

        The gateway streams continuously, so opening the port usually lands
        mid-message. The bytes before the first newline are therefore a partial
        fragment — discard them to resync rather than dispatch a broken line.
        """
        buf = bytearray()
        synced = False
        while self._running and self._serial is not None:
            try:
                chunk = self._serial.read(self._serial.in_waiting or 1)
                if not chunk:
                    continue
                buf.extend(chunk)
                while b"\n" in buf:
                    raw, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    if not synced:
                        synced = True
                        # The first line after connecting can be a mid-message
                        # fragment (we usually land mid-stream). A real gateway line
                        # is JSON starting with '{'; only drop it when it isn't one.
                        # Dropping unconditionally swallowed the first reply when the
                        # gateway was briefly silent (fresh connect, no node traffic,
                        # IDF logs off), which stalled WiFi-OTA staging.
                        if not raw.lstrip().startswith(b"{"):
                            continue
                    self._dispatch_line(raw)
            except serial.SerialException:
                logger.exception("Serial read error — gateway disconnected")
                if self._serial is not None:
                    self._serial.close()
                    self._serial = None
                self._running = False
                self._notify_disconnected()
                break
