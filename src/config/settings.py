"""Application settings manager backed by config/settings.yaml."""

import os
import shutil
import sys
from pathlib import Path

import yaml


class Settings:
    """Loads and persists application configuration from settings.yaml.

    Always:
    - ROOT   = ~/.local/share/SoftEdIBO  — writable user data (DB, config, robots)
    In a frozen (AppImage / PyInstaller) bundle:
    - BUNDLE = sys._MEIPASS  — read-only bundled assets (firmware, default config)
    In development:
    - BUNDLE = project root  — read-only assets from the repo
    """

    BUNDLE: Path = (
        Path(getattr(sys, "_MEIPASS", ""))          # read-only assets inside AppImage
        if getattr(sys, "frozen", False)
        else Path(__file__).parents[2]
    )
    # On Linux, prefer SNAP_REAL_HOME over HOME so that running inside a Snap
    # (e.g. VS Code) still resolves to the real user data directory.
    _real_home: Path = Path(os.environ.get("SNAP_REAL_HOME", Path.home()))
    ROOT: Path = (
        Path(os.environ.get("APPDATA", _real_home)) / "SoftEdIBO"
        if sys.platform == "win32"
        else _real_home / ".local" / "share" / "SoftEdIBO"
    )
    # Bundled default (read-only); user copy is at ROOT/config/settings.yaml
    _DEFAULT_BUNDLE: Path = BUNDLE / "config" / "settings.yaml"
    DEFAULT_PATH: Path = ROOT / "config" / "settings.yaml"

    def __init__(self, path: Path | None = None):
        self._path = path or self.DEFAULT_PATH
        self._ensure_user_config()
        self._data: dict = {}
        self.load()

    def _ensure_user_config(self) -> None:
        """On first frozen run, copy the bundled default config to the user dir."""
        if not self._path.exists() and self._DEFAULT_BUNDLE.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self._DEFAULT_BUNDLE, self._path)

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
        default = "COM3" if sys.platform == "win32" else "/dev/ttyUSB0"
        return self._data.get("gateway", {}).get("serial_port", default)

    @property
    def gateway_baud(self) -> int:
        """Baud rate for the ESP-NOW gateway."""
        return self._data.get("gateway", {}).get("baud_rate", 115200)
