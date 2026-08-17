"""OrganPanel - compact organ-status display shown beside the skin grid.

Replaces drawing organs on the grid: each organ is just a numbered round dot
(green = good, red = bad, empty outline = absent) plus a single LED dot that
mirrors the hardware 'background light' for the current state::

    Organs
    1   2   3
    *   *   o
    State:
      *        (LED colour)

The activity pushes updates via :meth:`update_state` (thread-safe - it fires
from the gateway thread). In simulation each organ dot is clickable to cycle
none -> good -> bad, feeding the matching parallel resistance into the simulated
organ circuit so the display + activity react exactly as on hardware.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout, QGroupBox, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
)

from src.activities.organ_matching import OrganMatcher
from src.hardware.skin import Skin

# Organ status colours - constant by design.
_GOOD = "#2ecc71"
_BAD = "#e74c3c"
_ABSENT_BG = "transparent"      # absent: no fill, black outline only
_LED_UNKNOWN = "#bdc3c7"        # LED grey before any state arrives
_DOT_PX = 26

_SIM_STATES = ("none", "good", "bad")


class OrganPanel(QGroupBox):
    """Numbered organ dots + an LED state dot for one skin."""

    _msg = Signal(str, str, object)   # state, led_color, verdicts

    def __init__(self, skin: Skin, parent=None) -> None:
        super().__init__("Organs", parent)
        self._skin = skin
        self._organs: list[dict] = list(getattr(skin, "organs", []) or [])
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(4)

        # Numbered organ dots (one column per organ: number on top, dot below).
        dots = QGridLayout()
        dots.setHorizontalSpacing(8)
        self._dots: list[QPushButton] = []
        sim = self._is_simulation()
        self._sim_states: dict[int, str] = {}
        for i, organ in enumerate(self._organs):
            dots.addWidget(QLabel(str(i + 1)), 0, i,
                           alignment=Qt.AlignmentFlag.AlignCenter)
            dot = QPushButton()
            dot.setFixedSize(_DOT_PX, _DOT_PX)
            dot.setToolTip(str(organ.get("id", i + 1)))
            if sim:
                self._sim_states[i] = "none"
                dot.clicked.connect(lambda _=False, idx=i: self._cycle_sim(idx))
            else:
                dot.setEnabled(False)
            self._dots.append(dot)
            dots.addWidget(dot, 1, i, alignment=Qt.AlignmentFlag.AlignCenter)
        outer.addLayout(dots)

        outer.addWidget(QLabel("State:"))
        self._led = QLabel()
        self._led.setFixedSize(_DOT_PX, _DOT_PX)
        outer.addWidget(self._led, alignment=Qt.AlignmentFlag.AlignCenter)

        self._style_dot(self._led, _LED_UNKNOWN)
        for dot in self._dots:
            self._style_dot(dot, _ABSENT_BG)

        self._msg.connect(self._apply, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_state(self, state: str, led_color: str,
                     verdicts: dict[str, str]) -> None:
        """Thread-safe: push the patient's state, LED colour and per-organ
        verdicts (``{organ_id: good|bad|absent}``)."""
        self._msg.emit(str(state), str(led_color), dict(verdicts or {}))

    def _apply(self, state: str, led_color: str, verdicts: dict) -> None:
        # In simulation we ARE the physical organs, so the dots show the
        # ground-truth states we clicked (see _render_sim_dots). The activity's
        # verdicts come from decoding a single parallel resistance, which is
        # ambiguous when organs share resistances - letting it recolour the dots
        # would make clicking one organ light up another. Only the LED/state,
        # which legitimately follows the cure decision, is driven from here.
        if not self._sim_states:
            for i, organ in enumerate(self._organs):
                verdict = verdicts.get(str(organ.get("id"))) or ""
                colour = {"good": _GOOD, "bad": _BAD}.get(verdict, _ABSENT_BG)
                self._style_dot(self._dots[i], colour)
        self._style_dot(self._led, led_color or _LED_UNKNOWN)
        self.setToolTip(f"State: {state}" if state else "")

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    @staticmethod
    def _style_dot(dot: QLabel | QPushButton, bg: str) -> None:
        radius = _DOT_PX // 2
        dot.setStyleSheet(
            f"background: {bg}; border: 2px solid #000; "
            f"border-radius: {radius}px;")

    # ------------------------------------------------------------------
    # Simulation drive
    # ------------------------------------------------------------------

    def _cycle_sim(self, index: int) -> None:
        cur = self._sim_states.get(index, "none")
        self._sim_states[index] = _SIM_STATES[
            (_SIM_STATES.index(cur) + 1) % len(_SIM_STATES)]
        self._render_sim_dots()
        self._push_sim()

    def _render_sim_dots(self) -> None:
        """Colour each dot from its own clicked state - ground truth in sim,
        so a button only ever toggles its own organ."""
        for i in range(len(self._organs)):
            state = self._sim_states.get(i, "none")
            colour = {"good": _GOOD, "bad": _BAD}.get(state, _ABSENT_BG)
            self._style_dot(self._dots[i], colour)

    def _push_sim(self) -> None:
        """Feed the simulated organ circuit with the parallel total of the
        plugged organs (None = cover off / open circuit)."""
        ctrl, slot = self._organ_controller_and_slot()
        setter = getattr(ctrl, "sim_set_organ", None)
        if setter is None:
            return
        plugged: list[float] = []
        for i, organ in enumerate(self._organs):
            state = self._sim_states.get(i, "none")
            if state == "good":
                plugged.append(float(organ.get("good_ohm", 0) or 0))
            elif state == "bad":
                plugged.append(float(organ.get("bad_ohm", 0) or 0))
        plugged = [v for v in plugged if v > 0]
        total = OrganMatcher.parallel_resistance(plugged) if plugged else None
        setter(total, slot)

    def _organ_controller_and_slot(self) -> tuple[Any, int]:
        """Controller + slot carrying this skin's organ circuit."""
        skin = self._skin
        ctrl = getattr(skin, "_ctrl", None)
        cfg = getattr(skin, "organ", None) or {}
        slot = int(cfg.get("slot", 0))
        mac = cfg.get("node_mac")
        if mac and getattr(ctrl, "mac_address", None) != mac:
            tc = getattr(skin, "touch_controller", None)
            if getattr(tc, "mac_address", None) == mac:
                ctrl = tc
        return ctrl, slot

    def _is_simulation(self) -> bool:
        from src.hardware.simulated_controller import SimulatedController
        from src.hardware.simulated_magnet_sensor import SimulatedMagnetSensor
        sim = (SimulatedController, SimulatedMagnetSensor)
        return (isinstance(getattr(self._skin, "_ctrl", None), sim)
                or isinstance(getattr(self._skin, "touch_controller", None), sim))
