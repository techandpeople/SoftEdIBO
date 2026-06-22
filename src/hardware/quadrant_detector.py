"""
Quadrant Detection System for SoftEdIBO Touch Sensing

Adapted from the thesis QuadrantPredictor (tools/quadrant_live_plot.py) to work
with the SoftEdIBO ESP-NOW pipeline.  Uses raw magnitude values (μT) directly —
no normalisation against a fullscale constant — so the detector is independent of
firmware configuration and behaves like the proven thesis implementation.

Quadrant layout:
    Q1(+)  Q2(-)
    Q3(-)  Q4(+)
"""

from __future__ import annotations

import math
import logging
from typing import List, Tuple, Dict, Any, Optional, Sequence
from enum import Enum

logger = logging.getLogger(__name__)


class QuadrantPosition(Enum):
    Q1     = "Q1"
    Q2     = "Q2"
    Q3     = "Q3"
    Q4     = "Q4"
    Q1_Q2  = "Q1-Q2"
    Q1_Q3  = "Q1-Q3"
    Q2_Q4  = "Q2-Q4"
    Q3_Q4  = "Q3-Q4"
    CENTER = "CENTER"
    NONE   = "NONE"


class TouchZone(Enum):
    TOP_LEFT     = "top_left"
    TOP_RIGHT    = "top_right"
    BOTTOM_LEFT  = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    CENTER       = "center"
    TOP_EDGE     = "top_edge"
    BOTTOM_EDGE  = "bottom_edge"
    LEFT_EDGE    = "left_edge"
    RIGHT_EDGE   = "right_edge"
    NONE         = "none"


_POSITION_TO_ZONE: dict[QuadrantPosition, TouchZone] = {
    QuadrantPosition.Q1:     TouchZone.TOP_LEFT,
    QuadrantPosition.Q2:     TouchZone.TOP_RIGHT,
    QuadrantPosition.Q3:     TouchZone.BOTTOM_LEFT,
    QuadrantPosition.Q4:     TouchZone.BOTTOM_RIGHT,
    QuadrantPosition.Q1_Q2:  TouchZone.TOP_EDGE,
    QuadrantPosition.Q1_Q3:  TouchZone.LEFT_EDGE,
    QuadrantPosition.Q2_Q4:  TouchZone.RIGHT_EDGE,
    QuadrantPosition.Q3_Q4:  TouchZone.BOTTOM_EDGE,
    QuadrantPosition.CENTER: TouchZone.CENTER,
    QuadrantPosition.NONE:   TouchZone.NONE,
}

_BETWEEN_PAIRS: dict[frozenset, QuadrantPosition] = {
    frozenset((0, 1)): QuadrantPosition.Q1_Q2,
    frozenset((0, 2)): QuadrantPosition.Q1_Q3,
    frozenset((1, 3)): QuadrantPosition.Q2_Q4,
    frozenset((2, 3)): QuadrantPosition.Q3_Q4,
}

_QUADRANT_NAMES = ["Q1", "Q2", "Q3", "Q4"]


