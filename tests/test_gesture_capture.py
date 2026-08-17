"""Tests for GestureCaptureSession - the guided capture state machine.

Drives synthetic magnet streams through the session and checks that prompts
advance per repetition, a compressions bout is captured as one merged gesture,
undo steps the prompt back, and the session goes inert once its plan is done.
Pure Python - no Qt, no sklearn.
"""

from src.ml import gesture_taxonomy as tax
from src.ml.gesture_capture import GestureCaptureSession


def _msg(mag, act):
    return {"type": "magnet", "mag": mag, "act": list(act)}


_HOT = _msg([5.0, 0.0, 0.0, 0.0], [0])
_IDLE = _msg([0.0, 0.0, 0.0, 0.0], [])


class _Driver:
    """Feeds gestures on a monotonic clock; returns the CaptureEvent per gesture.

    A gesture is ``pulses`` quick presses (kept within ``BOUT_GAP_MS`` so the
    merger groups them into one bout) followed by a long idle that flushes the
    merger.
    """

    def __init__(self, session: GestureCaptureSession):
        self.session = session
        self.t = 0.0

    def _feed(self, msg):
        self.t += 50.0
        return self.session.feed(msg, self.t)

    def gesture(self, pulses: int = 1):
        for _ in range(pulses):
            self._feed(_HOT)
            self._feed(_HOT)
            self._feed(_IDLE)        # release (still < gap from the next pulse)
            self.t += 100.0          # short inter-pulse idle -> stays merged
        self.t += tax.BOUT_GAP_MS + 300.0    # long idle past the gap -> flush
        return self.session.feed(_IDLE, self.t)


def test_prompt_advances_per_repetition():
    s = GestureCaptureSession([(tax.TAP, 2), (tax.PRESS, 1)])
    d = _Driver(s)
    assert s.current_gesture == tax.TAP and s.current_total == 2

    ev = d.gesture()
    assert ev is not None and ev.label == tax.TAP and ev.done == 1
    assert s.current_gesture == tax.TAP and s.current_done == 1

    d.gesture()
    assert s.current_gesture == tax.PRESS      # tap quota met -> next gesture
    assert s.current_done == 0

    ev = d.gesture()
    assert ev is not None
    assert ev.label == tax.PRESS
    assert s.is_complete and s.total_captured == 3


def test_compressions_bout_captured_as_one_merged_gesture():
    s = GestureCaptureSession([(tax.COMPRESSIONS, 1)])
    ev = _Driver(s).gesture(pulses=3)
    assert ev is not None and ev.label == tax.COMPRESSIONS
    assert ev.segment.n_pulses == 3            # merged, exactly like the trainer
    assert s.is_complete and s.total_captured == 1


def test_undo_steps_the_prompt_back():
    s = GestureCaptureSession([(tax.TAP, 2)])
    d = _Driver(s)
    d.gesture()
    assert s.current_done == 1 and s.total_captured == 1

    undone = s.undo_last()
    assert undone is not None
    assert s.current_done == 0 and s.total_captured == 0
    assert not s.is_complete

    assert s.undo_last() is None                # nothing left to undo

    d.gesture(); d.gesture()
    assert s.is_complete and s.total_captured == 2


def test_feed_is_noop_once_complete():
    s = GestureCaptureSession([(tax.TAP, 1)])
    d = _Driver(s)
    d.gesture()
    assert s.is_complete and s.total_captured == 1
    assert d.gesture() is None                  # further touches ignored
    assert s.total_captured == 1


def test_empty_or_zero_rep_plan_is_immediately_complete():
    assert GestureCaptureSession([]).is_complete
    assert GestureCaptureSession([(tax.TAP, 0)]).is_complete


def test_labeled_samples_carry_skin_metadata_and_counts():
    s = GestureCaptureSession([(tax.TAP, 1), (tax.PRESS, 1)])
    d = _Driver(s)
    d.gesture(); d.gesture()
    samples = s.labeled_samples("turtle_square", "wrinkles")
    assert len(samples) == 2
    assert [lab for _st, _sv, lab, _seg in samples] == [tax.TAP, tax.PRESS]
    assert all(st == "turtle_square" and sv == "wrinkles"
               for st, sv, _lab, _seg in samples)
    assert s.counts_by_gesture() == {tax.TAP: 1, tax.PRESS: 1}
