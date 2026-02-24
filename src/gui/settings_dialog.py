"""Application settings dialog."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QFileDialog

from src.config.settings import Settings
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
