"""
Quadrant Detection System for SoftEdIBO Touch Sensing

Adapted from the Tese project to provide precise touch position tracking
on soft robot skins using 4-quadrant magnetic sensor arrays.

Quadrant layout:
    Q1(+)  Q2(-)
    Q3(-)  Q4(+)

This system provides:
- Real-time quadrant detection with hysteresis
- Touch position estimation within and between quadrants
- Integration with existing ESP32 touch sensing infrastructure
"""

import logging
from typing import List, Tuple, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class QuadrantPosition(Enum):
    """Position enumeration for touch location."""
    Q1 = "Q1"           # Top-left (+)
    Q2 = "Q2"           # Top-right (-)
    Q3 = "Q3"           # Bottom-left (-)
    Q4 = "Q4"           # Bottom-right (+)
    Q1_Q2 = "Q1-Q2"     # Top edge (between Q1 and Q2)
    Q1_Q3 = "Q1-Q3"     # Left edge (between Q1 and Q3)
    Q2_Q4 = "Q2-Q4"     # Right edge (between Q2 and Q4)
    Q3_Q4 = "Q3-Q4"     # Bottom edge (between Q3 and Q4)
    CENTER = "CENTER"   # Center (all quadrants)
    NONE = "NONE"       # No touch detected


class TouchZone(Enum):
    """Coarse touch zones for simplified interaction."""
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    CENTER = "center"
    TOP_EDGE = "top_edge"
    BOTTOM_EDGE = "bottom_edge"
    LEFT_EDGE = "left_edge"
    RIGHT_EDGE = "right_edge"
    NONE = "none"


