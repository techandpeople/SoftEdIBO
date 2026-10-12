"""PatternsView - patterns pooled over the selected sessions."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from src.data.interaction_dynamics import InteractionDynamics
from src.data.session_data import SessionData
from src.gui.data.table_fill import fill_table
from src.gui.ui_patterns_view import Ui_PatternsView
from src.ml import interaction_clusters as clusters


class PatternsView(QWidget, Ui_PatternsView):
    """Shows turn-taking, behaviour transitions, reactions and touch styles
    of a set of sessions."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)

    def show_sessions(self, sessions: list[SessionData], source_key: str) -> None:
        """Compute and show the pooled tables for ``sessions``."""
        count = len(sessions)
        self.scope_label.setText(
            f"Pooled over {count} selected session(s). Select several to "
            "compare conditions." if count
            else "Select one or more sessions.")
        dynamics = InteractionDynamics(sessions, source_key)
        fill_table(self.turns_table, dynamics.turns(), hidden=())
        fill_table(self.behaviours_table, dynamics.behaviours(), hidden=())
        fill_table(self.reactions_table, dynamics.reactions(), hidden=())
        try:
            result = clusters.cluster_episodes(dynamics.episodes())
            note = result.note
        except clusters.MLNotInstalled:
            result = clusters.ClusterResult(
                clusters.Table("Styles", clusters.CLUSTER_COLUMNS, []))
            note = ("Styles need the optional machine-learning packages "
                    "(scikit-learn), which are not installed.")
        self.styles_note.setText(note or "-")
        fill_table(self.styles_table, result.summary, hidden=())
