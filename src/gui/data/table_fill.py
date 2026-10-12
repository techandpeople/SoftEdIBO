"""Show an analysis :class:`Table` in a QTableWidget."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem

from src.data.interaction_model import Table


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def fill_table(widget: QTableWidget, table: Table,
               hidden: tuple[str, ...] = ("session_id",)) -> None:
    """Replace ``widget``'s contents with ``table``.

    Column titles carry the column's meaning and unit as a tooltip, so the
    table explains itself. ``hidden`` columns are left out."""
    columns = [c for c in table.columns if c.name not in hidden]
    widget.setSortingEnabled(False)
    widget.clear()
    widget.setColumnCount(len(columns))
    widget.setRowCount(len(table.rows))
    for index, column in enumerate(columns):
        title = column.name.replace("_", " ")
        header = QTableWidgetItem(title)
        unit = f" [{column.unit}]" if column.unit else ""
        header.setToolTip(f"{column.description}{unit}")
        widget.setHorizontalHeaderItem(index, header)
    for row_index, row in enumerate(table.rows):
        for index, column in enumerate(columns):
            value = row.get(column.name)
            item = QTableWidgetItem()
            # Numbers sort as numbers, not as text.
            item.setData(Qt.ItemDataRole.DisplayRole,
                         value if isinstance(value, (int, float))
                         and not isinstance(value, bool) else _text(value))
            widget.setItem(row_index, index, item)
    widget.horizontalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.ResizeToContents)
    widget.setSortingEnabled(True)
