"""Application settings manager backed by config/settings.yaml."""

from pathlib import Path

import yaml


class Settings:
    """Loads and persists application configuration from settings.yaml.

    All file paths stored in the YAML (e.g. ``database.path``) are resolved
    relative to the project root, not the current working directory.
    """

    ROOT: Path = Path(__file__).parents[2]
    DEFAULT_PATH: Path = ROOT / "config" / "settings.yaml"

    def __init__(self, path: Path | None = None):
        self._path = path or self.DEFAULT_PATH
        self._data: dict = {}
        self.load()

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Reload configuration from disk."""
        with open(self._path, encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}

    def save(self) -> None:
        """Persist current configuration to disk."""
        with open(self._path, "w", encoding="utf-8") as f:
            yaml.dump(
                self._data, f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def data(self) -> dict:
        """Raw settings dictionary (mutable)."""
        return self._data

    @property
    def db_cfg(self) -> dict:
        """Raw database configuration block."""
        return self._data.get("database", {})

    @property
    def db_path(self) -> Path:
        """Resolved SQLite database file path (relative to project root)."""
        rel = self._data.get("database", {}).get("path", "data/softedibo.db")
        return self.ROOT / rel

    @property
    def gateway_port(self) -> str:
        """Serial port for the ESP-NOW gateway."""
        return self._data.get("gateway", {}).get("serial_port", "/dev/ttyUSB0")

    @property
    def gateway_baud(self) -> int:
        """Baud rate for the ESP-NOW gateway."""
        return self._data.get("gateway", {}).get("baud_rate", 115200)
