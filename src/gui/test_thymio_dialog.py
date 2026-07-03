"""Test Thymio dialog — jog the wheels, colour the LED, play sounds and watch the
live accelerometer/mic + impact readout for one Thymio.

The dialog drives the **live robot's link** (dongle or gateway/C6), so it exercises
exactly the transport a session uses. The sensor readout only fills when the robot
is on the gateway/C6 path — that link streams ``thymio_sensors`` (acc/mic) which the
:class:`~src.robots.thymio.thymio_gateway_link.ThymioGatewayLink` turns into
``on_sensors`` / ``on_impact`` callbacks. Those fire on the gateway's serial read
thread, so we marshal them to the GUI thread through Qt signals (same pattern as the
Test Actuators dialog's magnet stream).

The impact threshold is tunable live and written back to the robot config on close
(via the ``on_save_threshold`` callback), so a knock calibration sticks.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from src.gui.async_task import run_async
from src.gui.base_dialog import BaseDialog
from src.gui.ui_test_thymio_dialog import Ui_TestThymioDialog
from src.robots.base_robot import RobotStatus
from src.robots.thymio.thymio_robot import ThymioRobot

# Aseba leds.top channels are 0..32, not 0..255.
_LED_MAX = 32


class TestThymioDialog(BaseDialog, Ui_TestThymioDialog):
    """Interactive tester for one Thymio's wheels, LED, sound and sensors."""

    # Gateway read-thread → GUI-thread bridges (the link's callbacks fire off-thread).
    _sensors_received = Signal(object, int, object)   # (acc tuple, mic, ground tuple|None)
    _impact_received = Signal(int)                     # intensity level (1/2/3)
    _lifted_received = Signal(bool)                    # lifted off / set back down

    _IDLE_CHUNK = "QProgressBar::chunk { background-color: #3498db; }"
    _ACTIVE_CHUNK = "QProgressBar::chunk { background-color: #e74c3c; }"
    _LEVEL_NAMES = {1: "touch", 2: "knock", 3: "SLAP"}

    def __init__(self, robot: ThymioRobot, on_save_threshold=None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._robot = robot
        self._on_save_threshold = on_save_threshold
        self._knocks = 0
        self._lifted = False              # last-seen lift state (from the lift stream)
        self._we_connected = False        # did WE open the link (so we close it)?

        self.setWindowTitle(f"Test Thymio — {robot.name}")
        self.intro_label.setText(f"<b>{robot.name}</b>")
        self.dev_bar.setStyleSheet(self._IDLE_CHUNK)

        # Seed the threshold spin from the link's current value (config default),
        # then push spin edits straight to the live link so calibration is instant.
        self.threshold_spin.setValue(float(robot.impact_threshold) or self.threshold_spin.value())

        self._wire_controls()

        # Subscribe to the robot's sensor/impact/lift streams (no-op on a dongle/link-less
        # robot). The callbacks run on the read thread → emit signals to the GUI thread.
        self._sensors_received.connect(self._on_sensors_ui)
        self._impact_received.connect(self._on_impact_ui)
        self._lifted_received.connect(self._on_lifted_ui)
        robot.on_sensors(self._sensors_cb)
        robot.on_impact(self._impact_cb)
        robot.on_lifted(self._lifted_cb)

        self.finished.connect(self._on_closed)

        # Robots are built at startup but their wheeled-base link is only opened by
        # connect() — which nothing calls outside a session — so bring the link up
        # here (off-thread; the dongle path can block on discovery) or nothing moves
        # and no sensor stream arrives.
        self._ensure_connected()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if self._robot.status == RobotStatus.CONNECTED:
            self.status_label.setText("Connected.")
            return
        self.status_label.setText("Connecting to the Thymio…")
        self._set_controls_enabled(False)
        run_async(self._robot.connect, on_done=self._on_connected,
                  on_error=self._on_connect_failed, parent=self)

    def _on_connected(self, ok: bool) -> None:
        if ok:
            self._we_connected = True
            self.status_label.setText("Connected.")
            self._set_controls_enabled(True)
        else:
            self.status_label.setText(
                "Could not reach the Thymio's wheeled base. Check 'Drive wheels "
                "wirelessly' is saved, the transport is up (dongle plugged / gateway "
                "connected) and the robot is powered on.")

    def _on_connect_failed(self, exc: Exception) -> None:
        self.status_label.setText(f"Connect failed: {exc}")

    def _set_controls_enabled(self, on: bool) -> None:
        for w in (self.drive_group, self.leds_group, self.sound_group):
            w.setEnabled(on)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def _wire_controls(self) -> None:
        self.fwd_btn.clicked.connect(lambda: self._drive(1, 1))
        self.back_btn.clicked.connect(lambda: self._drive(-1, -1))
        self.left_btn.clicked.connect(lambda: self._drive(-1, 1))
        self.right_btn.clicked.connect(lambda: self._drive(1, -1))
        self.stop_btn.clicked.connect(self._stop)

        self.led_apply_btn.clicked.connect(self._apply_led)
        self.led_off_btn.clicked.connect(lambda: self._robot.set_leds(0, 0, 0))

        self.sys_play_btn.clicked.connect(
            lambda: self._robot.play_sound(system=self.sys_spin.value()))
        self.tone_play_btn.clicked.connect(
            lambda: self._robot.play_sound(freq=self.tone_freq_spin.value(),
                                           duration_ms=self.tone_dur_spin.value()))

        self.threshold_spin.valueChanged.connect(self._on_threshold_changed)
        self.stop_all_btn.clicked.connect(self._stop_all)
        self.close_btn.clicked.connect(self.accept)

    # ------------------------------------------------------------------
    # Drive / LED
    # ------------------------------------------------------------------

    def _drive(self, left_sign: int, right_sign: int) -> None:
        speed = self.speed_spin.value()
        self._robot.set_motors(left_sign * speed, right_sign * speed)

    def _stop(self) -> None:
        self._robot.set_motors(0, 0)

    def _apply_led(self) -> None:
        self._robot.set_leds(self.led_r_spin.value(),
                             self.led_g_spin.value(),
                             self.led_b_spin.value())

    def _stop_all(self) -> None:
        self._robot.set_motors(0, 0)
        self._robot.set_leds(0, 0, 0)

    # ------------------------------------------------------------------
    # Impact threshold
    # ------------------------------------------------------------------

    def _on_threshold_changed(self, value: float) -> None:
        # Live-tune the link; rescale the bar so a knock still fills a good part of it.
        self._robot.impact_threshold = float(value)
        self.dev_bar.setMaximum(max(10, int(value * 3)))

    # ------------------------------------------------------------------
    # Sensor stream (read thread → signal → GUI thread)
    # ------------------------------------------------------------------

    def _sensors_cb(self, acc, mic: int, ground) -> None:
        self._sensors_received.emit(acc, mic, ground)

    def _impact_cb(self, level: int) -> None:
        self._impact_received.emit(int(level))

    def _lifted_cb(self, lifted: bool) -> None:
        self._lifted_received.emit(bool(lifted))

    def _on_sensors_ui(self, acc, mic: int, ground) -> None:
        x, y, z = acc
        self.sensors_status_label.setText("Streaming acc + mic + ground from the Thymio.")
        self.acc_label.setText(f"acc:  x {x}   y {y}   z {z}")
        self.mic_bar.setValue(min(mic, self.mic_bar.maximum()))
        self.mic_value_label.setText(f"{mic}")
        dev = self._robot_link_deviation(acc)
        self.dev_bar.setValue(min(int(dev), self.dev_bar.maximum()))
        thr = self.threshold_spin.value()
        over = dev >= thr
        self.dev_bar.setStyleSheet(self._ACTIVE_CHUNK if over else self._IDLE_CHUNK)
        self.dev_value_label.setText(f"{dev:.0f} / {thr:.0f}")
        if ground is not None:
            place = "LIFTED" if self._lifted else "on the table"
            self.ground_label.setText(
                f"ground: {ground[0]}  {ground[1]}      {place}")

    @staticmethod
    def _robot_link_deviation(acc) -> float:
        from src.robots.thymio.thymio_gateway_link import ThymioGatewayLink
        return ThymioGatewayLink.acc_deviation(tuple(acc))

    def _on_impact_ui(self, level: int) -> None:
        self._knocks += 1
        name = self._LEVEL_NAMES.get(level, "hit")
        self.impact_label.setText(f"knocks: {self._knocks}  ● {name}")

    def _on_lifted_ui(self, lifted: bool) -> None:
        self._lifted = lifted   # remembered so the next sensor frame keeps showing it

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _on_closed(self) -> None:
        self._robot.remove_sensors_listener(self._sensors_cb)
        self._robot.remove_impact_listener(self._impact_cb)
        self._robot.remove_lifted_listener(self._lifted_cb)
        self._robot.set_motors(0, 0)
        # If we opened the link just for this test, close it again so we don't leave
        # the C6 poller running / the robot "connected" outside a session.
        if self._we_connected:
            self._robot.disconnect()
        # Persist the calibrated threshold back to the robot config, if the host
        # gave us a saver (so knocking-to-calibrate sticks across sessions).
        if self._on_save_threshold is not None:
            self._on_save_threshold(float(self.threshold_spin.value()))
