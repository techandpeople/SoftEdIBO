"""Session trash — soft-delete domain logic, Qt-free and testable.

Wraps the database's trash tables together with the on-disk sensor recordings so
a deleted session (DB record + events + assignments + participant links + its
recording) moves to a recoverable trash as one unit. Both the data panel (which
trashes sessions) and the trash dialog (which lists / restores / purges them)
call into here, so the recording-file bookkeeping lives in exactly one place.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.data.database import Database
from src.data.models import TrashedSession

logger = logging.getLogger(__name__)


class SessionTrash:
    """Recoverable soft-delete for sessions and their recordings.

    Args:
        db: Open database whose trash tables back the soft-delete.
        recordings_dir: Folder holding ``<session_id>.jsonl`` recordings; trashed
            recordings are tucked into its hidden ``.trash`` subfolder.
    """

    def __init__(self, db: Database, recordings_dir: Path):
        self._db = db
        self._recordings_dir = Path(recordings_dir)

    def trash(self, session_id: str) -> bool:
        """Move a session (and its recording) to the trash. False if unknown."""
        if not self._db.trash_session(session_id):
            return False
        self._move(self._recording_path(session_id),
                   self._trashed_recording_path(session_id))
        return True

    def list(self) -> list[TrashedSession]:
        """Return trashed sessions, most-recently-trashed first."""
        return self._db.list_trashed_sessions()

    def restore(self, session_id: str) -> bool:
        """Restore a trashed session (and its recording). False if not in trash."""
        if not self._db.restore_session(session_id):
            return False
        self._move(self._trashed_recording_path(session_id),
                   self._recording_path(session_id))
        return True

    def purge(self, session_id: str) -> None:
        """Permanently delete one trashed session and its recording."""
        self._db.purge_session(session_id)
        self._delete_recording(session_id)

    def empty(self) -> list[str]:
        """Permanently delete every trashed session; return the purged IDs."""
        ids = self._db.empty_trash()
        for session_id in ids:
            self._delete_recording(session_id)
        return ids

    # ------------------------------------------------------------------
    # Recording files
    # ------------------------------------------------------------------

    def _recording_path(self, session_id: str) -> Path:
        return self._recordings_dir / f"{session_id}.jsonl"

    def _trashed_recording_path(self, session_id: str) -> Path:
        return self._recordings_dir / ".trash" / f"{session_id}.jsonl"

    @staticmethod
    def _move(src: Path, dst: Path) -> None:
        if not src.exists():
            return
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
        except OSError:
            logger.warning("Could not move recording %s -> %s", src, dst, exc_info=True)

    def _delete_recording(self, session_id: str) -> None:
        for path in (self._trashed_recording_path(session_id),
                     self._recording_path(session_id)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not delete recording %s", path, exc_info=True)
