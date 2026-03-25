"""OTA self-updater for SoftEdIBO.

Supports two environments:
- **Linux AppImage**: replaces the AppImage atomically, restarts via ``os.execv``.
- **Windows frozen (PyInstaller)**: downloads a zip, launches a PowerShell script
  that extracts it after the app exits, then restarts ``SoftEdIBO.exe``.

Only active when running as a frozen binary and ``GITHUB_REPO`` is configured.
Uses ``QNetworkAccessManager`` for fully async HTTP — no threads, no blocking.

Typical flow
------------
1. ``AppUpdater.check()`` is called a few seconds after startup.
2. GitHub API returns the latest release tag.
3. If newer, ``update_available(version, url)`` is emitted.
4. The user clicks "Update" => ``AppUpdater.download(url)`` starts.
5. ``download_progress`` updates the UI.
6. ``download_done`` fires => caller applies the update and restarts.
"""

import json
import logging
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import IO

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from src._version import GITHUB_REPO, __build_time__, __commit__, __version__

logger = logging.getLogger(__name__)

_API_LATEST  = "https://api.github.com/repos/{repo}/releases/latest"
_API_NIGHTLY = "https://api.github.com/repos/{repo}/releases/tags/nightly"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_appimage() -> bool:
    """Return True when running as a Linux AppImage."""
    return bool(os.environ.get("APPIMAGE"))


def _is_frozen_windows() -> bool:
    """Return True when running as a frozen PyInstaller app on Windows."""
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def _can_update() -> bool:
    """Return True if OTA updates are supported in the current environment."""
    return is_appimage() or _is_frozen_windows()


def _is_newer(remote: str, local: str) -> bool:
    """Return True if *remote* semver tag is strictly newer than *local*."""
    def parse(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())
    try:
        return parse(remote) > parse(local)
    except ValueError:
        return False


def _appimage_path() -> Path | None:
    p = os.environ.get("APPIMAGE")
    return Path(p) if p else None


# ---------------------------------------------------------------------------
# Updater
# ---------------------------------------------------------------------------