class QuadrantDetector:
    """
    Detects active quadrant(s) from 4 raw magnitude readings (μT).

    Ported from the thesis QuadrantPredictor — uses absolute thresholds in the
    same units as the input (μT), applies optional EMA smoothing, and implements
    Schmitt-trigger hysteresis so touched quadrants stay active until the signal
    falls below (threshold − hysteresis).

    Default thresholds (100 μT) are conservative; tune via the Touch Tuning panel
    after a Re-zero so resting values settle below the threshold.
    """

    def __init__(
        self,
        thresholds: List[float] | None = None,
        hysteresis: float = 20.0,
        ema_alpha: float = 0.25,
        between_min: float = 50.0,
        between_max: float = 120.0,
        magnet_strength: str = "strong",
    ) -> None:
        default = 100.0
        self.thresholds     = list(thresholds) if thresholds else [default] * 4
        self.hysteresis     = max(0.0, hysteresis)
        self.ema_alpha      = max(0.0, min(1.0, ema_alpha))
        self.between_min    = between_min
        self.between_max    = between_max
        self.magnet_strength = magnet_strength

        self.active_state   = [False, False, False, False]
        self._ema_state     = [0.0, 0.0, 0.0, 0.0]
        self.sensor_values  = [0.0, 0.0, 0.0, 0.0]   # smoothed display values

    # ------------------------------------------------------------------
    # Core update (mirrors thesis QuadrantPredictor.predict)
    # ------------------------------------------------------------------

    def update(self, raw_values: List[float]) -> Tuple[List[bool], List[float]]:
        """
        Update detector state from raw per-sensor magnitudes (μT).

        Returns:
            (active_quadrants, smoothed_values)
        """
        if len(raw_values) != 4:
            raise ValueError(f"Expected 4 sensor values, got {len(raw_values)}")

        # Sanitise NaN / negative
        clean = [0.0 if math.isnan(v) else max(0.0, float(v)) for v in raw_values]

        # Schmitt-trigger hysteresis (thesis pattern)
        for i in range(4):
            if self.active_state[i]:
                off = max(0.0, self.thresholds[i] - self.hysteresis)
                self.active_state[i] = clean[i] >= off
            else:
                self.active_state[i] = clean[i] >= self.thresholds[i]

        # EMA smoothing for display (thesis: alpha=0.25)
        for i in range(4):
            self._ema_state[i] = (
                self.ema_alpha * clean[i]
                + (1.0 - self.ema_alpha) * self._ema_state[i]
            )
        self.sensor_values = list(self._ema_state)

        return self.active_state[:], self.sensor_values[:]

    def reset(self) -> None:
        self.active_state = [False, False, False, False]
        self._ema_state   = [0.0, 0.0, 0.0, 0.0]
        self.sensor_values = [0.0, 0.0, 0.0, 0.0]

    def set_thresholds(self, thresholds: List[float]) -> None:
        if len(thresholds) != 4:
            raise ValueError(f"Expected 4 thresholds, got {len(thresholds)}")
        self.thresholds = [max(0.0, float(t)) for t in thresholds]

    def set_hysteresis(self, h: float) -> None:
        self.hysteresis = max(0.0, float(h))

    # ------------------------------------------------------------------
    # Quadrant / zone helpers
    # ------------------------------------------------------------------

    def get_active_quadrants(self) -> List[str]:
        return [_QUADRANT_NAMES[i] for i, a in enumerate(self.active_state) if a]

    def get_dominant_quadrant(self) -> Tuple[str, float]:
        """Return (quadrant_name, signal_strength_μT) for the strongest active sensor."""
        active_idx = [i for i, a in enumerate(self.active_state) if a]
        if not active_idx:
            return ("NONE", 0.0)
        best = max(active_idx, key=lambda i: self.sensor_values[i])
        return (_QUADRANT_NAMES[best], self.sensor_values[best])

    def get_confidence(self) -> float:
        """Confidence = (primary − second) / primary (thesis formula)."""
        active_idx = [i for i, a in enumerate(self.active_state) if a]
        if not active_idx:
            return 0.0
        primary = max(self.sensor_values[i] for i in active_idx)
        second  = max((self.sensor_values[i] for i in active_idx
                       if self.sensor_values[i] < primary), default=0.0)
        return max(0.0, min(1.0, (primary - second) / max(primary, 1e-6)))

    def estimate_position(self) -> QuadrantPosition:
        active = self.active_state
        if not any(active):
            return QuadrantPosition.NONE

        count = sum(active)
        if count == 1:
            return [QuadrantPosition.Q1, QuadrantPosition.Q2,
                    QuadrantPosition.Q3, QuadrantPosition.Q4][active.index(True)]
        if count == 4:
            return QuadrantPosition.CENTER

        for pair, pos in _BETWEEN_PAIRS.items():
            i, j = tuple(pair)
            if active[i] and active[j] and count == 2:
                return pos

        dominant, _ = self.get_dominant_quadrant()
        try:
            return QuadrantPosition[dominant]
        except KeyError:
            return QuadrantPosition.NONE

    def get_touch_zone(self) -> TouchZone:
        return _POSITION_TO_ZONE.get(self.estimate_position(), TouchZone.NONE)

    def get_position_confidence(self) -> float:
        return self.get_confidence()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thresholds":      self.thresholds,
            "hysteresis":      self.hysteresis,
            "magnet_strength": self.magnet_strength,
            "active_quadrants": self.get_active_quadrants(),
            "sensor_values":   self.sensor_values,
            "position":        self.estimate_position().value,
            "touch_zone":      self.get_touch_zone().value,
            "confidence":      self.get_confidence(),
        }


