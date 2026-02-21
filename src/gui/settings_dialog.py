"""Application settings dialog."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QFileDialog

from src.config.settings import Settings
from src.gui.ui_settings_dialog import Ui_SettingsDialog


class SettingsDialog(QDialog, Ui_SettingsDialog):
    """Dialog for editing application settings stored in settings.yaml.

    Signals:
        settings_saved: Emitted after settings are written to disk.
            Only gateway/Thymio changes apply immediately; database changes
            require a restart.
    """

    settings_saved = Signal()

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self._settings = settings
        self._load()

        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        self.browse_btn.clicked.connect(self._browse_db)
        self.button_box.accepted.connect(self._on_save)
        self.button_box.rejected.connect(self.reject)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Populate fields from current settings."""
        # Gateway
        self.serial_port_edit.setText(self._settings.gateway_port)
        baud = str(self._settings.gateway_baud)
        idx = self.baud_rate_combo.findText(baud)
        if idx >= 0:
            self.baud_rate_combo.setCurrentIndex(idx)
        else:
            self.baud_rate_combo.addItem(baud)
            self.baud_rate_combo.setCurrentIndex(self.baud_rate_combo.count() - 1)

        # Database
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

        # Thymio
        thymio = self._settings.data.get("thymio", {})
        self.thymio_host_edit.setText(thymio.get("host", "localhost"))
        self.thymio_port_spin.setValue(int(thymio.get("port", 8596)))

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

        # Gateway
        d.setdefault("gateway", {})
        d["gateway"]["serial_port"] = self.serial_port_edit.text().strip()
        d["gateway"]["baud_rate"] = int(self.baud_rate_combo.currentText())

        # Database
        d.setdefault("database", {})
        backend = "sqlite" if self.backend_combo.currentIndex() == 0 else "postgresql"
        d["database"]["backend"] = backend
        d["database"]["path"] = self.db_path_edit.text().strip()
        d["database"]["host"] = self.pg_host_edit.text().strip()
        d["database"]["port"] = self.pg_port_spin.value()
        d["database"]["name"] = self.pg_name_edit.text().strip()
        d["database"]["user"] = self.pg_user_edit.text().strip()
        d["database"]["password"] = self.pg_password_edit.text()

        # Thymio
        d.setdefault("thymio", {})
        d["thymio"]["host"] = self.thymio_host_edit.text().strip()
        d["thymio"]["port"] = self.thymio_port_spin.value()

        self._settings.save()
        self.settings_saved.emit()
        self.accept()
