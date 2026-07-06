"""GestureCaptureSession — guided, prompt-driven collection of labelled touches.

The Train Touch dialog can only build a dataset as a *byproduct* of a study
session (record + tag afterwards). This drives the opposite, deliberate flow: the
app prompts "do gesture X", the user performs it N times, repeat per gesture. The
label is known from the prompt, so no post-hoc tagging is needed.

Mechanically it is the live inference loop of
:class:`~src.ml.touch_classifier.LiveTouchClassifier` with the classifier
replaced by the current prompt: feed the magnet stream through a
:class:`~src.ml.touch_segmenter.TouchSegmenter` and a
:class:`~src.ml.touch_segmenter.PulseMerger`, and each merged gesture that comes
out is captured under the gesture being prompted. Because ``PulseMerger`` groups
a run of presses (a ``compressions`` bout) exactly as the trainer's
``merge_segments`` does — using the same :data:`~src.ml.gesture_taxonomy.BOUT_GAP_MS`
silence window — a captured bout is already one segment, the same thing inference
sees (train/serve parity), with no ``group_id`` bookkeeping.

Pure Python and Qt-free (the dialog owns the gateway subscription and the
GUI-thread hop), so the capture logic is unit-testable on synthetic streams.
Capture on a *resting* skin (no actuation): at rest the raw magnet stream equals
the pressure-compensated stream inference consumes, so there is no skew.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.ml import gesture_taxonomy as tax
from src.ml.touch_segmenter import PulseMerger, TouchSegment, TouchSegmenter


@dataclass
class CaptureEvent:
    """One gesture just captured under the active prompt.

    ``done``/``total`` are the prompt's repetition progress *after* this capture,
    so a UI can render "2 / 5" straight from the event.
    """
    label: str
    segment: TouchSegment
    done: int
    total: int


@dataclass
class _Capture:
    """An accepted sample, tagged with the plan entry it belongs to (for undo)."""
    idx: int
    label: str
    segment: TouchSegment


@dataclass
class GestureCaptureSession:
    """Prompt-driven capture over a fixed ``plan`` of ``(gesture, reps)`` steps.

    Feed the magnet stream in with :meth:`feed`; it returns a :class:`CaptureEvent`
    each time a gesture is completed, advancing the prompt. Read ``current_*`` /
    ``is_complete`` to drive the UI. Entries with ``reps <= 0`` are skipped.
    """

    plan: list[tuple[str, int]]
    gap_ms: float = tax.BOUT_GAP_MS

    _segmenter: TouchSegmenter = field(default_factory=TouchSegmenter, init=False)
    _merger: PulseMerger = field(init=False)
    _counts: list[int] = field(init=False)
    _idx: int = field(init=False)
    captured: list[_Capture] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.plan = [(str(g), int(n)) for g, n in self.plan]
        self._merger = PulseMerger(self.gap_ms)
        self._counts = [0] * len(self.plan)
        self._idx = self._first_incomplete(0)

    # ------------------------------------------------------------------
    # Prompt state
    # ------------------------------------------------------------------

    def _first_incomplete(self, start: int) -> int:
        """Index of the first plan entry still needing reps, else ``len(plan)``."""
        for i in range(max(start, 0), len(self.plan)):
            if self._counts[i] < self.plan[i][1]:
                return i
        return len(self.plan)

    @property
    def is_complete(self) -> bool:
        return self._idx >= len(self.plan)

    @property
    def current_gesture(self) -> str:
        return "" if self.is_complete else self.plan[self._idx][0]

    @property
    def current_total(self) -> int:
        return 0 if self.is_complete else self.plan[self._idx][1]

    @property
    def current_done(self) -> int:
        return 0 if self.is_complete else self._counts[self._idx]

    @property
    def total_captured(self) -> int:
        return len(self.captured)

    def counts_by_gesture(self) -> dict[str, int]:
        """``{gesture: captured_count}`` summed across plan entries (for a tally)."""
        out: dict[str, int] = {}
        for (g, _reps), n in zip(self.plan, self._counts):
            out[g] = out.get(g, 0) + n
        return out

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def feed(self, msg: dict, t_ms: float) -> CaptureEvent | None:
        """Process one ``type:"magnet"`` message at time ``t_ms`` (monotonic ms).

        Returns a :class:`CaptureEvent` when a gesture completes under the active
        prompt, else ``None``. No-op once :attr:`is_complete`.
        """
        if self.is_complete:
            return None
        seg = self._segmenter.feed(msg, t_ms)
        gesture = self._merger.feed(seg, t_ms, self._segmenter.is_active)
        if gesture is None:
            return None
        idx = self._idx
        label = self.plan[idx][0]
        self.captured.append(_Capture(idx, label, gesture))
        self._counts[idx] += 1
        self._idx = self._first_incomplete(idx)
        return CaptureEvent(label=label, segment=gesture,
                            done=self._counts[idx], total=self.plan[idx][1])

    def undo_last(self) -> _Capture | None:
        """Drop the most recent capture and re-prompt its gesture. ``None`` if
        nothing has been captured yet."""
        if not self.captured:
            return None
        c = self.captured.pop()
        self._counts[c.idx] -= 1
        self._idx = c.idx   # that slot is free again — prompt it next
        return c

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def labeled_samples(self, skin_type: str, skin_variant: str = ""
                        ) -> list[tuple[str, str, str, TouchSegment]]:
        """``(skin_type, skin_variant, label, segment)`` per capture, for
        :func:`src.ml.training.train_from_segments`."""
        return [(skin_type, skin_variant, c.label, c.segment)
                for c in self.captured]
