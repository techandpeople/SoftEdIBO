"""Pulse every skin LED ring while a calibration sweep is running.

A shared, gateway-level visual cue: whenever any calibration (fill, duty,
deflate or touch coupling) is in progress, every configured actuator node
softly pulses its LED ring so operators can see at a glance that the rig is
busy driving pumps and should not be touched. Best-effort — a dropped or
offline node just does not light — and idempotent, so overlapping ``on``
calls (e.g. a queued "Calibrate All" batch) keep the rings lit until the
matching ``off``.

Sends raw ``set_led`` frames through the gateway, matching how the
calibration dialogs already drive nodes (they hold a ``Gateway`` + MACs, not
``ESP32Controller`` objects).
"""
from __future__ import annotations

import logging
from typing import Any

from src.hardware.fill_calibration import iter_actuator_chambers

logger = logging.getLogger(__name__)

# Cyan — deliberately distinct from the purple/yellow behaviour phase colours
# so a pulsing ring reads as "calibrating", not as a healing state.
CALIBRATING_COLOR = "#00C8FF"
# One fade in/out breathing cycle; "pulse" is a smooth triangle (blink w/ fade).
PULSE_PERIOD_MS = 1200
# Cross-fade when turning the pulse on and off, so it eases in and out.
FADE_MS = 300


class CalibrationLedIndicator:
    """Pulse all actuator LED rings while a calibration is active.

    ``on``/``off`` are idempotent: the rings start pulsing on the first ``on``
    and turn off on the next ``off``; repeated calls in between are no-ops.
    Call it from a dialog's existing "busy" hook (``_set_buttons_enabled`` /
    ``_set_running``) so it also covers abort and dialog-close automatically.
    """

    def __init__(self, settings: Any, gateway: Any) -> None:
        self._settings = settings
        self._gateway = gateway
        self._on = False

    def on(self) -> None:
        """Start pulsing every actuator ring (no-op if already pulsing)."""
        if self._on:
            return
        self._on = True
        self._send(color=CALIBRATING_COLOR, pattern="pulse",
                   period_ms=PULSE_PERIOD_MS)

    def off(self) -> None:
        """Fade the rings off again (no-op if not currently pulsing)."""
        if not self._on:
            return
        self._on = False
        self._send(color="#000000", pattern="off", period_ms=0)

    def _macs(self) -> list[str]:
        """Unique MACs of configured actuator nodes (the ones with LED rings)."""
        data = getattr(self._settings, "data", None) or {}
        seen: set[str] = set()
        out: list[str] = []
        for ch in iter_actuator_chambers(data):
            mac = ch.get("mac")
            if mac and mac not in seen:
                seen.add(mac)
                out.append(mac)
        return out

    def _send(self, *, color: str, pattern: str, period_ms: int) -> None:
        if self._gateway is None:
            return
        for mac in self._macs():
            try:
                # ``set_led`` is idempotent, so repeat to ride out ESP-NOW drops
                # (a lost frame would otherwise leave that ring dark / lit).
                self._gateway.send(mac, "set_led", repeat=2, color=color,
                                   pattern=pattern, period_ms=period_ms,
                                   fade_ms=FADE_MS)
            except Exception:   # noqa: BLE001 — the indicator is best-effort
                logger.exception("calibration LED %s failed for %s", pattern, mac)
