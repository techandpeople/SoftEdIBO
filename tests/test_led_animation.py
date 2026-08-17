"""Tests for the LED animation maths and the LED ring test panel.

Covers the preview helpers that mirror the firmware (comet, cross-fade lerp) and
the frames the LED tester asks its host to send (whole / halves / quarters, the
comet pattern, and the smooth-transition ``fade_ms``).
"""

from typing import cast

from PySide6.QtGui import QColor

from src.gui.led_animation import (AnimationPattern, comet_pixels, lerp_color,
                                    pattern_scale)
from src.hardware.gateway import Gateway


class TestAnimationMaths:
    def test_lerp_color_endpoints_and_midpoint(self):
        a, b = QColor("#000000"), QColor("#ffffff")
        assert lerp_color(a, b, 0.0).name() == "#000000"
        assert lerp_color(a, b, 1.0).name() == "#ffffff"
        mid = lerp_color(a, b, 0.5)
        assert mid.red() == mid.green() == mid.blue()
        assert 120 <= mid.red() <= 135              # ~half brightness

    def test_lerp_color_clamps(self):
        a, b = QColor("#000000"), QColor("#ffffff")
        assert lerp_color(a, b, -1.0).name() == "#000000"
        assert lerp_color(a, b, 2.0).name() == "#ffffff"

    def test_pattern_scale_pulse_is_triangle(self):
        assert pattern_scale(AnimationPattern.PULSE, 0.0) == 0.0
        assert pattern_scale(AnimationPattern.PULSE, 0.5) == 1.0
        assert abs(pattern_scale(AnimationPattern.PULSE, 0.25) - 0.5) < 1e-6
        # solid / comet never dim the whole ring
        assert pattern_scale(AnimationPattern.SOLID, 0.3) == 1.0

    def test_one_comet_has_a_single_bright_head(self):
        px = comet_pixels([QColor("#ff0000")], 24, head_frac=0.0)
        # head at pixel 0 is full red; the pixel just behind the tail is dark
        assert px[0].red() > 240
        assert px[12].red() < 20                     # opposite side is dark

    def test_two_comets_are_opposite_and_coloured_per_segment(self):
        red, green = QColor("#ff0000"), QColor("#00ff00")
        px = comet_pixels([red, green], 24, head_frac=0.0)
        # comet 0 (red) head at pixel 0, comet 1 (green) head 180° round at pixel 12
        assert px[0].red() > 240 and px[0].green() < 20
        assert px[12].green() > 240 and px[12].red() < 20


class TestLedRingTesterFrames:
    def _tester(self, qtbot, count=24):
        from src.gui.led_ring_tester import LedRingTester
        sent = []
        t = LedRingTester(count,
                          lambda idx, cols, pat, fade, angle:
                              sent.append((idx, cols, pat, fade, angle)))
        qtbot.addWidget(t)
        return t, sent

    def test_choosing_a_colour_applies_live(self, qtbot):
        # No "apply" step — picking a colour sends the whole-ring frame at once.
        t, sent = self._tester(qtbot)
        t._set_color("#0000ff")
        assert sent[-1] == (None, ["#0000ff"], "solid", 250, 0.0)

    def test_halves_keep_last_two_clicks_in_order(self, qtbot):
        t, sent = self._tester(qtbot)
        t._set_segments(2)
        t._set_color("#ff0000")
        t._set_color("#00ff00")
        t._set_color("#0000ff")           # a third click drops the oldest
        assert sent[-1] == (None, ["#00ff00", "#0000ff"], "solid", 250, 0.0)

    def test_quarters_comet_sends_four_colours(self, qtbot):
        t, sent = self._tester(qtbot)
        t._set_segments(4)
        for c in ("#ff0000", "#00ff00", "#0000ff", "#ffff00"):
            t._set_color(c)
        t._set_pattern("comet")           # applies live
        assert sent[-1] == (None,
                            ["#ff0000", "#00ff00", "#0000ff", "#ffff00"],
                            "comet", 250, 0.0)

    def test_transition_time_box_flows_into_the_next_change(self, qtbot):
        t, sent = self._tester(qtbot)
        t.fade_spin.setValue(600)
        t._set_pattern("pulse")           # the change carries the new fade time
        assert sent[-1][3] == 600

    def test_dragging_the_handle_sends_the_angle_on_release(self, qtbot):
        t, sent = self._tester(qtbot)
        t._set_segments(2)
        n = len(sent)
        t._ring.angleChanged.emit(90.0)   # live drag — tracks, no send
        assert len(sent) == n
        t._ring.angleReleased.emit(90.0)  # drop — one send carrying the angle
        assert sent[-1][4] == 90.0

    def test_off_sends_off(self, qtbot):
        t, sent = self._tester(qtbot)
        t._on_off()
        assert sent[-1][1] is None and sent[-1][2] == "off"


