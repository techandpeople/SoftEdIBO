"""Dialog for adding or editing a participant."""

from PySide6.QtWidgets import QMessageBox, QWidget

from src.data.database import Database
from src.data.models import ParticipantRecord
from src.gui.base_dialog import BaseDialog
from src.gui.ui_participant_dialog import Ui_ParticipantDialog


class ParticipantDialog(BaseDialog, Ui_ParticipantDialog):
    """Dialog to create or edit a participant.

    Args:
        participant_id: The ID to show (auto-generated for new participants).
        db: Database instance used to persist the record on save.
        record: Existing record to edit, or ``None`` for a new participant.
        parent: Optional parent widget.
    """

    def __init__(
        self,
        participant_id: str,
        db: Database,
        record: ParticipantRecord | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._db = db
        self._participant_id = participant_id

        self.setupUi(self)
        self.setWindowTitle("Edit Participant" if record else "Add Participant")

        self.id_edit.setText(participant_id)

        if record is not None:
            self.alias_edit.setText(record.alias)
            self.age_spin.setValue(record.age if record.age is not None else 0)

        self.save_btn.clicked.connect(self._on_save)
        self.apply_btn.clicked.connect(self._on_apply)
        self.cancel_btn.clicked.connect(self.reject)

    def _commit(self) -> bool:
        """Validate and persist the participant, without closing. Returns True
        on success. Shared by Save (which then closes) and Apply (which stays
        open so the user can keep editing)."""
        alias = self.alias_edit.text().strip()
        if not alias:
            QMessageBox.warning(self, "Validation", "Alias cannot be empty.")
            return False
        age_value = self.age_spin.value()
        age = age_value if age_value > 0 else None
        self._db.save_participant(
            ParticipantRecord(
                participant_id=self._participant_id,
                alias=alias,
                age=age,
            )
        )
        # Now persisted - a further Apply re-saves the same record (upsert).
        self.setWindowTitle("Edit Participant")
        return True

    def _on_apply(self) -> None:
        self._commit()

    def _on_save(self) -> None:
        if self._commit():
            self.accept()
