"""Live touch reading: the taxonomy's rules, and a trained model when there is one.

:class:`TouchGestureModel` loads the per-skin-type gesture model
(``models/touch_<type>.joblib``) and names the gesture of a
:class:`TouchSegment`. scikit-learn / joblib are imported **lazily**, so
importing this module - and running the app - never requires them. With no
model (or no ML libs) it names nothing and the rules do.

:class:`LiveTouchClassifier` subscribes to a skin's magnet stream, segments
it, reads each finished touch on the taxonomy's axes (see
:mod:`src.ml.touch_axes`) and emits the gesture. It also measures how much
the skin's chamber pressure rose during the touch, which is what tells a
squeeze from a plain multi-zone press.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from src.config.settings import Settings
from src.hardware.air_chamber import ChamberState
from src.ml import gesture_taxonomy as tax
from src.ml.pressure_rise import PressureRiseMeter
from src.ml.touch_axes import TouchReading, activation_threshold, read_touch
from src.ml.touch_features import full_feature_vector
from src.ml.touch_segmenter import PulseMerger, TouchSegment, TouchSegmenter

logger = logging.getLogger(__name__)

# Chamber states in which the robot itself is changing the pressure.
_DRIVEN_STATES = (ChamberState.INFLATING, ChamberState.DEFLATING)


def model_path(skin_type: str) -> Path:
    """Conventional on-disk path of a skin type's trained model."""
    return Settings.ROOT / "models" / f"touch_{skin_type}.joblib"


class TouchGestureModel:
    """Names a segment's gesture with a per-skin-type model, if present.

    Args:
        skin_type: Selects the model. Empty / unknown type -> never predicts.
        path: Optional explicit model path (defaults to :func:`model_path`).
    """

    def __init__(self, skin_type: str, path: str | Path | None = None,
                 skin_variant: str = ""):
        self.skin_type = skin_type or ""
        self.skin_variant = skin_variant or ""
        self._path = Path(path) if path else model_path(self.skin_type)
        self._model = None
        self._loaded = False

    @property
    def has_model(self) -> bool:
        """True if a trained model is available (lazily loads on first check)."""
        self._ensure_loaded()
        return self._model is not None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.skin_type or not self._path.exists():
            return
        try:
            import joblib  # lazy - only needed when a model actually exists
            from src.ml.training import unpack_model
            self._model = unpack_model(joblib.load(self._path))
        except Exception:   # noqa: BLE001 - missing lib / bad file -> stay inert
            self._model = None
        if self._model is None:
            logger.warning(
                "Touch model for %s (%s) does not fit this version - retrain "
                "it in Tools -> Touch Gestures. Rules are used meanwhile.",
                self.skin_type, self._path)
        else:
            logger.info("Loaded touch gesture model for %s from %s",
                        self.skin_type, self._path)

    def predict(self, seg: TouchSegment) -> str | None:
        """The segment's gesture, or ``None`` when no model is loaded."""
        self._ensure_loaded()
        if self._model is None:
            return None
        try:
            pred = self._model.predict(
                [full_feature_vector(seg, self.skin_variant)])[0]
            return str(pred)
        except Exception:   # noqa: BLE001 - never break a session on inference
            logger.exception("Touch inference failed for %s", self.skin_type)
            return None


class LiveTouchClassifier:
    """Subscribes to a skin's magnet stream and emits a reading per touch.

    Args:
        skin: A ``Skin`` (real or simulated); its ``skin_type`` selects the
            gesture model, its touch config the force scale, and its chambers
            give the pressure rise.
        on_gesture: Called ``(skin_id, gesture, segment, reading)`` when a
            touch ends. ``reading`` is the :class:`TouchReading` (axes and
            descriptors) the gesture was derived from.
    """

    def __init__(self, skin: Any,
                 on_gesture: Callable[[str, str, TouchSegment, TouchReading], None]):
        self._skin = skin
        self._on_gesture = on_gesture
        self._model = TouchGestureModel(
            getattr(skin, "skin_type", ""),
            skin_variant=getattr(skin, "skin_variant", ""))
        self._threshold_ut = activation_threshold(getattr(skin, "touch", None))
        self._seg = TouchSegmenter()
        # Group a run of presses (a compressions bout) into one gesture before
        # reading it - training merges them over the same BOUT_GAP_MS silence
        # window, so inference must too (train/serve parity).
        self._merger = PulseMerger(tax.BOUT_GAP_MS)
        self._pressure = PressureRiseMeter()
        self._t0: float | None = None

    def attach(self) -> bool:
        """Subscribe to the skin's compensated magnet stream. Returns False if
        the skin has no touch stream to read."""
        from src.hardware.touch_source import subscribe_skin_magnet
        return subscribe_skin_magnet(self._skin, self._handle_magnet)

    def detach(self) -> None:
        """Stop consuming the skin's magnet stream (undo :meth:`attach`)."""
        from src.hardware.touch_source import unsubscribe_skin_magnet
        unsubscribe_skin_magnet(self._skin, self._handle_magnet)

    def read(self, gesture: TouchSegment,
             pressure_rise_kpa: float | None = None) -> TouchReading:
        """Read one finished (merged) touch on the taxonomy's axes."""
        return read_touch(gesture, self._threshold_ut,
                          gesture=self._model.predict(gesture),
                          pressure_rise_kpa=pressure_rise_kpa)

    def _resting_pressures(self) -> dict[int, float]:
        """Pressure (kPa) of the chambers the robot is not driving right now.
        A chamber being inflated or deflated changes by itself, so it says
        nothing about the hand on the skin."""
        readings: dict[int, float] = {}
        chambers = getattr(self._skin, "chambers", None) or {}
        for chamber_id, chamber in chambers.items():
            state = getattr(chamber, "state", None)
            kpa = getattr(chamber, "kpa", None)
            if state in _DRIVEN_STATES or not isinstance(kpa, (int, float)):
                continue
            readings[int(chamber_id)] = float(kpa)
        return readings

    def _track_pressure(self) -> None:
        """Follow the chamber pressure from the first press of a bout."""
        if not self._pressure.is_open:
            if self._seg.is_active:
                self._pressure.begin(self._resting_pressures())
            return
        self._pressure.sample(self._resting_pressures())

    def _handle_magnet(self, data: dict) -> None:
        import time
        now_ms = time.monotonic() * 1000.0
        if self._t0 is None:
            self._t0 = now_ms
        t = now_ms - self._t0
        seg = self._seg.feed(data, t)
        self._track_pressure()
        gesture = self._merger.feed(seg, t, self._seg.is_active)
        if gesture is not None:
            reading = self.read(gesture, self._pressure.end())
            if reading.gesture != tax.UNKNOWN:
                self._on_gesture(getattr(self._skin, "skin_id", ""),
                                 reading.gesture, gesture, reading)