class TestSendLedRouting:
    """The dialog turns the tester callback into set_led / set_led_halves frames."""

    def _dialog(self, qtbot):
        from src.gui.test_actuators_dialog import TestActuatorsDialog

        class _Gw:
            def __init__(self):
                self.sent = []

            def send(self, mac, cmd, **kw):
                self.sent.append((cmd, kw))

            def on_message(self, _cb):
                pass

        gw = _Gw()
        dlg = TestActuatorsDialog("AA:BB:CC:DD:EE:FF", [], cast(Gateway, gw),
                                  led_count=0)
        qtbot.addWidget(dlg)
        gw.sent.clear()
        return dlg, gw

    def test_single_colour_routes_to_set_led(self, qtbot):
        dlg, gw = self._dialog(qtbot)
        dlg._send_led(None, None, ["#ff0000"], "comet", 300)
        cmd, kw = gw.sent[-1]
        assert cmd == "set_led"
        assert kw == {"color": "#ff0000", "pattern": "comet", "fade_ms": 300}

    def test_two_colours_route_to_set_led_halves(self, qtbot):
        dlg, gw = self._dialog(qtbot)
        dlg._send_led(None, None, ["#ff0000", "#00ff00"], "comet", 200)
        cmd, kw = gw.sent[-1]
        assert cmd == "set_led_halves"
        assert kw == {"colors": ["#ff0000", "#00ff00"], "pattern": "comet",
                      "fade_ms": 200}

    def test_ring_index_is_forwarded(self, qtbot):
        dlg, gw = self._dialog(qtbot)
        dlg._send_led(2, None, ["#ff0000"], "solid", 250)
        cmd, kw = gw.sent[-1]
        assert cmd == "set_led" and kw["ring"] == 2

    def test_angle_is_forwarded(self, qtbot):
        dlg, gw = self._dialog(qtbot)
        dlg._send_led(None, None, ["#ff0000", "#00ff00"], "solid", 250, 90.0)
        cmd, kw = gw.sent[-1]
        assert cmd == "set_led_halves" and kw["angle"] == 90.0


class TestRingWidgetAngle:
    """The preview widget rotates its arcs by the angle, mirroring the firmware."""

    def test_halves_base_rotates_with_angle(self, qtbot):
        from src.gui.led_ring_tester import LedRingWidget
        from src.gui.led_animation import AnimationPattern
        w = LedRingWidget(24)
        qtbot.addWidget(w)
        red, green = QColor("#ff0000"), QColor("#00ff00")
        # angle 0: pixel 0 is red (arc 0), pixel 12 is green (arc 1)
        w.set_look([red, green], AnimationPattern.SOLID, 0, 0, 0.0)
        assert w._base[0].name() == "#ff0000"
        assert w._base[12].name() == "#00ff00"
        # angle 180° (half a ring) swaps which arc each pixel belongs to
        w.set_look([red, green], AnimationPattern.SOLID, 0, 0, 180.0)
        assert w._base[0].name() == "#00ff00"
        assert w._base[12].name() == "#ff0000"

    def test_angle_at_maps_top_to_zero(self, qtbot):
        from PySide6.QtCore import QPointF
        from src.gui.led_ring_tester import LedRingWidget
        w = LedRingWidget(24)
        qtbot.addWidget(w)
        w.resize(200, 200)
        cx, cy, _r, _l = w._center_radius()
        # a point straight above the centre is angle 0 (top)
        assert abs(w._angle_at(QPointF(cx, cy - 50))) < 1.0
        # straight right is 90°
        assert abs(w._angle_at(QPointF(cx + 50, cy)) - 90.0) < 1.0


class TestControllerBaseAngle:
    """The saved per-ring mounting angle is added to every LED command."""

    def _ctrl(self):
        from src.hardware.esp32_controller import ESP32Controller

        class _Gw:
            def __init__(self):
                self.sent = []
                self.is_connected = True

            def on_message(self, _cb):
                pass

            def send(self, mac, cmd, **kw):
                self.sent.append((cmd, kw))
                return True

        gw = _Gw()
        return ESP32Controller("AA:BB:CC:DD:EE:FF", cast(Gateway, gw)), gw

    def test_no_offset_by_default(self):
        ctrl, gw = self._ctrl()
        ctrl.set_led("#ff0000")
        assert "angle" not in gw.sent[-1][1]

    def test_base_angle_applied_when_command_has_none(self):
        ctrl, gw = self._ctrl()
        ctrl.set_led_angles({0: 90})
        ctrl.set_led("#ff0000")
        assert gw.sent[-1][1]["angle"] == 90.0

    def test_base_and_command_angles_add(self):
        ctrl, gw = self._ctrl()
        ctrl.set_led_angles({0: 90})
        ctrl.set_led_halves(["#ff0000", "#00ff00"], angle=30)
        assert gw.sent[-1][1]["angle"] == 120.0

    def test_offset_is_per_ring(self):
        ctrl, gw = self._ctrl()
        ctrl.set_led_angles({1: 45})
        ctrl.set_led("#ff0000", ring=0)      # ring 0 has no offset
        assert "angle" not in gw.sent[-1][1]
        ctrl.set_led("#ff0000", ring=1)      # ring 1 does
        assert gw.sent[-1][1]["angle"] == 45.0