class TouchPositionTracker:
    """
    Tracks touch positions over time with smoothing and state management.
    """

    def __init__(
        self,
        detector: QuadrantDetector,
        smoothing_alpha: float = 0.3,
        min_touch_duration_ms: int = 100,
    ) -> None:
        self.detector               = detector
        self.smoothing_alpha        = smoothing_alpha
        self.min_touch_duration_ms  = min_touch_duration_ms

        self.current_position   = QuadrantPosition.NONE
        self.current_zone       = TouchZone.NONE
        self.confidence         = 0.0
        self.smoothed_position  = QuadrantPosition.NONE
        self.smoothed_confidence = 0.0

        self.touch_start_time   = None
        self.touch_duration_ms  = 0
        self.is_valid_touch     = False
        self.position_changed   = False
        self.touch_started      = False
        self.touch_ended        = False

    def update(self, sensor_values: List[float], current_time_ms: int) -> Dict[str, Any]:
        self.position_changed = False
        self.touch_started    = False
        self.touch_ended      = False

        self.detector.update(sensor_values)

        new_position  = self.detector.estimate_position()
        new_zone      = self.detector.get_touch_zone()
        new_confidence = self.detector.get_position_confidence()

        if new_position != self.current_position:
            self.position_changed = True
            self.current_position = new_position
            self.current_zone     = new_zone

        self.confidence = (self.smoothing_alpha * new_confidence
                           + (1 - self.smoothing_alpha) * self.smoothed_confidence)
        self.smoothed_confidence = self.confidence

        was_touching = self.touch_start_time is not None
        is_touching  = new_position != QuadrantPosition.NONE

        if is_touching and not was_touching:
            self.touch_start_time = current_time_ms
            self.touch_started    = True
            self.is_valid_touch   = False
            logger.debug("Touch started at %s", new_zone.value)

        elif not is_touching and was_touching:
            if self.touch_start_time is not None:
                self.touch_duration_ms = current_time_ms - self.touch_start_time
                self.is_valid_touch    = self.touch_duration_ms >= self.min_touch_duration_ms
            self.touch_start_time = None
            self.touch_ended      = True
            self.current_position = QuadrantPosition.NONE
            self.current_zone     = TouchZone.NONE

        if new_position != QuadrantPosition.NONE and self.confidence > 0.6:
            self.smoothed_position = new_position

        return {
            "position":         self.current_position.value,
            "zone":             self.current_zone.value,
            "confidence":       self.confidence,
            "smoothed_position": self.smoothed_position.value,
            "is_touching":      is_touching,
            "is_valid_touch":   self.is_valid_touch,
            "touch_duration_ms": self.touch_duration_ms,
            "events": {
                "position_changed": self.position_changed,
                "touch_started":    self.touch_started,
                "touch_ended":      self.touch_ended,
            },
            "raw": {
                "active_quadrants": self.detector.get_active_quadrants(),
                "sensor_values":    self.detector.sensor_values,
            },
        }

    def reset(self) -> None:
        self.detector.reset()
        self.current_position    = QuadrantPosition.NONE
        self.current_zone        = TouchZone.NONE
        self.confidence          = 0.0
        self.smoothed_position   = QuadrantPosition.NONE
        self.smoothed_confidence = 0.0
        self.touch_start_time    = None
        self.touch_duration_ms   = 0
        self.is_valid_touch      = False
        self.position_changed    = False
        self.touch_started       = False
        self.touch_ended         = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detector":          self.detector.to_dict(),
            "current_position":  self.current_position.value,
            "current_zone":      self.current_zone.value,
            "confidence":        self.confidence,
            "smoothed_position": self.smoothed_position.value,
            "is_touching":       self.touch_start_time is not None,
            "is_valid_touch":    self.is_valid_touch,
            "touch_duration_ms": self.touch_duration_ms,
        }
