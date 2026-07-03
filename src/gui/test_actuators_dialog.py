"""Test actuators dialog — inflate/deflate individual chambers via the gateway."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.skin_config import FILL_MODE_PRESSURE, normalize_fill_mode
from src.gui.led_ring_tester import LedRingTester
from src.gui.sensor_tester import SensorTester
from src.gui.base_dialog import BaseDialog
from src.gui.ui_test_actuators_dialog import Ui_TestActuatorsDialog
from src.hardware.gateway import Gateway
from src.hardware.fill_profile import FillProfile
from src.hardware.units import kpa_to_pct


class TestActuatorsDialog(BaseDialog, Ui_TestActuatorsDialog):
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
        gateway: Connected SoftEdIBO gateway.
        led_count: LED count for a single-ring node (back-compat fallback when
            ``led_rings`` is None).
        led_rings: Per-ring LED counts for the node (e.g. ``[24, 16, 16, 16]``
            for node_multiplexed). Each ring gets its own tester tab and is
            addressed via the ``set_led`` ``ring`` field. None falls back to a
            single ring of ``led_count``.
        parent: Optional parent widget.
    """

    # Emitted from the gateway read thread; connected to _update_pressure (main thread)
    _pressure_received = Signal(int, int, float)   # chamber, pressure_pct, kpa
    # Emitted from the gateway read thread with the node's ACTUAL valve outputs, so
    # a valve shows open whoever opened it (manual toggle, inflate/deflate, the
    # closed-loop control, or the firmware dead-man closing it). Main thread.
    _valves_received = Signal(int, int, int)       # chamber, inflate_open, deflate_open
    # Emitted from the gateway read thread with the node's magnet-sensor stream
    # (per-sensor µT). Connected to _on_magnet_data on the main thread, which
    # lazily builds the sensor tester the first time a node actually streams it.
    _magnet_received = Signal(list)                # per-sensor magnitudes (µT)

    # Monospace valve/pump button base; the open variant adds a green fill so an
    # actually-open valve is obvious at a glance.
    _VALVE_STYLE = "font-family: Courier; font-size: 10pt;"
    _VALVE_OPEN_STYLE = (
        "font-family: Courier; font-size: 10pt; "
        "background-color: #8FD98F; font-weight: bold;")

    def __init__(
        self,
        mac: str,
        skin_cfgs: list[dict],
        gateway: Gateway,
        led_count: int = 24,
        led_rings: list[int] | None = None,
        led_angles: dict[int, float] | None = None,
        on_save_angle=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._mac = mac
        self._gateway = gateway
        # Saved per-ring LED mounting angles (ring index → degrees), shown as each
        # ring tester's starting angle. ``on_save_angle(ring, deg)`` persists a new
        # one to the skin config (None = saving unavailable, hides the Save button).
        self._led_angles = {int(k): float(v) for k, v in (led_angles or {}).items()}
        self._on_save_angle = on_save_angle
        self._active = True
        # True while STOP ALL has the node latched off. We do NOT auto-resume
        # (that would throw away the firmware's continuous "stay-off" enforcement
        # and lose the stop if a single ESP-NOW frame drops); instead we re-arm
        # lazily right before the next actuation. See _stop_all / _arm.
        self._stopped = False
        self._pressure_labels: dict[int, QLabel] = {}   # slot => label
        # (slot, side) => (last-reported-open, button). The open flag is the node's
        # ACTUAL valve state from status (display), NOT what the user clicked.
        self._valve_states: dict[tuple[int, int], tuple[bool, QPushButton]] = {}
        # (slot, side) => True while the user holds a manual valve override open.
        # Kept apart from _valve_states (display) so a transient firmware close
        # (e.g. a dropped keepalive frame) doesn't drop the user's intent; the
        # keepalive re-asserts these to refresh the firmware dead-man.
        self._valve_intent: dict[tuple[int, int], bool] = {}
        # Per-chamber inflate/deflate buttons, so the continuous-run toggle can
        # update their text. (slot, direction) => button; direction 0=inflate, 1=deflate.
        self._chamber_btns: dict[tuple[int, int], QPushButton] = {}
        # slot => chamber config dict (max/min pressure, fill mode + calibration).
        # Used to push the configured limits to the node before actuating and to
        # inflate time-mode chambers by their calibrated time window instead of
        # closing the loop on the laggy gauge sensor — matching Skin in production.
        self._chamber_cfgs: dict[int, dict] = {}
        # Live magnet-sensor readout. Built lazily the first time the node streams
        # a ``magnet`` frame, so nodes without touch sensors show nothing extra.
        self._sensor_tester: SensorTester | None = None

        self.setupUi(self)
        self.setWindowTitle(f"Test Actuators — {mac}")
        self.close_btn.clicked.connect(self.accept)

        # The "Ignore max pressure" checkbox (cont_cb), pump group and continuous
        # run group all live in the .ui (left column); we only wire their signals
        # here. cont_cb stays hidden until there are chambers to drive.
        if not skin_cfgs:
            self.no_chambers_label.setVisible(True)
        else:
            self.chambers_scroll.setVisible(True)
            self.cont_cb.setVisible(True)
            for skin_cfg in skin_cfgs:
                self.chambers_vbox.addWidget(self._build_chamber_group(skin_cfg))
            self.chambers_vbox.addStretch()
            # Apply the configured limits up front so the firmware's reported
            # pressure %% matches each chamber's min/max from the start, not only
            # after that chamber is first actuated (otherwise it uses boot defaults).
            self._push_all_limits()

        # LED ring tester(s). Inserted into the left column just above the pump
        # group (its count/size is only known from the node config, so it can't be
        # authored in the .ui). node_direct has a single ring; node_multiplexed
        # drives four independently-addressable rings — one tester tab each, so the
        # user can assign colours to each ring (and each LED) separately.
        rings = led_rings if led_rings is not None else (
            [led_count] if led_count > 0 else [])
        if len(rings) == 1:
            # Single ring: no "ring" field (back-compat with node_direct frames).
            self._led_tester = LedRingTester(
                rings[0],
                lambda idx, cols, pat, fade, angle:
                    self._send_led(None, idx, cols, pat, fade, angle),
                initial_angle=self._led_angles.get(0, 0.0),
                on_save_angle=self._angle_saver(0))
            self.left_col.insertWidget(
                self.left_col.indexOf(self.pump_group), self._led_tester)
        elif len(rings) > 1:
            self._led_tabs = QTabWidget()
            for k, size in enumerate(rings):
                tester = LedRingTester(
                    size,
                    lambda idx, cols, pat, fade, angle, r=k:
                        self._send_led(r, idx, cols, pat, fade, angle),
                    initial_angle=self._led_angles.get(k, 0.0),
                    on_save_angle=self._angle_saver(k))
                self._led_tabs.addTab(tester, f"Ring {k} · {size} LEDs")
            self.left_col.insertWidget(
                self.left_col.indexOf(self.pump_group), self._led_tabs)

        # Pump controls live in the .ui (pump_group); wire the toggles here.
        # pump => (on, button).
        self._pump_states: dict[int, tuple[bool, QPushButton]] = {
            0: (False, self.pump_inf_btn),
            1: (False, self.pump_def_btn),
        }
        self.pump_inf_btn.clicked.connect(
            lambda _=False: self._toggle_pump(0, self.pump_inf_btn))
        self.pump_def_btn.clicked.connect(
            lambda _=False: self._toggle_pump(1, self.pump_def_btn))
        self.stop_all_btn.clicked.connect(self._stop_all)

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
        # The continuous-run group + buttons live in the .ui (run_group); wire the
        # toggles here.
        self.run_inf_btn.clicked.connect(lambda _=False: self._toggle_run(0))
        self.run_def_btn.clicked.connect(lambda _=False: self._toggle_run(1))

        # Manual valve/pump dead-man keepalive. The firmware auto-closes a manually
        # opened valve / pump after MANUAL_MAX_ON_MS (~5 s) so a lost "off" frame
        # can't leave it running. Re-assert the held overrides well inside that
        # window so they stay put while the dialog is open; the timer stops on
        # close / STOP ALL, so the dead-man still fires if the link drops.
        self._manual_keepalive = QTimer(self)
        self._manual_keepalive.setInterval(1500)
        self._manual_keepalive.timeout.connect(self._send_manual_keepalive)

        self._pressure_received.connect(self._update_pressure)
        self._valves_received.connect(self._update_valves)
        self._magnet_received.connect(self._on_magnet_data)
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

            # Manual valve toggle controls (monospace font for fixed width). The
            # displayed open/closed state is driven by the node's status (see
            # _update_valves), not just by clicks; whatsThis explains both.
            valve_help = (
                "Toggle this valve's manual override. The label and green fill "
                "follow the node's ACTUAL valve state reported ~2×/s, so it also "
                "lights up when an inflate/deflate or the closed-loop control "
                "opens the valve — not only when you click here. While held open "
                "the dialog re-asserts it so the firmware dead-man doesn't close "
                "it after ~5 s; closing the dialog or STOP ALL releases it.")

            val_inf_btn = QPushButton("Inflate Valve: CLOSED")
            val_inf_btn.setMaximumWidth(180)
            val_inf_btn.setStyleSheet(self._VALVE_STYLE)
            val_inf_btn.setWhatsThis(valve_help)
            val_inf_btn.clicked.connect(lambda _=False, s=slot, btn=val_inf_btn: self._toggle_valve(s, 0, btn))
            self._valve_states[(slot, 0)] = (False, val_inf_btn)
            slot_row.addWidget(val_inf_btn)

            val_def_btn = QPushButton("Deflate Valve: CLOSED")
            val_def_btn.setMaximumWidth(180)
            val_def_btn.setStyleSheet(self._VALVE_STYLE)
            val_def_btn.setWhatsThis(valve_help)
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
        if not self._active or data.get("source") != self._mac:
            return
        msg_type = data.get("type")
        if msg_type == "magnet":
            mag = data.get("mag")
            if isinstance(mag, list):
                self._magnet_received.emit([float(v) for v in mag])
            return
        if msg_type != "status":
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
        # ``vi``/``vd`` (actual inflate/deflate valve outputs) only arrive from
        # firmware new enough to send them; older nodes simply leave the valve
        # buttons driven by clicks alone (no regression).
        vi = data.get("vi")
        vd = data.get("vd")
        if isinstance(chamber, int) and isinstance(vi, int) and isinstance(vd, int):
            self._valves_received.emit(chamber, vi, vd)

    def _on_magnet_data(self, mag: list) -> None:
        """Main thread: feed the sensor tester, building it on the first frame.

        The tester is created lazily (and only if the node actually streams a
        ``magnet`` frame) so a node with no touch sensors shows no extra panel.
        The sensor count comes from the first frame's ``mag`` length."""
        if self._sensor_tester is None:
            self._sensor_tester = SensorTester(
                len(mag), self._rezero_sensors, self._configure_sensors)
            # Insert into the right column, above its trailing spacer (index 0), so
            # the panel sits to the RIGHT of the chambers/pump/run controls and is
            # pinned to the top-right rather than stacked below everything.
            self.right_col.insertWidget(0, self._sensor_tester)
            # Show it now so the layout accounts for it immediately: a widget
            # inserted into an already-shown dialog is otherwise only shown on the
            # next event loop pass, and until then the layout treats it as zero —
            # so _grow_to_fit would read a too-small hint and not make room.
            self._sensor_tester.show()
            # The panel is a new right-hand column (not inside the chamber scroll
            # area), so it must not squeeze the rest: grow the dialog to fit its
            # new preferred size rather than compress the columns beside it.
            self._grow_to_fit()
        self._sensor_tester.update_values(mag)

    def _grow_to_fit(self) -> None:
        """Grow the dialog to its preferred size (never shrink, never past the
        screen). Called when a panel is added after construction so the added
        content isn't squeezed below its minimum. Grows width as well as height
        because the sensor tester is added as a new right-hand column."""
        self.layout().activate()   # refresh the size hint after the insert
        hint = self.sizeHint()
        target_w, target_h = hint.width(), hint.height()
        screen = self.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            target_w = min(target_w, avail.width())
            target_h = min(target_h, avail.height())
        self.resize(max(self.width(), target_w), max(self.height(), target_h))

    def _rezero_sensors(self) -> None:
        """Re-zero the node's magnet baseline (sensor tester Re-zero button)."""
        self._gateway.send(self._mac, "rebaseline")

    def _configure_sensors(self, threshold_ut: float) -> None:
        """Push the tester's µT threshold to the node so its own touch detection
        (the ``act`` set the activities and monitor consume) flips a sensor active
        at exactly this µT."""
        if threshold_ut <= 0:
            return
        self._gateway.send(self._mac, "configure", repeat=2,
                           act_threshold_ut=threshold_ut)

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

        Uses the shared ``units.kpa_to_pct`` (mirror of firmware
        ``units::kpaToPct``) against the limits the user set in the config rows,
        so the readout reflects the configured max even before (or if) the
        node's ``set_max_pressure`` takes effect."""
        cfg = self._chamber_cfgs.get(chamber, {})
        return kpa_to_pct(kpa,
                          float(cfg.get("min_pressure", 0.0)),
                          float(cfg.get("max_pressure", 8.0)))

    def _update_valves(self, chamber: int, inflate_open: int, deflate_open: int) -> None:
        """Reflect the node's ACTUAL valve outputs on the per-chamber buttons.

        Called in the main thread via Signal from each status broadcast. This is
        display only — it never sends a command — so a valve shows open whoever
        opened it (a manual toggle, an inflate/deflate, the closed-loop control,
        or the firmware's own dead-man closing it again)."""
        self._set_valve_button((chamber, 0), bool(inflate_open))
        self._set_valve_button((chamber, 1), bool(deflate_open))

    def _set_valve_button(self, key: tuple[int, int], is_open: bool) -> None:
        """Set one valve button's stored display state, label and colour.

        Display only: stores the open flag in ``_valve_states`` (the actual state,
        for the readout) and never touches ``_valve_intent`` (the user's hold) or
        sends a command."""
        entry = self._valve_states.get(key)
        if entry is None:
            return
        _, btn = entry
        self._valve_states[key] = (is_open, btn)
        side_name = "Inflate" if key[1] == 0 else "Deflate"
        btn.setText(f"{side_name} Valve: {'OPEN  ' if is_open else 'CLOSED'}")
        btn.setStyleSheet(self._VALVE_OPEN_STYLE if is_open else self._VALVE_STYLE)

    def _on_closed(self) -> None:
        self._active = False
        # Stop re-asserting manual overrides; the firmware dead-man then closes any
        # held valve/pump within ~5 s of the last keepalive.
        self._manual_keepalive.stop()
        # A continuous run ignores the firmware dead-man, so it would keep going
        # after the dialog closes — always stop it on the way out.
        self._stop_run()
        # If we left the node latched off via STOP ALL, re-arm it so the rest of
        # the app can drive it again (everything is already off, so this is safe).
        self._arm()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _angle_saver(self, ring: int):
        """A ``(angle) -> None`` callback that persists ``ring``'s mounting angle,
        or None when the host provided no save hook (hides the Save button)."""
        if self._on_save_angle is None:
            return None

        def _save(angle: float, r: int = ring) -> None:
            self._led_angles[r] = float(angle)
            self._on_save_angle(r, float(angle))

        return _save

    def _send_led(self, ring: int | None, index: int | None,
                  colors: list[str] | None, pattern: str = "solid",
                  fade_ms: int | None = None, angle: float | None = None) -> None:
        """Forward an LED change to the node. ``colors`` None => turn off;
        ``index`` set => a single pixel (colours[0]); one colour => whole ring
        (``set_led``); 2+ colours => an arc split (``set_led_halves``, one comet
        per colour when ``pattern`` is "comet"). ``ring`` selects one of a
        multi-ring node's rings (node_multiplexed); None addresses the node's
        single ring (and is omitted from the frame for node_direct back-compat).
        ``fade_ms`` is the cross-fade time; ``angle`` (0-360°) rotates the split."""
        payload: dict = {} if ring is None else {"ring": ring}
        if fade_ms is not None:
            payload["fade_ms"] = int(fade_ms)
        if angle is not None:
            payload["angle"] = float(angle)
        if colors is None:
            self._gateway.send(self._mac, "set_led", pattern="off", **payload)
        elif index is not None:
            self._gateway.send(self._mac, "set_led", color=colors[0],
                               index=index, pattern=pattern, **payload)
        elif len(colors) == 1:
            self._gateway.send(self._mac, "set_led", color=colors[0],
                               pattern=pattern, **payload)
        else:
            self._gateway.send(self._mac, "set_led_halves", colors=colors,
                               pattern=pattern, **payload)

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
        """Inflate several chambers at once.

        When ``slots`` is the whole node (the common case — node_direct is one
        skin of 3 chambers), send a SINGLE broadcast frame (``chamber=-1``) the
        node fans out to every chamber, so all inflate valves open together: the
        shared pump then runs continuously while ANY valve is open and each
        chamber closes its own valve as it reaches its max (see the node's
        ``recalcPumps``). One frame also can't be dropped per-chamber, so we never
        get "only one inflating" from a lost ESP-NOW frame (no command-level
        retry). The fill is closed-loop to each chamber's configured max (a shared
        frame can't carry per-chamber calibrated fill windows — fine for a bench
        "fill all"). A partial selection still falls back to per-slot."""
        if set(slots) == set(self._chamber_cfgs):
            self._broadcast_actuate("inflate", slots)
        else:
            for slot in slots:
                self._inflate_slot(slot)

    def _deflate_slots(self, slots: list[int]) -> None:
        """Deflate several chambers — one broadcast frame for the whole node
        (see :meth:`_inflate_slots`), else per-slot. Deflate has no calibrated
        time window, so the whole-node case can always go single-frame."""
        if set(slots) == set(self._chamber_cfgs):
            self._broadcast_actuate("deflate", slots)
        else:
            for slot in slots:
                self._deflate_slot(slot)

    def _broadcast_actuate(self, command: str, slots: list[int]) -> None:
        """Re-push every chamber's configured limits, then send ONE
        ``chamber=-1`` inflate/deflate (delta=100 → toward each chamber's
        configured max/min) that the node fans out to all chambers. A single
        frame can't be partially dropped, fixing the "only one chamber actuates"
        burst-loss."""
        self._arm()
        for slot in slots:
            self._push_limits(slot)
        self._gateway.send(self._mac, command, chamber=-1, delta=100)

    def _toggle_valve(self, chamber: int, side: int, btn: QPushButton) -> None:
        """Toggle the manual valve override.

        Flips the user's *intent* (held vs released), which the keepalive then
        re-asserts so the firmware dead-man doesn't close a held valve after
        ~5 s. The button's authoritative open/closed display comes from the
        node's status (see :meth:`_update_valves`); we set it optimistically here
        so the click feels immediate, and the next status confirms or corrects."""
        key = (chamber, side)
        want_open = not self._valve_intent.get(key, False)
        self._valve_intent[key] = want_open

        # Send command to firmware (re-arm first if STOP ALL latched the node)
        if want_open:
            self._arm()
        self._gateway.send(self._mac, "valve_manual", chamber=chamber,
                          side=side, open=1 if want_open else 0)
        self._set_valve_button(key, want_open)   # optimistic; status confirms
        self._refresh_manual_keepalive()

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
        # A manual pump shares the firmware dead-man (~5 s); keep it alive while on.
        self._refresh_manual_keepalive()

    def _chamber_dir(self, slot: int, direction: int) -> None:
        """Per-chamber Inflate (``direction`` 0) / Deflate (1) button.

        If this chamber+direction is the active continuous run, stop it. Else,
        with 'Ignore max pressure' checked, start a continuous run of just this
        chamber (opens its valve + drives the pump, ignoring the pressure cap,
        until stopped); otherwise do a one-shot fill toward the configured
        max/min."""
        if self._run == (direction, slot):
            self._stop_run()
        elif self.cont_cb.isChecked():
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

    def _refresh_manual_keepalive(self) -> None:
        """Run the manual dead-man keepalive only while a valve/pump is held on."""
        any_on = (any(self._valve_intent.values())
                  or any(on for on, _ in self._pump_states.values()))
        if any_on and not self._manual_keepalive.isActive():
            self._manual_keepalive.start()
        elif not any_on and self._manual_keepalive.isActive():
            self._manual_keepalive.stop()

    def _send_manual_keepalive(self) -> None:
        """Re-assert every held manual valve/pump to refresh the firmware dead-man.

        ``valve_manual`` / ``pump_manual`` only reset the node's per-override
        timeout (they're idempotent), so this keeps the held overrides open
        without re-driving anything. Stops itself once nothing is held."""
        sent = False
        for (chamber, side), want_open in self._valve_intent.items():
            if want_open:
                self._gateway.send(self._mac, "valve_manual", chamber=chamber,
                                   side=side, open=1)
                sent = True
        for pump, (on, _) in self._pump_states.items():
            if on:
                self._gateway.send(self._mac, "pump_manual", pump=pump, on=1)
                sent = True
        if not sent:
            self._manual_keepalive.stop()

    def _refresh_run_buttons(self) -> None:
        run = self._run
        self.run_inf_btn.setText(
            f"Run Inflate ∞: {'ON ' if run == (0, -1) else 'OFF'}")
        self.run_def_btn.setText(
            f"Run Deflate ∞: {'ON ' if run == (1, -1) else 'OFF'}")
        for (slot, direction), btn in self._chamber_btns.items():
            default = "Inflate" if direction == 0 else "Deflate"
            btn.setText("⏹ Stop" if run == (direction, slot) else default)

    def _reset_manual_ui(self) -> None:
        """Reset the manual valve/pump toggle buttons + intent to OFF/CLOSED.

        Releases every held valve override and stops the keepalive; the next
        status broadcast then drives the valve buttons from the real state."""
        self._valve_intent.clear()
        for key in self._valve_states:
            self._set_valve_button(key, False)
        for pump, (_, btn) in self._pump_states.items():
            self._pump_states[pump] = (False, btn)
            pump_name = "Inflate" if pump == 0 else "Deflate"
            btn.setText(f"{pump_name} Pump: OFF")
        self._refresh_manual_keepalive()

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
