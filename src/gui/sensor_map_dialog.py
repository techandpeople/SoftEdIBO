"""Map Pressure Sensors dialog (Tools menu).

Finds which mux input carries each chamber's pressure sensor on a
node_multiplexed and stores the map on the node entry. The measuring itself is
:class:`src.hardware.sensor_mapping.SensorMapSession`; this dialog only picks
the node, feeds the session the node's frames and a timer, and shows / saves
the result. Layout: ``ui/sensor_map_dialog.ui``.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QMessageBox, QTableWidgetItem, QWidget

from src.gui.base_dialog import BaseDialog
from src.gui.ui_sensor_map_dialog import Ui_SensorMapDialog
from src.hardware.sensor_mapping import (
    NO_SENSOR,
    SensorMapSession,
    SensorMatch,
    channel_label,
    iter_multiplexed_nodes,
    set_sensor_channels,
)
from src.robots._robot_builder import multiplexed_configure

_TICK_MS = 50
_COL_CHAMBER, _COL_SAVED, _COL_MEASURED, _COL_RISE, _COL_NOTE = range(5)


class SensorMapDialog(BaseDialog, Ui_SensorMapDialog):
    """Measure and save a multiplexed node's chamber -> sensor input map."""

    # gateway read thread -> GUI thread: (mac, adc counts of every mux input)
    _raw = Signal(str, object)
    # gateway read thread -> GUI thread: (mac, chamber states, inflate valves)
    _status = Signal(str, object, object)
    # Emitted after a map is written to settings, so the app rebuilds its robots.
    saved = Signal()

    def __init__(self, settings: Any, gateway: Any,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self._settings = settings
        self._gateway = gateway
        self._active = True
        self._session: SensorMapSession | None = None
        self._nodes: list[dict[str, Any]] = []

        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.verticalHeader().setVisible(False)

        self.node_combo.currentIndexChanged.connect(self._on_node_changed)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.reset_btn.clicked.connect(self._reset)
        self.save_btn.clicked.connect(self._save)
        self.close_btn.clicked.connect(self.reject)

        self._tick = QTimer(self)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._on_tick)

        queued = Qt.ConnectionType.QueuedConnection
        self._raw.connect(self._on_raw, queued)
        self._status.connect(self._on_status, queued)
        if gateway is not None:
            gateway.on_message(self._on_gateway_message)
        # finished covers accept/reject/Esc too (closeEvent does not).
        self.finished.connect(self._on_finished)

        self._reload_nodes()

    # ------------------------------------------------------------------
    # Node list + table
    # ------------------------------------------------------------------

    def _reload_nodes(self, keep_mac: str = "") -> None:
        self._nodes = iter_multiplexed_nodes(self._settings.data)
        self.node_combo.blockSignals(True)
        self.node_combo.clear()
        for node in self._nodes:
            robots = ", ".join(node["robots"])
            self.node_combo.addItem(
                f"{node['mac']} ({robots})" if robots else node["mac"])
        self.node_combo.blockSignals(False)
        macs = [n["mac"] for n in self._nodes]
        if keep_mac in macs:
            self.node_combo.setCurrentIndex(macs.index(keep_mac))
        self.start_btn.setEnabled(bool(self._nodes))
        self.reset_btn.setEnabled(bool(self._nodes))
        if not self._nodes:
            self.status_label.setText(
                "No node_multiplexed configured. Add one to a robot first.")
        self._fill_table()

    def _node(self) -> dict[str, Any] | None:
        index = self.node_combo.currentIndex()
        return self._nodes[index] if 0 <= index < len(self._nodes) else None

    def _on_node_changed(self, _index: int) -> None:
        self._session = None
        self.save_btn.setEnabled(False)
        self.progress.setValue(0)
        self._fill_table()

    def _fill_table(self) -> None:
        """One row per chamber: the saved input, then this run's measurement."""
        node = self._node()
        slots = range(node["max_slots"]) if node else range(0)
        saved = node["sensor_channels"] if node else []
        results = self._session.results if self._session else {}
        self.result_table.setRowCount(len(slots))
        for row, slot in enumerate(slots):
            saved_text = (channel_label(saved[slot]) if slot < len(saved)
                          else f"default ({channel_label(slot)})")
            match = results.get(slot)
            cells = [str(slot), saved_text, *self._measured_cells(match)]
            for col, text in enumerate(cells):
                self.result_table.setItem(row, col, QTableWidgetItem(text))
        self.result_table.resizeColumnsToContents()

    def _measured_cells(self, match: SensorMatch | None) -> list[str]:
        if match is None:
            return ["", "", ""]
        if match.found:
            shared = self._session.shared_channels() if self._session else {}
            others = [s for s in shared.get(match.channel, []) if s != match.slot]
            note = (f"same input as chamber {others[0]} - the stronger rise keeps it"
                    if others else "")
            return [channel_label(match.channel), f"{match.rise_kpa:.1f}", note]
        note = ("no sensor input followed this chamber" if match.actuated else
                "the node did not open this chamber (stopped, or nothing connected?)")
        return [channel_label(NO_SENSOR), f"{match.rise_kpa:.1f}", note]

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    @staticmethod
    def _configure_payload(node: dict[str, Any], channels: list[int]) -> dict[str, Any]:
        """The node's ``configure`` with ``channels`` as its sensor map."""
        payload = multiplexed_configure({
            "node_type": "node_multiplexed", "max_slots": node["max_slots"],
            "organ_channels": node["organ_channels"],
            "sensor_channels": channels})
        return payload or {}

    def _send(self, command: str, **params: Any) -> bool:
        node = self._node()
        if node is None or self._gateway is None:
            return False
        return bool(self._gateway.send(node["mac"], command, **params))

    def _start(self) -> None:
        node = self._node()
        if node is None:
            return
        if self._gateway is None or not self._gateway.is_connected:
            QMessageBox.warning(self, "Map Pressure Sensors",
                                "Connect the gateway first.")
            return
        # Claim the board for this run: it must be sized (and armed by a
        # configure) before it accepts the pulses. The map it carries does not
        # matter here - the run reads the raw inputs.
        self._send("configure",
                   **self._configure_payload(node, node["sensor_channels"]))
        self._session = SensorMapSession(
            list(range(node["max_slots"])), self._send,
            stop_kpa=self.stop_spin.value(),
            pulse_ms=self.pulse_spin.value())
        self._session.start()
        self._set_running(True)
        self._tick.start()
        self._refresh()

    def _stop(self) -> None:
        if self._session is not None:
            self._session.stop()
        self._tick.stop()
        self._set_running(False)
        self.status_label.setText("Stopped.")

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.node_combo.setEnabled(not running)
        self.stop_spin.setEnabled(not running)
        self.pulse_spin.setEnabled(not running)
        self.reset_btn.setEnabled(not running)
        if running:
            self.save_btn.setEnabled(False)

    def _on_tick(self) -> None:
        session = self._session
        if session is None:
            return
        session.tick()
        self._refresh()
        if not session.running:
            self._tick.stop()
            self._set_running(False)
            self.save_btn.setEnabled(session.finished)

    def _refresh(self) -> None:
        session = self._session
        if session is None:
            return
        self.progress.setValue(session.progress)
        self.status_label.setText(session.status)
        self._fill_table()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _store(self, channels: list[int], message: str) -> None:
        """Write the map to settings, push it to the node, refresh the view."""
        node = self._node()
        if node is None:
            return
        mac = node["mac"]
        set_sensor_channels(self._settings.data, mac, channels)
        self._settings.save()
        self._send("configure", **self._configure_payload(node, channels))
        self.saved.emit()
        self._session = None
        self.save_btn.setEnabled(False)
        self._reload_nodes(keep_mac=mac)
        self.status_label.setText(message)

    def _save(self) -> None:
        session = self._session
        if session is None or not session.finished:
            return
        self._store(session.channels(),
                    "Saved. Now zero the sensors (Skin Config -> Zero Sensors): "
                    "the stored zero offsets belonged to the old inputs.")

    def _reset(self) -> None:
        self._store([], "Map cleared: chamber N reads sensor input N again.")

    # ------------------------------------------------------------------
    # Gateway plumbing (read thread -> Signal -> GUI thread)
    # ------------------------------------------------------------------

    def _on_gateway_message(self, data: dict) -> None:
        if not self._active:
            return
        mac = data.get("source")
        if not isinstance(mac, str):
            return
        kind = data.get("type")
        if kind == "raw" and isinstance(data.get("adc"), list):
            self._raw.emit(mac, data["adc"])
        elif kind == "status" and isinstance(data.get("st"), list):
            vi = data.get("vi")
            self._status.emit(mac, data["st"], vi if isinstance(vi, list) else [])

    def _is_current(self, mac: str) -> bool:
        node = self._node()
        return node is not None and node["mac"] == mac

    def _on_raw(self, mac: str, adc: Any) -> None:
        if self._session is not None and self._is_current(mac):
            self._session.on_raw(adc)

    def _on_status(self, mac: str, states: Any, inflate_open: Any) -> None:
        if self._session is not None and self._is_current(mac):
            self._session.on_status(states, inflate_open)

    def _on_finished(self, _result: int = 0) -> None:
        """Stop the run and detach from the gateway: an exec()'d dialog stays
        alive after closing and would otherwise keep handling every frame."""
        self._shutdown()
        if self._gateway is not None:
            self._gateway.remove_message_callback(self._on_gateway_message)

    def _shutdown(self) -> None:
        self._active = False
        self._tick.stop()
        if self._session is not None:
            self._session.stop()

    def closeEvent(self, ev) -> None:   # noqa: N802 (Qt override)
        self._shutdown()
        super().closeEvent(ev)
