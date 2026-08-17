"""Tests for SensorGridView (geometry-driven touch grid) + threshold persistence.

The grid's shape/size comes from the skin geometry (2x2 'D' for the Thymio, a
plain 2x2 fallback, or an explicit bigger grid for future capacitive touch), and
a sensor lights up only once its magnitude reaches the live threshold. The
sensitivity itself is saved per skin type in Settings.
"""

import pytest

from src.config.settings import Settings
from src.gui.sensor_grid_view import (
    CAPACITIVE_GRID_EXAMPLE,
    SensorGridView,
    _cells_by_value,
)
from src.hardware.skin_geometry import geometry_for


@pytest.fixture
def slides_on(monkeypatch):
    """Enable the (default-off) motion trail for the trail-logic tests."""
    monkeypatch.setattr("src.ml.gesture_taxonomy.SLIDE_DETECTION_ENABLED", True)


def test_cells_by_value_maps_indices_to_positions():
    cells = _cells_by_value([[0, 1], [2, 3]])
    assert cells == {0: (0, 0), 1: (0, 1), 2: (1, 0), 3: (1, 1)}
    # Negative cells (empty) are skipped.
    assert _cells_by_value([[0, -1], [-1, 1]]) == {0: (0, 0), 1: (1, 1)}


def test_thymio_geometry_yields_2x2_d_grid(qtbot):
    # Four sensors in a 2x2: top pair in the bulge, bottom pair on the flat edge.
    grid = SensorGridView(geometry_for("thymio"), lambda: 100.0)
    qtbot.addWidget(grid)
    assert (grid._cols, grid._rows) == (2, 2)
    assert grid._shape == "thymio"                 # the 'D' outline
    assert sorted(grid._cells) == [0, 1, 2, 3]


def test_active_sensors_track_the_threshold(qtbot):
    thr = {"v": 100.0}
    grid = SensorGridView(geometry_for("thymio"), lambda: thr["v"])
    qtbot.addWidget(grid)
    grid.feed([50.0, 150.0, 0.0, 200.0])
    assert grid.active_sensors() == {1, 3}         # only >=100 uT
    thr["v"] = 40.0                                # more sensitive
    assert grid.active_sensors() == {0, 1, 3}      # 50 now counts, 0 still not
    thr["v"] = 300.0                               # less sensitive
    assert grid.active_sensors() == set()


def test_fallback_grid_without_geometry(qtbot):
    grid = SensorGridView(None, lambda: 100.0)
    qtbot.addWidget(grid)
    assert (grid._cols, grid._rows, grid._shape) == (2, 2, "rect")
    assert sorted(grid._cells) == [0, 1, 2, 3]


def test_explicit_capacitive_grid_overrides_geometry(qtbot):
    grid = SensorGridView(None, lambda: 100.0, grid=CAPACITIVE_GRID_EXAMPLE)
    qtbot.addWidget(grid)
    assert (grid._cols, grid._rows) == (4, 4)
    assert len(grid._cells) == 16


def test_trail_disabled_by_default(qtbot):
    """With the master switch off (default) the grid builds no motion trail."""
    grid = SensorGridView(geometry_for("thymio"), lambda: 100.0)
    qtbot.addWidget(grid)
    grid.feed([300.0, 0.0, 0.0, 0.0])
    grid.feed([0.0, 300.0, 0.0, 0.0])
    assert grid._trail == []


def test_live_arrow_gated_by_lateral_shear(qtbot, slides_on):
    """With vec data, a vertical push that migrates sensors must not show the
    slide arrow; a lateral drag must. Without vec, continuity alone decides."""
    grid = SensorGridView(geometry_for("thymio"), lambda: 100.0)
    qtbot.addWidget(grid)
    down = [[0, 0, 300]] * 4
    grid.feed([300.0, 0.0, 0.0, 0.0], down)     # push S0 (pure z)
    grid.feed([0.0, 300.0, 0.0, 0.0], down)     # push S1 (pure z, continuous)
    assert not grid._trail_is_slide()           # arrow suppressed

    shear = [[300, 100, 60]] * 4
    grid2 = SensorGridView(geometry_for("thymio"), lambda: 100.0)
    qtbot.addWidget(grid2)
    grid2.feed([300.0, 0.0, 0.0, 0.0], shear)   # drag across (lateral x/y)
    grid2.feed([0.0, 300.0, 0.0, 0.0], shear)
    assert grid2._trail_is_slide()

    grid3 = SensorGridView(geometry_for("thymio"), lambda: 100.0)
    qtbot.addWidget(grid3)
    grid3.feed([300.0, 0.0, 0.0, 0.0])          # no vec -> fallback allows
    assert grid3._trail_is_slide()


def test_wandering_trail_is_not_a_slide(qtbot, slides_on):
    """A centroid that circles between sensors (chamber inflation shifting the
    magnet) must not draw a direction arrow, even with continuous contact."""
    grid = SensorGridView(geometry_for("thymio"), lambda: 100.0)
    qtbot.addWidget(grid)
    # Oscillate the touch between S1 and S2 several times (no release).
    for _ in range(3):
        grid.feed([0.0, 300.0, 0.0, 0.0])
        grid.feed([0.0, 0.0, 300.0, 0.0])
    assert not grid._trail_is_slide()


def test_trail_breaks_when_contact_releases(qtbot, slides_on):
    """Touching one sensor, releasing, then touching another must start a NEW
    trail - separate taps never draw a slide path between them."""
    import time
    grid = SensorGridView(geometry_for("thymio"), lambda: 100.0)
    qtbot.addWidget(grid)
    grid.feed([300.0, 0.0, 0.0, 0.0])          # touch S0
    assert len(grid._trail) == 1
    grid.feed([310.0, 0.0, 0.0, 0.0])          # continuous contact - grows
    assert len(grid._trail) == 2
    time.sleep(0.2)                             # release > _TRAIL_BREAK_MS
    grid.feed([0.0, 300.0, 0.0, 0.0])          # touch S1 - fresh trail
    assert len(grid._trail) == 1


# ---------------------------------------------------------------------------
# Per-skin-type sensitivity persistence
# ---------------------------------------------------------------------------

def test_touch_threshold_saved_and_reloaded_per_type(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("{}\n", encoding="utf-8")

    s = Settings(path)
    assert s.touch_threshold_ut("thymio") is None      # nothing saved yet
    s.set_touch_threshold_ut("thymio", 120.0)
    s.set_touch_threshold_ut("turtle_square", 90.0)

    # Persisted to disk and independent per type.
    reloaded = Settings(path)
    assert reloaded.touch_threshold_ut("thymio") == 120.0
    assert reloaded.touch_threshold_ut("turtle_square") == 90.0
    assert reloaded.touch_threshold_ut("tree_round") is None
    # Empty skin type is ignored (no crash, nothing stored).
    reloaded.set_touch_threshold_ut("", 50.0)
    assert reloaded.touch_threshold_ut("") is None
