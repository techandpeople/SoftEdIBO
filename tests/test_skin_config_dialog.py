"""SkinConfigDialog: suggested skin id, duplicate-id guard and sizing."""

from typing import Any, cast

from PySide6.QtWidgets import QMessageBox, QScrollArea

from src.gui.skin_config_dialog import SkinConfigDialog

MAC = "AA:BB:CC:DD:EE:FF"


class _Gateway:
    is_connected = False


class _Settings:
    def __init__(self, data):
        self.data = data
        self.saves = 0

    def save(self):
        self.saves += 1


def _data() -> dict:
    node = {"mac": MAC, "node_type": "node_direct", "max_slots": 3}
    return {"robots": {
        "turtles": [{"id": "turtle-1", "nodes": [], "skins": [
            {"skin_id": "turtle-1", "chambers": []}]}],
        "thymios": [
            {"thymio_id": "thymio-1", "nodes": [dict(node)], "skins": [
                {"skin_id": "thymio-1",
                 "chambers": [{"mac": MAC, "slot": 0}]}]},
            {"thymio_id": "thymio-2", "nodes": [dict(node)], "skins": []},
        ],
    }}


def _dialog(qtbot, settings, robot_index=1, skin_index=-1) -> SkinConfigDialog:
    dlg = SkinConfigDialog(
        robot_type="thymio", robot_index=robot_index, skin_index=skin_index,
        settings=cast(Any, settings), gateway=cast(Any, _Gateway()))
    qtbot.addWidget(dlg)
    return dlg


def test_new_skin_is_offered_a_globally_unique_id(qtbot):
    dlg = _dialog(qtbot, _Settings(_data()))
    # thymio-1 belongs to the OTHER Thymio, so this one gets thymio-2.
    assert dlg.skin_id_edit.text() == "thymio-2"


def test_existing_skin_keeps_its_id(qtbot):
    dlg = _dialog(qtbot, _Settings(_data()), robot_index=0, skin_index=0)
    assert dlg.skin_id_edit.text() == "thymio-1"


def test_duplicate_skin_id_is_refused(qtbot, monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _parent, title, *_a, **_k: warnings.append(title))
    settings = _Settings(_data())
    dlg = _dialog(qtbot, settings)
    dlg.skin_id_edit.setText("Thymio-1")
    assert dlg._commit() is False
    assert warnings == ["Duplicate Skin ID"]
    assert settings.saves == 0


def test_chamber_rows_are_not_in_their_own_scroll_area(qtbot):
    dlg = _dialog(qtbot, _Settings(_data()))
    parent = dlg.rows_container.parentWidget()
    while parent is not None and parent is not dlg.left_panel:
        assert not isinstance(parent, QScrollArea)
        parent = parent.parentWidget()
    assert parent is dlg.left_panel


def test_dialog_shows_the_left_column_in_full_and_fits_the_screen(qtbot):
    dlg = _dialog(qtbot, _Settings(_data()))
    dlg._on_add_chamber()
    dlg._on_add_chamber()
    dlg.show()
    qtbot.waitExposed(dlg)
    viewport = dlg.left_scroll.viewport()
    # No field cut off sideways, and no vertical scrolling on a normal screen.
    assert viewport.width() >= dlg.left_panel.minimumSizeHint().width()
    avail = dlg.screen().availableGeometry()
    if dlg.left_panel.sizeHint().height() + 120 < avail.height():
        assert viewport.height() >= dlg.left_panel.minimumSizeHint().height()
    # Growing to fit never pushes the dialog past the screen (it may already
    # open at its designed size on a very small screen).
    assert dlg.width() <= max(avail.width(), 900)
    assert dlg.height() <= max(avail.height(), 620)
