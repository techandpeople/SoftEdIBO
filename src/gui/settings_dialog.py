"""Application settings dialog."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
)

from src.config.settings import Settings
from src.hardware.serial_ports import list_esp32_ports
from src.gui.ui_settings_dialog import Ui_SettingsDialog


class SettingsDialog(QDialog, Ui_SettingsDialog):
    """Dialog for editing application settings stored in settings.yaml.

    Signals:
        settings_saved: Emitted after settings are written to disk.
            Database changes require a restart.
    """

    settings_saved = Signal()

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self._settings = settings
        self._build_gateway_section()
        self._load()

        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        self.browse_btn.clicked.connect(self._browse_db)
        self.button_box.accepted.connect(self._on_save)
        self.button_box.rejected.connect(self.reject)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_gateway_section(self) -> None:
        """Add a Gateway group (USB port, baud, auto-connect) before the buttons.

        Built programmatically so we don't have to regenerate the .ui — same
        approach as the Tools menu actions added in MainWindow.
        """
        group = QGroupBox("ESP-NOW Gateway", self)
        form = QFormLayout(group)

        self.gw_port_combo = QComboBox()
        self.gw_port_combo.setEditable(True)
        for p in list_esp32_ports():
            self.gw_port_combo.addItem(p.device, p.device)
        form.addRow("USB port", self.gw_port_combo)

        self.gw_baud_combo = QComboBox()
        self.gw_baud_combo.addItems(["115200", "921600", "230400", "57600"])
        form.addRow("Baud rate", self.gw_baud_combo)

        self.gw_auto_check = QCheckBox("Connect automatically on startup")
        form.addRow("", self.gw_auto_check)

        # Insert just above the Save/Cancel button box.
        self.verticalLayout.insertWidget(self.verticalLayout.count() - 1, group)

    def _load(self) -> None:
        """Populate fields from current settings."""
        # Gateway
        port = self._settings.gateway_port
        idx = self.gw_port_combo.findData(port)
        if idx >= 0:
            self.gw_port_combo.setCurrentIndex(idx)
        else:
            self.gw_port_combo.setCurrentText(port)
        baud_idx = self.gw_baud_combo.findText(str(self._settings.gateway_baud))
        if baud_idx >= 0:
            self.gw_baud_combo.setCurrentIndex(baud_idx)
        self.gw_auto_check.setChecked(self._settings.gateway_auto_connect)

        db = self._settings.db_cfg
        backend = db.get("backend", "sqlite").lower()
        self.backend_combo.setCurrentIndex(0 if backend == "sqlite" else 1)
        self.db_path_edit.setText(db.get("path", "data/softedibo.db"))
        self.pg_host_edit.setText(db.get("host", "localhost"))
        self.pg_port_spin.setValue(int(db.get("port", 5432)))
        self.pg_name_edit.setText(db.get("name", "softedibo"))
        self.pg_user_edit.setText(db.get("user", ""))
        self.pg_password_edit.setText(db.get("password", ""))
        self._on_backend_changed(self.backend_combo.currentIndex())

    def _on_backend_changed(self, index: int) -> None:
        is_sqlite = index == 0
        self.sqlite_group.setEnabled(is_sqlite)
        self.pg_group.setEnabled(not is_sqlite)

    def _browse_db(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Select Database File",
            str(self._settings.ROOT / self.db_path_edit.text()),
            "SQLite databases (*.db);;All files (*)",
        )
        if path:
            self.db_path_edit.setText(path)

    def _on_save(self) -> None:
        d = self._settings.data

        d.setdefault("gateway", {})
        port = self.gw_port_combo.currentData() or self.gw_port_combo.currentText()
        d["gateway"]["serial_port"] = port.strip()
        d["gateway"]["baud_rate"] = int(self.gw_baud_combo.currentText())
        d["gateway"]["auto_connect"] = self.gw_auto_check.isChecked()

        d.setdefault("database", {})
        backend = "sqlite" if self.backend_combo.currentIndex() == 0 else "postgresql"
        d["database"]["backend"] = backend
        d["database"]["path"] = self.db_path_edit.text().strip()
        d["database"]["host"] = self.pg_host_edit.text().strip()
        d["database"]["port"] = self.pg_port_spin.value()
        d["database"]["name"] = self.pg_name_edit.text().strip()
        d["database"]["user"] = self.pg_user_edit.text().strip()
        d["database"]["password"] = self.pg_password_edit.text()

        self._settings.save()
        self.settings_saved.emit()
        self.accept()
