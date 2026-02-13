"""Participant model for study sessions."""

from typing import Any


class Participant:
    """Represents a participant in a study session."""

    def __init__(self, participant_id: str, alias: str, age: int | None = None):
        """Initialize a participant.

        Args:
            participant_id: Unique anonymous identifier.
            alias: Display name/alias for the participant.
            age: Optional age of the participant.
        """
        self.participant_id = participant_id
        self.alias = alias
        self.age = age

    def to_dict(self) -> dict[str, Any]:
        """Serialize participant data."""
        return {
            "participant_id": self.participant_id,
            "alias": self.alias,
            "age": self.age,
        }

    def __repr__(self) -> str:
        return f"Participant(id={self.participant_id!r}, alias={self.alias!r})"
