"""Shared per-Thymio configuration form (ID, skin node MAC, wireless transport).

One widget, two hosts: the robot panel's quick add/configure dialog and the
per-robot RobotConfigDialog both embed this form, so the Thymio schema
(``thymio_id``/``wireless``/``wireless_via``/``node_id``/``channel``/
``thymio_addr``) is defined in exactly one place. Layout and help strings live
in ``ui/thymio_config_form.ui``. The robot's skin nodes are NOT configured
here — they are their own entries (robot panel "+ Node" → ``cfg["nodes"]``).

The Discover button sniffs the 802.15.4 channel through the gateway's C6 and
fills the address field; the gateway is fetched lazily via the injected
``gateway_provider`` so the form works both where a live robot exists
(RobotConfigDialog) and where only the panel's gateway does (RobotPanel).
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtWidgets import QMessageBox, QWidget

from src.gui.ui_thymio_config_form import Ui_ThymioConfigForm

_DISCOVER_TITLE = "Discover Thymios"


class ThymioConfigForm(QWidget, Ui_ThymioConfigForm):
    """Form editing one Thymio's config dict (see :meth:`values`).

    Args:
        gateway_provider: Zero-arg callable returning the SoftEdIBO gateway to
            sniff through (or ``None`` when unavailable). Injected so the form
            stays host-agnostic.
        parent: Optional parent widget.
    """

    def __init__(self, gateway_provider: Callable[[], Any],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)

        # Combo items carry the settings value as userData, which the .ui
        # format can't express — populate here.
        self.via_combo.addItem("RF dongle", "dongle")
        self.via_combo.addItem("Gateway C6 — no dongle", "gateway")

        self._gateway_provider = gateway_provider

        self.wireless_check.toggled.connect(lambda _=False: self._update_enables())
        self.via_combo.currentIndexChanged.connect(lambda _=0: self._update_enables())
        self.discover_btn.clicked.connect(self._on_discover)
        self._update_enables()

    # ------------------------------------------------------------------
    # Values
    # ------------------------------------------------------------------

    def set_values(self, cfg: dict) -> None:
        """Load the form from a settings.yaml thymio entry (missing keys → defaults)."""
        self.id_edit.setText(cfg.get("thymio_id", ""))
        self.wireless_check.setChecked(bool(cfg.get("wireless", False)))
        via = cfg.get("wireless_via", "dongle")
        self.via_combo.setCurrentIndex(1 if via == "gateway" else 0)
        self.node_id_spin.setValue(int(cfg.get("node_id", 0) or 0))
        self.channel_spin.setValue(int(cfg.get("channel", 25)))
        self.addr_edit.setText(cfg.get("thymio_addr", ""))
        self.impact_spin.setValue(float(cfg.get("impact_threshold", 20.0) or 20.0))
        self._update_enables()

    def values(self) -> dict:
        """The edited config keys, ready to merge into the settings entry."""
        return {
            "thymio_id": self.id_edit.text().strip(),
            "wireless": self.wireless_check.isChecked(),
            "wireless_via": self.via_combo.currentData(),
            "node_id": self.node_id_spin.value(),
            "channel": self.channel_spin.value(),
            "thymio_addr": self.addr_edit.text().strip(),
            "impact_threshold": self.impact_spin.value(),
        }

    def thymio_id(self) -> str:
        """The (stripped) Thymio ID currently typed."""
        return self.id_edit.text().strip()

    def set_impact_threshold(self, value: float) -> None:
        """Write a calibrated impact threshold into the form (from Test Thymio), so
        saving the config persists it."""
        self.impact_spin.setValue(float(value))

    # ------------------------------------------------------------------
    # Behaviour
    # ------------------------------------------------------------------

    def _update_enables(self) -> None:
        on = self.wireless_check.isChecked()
        gw = on and self.via_combo.currentData() == "gateway"
        self.via_combo.setEnabled(on)
        self.node_id_spin.setEnabled(on and not gw)
        self.channel_spin.setEnabled(gw)
        self.addr_edit.setEnabled(gw)
        self.discover_btn.setEnabled(gw)
        self.impact_spin.setEnabled(gw)

    def _on_discover(self) -> None:
        """Open the guided dongle-free scan and take the picked address.

        The dialog runs a long sniff through the gateway's C6 while the user
        powers the Thymios on (they announce themselves at boot), listing the
        addresses live — see :class:`ThymioDiscoverDialog`.
        """
        gateway = self._gateway_provider()
        if gateway is None or not getattr(gateway, "is_connected", False):
            QMessageBox.warning(self, _DISCOVER_TITLE,
                                "The gateway isn't connected — connect it first.")
            return
        from PySide6.QtWidgets import QDialog
        from src.gui.thymio_discover_dialog import ThymioDiscoverDialog

        dlg = ThymioDiscoverDialog(gateway, self.channel_spin.value(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.address():
            self.addr_edit.setText(dlg.address())
