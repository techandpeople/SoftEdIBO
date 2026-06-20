"""TouchGestureClassifier — per-skin-type touch-gesture inference.

Loads a trained model for a given ``skin_type`` (``models/touch_<type>.joblib``)
and predicts a gesture label from a :class:`TouchSegment`. scikit-learn / joblib
are imported **lazily**, so importing this module — and running the app — never
requires them. With no model (or no ML libs) the classifier is inert and returns
``unknown``, so wiring it in is always safe.

A live adapter (:class:`LiveTouchClassifier`) subscribes to a skin's
``on_magnet``, segments the stream, and emits a ``gesture`` event per touch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from src.config.settings import Settings
from src.ml import gesture_taxonomy as tax
from src.ml.touch_features import full_feature_vector
from src.ml.touch_segmenter import TouchSegment, TouchSegmenter

logger = logging.getLogger(__name__)


def model_path(skin_type: str) -> Path:
    """Conventional on-disk path of a skin type's trained model."""
    return Settings.ROOT / "models" / f"touch_{skin_type}.joblib"


class TouchGestureClassifier:
    """Predicts a gesture label for a segment, using a per-type model if present.

    Args:
        skin_type: Selects the model. Empty / unknown type → always ``unknown``.
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
            import joblib  # lazy — only needed when a model actually exists
            self._model = joblib.load(self._path)
            logger.info("Loaded touch model for %s from %s",
                        self.skin_type, self._path)
        except Exception:   # noqa: BLE001 — missing lib / bad file → stay inert
            logger.warning("No usable touch model for %s (%s); classifier inert",
                           self.skin_type, self._path)
            self._model = None

    def predict(self, seg: TouchSegment) -> str:
        """Return a gesture label, or ``unknown`` when no model is loaded."""
        self._ensure_loaded()
        if self._model is None:
            return tax.UNKNOWN
        try:
            pred = self._model.predict(
                [full_feature_vector(seg, self.skin_variant)])[0]
            return str(pred)
        except Exception:   # noqa: BLE001 — never break a session on inference
            logger.exception("Touch inference failed for %s", self.skin_type)
            return tax.UNKNOWN


class LiveTouchClassifier:
    """Subscribes to a skin's magnet stream and emits a gesture per touch.

    Args:
        skin: A ``Skin`` (real or simulated); its ``skin_type`` selects the
            model and its ``touch_controller`` provides ``on_magnet``.
        on_gesture: Called ``(skin_id, label, segment)`` when a touch ends and a
            model classified it. Not called while the classifier is inert.
    """

    def __init__(self, skin: Any,
                 on_gesture: Callable[[str, str, TouchSegment], None]):
        self._skin = skin
        self._on_gesture = on_gesture
        self._clf = TouchGestureClassifier(
            getattr(skin, "skin_type", ""),
            skin_variant=getattr(skin, "skin_variant", ""))
        self._seg = TouchSegmenter()
        self._t0: float | None = None

    def attach(self) -> bool:
        """Subscribe to the skin's magnet controller. Returns False if there is
        nothing to attach to or no model to run (stays inert)."""
        tc = getattr(self._skin, "touch_controller", None)
        on_magnet = getattr(tc, "on_magnet", None) if tc is not None else None
        if on_magnet is None or not self._clf.has_model:
            return False
        on_magnet(self._handle_magnet)
        return True

    def _handle_magnet(self, data: dict) -> None:
        import time
        now_ms = time.monotonic() * 1000.0
        if self._t0 is None:
            self._t0 = now_ms
        seg = self._seg.feed(data, now_ms - self._t0)
        if seg is not None:
            label = self._clf.predict(seg)
            if label != tax.UNKNOWN:
                self._on_gesture(getattr(self._skin, "skin_id", ""), label, seg)