class QuadrantDetector:
    """
    Detects active quadrant(s) from 4 sensor readings with hysteresis.

    Attributes:
        thresholds: Per-quadrant detection thresholds (0.0-1.0)
        hysteresis: Threshold hysteresis to avoid flicker
        active_state: Current active state for each quadrant
    """

    def __init__(
        self,
        thresholds: List[float] = None,
        hysteresis: float = 0.05,
        magnet_strength: str = "weak",
    ):
        """
        Initialize quadrant detector.

        Args:
            thresholds: Per-quadrant detection thresholds (default 0.3 for all)
            hysteresis: Threshold hysteresis to avoid flicker (default 0.05)
            magnet_strength: "weak" or "strong" - affects default thresholds
        """
        if thresholds is None:
            # Default thresholds based on magnet strength
            default_threshold = 0.3 if magnet_strength == "weak" else 0.2
            thresholds = [default_threshold] * 4

        self.thresholds = thresholds
        self.hysteresis = hysteresis
        self.magnet_strength = magnet_strength

        # Internal state for hysteresis
        self.active_state = [False, False, False, False]

        # Sensor value tracking for position estimation
        self.sensor_values = [0.0, 0.0, 0.0, 0.0]
        self.normalized_values = [0.0, 0.0, 0.0, 0.0]

    def update(self, sensor_values: List[float]) -> Tuple[List[bool], List[float]]:
        """
        Update active state based on sensor readings.

        Sensor layout matches the Tese hardware:
            S0 (index 0) → Q1 top-left  (+)
            S1 (index 1) → Q2 top-right (-)
            S2 (index 2) → Q3 bot-left  (-)
            S3 (index 3) → Q4 bot-right (+)

        Args:
            sensor_values: Normalised sensor readings [s0..s3], typically 0.0–1.0.

        Returns:
            Tuple of (active_quadrants, normalized_values).
        """
        if len(sensor_values) != 4:
            raise ValueError(f"Expected 4 sensor values, got {len(sensor_values)}")

        self.sensor_values = sensor_values[:]
        self.normalized_values = [min(max(v, 0.0), 1.0) for v in sensor_values]

        for i in range(4):
            val = self.normalized_values[i]
            if self.active_state[i]:
                if val < (self.thresholds[i] - self.hysteresis):
                    self.active_state[i] = False
            else:
                if val > self.thresholds[i]:
                    self.active_state[i] = True

        return self.active_state[:], self.normalized_values

    def reset(self):
        """Reset detector state."""
        self.active_state = [False, False, False, False]
        self.sensor_values = [0.0, 0.0, 0.0, 0.0]
        self.normalized_values = [0.0, 0.0, 0.0, 0.0]

    def set_thresholds(self, thresholds: List[float]):
        """Update thresholds."""
        if len(thresholds) != 4:
            raise ValueError(f"Expected 4 thresholds, got {len(thresholds)}")
        self.thresholds = list(thresholds)

    def set_hysteresis(self, hysteresis: float):
        """Update hysteresis value."""
        self.hysteresis = max(0.0, hysteresis)

    def get_active_quadrants(self) -> List[str]:
        """Return names of active quadrants."""
        quadrant_names = ["Q1", "Q2", "Q3", "Q4"]
        return [quadrant_names[i] for i, active in enumerate(self.active_state) if active]

    def get_dominant_quadrant(self) -> Tuple[str, float]:
        """
        Get the quadrant with strongest sensor reading.

        Returns:
            Tuple of (quadrant_name, strength)
        """
        if not any(self.active_state):
            return ("NONE", 0.0)

        # Sensor i corresponds directly to quadrant i.
        quadrant_names = ["Q1", "Q2", "Q3", "Q4"]
        best = max(
            ((i, self.normalized_values[i]) for i in range(4) if self.active_state[i]),
            key=lambda x: x[1],
        )
        return (quadrant_names[best[0]], best[1])

    def estimate_position(self) -> QuadrantPosition:
        """
        Estimate touch position based on active quadrants and sensor values.

        Returns:
            QuadrantPosition enum indicating the estimated touch location
        """
        active = self.active_state

        if not any(active):
            return QuadrantPosition.NONE

        count = sum(active)
        if count == 1:
            return [QuadrantPosition.Q1, QuadrantPosition.Q2,
                    QuadrantPosition.Q3, QuadrantPosition.Q4][active.index(True)]

        if count == 4:
            return QuadrantPosition.CENTER

        _PAIRS: dict[tuple[int, int], QuadrantPosition] = {
            (0, 1): QuadrantPosition.Q1_Q2,
            (0, 2): QuadrantPosition.Q1_Q3,
            (1, 3): QuadrantPosition.Q2_Q4,
            (2, 3): QuadrantPosition.Q3_Q4,
        }
        for (i, j), pos in _PAIRS.items():
            if active[i] and active[j] and count == 2:
                return pos

        dominant, _ = self.get_dominant_quadrant()
        return QuadrantPosition[dominant]

    def get_touch_zone(self) -> TouchZone:
        """
        Get coarse touch zone for simplified interaction.

        Returns:
            TouchZone enum indicating the coarse touch location
        """
        position = self.estimate_position()

        position_to_zone = {
            QuadrantPosition.Q1: TouchZone.TOP_LEFT,
            QuadrantPosition.Q2: TouchZone.TOP_RIGHT,
            QuadrantPosition.Q3: TouchZone.BOTTOM_LEFT,
            QuadrantPosition.Q4: TouchZone.BOTTOM_RIGHT,
            QuadrantPosition.Q1_Q2: TouchZone.TOP_EDGE,
            QuadrantPosition.Q1_Q3: TouchZone.LEFT_EDGE,
            QuadrantPosition.Q2_Q4: TouchZone.RIGHT_EDGE,
            QuadrantPosition.Q3_Q4: TouchZone.BOTTOM_EDGE,
            QuadrantPosition.CENTER: TouchZone.CENTER,
            QuadrantPosition.NONE: TouchZone.NONE,
        }

        return position_to_zone.get(position, TouchZone.NONE)

    def get_position_confidence(self) -> float:
        """
        Get confidence score for current position estimate.

        Returns:
            Float between 0.0 and 1.0 indicating confidence level
        """
        if not any(self.active_state):
            return 0.0

        # High confidence if single quadrant is clearly dominant
        active_count = sum(self.active_state)
        if active_count == 1:
            return 0.9

        # Lower confidence for multiple active quadrants
        # Calculate how "centered" the activation is
        max_val = max(self.normalized_values)
        min_active = min(v for i, v in enumerate(self.normalized_values) if self.active_state[i % 4])

        if max_val > 0:
            spread = max_val - min_active
            # Higher spread = more confident (clear distinction between sensors)
            confidence = 0.5 + min(spread, 0.5)
            return confidence

        return 0.5

    def to_dict(self) -> Dict[str, Any]:
        """Serialize detector state."""
        return {
            "thresholds": self.thresholds,
            "hysteresis": self.hysteresis,
            "magnet_strength": self.magnet_strength,
            "active_quadrants": self.get_active_quadrants(),
            "sensor_values": self.sensor_values,
            "position": self.estimate_position().value,
            "touch_zone": self.get_touch_zone().value,
            "confidence": self.get_position_confidence(),
        }


