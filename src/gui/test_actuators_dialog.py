"""Test actuators dialog — inflate/deflate individual chambers via the gateway."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core.skin_config import FILL_MODE_PRESSURE, normalize_fill_mode
from src.gui.led_ring_tester import LedRingTester
from src.gui.ui_test_actuators_dialog import Ui_TestActuatorsDialog
from src.hardware.espnow_gateway import ESPNowGateway
from src.hardware.fill_profile import FillProfile


class TestActuatorsDialog(QDialog, Ui_TestActuatorsDialog):
    """Dialog for sending inflate/deflate commands to a node's chambers.

    Commands are sent directly via the gateway without going through the
    robot layer, so the dialog works with the current (possibly unsaved)
    node configuration.

    Args:
        mac: Target ESP32 MAC address.
        skin_cfgs: List of skin config dicts. Each is ``skin_id`` plus a
            ``chambers`` list of per-chamber dicts (``slot``, ``max_pressure``,
            ``min_pressure``, ``fill_mode`` and optional ``fill_time_ms`` /
            ``fill_profile``). The limits are pushed to the node before each
            actuation, and time-mode chambers inflate by their calibrated time
            window — mirroring how :class:`~src.hardware.skin.Skin` drives them.
        gateway: Connected ESP-NOW gateway.
        parent: Optional parent widget.
    """

    # Emitted from the gateway read thread; connected to _update_pressure (main thread)
    _pressure_received = Signal(int, int, float)   # chamber, pressure_pct, kpa

    def __init__(
        self,
        mac: str,
        skin_cfgs: list[dict],
        gateway: ESPNowGateway,
        led_count: int = 24,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._mac = mac
        self._gateway = gateway
        self._active = True
        # True while STOP ALL has the node latched off. We do NOT auto-resume
        # (that would throw away the firmware's continuous "stay-off" enforcement
        # and lose the stop if a single ESP-NOW frame drops); instead we re-arm
        # lazily right before the next actuation. See _stop_all / _arm.
        self._stopped = False
        self._pressure_labels: dict[int, QLabel] = {}   # slot => label
        self._valve_states: dict[tuple[int, int], tuple[bool, QPushButton]] = {}  # (slot, side) => (open, button)
        # Per-chamber inflate/deflate buttons, so the continuous-run toggle can
        # update their text. (slot, direction) => button; direction 0=inflate, 1=deflate.
        self._chamber_btns: dict[tuple[int, int], QPushButton] = {}
        # slot => chamber config dict (max/min pressure, fill mode + calibration).
        # Used to push the configured limits to the node before actuating and to
        # inflate time-mode chambers by their calibrated time window instead of
        # closing the loop on the laggy gauge sensor — matching Skin in production.
        self._chamber_cfgs: dict[int, dict] = {}

        self.setupUi(self)
        self.setWindowTitle(f"Test Actuators — {mac}")
        self.close_btn.clicked.connect(self.accept)

        # When checked, a per-chamber Inflate/Deflate button starts a continuous
        # run of just that chamber (ignores the pressure cap, runs until the
        # button is pressed again) instead of a one-shot fill to max/min.
        self._cont_cb = QCheckBox(
            "Ignore max pressure — per-chamber Inflate/Deflate runs until stopped")
        self._cont_cb.setWhatsThis(
            "While checked, a chamber's Inflate or Deflate button toggles a "
            "continuous run of that one chamber: it opens the matching valve and "
            "drives the pump, ignoring the configured max/min pressure, until you "
            "press the button (now ⏹ Stop) again. While unchecked, those "
            "buttons do a one-shot fill toward the configured max/min.")

        if not skin_cfgs:
            self.no_chambers_label.setVisible(True)
        else:
            self.chambers_scroll.setVisible(True)
            for skin_cfg in skin_cfgs:
                self.chambers_vbox.addWidget(self._build_chamber_group(skin_cfg))
            self.chambers_vbox.addStretch()
            self.verticalLayout.insertWidget(
                self.verticalLayout.indexOf(self.chambers_scroll) + 1,
                self._cont_cb)
            # Apply the configured limits up front so the firmware's reported
            # pressure %% matches each chamber's min/max from the start, not only
            # after that chamber is first actuated (otherwise it uses boot defaults).
            self._push_all_limits()

        # WS2812 LED ring tester (node_direct boards). Insert before the
        # Close button (the last widget in the dialog's vertical layout).
        if led_count > 0:
            self._led_tester = LedRingTester(led_count, self._send_led)
            self.verticalLayout.insertWidget(
                self.verticalLayout.count() - 1, self._led_tester)

        # Pump controls (toggle style, monospace font for fixed width)
        self._pump_states: dict[int, tuple[bool, QPushButton]] = {}  # pump => (on, button)
        pump_group = QGroupBox("Pump Control")
        pump_layout = QHBoxLayout(pump_group)
        monospace_style = "font-family: Courier; font-size: 10pt;"

        pump_inf_btn = QPushButton("Inflate Pump: OFF")
        pump_inf_btn.setMaximumWidth(160)
        pump_inf_btn.setStyleSheet(monospace_style)
        pump_inf_btn.clicked.connect(lambda _=False, p=0, btn=pump_inf_btn: self._toggle_pump(p, btn))
        self._pump_states[0] = (False, pump_inf_btn)
        pump_layout.addWidget(pump_inf_btn)

        pump_def_btn = QPushButton("Deflate Pump: OFF")
        pump_def_btn.setMaximumWidth(160)
        pump_def_btn.setStyleSheet(monospace_style)
        pump_def_btn.clicked.connect(lambda _=False, p=1, btn=pump_def_btn: self._toggle_pump(p, btn))
        self._pump_states[1] = (False, pump_def_btn)
        pump_layout.addWidget(pump_def_btn)

        stop_all_btn = QPushButton("⏹ STOP ALL (Close valves + Off pumps)")
        stop_all_btn.setStyleSheet("background-color: #FF6B6B; font-weight: bold;")
        stop_all_btn.clicked.connect(self._stop_all)
        pump_layout.addWidget(stop_all_btn)

        pump_layout.addStretch()
        self.verticalLayout.insertWidget(
            self.verticalLayout.count() - 1, pump_group)

        # Continuous run (bench wiring test): drive one pump + all of its valves
        # wide open INDEFINITELY, ignoring pressure and the firmware dead-man.
        # Use when the pressure sensor reads wrong and the normal inflate/deflate
        # keeps cutting out. Mutually exclusive; closing the dialog stops it.
        # Active continuous run as (direction, chamber): direction 0=inflate,
        # 1=deflate; chamber -1 = all chambers (global run), else a single
        # chamber. None = no run. One firmware latch → at most one run at a time.
        self._run: tuple[int, int] | None = None
        # Dead-man keepalive for the node_direct continuous run. That run bypasses
        # every firmware safety, so the node ends it if these keepalives stop
        # arriving (dialog gone, USB/ESP-NOW link dropped). See _send_run_keepalive.
        self._run_keepalive = QTimer(self)
        self._run_keepalive.setInterval(1000)
        self._run_keepalive.timeout.connect(self._send_run_keepalive)
        run_group = QGroupBox("Continuous Run (ignores pressure)")
        run_group.setWhatsThis(
            "Drive one pump and all of its valves wide open indefinitely, "
            "ignoring the pressure reading and the firmware dead-man timeout. "
            "Use this to check pump/valve wiring when the pressure sensor reads "
            "wrong. Press the active button again, STOP ALL, or close the dialog "
            "to stop.")
        run_layout = QHBoxLayout(run_group)

        self._run_inf_btn = QPushButton("Run Inflate ∞: OFF")
        self._run_inf_btn.setStyleSheet(monospace_style)
        self._run_inf_btn.setWhatsThis(
            "Turn the inflate pump on and open every inflate valve, and keep them "
            "on until stopped. Ignores pressure limits.")
        self._run_inf_btn.clicked.connect(lambda _=False: self._toggle_run(0))
        run_layout.addWidget(self._run_inf_btn)

        self._run_def_btn = QPushButton("Run Deflate ∞: OFF")
        self._run_def_btn.setStyleSheet(monospace_style)
        self._run_def_btn.setWhatsThis(
            "Turn the deflate pump on and open every deflate valve, and keep them "
            "on until stopped. Ignores pressure limits.")
        self._run_def_btn.clicked.connect(lambda _=False: self._toggle_run(1))
        run_layout.addWidget(self._run_def_btn)

        run_layout.addStretch()
        self.verticalLayout.insertWidget(
            self.verticalLayout.count() - 1, run_group)

        self._pressure_received.connect(self._update_pressure)
        self._gateway.on_message(self._on_gateway_message)
        self.finished.connect(self._on_closed)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_chamber_group(self, skin_cfg: dict) -> QGroupBox:
        skin_id = skin_cfg.get("skin_id", "—")
        chamber_cfgs: list[dict] = skin_cfg.get("chambers", [])
        for cfg in chamber_cfgs:
            self._chamber_cfgs[int(cfg["slot"])] = cfg
        slots: list[int] = sorted(int(c["slot"]) for c in chamber_cfgs)

        box = QGroupBox(f"Air Chamber: {skin_id}")
        vbox = QVBoxLayout(box)

        # Inflate All / Deflate All row
        all_row = QHBoxLayout()
        inf_all = QPushButton("Inflate All")
        def_all = QPushButton("Deflate All")
        inf_all.clicked.connect(lambda _=False, sl=slots: self._inflate_slots(sl))
        def_all.clicked.connect(lambda _=False, sl=slots: self._deflate_slots(sl))
        all_row.addWidget(inf_all)
        all_row.addWidget(def_all)
        all_row.addStretch()
        vbox.addLayout(all_row)

        # Per-slot rows
        for slot in slots:
            slot_row = QHBoxLayout()
            slot_row.addWidget(QLabel(f"  Slot {slot}:"))
            inf_btn = QPushButton("Inflate")
            def_btn = QPushButton("Deflate")
            inf_btn.clicked.connect(lambda _=False, s=slot: self._chamber_dir(s, 0))
            def_btn.clicked.connect(lambda _=False, s=slot: self._chamber_dir(s, 1))
            self._chamber_btns[(slot, 0)] = inf_btn
            self._chamber_btns[(slot, 1)] = def_btn
            slot_row.addWidget(inf_btn)
            slot_row.addWidget(def_btn)

            # Manual valve toggle controls (monospace font for fixed width)
            monospace_style = "font-family: Courier; font-size: 10pt;"

            val_inf_btn = QPushButton("Inflate Valve: CLOSED")
            val_inf_btn.setMaximumWidth(180)
            val_inf_btn.setStyleSheet(monospace_style)
            val_inf_btn.clicked.connect(lambda _=False, s=slot, btn=val_inf_btn: self._toggle_valve(s, 0, btn))
            self._valve_states[(slot, 0)] = (False, val_inf_btn)
            slot_row.addWidget(val_inf_btn)

            val_def_btn = QPushButton("Deflate Valve: CLOSED")
            val_def_btn.setMaximumWidth(180)
            val_def_btn.setStyleSheet(monospace_style)
            val_def_btn.clicked.connect(lambda _=False, s=slot, btn=val_def_btn: self._toggle_valve(s, 1, btn))
            self._valve_states[(slot, 1)] = (False, val_def_btn)
            slot_row.addWidget(val_def_btn)

            pressure_lbl = QLabel("—")
            pressure_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            pressure_lbl.setMinimumWidth(110)
            slot_row.addWidget(pressure_lbl)
            self._pressure_labels[slot] = pressure_lbl
            vbox.addLayout(slot_row)

        return box

    # ------------------------------------------------------------------
    # Pressure updates (gateway callback => signal => main thread)
    # ------------------------------------------------------------------

    def _on_gateway_message(self, data: dict) -> None:
        """Called from the gateway read thread."""
        if not self._active:
            return
        if data.get("source") != self._mac or data.get("type") != "status":
            return
        chamber = data.get("chamber")
        pressure = data.get("pressure")
        kpa = data.get("kpa")
        if isinstance(chamber, int) and isinstance(pressure, int):
            # ``kpa`` only arrives from firmware new enough to send it; NaN flags
            # "unknown" so the label can fall back to just the percentage.
            self._pressure_received.emit(
                chamber, pressure,
                float(kpa) if isinstance(kpa, (int, float)) else float("nan"))

    def _update_pressure(self, chamber: int, pressure: int, kpa: float) -> None:
        """Called in the main thread via Signal.

        The percentage is derived here from the chamber's *configured* min/max
        (kPa), not from the node's ``pressure`` field. The node computes its
        percent against the limits it currently holds, which lag the config until
        ``set_max_pressure`` actually lands — a dropped frame or a continuous
        "ignore max" run leaves it on the 8 kPa boot default, so every reading
        above that clamps to 100 %. Recomputing from the config the dialog already
        holds keeps the readout honest regardless of what the node holds."""
        lbl = self._pressure_labels.get(chamber)
        if not lbl:
            return
        if kpa == kpa:   # not NaN → firmware reported real kPa
            lbl.setText(f"{kpa:.2f} kPa  ({self._pct_for_kpa(chamber, kpa)}%)")
        else:
            lbl.setText(f"{pressure}%")

    def _pct_for_kpa(self, chamber: int, kpa: float) -> int:
        """Percent of the chamber's configured [min, max] kPa range (0-100).

        Mirrors firmware ``units::kpaToPct`` but uses the limits the user set in
        the config rows, so the readout reflects the configured max even before
        (or if) the node's ``set_max_pressure`` takes effect."""
        cfg = self._chamber_cfgs.get(chamber, {})
        min_kpa = float(cfg.get("min_pressure", 0.0))
        max_kpa = float(cfg.get("max_pressure", 8.0))
        span = max_kpa - min_kpa
        if span <= 0.0:
            return 0
        return max(0, min(100, round((kpa - min_kpa) * 100.0 / span)))

    def _on_closed(self) -> None:
        self._active = False
        # A continuous run ignores the firmware dead-man, so it would keep going
        # after the dialog closes — always stop it on the way out.
        self._stop_run()
        # If we left the node latched off via STOP ALL, re-arm it so the rest of
        # the app can drive it again (everything is already off, so this is safe).
        self._arm()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _send_led(self, index: int | None, color_hex: str | None, pattern: str = "solid") -> None:
        """Forward an LED change to the node. color_hex None => turn off;
        index None => whole ring; otherwise a single pixel."""
        if color_hex is None:
            self._gateway.send(self._mac, "set_led", pattern="off")
        elif index is None:
            self._gateway.send(self._mac, "set_led", color=color_hex, pattern=pattern)
        else:
            self._gateway.send(self._mac, "set_led", color=color_hex,
                               index=index, pattern=pattern)

    def _arm(self) -> None:
        """Release the STOP ALL latch (if set) before the next actuation.

        STOP ALL leaves the node latched off so a single delivered ``stop`` frame
        keeps everything off via the firmware's continuous enforcement. The node
        drops every actuation command while latched, so any per-slot / manual
        control must re-arm it first.
        """
        if self._stopped:
            self._gateway.send(self._mac, "resume")
            self._stopped = False

    def _push_limits(self, slot: int) -> None:
        """Push this chamber's configured max+min pressure to the node before
        actuating, so a one-shot inflate/deflate targets the configured limits.

        The dialog drives the node directly (it never builds a Skin), so unlike a
        live session nothing has applied these limits — without this the firmware
        uses its boot defaults (8 kPa / 0 kPa), so deflate toward 0 kPa stops at
        once and inflate stops at the default cap, never reaching what was set.
        Order matches Skin._push_pressure_limits (max first, then min)."""
        cfg = self._chamber_cfgs.get(slot)
        if not cfg:
            return
        self._gateway.send(self._mac, "set_max_pressure", chamber=slot,
                           value=float(cfg["max_pressure"]))
        self._gateway.send(self._mac, "set_min_pressure", chamber=slot,
                           value=float(cfg["min_pressure"]))

    def _push_all_limits(self) -> None:
        """Push every configured chamber's limits once on open, mirroring
        Skin._push_pressure_limits. Without this the firmware reports its boot
        defaults (8 kPa / 0 kPa) until a chamber is first actuated, so the status
        %% shown next to the kPa reading wouldn't match the configured min/max."""
        for slot in self._chamber_cfgs:
            self._push_limits(slot)

    def _inflate_ms(self, slot: int) -> int | None:
        """Full-window calibrated fill time (ms) for a time-mode chamber, else None.

        Mirrors Skin._inflate: a chamber in ``time`` mode with a calibration curve
        inflates by a time window so the laggy gauge sensor never closes the loop;
        a ``pressure``-mode chamber (or one with no curve) stays closed-loop."""
        cfg = self._chamber_cfgs.get(slot)
        if not cfg or normalize_fill_mode(cfg.get("fill_mode")) == FILL_MODE_PRESSURE:
            return None
        profile = (FillProfile.from_list(cfg.get("fill_profile"))
                   or FillProfile.linear(cfg.get("fill_time_ms")))
        if profile is None or profile.is_empty:
            return None
        return int(round(profile.time_for_pct(100)))

    def _inflate_slot(self, slot: int) -> None:
        # The node reads "delta" (percent of this chamber's range), NOT "value":
        # a "value" key is silently ignored and the firmware falls back to 10 %.
        # delta=100 inflates toward the chamber's configured max pressure; for a
        # calibrated time-mode chamber send the time window too (the firmware then
        # ignores the gauge sensor for the fill, capping only at HARD_MAX).
        self._arm()
        self._push_limits(slot)
        ms = self._inflate_ms(slot)
        if ms:
            self._gateway.send(self._mac, "inflate", chamber=slot, delta=100, ms=ms)
        else:
            self._gateway.send(self._mac, "inflate", chamber=slot, delta=100)

    def _deflate_slot(self, slot: int) -> None:
        # delta=100 deflates toward the chamber's configured min pressure (the
        # firmware's deflate time cap is always armed as the vacuum backstop).
        self._arm()
        self._push_limits(slot)
        self._gateway.send(self._mac, "deflate", chamber=slot, delta=100)

    def _inflate_slots(self, slots: list[int]) -> None:
        for slot in slots:
            self._inflate_slot(slot)

    def _deflate_slots(self, slots: list[int]) -> None:
        for slot in slots:
            self._deflate_slot(slot)

    def _toggle_valve(self, chamber: int, side: int, btn: QPushButton) -> None:
        """Toggle valve open/closed and update button appearance."""
        key = (chamber, side)
        is_open, _ = self._valve_states.get(key, (False, btn))
        is_open = not is_open

        # Update state
        self._valve_states[key] = (is_open, btn)

        # Update button appearance (fixed-width for consistent size)
        side_name = "Inflate" if side == 0 else "Deflate"
        status = "OPEN  " if is_open else "CLOSED"
        btn.setText(f"{side_name} Valve: {status}")

        # Send command to firmware (re-arm first if STOP ALL latched the node)
        if is_open:
            self._arm()
        self._gateway.send(self._mac, "valve_manual", chamber=chamber,
                          side=side, open=1 if is_open else 0)

    def _toggle_pump(self, pump: int, btn: QPushButton) -> None:
        """Toggle pump on/off and update button appearance."""
        is_on, _ = self._pump_states.get(pump, (False, btn))
        is_on = not is_on

        # Update state
        self._pump_states[pump] = (is_on, btn)

        # Update button appearance (fixed-width for consistent size)
        pump_name = "Inflate" if pump == 0 else "Deflate"
        status = "ON " if is_on else "OFF"
        btn.setText(f"{pump_name} Pump: {status}")

        # Send command to firmware (re-arm first if STOP ALL latched the node)
        if is_on:
            self._arm()
        self._gateway.send(self._mac, "pump_manual", pump=pump, on=1 if is_on else 0)

    def _chamber_dir(self, slot: int, direction: int) -> None:
        """Per-chamber Inflate (``direction`` 0) / Deflate (1) button.

        If this chamber+direction is the active continuous run, stop it. Else,
        with 'Ignore max pressure' checked, start a continuous run of just this
        chamber (opens its valve + drives the pump, ignoring the pressure cap,
        until stopped); otherwise do a one-shot fill toward the configured
        max/min."""
        if self._run == (direction, slot):
            self._stop_run()
        elif self._cont_cb.isChecked():
            self._start_run(direction, slot)
        elif direction == 0:
            self._inflate_slot(slot)
        else:
            self._deflate_slot(slot)

    def _toggle_run(self, direction: int) -> None:
        """Global continuous run: every chamber's valve of ``direction`` (0=inflate,
        1=deflate) wide open + the pump, indefinitely. See :meth:`_start_run`."""
        if self._run == (direction, -1):
            self._stop_run()
        else:
            self._start_run(direction, -1)

    def _start_run(self, direction: int, chamber: int) -> None:
        """Start a continuous open-loop run, ignoring pressure and the dead-man.

        ``test_run`` makes the firmware drive the inflate/deflate pump plus the
        matching valve(s) wide open until stopped — every chamber when
        ``chamber`` is -1, else just that one. There's a single firmware latch,
        so any run replaces the previous one; ``_refresh_run_buttons`` reverts
        the old button. ``testRun`` clears manual overrides, so reset those too."""
        self._arm()
        self._reset_manual_ui()
        self._run = (direction, chamber)
        self._gateway.send(self._mac, "test_run", dir=direction, chamber=chamber)
        self._run_keepalive.start()
        self._refresh_run_buttons()

    def _stop_run(self) -> None:
        if self._run is None:
            return
        self._run_keepalive.stop()
        self._run = None
        self._gateway.send(self._mac, "test_stop")
        self._refresh_run_buttons()

    def _send_run_keepalive(self) -> None:
        """Re-send the active continuous run as a dead-man keepalive.

        node_direct's ``test_run`` short-circuits every firmware safety (pressure
        cutoff, dead-man, watchdog), so the node force-stops the run if these
        keepalives stop arriving. Re-sending the same dir/chamber only refreshes
        the node's dead-man timer — it does not re-assert the hardware. No-op on
        boards without ``test_run`` (node_multiplexed)."""
        run = self._run
        if run is None:
            self._run_keepalive.stop()
            return
        direction, chamber = run
        self._gateway.send(self._mac, "test_run", dir=direction, chamber=chamber)

    def _refresh_run_buttons(self) -> None:
        run = self._run
        self._run_inf_btn.setText(
            f"Run Inflate ∞: {'ON ' if run == (0, -1) else 'OFF'}")
        self._run_def_btn.setText(
            f"Run Deflate ∞: {'ON ' if run == (1, -1) else 'OFF'}")
        for (slot, direction), btn in self._chamber_btns.items():
            default = "Inflate" if direction == 0 else "Deflate"
            btn.setText("⏹ Stop" if run == (direction, slot) else default)

    def _reset_manual_ui(self) -> None:
        """Reset the manual valve/pump toggle buttons to OFF/CLOSED (UI only)."""
        for key, (_, btn) in self._valve_states.items():
            _, side = key
            self._valve_states[key] = (False, btn)
            side_name = "Inflate" if side == 0 else "Deflate"
            btn.setText(f"{side_name} Valve: CLOSED")
        for pump, (_, btn) in self._pump_states.items():
            self._pump_states[pump] = (False, btn)
            pump_name = "Inflate" if pump == 0 else "Deflate"
            btn.setText(f"{pump_name} Pump: OFF")

    def _stop_all(self) -> None:
        """Halt everything and LATCH the node off until the next actuation.

        ``stop`` (firmware ``emergencyStopAll``) cuts both pumps, closes every
        valve, clears manual overrides AND resets every chamber to IDLE. Crucially
        we do NOT immediately ``resume``: the firmware keeps re-asserting the
        all-off state every loop *while latched*, so a single delivered ``stop``
        frame holds everything off permanently — even if a later frame drops. The
        previous code resumed in the same breath, which discarded that continuous
        enforcement; if the lone ``stop`` frame was lost over ESP-NOW the actuator
        kept running until its own 5 s safety timeout (the reported bug).

        The node ignores actuation commands while latched, so the per-slot / manual
        controls re-arm it lazily via :meth:`_arm` on the next press, and the dialog
        re-arms on close so the rest of the app isn't left with a stopped node.

        ``stop`` is sent a few times because ESP-NOW is best-effort; ``stop`` is
        idempotent, so extra frames only improve the odds one lands.
        """
        self._stopped = True
        for _ in range(3):
            self._gateway.send(self._mac, "stop")

        # Reflect the halted hardware in the UI ("stop"/emergencyStopAll also
        # cancels any continuous run, so clear those toggles too).
        self._reset_manual_ui()
        self._run_keepalive.stop()
        self._run = None
        self._refresh_run_buttons()
