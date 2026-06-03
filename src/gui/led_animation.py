"""LED animation utilities — reusable color animation for LEDs.

Provides pattern-based color animation (solid, blink, pulse) that can be used
in tests, activities, or any UI component displaying LEDs.
"""

from enum import Enum
from PySide6.QtGui import QColor
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QWidget


class AnimationPattern(Enum):
    """LED animation patterns."""
    SOLID = "solid"
    BLINK = "blink"
    PULSE = "pulse"


def apply_animation(color: QColor, pattern: AnimationPattern, step: int, max_steps: int = 10) -> QColor:
    """Apply animation effect to a color based on pattern and current step.

    Mimics the firmware's LED animation (leds.h):
    - BLINK: on/off with hard edges
    - PULSE: triangle ramp (0 -> 1 -> 0) for smooth fade in/out

    Args:
        color: Base color to animate.
        pattern: Animation pattern to apply.
        step: Current animation step (0..max_steps-1).
        max_steps: Total steps in animation cycle.

    Returns:
        Animated color (modified copy of input).
    """
    result = QColor(color)

    if pattern == AnimationPattern.BLINK:
        # On/off: first half bright, second half off
        if step >= max_steps // 2:
            result = QColor("#202020")  # Off color

    elif pattern == AnimationPattern.PULSE:
        # Triangle ramp 0..1..0 (matches firmware leds.h)
        # Maps step (0..max_steps) to scale (0..1..0)
        frac = step / max_steps
        scale = frac * 2.0 if frac < 0.5 else (1.0 - frac) * 2.0

        # Apply brightness scale
        h, s, v = result.hue(), result.saturation(), result.value()
        result.setHsv(h, s, int(v * scale))

    return result


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