class TouchPositionTracker:
    """
    Tracks touch positions over time with smoothing and state management.

    This class provides higher-level tracking functionality on top of the
    quadrant detector, including position smoothing, state transitions,
    and touch event detection.
    """

    def __init__(
        self,
        detector: QuadrantDetector,
        smoothing_alpha: float = 0.3,
        min_touch_duration_ms: int = 100,
    ):
        """
        Initialize touch position tracker.

        Args:
            detector: QuadrantDetector instance for low-level detection
            smoothing_alpha: EMA smoothing factor (0.0-1.0, higher = less smoothing)
            min_touch_duration_ms: Minimum duration for valid touch event
        """
        self.detector = detector
        self.smoothing_alpha = smoothing_alpha
        self.min_touch_duration_ms = min_touch_duration_ms

        # Tracking state
        self.current_position = QuadrantPosition.NONE
        self.current_zone = TouchZone.NONE
        self.confidence = 0.0

        # Smoothed values
        self.smoothed_position = QuadrantPosition.NONE
        self.smoothed_confidence = 0.0

        # Touch timing
        self.touch_start_time = None
        self.touch_duration_ms = 0
        self.is_valid_touch = False

        # Event detection
        self.position_changed = False
        self.touch_started = False
        self.touch_ended = False

    def update(self, sensor_values: List[float], current_time_ms: int) -> Dict[str, Any]:
        """
        Update tracker with new sensor readings.

        Args:
            sensor_values: Raw sensor readings [s1, s2, s3, s4]
            current_time_ms: Current time in milliseconds

        Returns:
            Dict with tracking state and events
        """
        # Reset event flags
        self.position_changed = False
        self.touch_started = False
        self.touch_ended = False

        # Update detector
        self.detector.update(sensor_values)

        # Get new position
        new_position = self.detector.estimate_position()
        new_zone = self.detector.get_touch_zone()
        new_confidence = self.detector.get_position_confidence()

        # Check for position change
        if new_position != self.current_position:
            self.position_changed = True
            self.current_position = new_position
            self.current_zone = new_zone

        # Update confidence with smoothing
        self.confidence = (self.smoothing_alpha * new_confidence +
                          (1 - self.smoothing_alpha) * self.smoothed_confidence)
        self.smoothed_confidence = self.confidence

        # Touch state management
        was_touching = self.touch_start_time is not None
        is_touching = new_position != QuadrantPosition.NONE

        if is_touching and not was_touching:
            # Touch started
            self.touch_start_time = current_time_ms
            self.touch_started = True
            self.is_valid_touch = False
            logger.debug(f"Touch started at {new_zone.value}")

        elif not is_touching and was_touching:
            # Touch ended
            if self.touch_start_time is not None:
                self.touch_duration_ms = current_time_ms - self.touch_start_time
                self.is_valid_touch = self.touch_duration_ms >= self.min_touch_duration_ms

                if self.is_valid_touch:
                    logger.debug(f"Valid touch ended: duration={self.touch_duration_ms}ms, zone={self.current_zone.value}")
                else:
                    logger.debug(f"Touch too short: duration={self.touch_duration_ms}ms")

            self.touch_start_time = None
            self.touch_ended = True
            self.current_position = QuadrantPosition.NONE
            self.current_zone = TouchZone.NONE

        # Update smoothed position (simple hysteresis)
        if new_position != QuadrantPosition.NONE and self.confidence > 0.6:
            self.smoothed_position = new_position

        return {
            "position": self.current_position.value,
            "zone": self.current_zone.value,
            "confidence": self.confidence,
            "smoothed_position": self.smoothed_position.value,
            "is_touching": is_touching,
            "is_valid_touch": self.is_valid_touch,
            "touch_duration_ms": self.touch_duration_ms,
            "events": {
                "position_changed": self.position_changed,
                "touch_started": self.touch_started,
                "touch_ended": self.touch_ended,
            },
            "raw": {
                "active_quadrants": self.detector.get_active_quadrants(),
                "sensor_values": self.detector.sensor_values,
            }
        }

    def reset(self):
        """Reset tracker state."""
        self.detector.reset()
        self.current_position = QuadrantPosition.NONE
        self.current_zone = TouchZone.NONE
        self.confidence = 0.0
        self.smoothed_position = QuadrantPosition.NONE
        self.smoothed_confidence = 0.0
        self.touch_start_time = None
        self.touch_duration_ms = 0
        self.is_valid_touch = False
        self.position_changed = False
        self.touch_started = False
        self.touch_ended = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize tracker state."""
        return {
            "detector": self.detector.to_dict(),
            "current_position": self.current_position.value,
            "current_zone": self.current_zone.value,
            "confidence": self.confidence,
            "smoothed_position": self.smoothed_position.value,
            "is_touching": self.touch_start_time is not None,
            "is_valid_touch": self.is_valid_touch,
            "touch_duration_ms": self.touch_duration_ms,
        }