class AppUpdater(QObject):
    """Async OTA updater using QNetworkAccessManager (no threads)."""

    #: Emitted when a newer version is available. Args: (version_tag, download_url)
    update_available = Signal(str, str)

    #: Emitted during download. Args: (bytes_received, bytes_total)
    download_progress = Signal(int, int)

    #: Emitted when the download is complete. Arg: Path to the downloaded file.
    download_done = Signal(Path)

    #: Emitted on network or I/O errors.
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self._download_reply: QNetworkReply | None = None
        self._download_file: IO[bytes] | None = None
        self._tmp_path: Path | None = None

    # ------------------------------------------------------------------
    # Version check
    # ------------------------------------------------------------------

    def check(self) -> None:
        """Async version check. Safe to call at startup — returns immediately.

        - nightly => checks the ``nightly`` release, compares build timestamps.
        - stable  => checks the latest stable release, compares semver.
        - dev     => never updated.
        """
        if not _can_update() or not GITHUB_REPO or __version__ == "dev":
            logger.debug("Update check skipped (frozen=%s, repo=%s, ver=%s)",
                         getattr(sys, "frozen", False), GITHUB_REPO, __version__)
            return

        if __version__ == "nightly":
            url = _API_NIGHTLY.format(repo=GITHUB_REPO)
        else:
            url = _API_LATEST.format(repo=GITHUB_REPO)
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"Accept", b"application/vnd.github.v3+json")
        request.setRawHeader(b"User-Agent", b"SoftEdIBO-Updater")

        reply = self._nam.get(request)
        reply.finished.connect(lambda: self._on_check_finished(reply))

    def _on_check_finished(self, reply: QNetworkReply) -> None:
        reply.deleteLater()
        if reply.error() != reply.NetworkError.NoError:
            logger.debug("Update check failed: %s", reply.errorString())
            return

        try:
            data = json.loads(reply.readAll().data())
            tag = data["tag_name"]
            assets = data.get("assets", [])

            if _is_frozen_windows():
                asset = next(
                    (a for a in assets
                     if a["name"].endswith(".zip") and "windows" in a["name"].lower()),
                    None,
                )
            else:
                asset = next(
                    (a for a in assets if a["name"].endswith(".AppImage")),
                    None,
                )
        except (KeyError, ValueError, json.JSONDecodeError):
            return

        if not asset:
            return

        download_url = asset["browser_download_url"]

        if __version__ == "nightly":
            # SHA is embedded in the release body as <!-- commit: <sha> -->
            # target_commitish is not updated when overwriting a rolling release.
            body = data.get("body", "")
            m = re.search(r"<!--\s*commit:\s*([0-9a-f]{40})\s*-->", body)
            remote_sha = m.group(1) if m else ""
            if remote_sha and __commit__ and remote_sha != __commit__:
                self.update_available.emit(tag, download_url)
        else:
            # Stable: only notify if the remote semver tag is strictly newer.
            if _is_newer(tag, __version__):
                self.update_available.emit(tag, download_url)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download(self, url: str) -> None:
        """Start downloading the update. Streams to disk — no big RAM spike."""
        if _is_frozen_windows():
            self._tmp_path = Path(sys.executable).parent / "SoftEdIBO-update.zip"
        else:
            appimage = _appimage_path()
            if not appimage:
                return
            # Try same directory first (enables atomic rename).
            # Fall back to /tmp if the directory is not writable.
            try:
                fd, tmp = tempfile.mkstemp(
                    suffix=".AppImage",
                    prefix=".softedibo-update-",
                    dir=str(appimage.parent),
                )
            except OSError:
                logger.warning("Cannot write to %s, falling back to /tmp", appimage.parent)
                fd, tmp = tempfile.mkstemp(
                    suffix=".AppImage",
                    prefix=".softedibo-update-",
                )
            os.close(fd)
            self._tmp_path = Path(tmp)
            logger.info("Downloading update to %s", self._tmp_path)

        self._download_file = open(self._tmp_path, "wb")

        request = QNetworkRequest(QUrl(url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        request.setRawHeader(b"User-Agent", b"SoftEdIBO-Updater")

        self._download_reply = self._nam.get(request)
        self._download_reply.readyRead.connect(self._on_chunk)
        self._download_reply.downloadProgress.connect(
            lambda recv, total: self.download_progress.emit(int(recv), int(total))
        )
        self._download_reply.finished.connect(self._on_download_finished)

    def _on_chunk(self) -> None:
        if self._download_file:
            self._download_file.write(bytes(self._download_reply.readAll()))

    def _on_download_finished(self) -> None:
        reply = self._download_reply
        self._download_reply = None

        if self._download_file:
            self._download_file.close()
            self._download_file = None

        reply.deleteLater()

        if reply.error() != reply.NetworkError.NoError:
            logger.error("Download failed: %s", reply.errorString())
            if self._tmp_path:
                self._tmp_path.unlink(missing_ok=True)
            self.error.emit(reply.errorString())
            return

        logger.info("Download complete: %s (%d bytes)",
                     self._tmp_path, self._tmp_path.stat().st_size)

        if _is_frozen_windows():
            self.download_done.emit(self._tmp_path)
        else:
            # Linux: atomic rename over the running AppImage.
            appimage = _appimage_path()
            logger.info("Applying update: %s -> %s", self._tmp_path, appimage)
            try:
                self._tmp_path.chmod(
                    self._tmp_path.stat().st_mode
                    | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                )
                # Try atomic rename first; falls back to copy+delete
                # if temp is on a different filesystem.
                try:
                    os.rename(self._tmp_path, appimage)
                except OSError:
                    import shutil
                    shutil.move(str(self._tmp_path), str(appimage))
            except OSError as exc:
                logger.error("Failed to apply update: %s", exc)
                self._tmp_path.unlink(missing_ok=True)
                self.error.emit(f"Failed to apply update: {exc}")
                return
            logger.info("Update applied successfully")
            self.download_done.emit(appimage)

    def cancel(self) -> None:
        """Abort an in-progress download."""
        if self._download_reply:
            self._download_reply.abort()
        if self._download_file:
            self._download_file.close()
            self._download_file = None
        if self._tmp_path:
            self._tmp_path.unlink(missing_ok=True)
