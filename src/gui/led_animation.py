"""LED animation utilities — reusable colour animation for LEDs.

Pure, hardware-free maths that mirrors the node firmware (``leds.h`` on both
boards) so an on-screen preview looks like the real ring:

- brightness patterns (``solid`` / ``blink`` / ``pulse``) scale a colour over time
- ``comet`` paints one or more rotating comets (a bright head with a fading tail)
- every change cross-fades from the previously displayed colour (``lerp_color``)

The :class:`LedAnimator` timer drives repaints; the widget that owns the LEDs
calls these helpers each tick with a wall-clock time so motion stays smooth
regardless of the tick rate.
"""

from enum import Enum

from PySide6.QtGui import QColor
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QWidget


class AnimationPattern(Enum):
    """LED animation patterns (mirror the firmware ``Pattern`` enum)."""
    SOLID = "solid"
    BLINK = "blink"
    PULSE = "pulse"
    COMET = "comet"


def pattern_scale(pattern: AnimationPattern, frac: float) -> float:
    """Brightness scale (0..1) of a blink/pulse pattern at cycle fraction ``frac``.

    Mirrors the firmware: BLINK is on for the first half of the cycle then off;
    PULSE is a triangle ramp 0 → 1 → 0. SOLID/COMET return 1 (COMET handles its
    own per-pixel brightness).
    """
    frac = frac % 1.0
    if pattern == AnimationPattern.BLINK:
        return 1.0 if frac < 0.5 else 0.0
    if pattern == AnimationPattern.PULSE:
        return frac * 2.0 if frac < 0.5 else (1.0 - frac) * 2.0
    return 1.0


def apply_animation(color: QColor, pattern: AnimationPattern, step: int,
                    max_steps: int = 10) -> QColor:
    """Apply a brightness pattern to a colour at integer ``step`` of ``max_steps``.

    Kept for step-based callers; new code should prefer :func:`pattern_scale`
    with a wall-clock fraction for smoother motion.
    """
    if pattern in (AnimationPattern.SOLID, AnimationPattern.COMET):
        return QColor(color)
    scale = pattern_scale(pattern, step / max_steps if max_steps else 0.0)
    if pattern == AnimationPattern.BLINK and scale < 0.5:
        return QColor("#202020")            # off colour (matches the panel's dark)
    result = QColor(color)
    h, s, v = result.hue(), result.saturation(), result.value()
    result.setHsv(h, s, int(v * scale))
    return result


def lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    """Linear cross-fade between two colours (t=0 → a, t=1 → b)."""
    t = max(0.0, min(1.0, t))
    return QColor(
        round(a.red()   + (b.red()   - a.red())   * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue()  + (b.blue()  - a.blue())  * t),
    )


def comet_pixels(seg_colors: list[QColor], count: int, head_frac: float
                 ) -> list[QColor]:
    """Render ``count`` LEDs as one comet per segment colour.

    ``head_frac`` is the lead comet's head position as a fraction of the ring
    (0..1, increasing = clockwise). Comet ``j`` sits ``j/k`` of the way further
    round and trails a 1 → 0 fade behind its head, coloured by ``seg_colors[j]``
    — so one colour gives one comet, two give two comets 180° apart. Mirrors the
    firmware ``renderComet_``.
    """
    k = max(1, len(seg_colors))
    head = (head_frac % 1.0) * count
    spacing = count / k
    tail = min(spacing * 0.7, count / 3.0)
    tail = max(1.0, tail)
    out: list[QColor] = []
    for i in range(count):
        best, best_j = 0.0, 0
        for j in range(k):
            h = head + j * spacing
            d = (h - i) % count           # how far pixel i sits behind head h
            level = 1.0 - d / tail
            if level > best:
                best, best_j = level, j
        c = seg_colors[best_j]
        out.append(QColor(round(c.red() * best), round(c.green() * best),
                          round(c.blue() * best)))
    return out


class LedAnimator(QWidget):
    """Timer-driven LED animator — emits animation updates at fixed intervals.

    Useful for coordinating animation across multiple LED displays.
    """

    stepped = Signal(int)  # Emitted with step value (0..max_steps-1) on each tick

    def __init__(self, interval_ms: int = 50, max_steps: int = 10, parent=None):
        super().__init__(parent)
        self._timer = QTimer()
        self._timer.timeout.connect(self._on_tick)
        self._step = 0
        self._interval = interval_ms
        self._max_steps = max_steps

    def start(self) -> None:
        """Start the animation timer."""
        self._step = 0
        self._timer.start(self._interval)

    def stop(self) -> None:
        """Stop the animation timer."""
        self._timer.stop()

    def set_interval(self, ms: int) -> None:
        """Set animation interval in milliseconds."""
        self._interval = ms
        if self._timer.isActive():
            self._timer.stop()
            self._timer.start(self._interval)

    def set_max_steps(self, steps: int) -> None:
        """Set number of animation steps per cycle."""
        self._max_steps = max(1, steps)

    def _on_tick(self) -> None:
        """Internal: advance animation step and emit signal."""
        self._step = (self._step + 1) % self._max_steps
        self.stepped.emit(self._step)
