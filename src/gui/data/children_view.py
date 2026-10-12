"""ChildrenView - per-child profiles and between-children alignment."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from src.data.interaction_model import InteractionModel, PAIR_COLUMNS, PROFILE_COLUMNS, Table
from src.gui.data.table_fill import fill_table
from src.gui.ui_children_view import Ui_ChildrenView


class ChildrenView(QWidget, Ui_ChildrenView):
    """Shows the Profiles and Pairs tables of one session."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setupUi(self)
        self.show_session(None)

    def show_session(self, model: InteractionModel | None) -> None:
        """Show ``model``'s tables (empty tables when no session is selected)."""
        profiles = model.profiles() if model else Table("Profiles", PROFILE_COLUMNS)
        pairs = model.pairs() if model else Table("Pairs", PAIR_COLUMNS)
        fill_table(self.profiles_table, profiles)
        fill_table(self.pairs_table, pairs)
