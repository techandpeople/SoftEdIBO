"""TouchSegmenter — turns a magnet stream into discrete touch segments.

A touch *segment* is the window from the first sensor becoming active until all
sensors go inactive again — the same press→release edge convention the live
``TouchEventRouter.handle_magnet`` uses (``act`` set going non-empty → empty).
Each segment keeps the per-sample ``mag`` vectors, the ``act`` sets and the
timestamps, so feature extraction (``touch_features``) can work offline from a
recording or live.

Geometry-agnostic and dependency-free: it only needs ``mag``/``act`` and a
timestamp per message; it never assumes a sensor count or a layout.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TouchSegment:
    """One press→release touch.

    Attributes:
        start_ms / end_ms: segment bounds (ms, from the message timestamps).
        mags: per-sample list of per-sensor magnitudes (``mag`` vectors).
        acts: per-sample set of active sensor indices.
        times_ms: timestamp of each sample (ms).
        n_pulses: how many press→release touches this segment represents. 1 for a
            plain segment; >1 when several touches were merged into one logical
            gesture (e.g. a double/triple tap — see :func:`merge_segments`).
    """
    start_ms: float
    end_ms: float
    mags: list[list[float]] = field(default_factory=list)
    acts: list[set[int]] = field(default_factory=list)
    times_ms: list[float] = field(default_factory=list)
    n_pulses: int = 1

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms

    @property
    def sensor_count(self) -> int:
        return max((len(m) for m in self.mags), default=0)


def merge_segments(segments) -> TouchSegment | None:
    """Merge several touch segments into one logical gesture (e.g. a multi-tap).

    Samples are concatenated in start-time order; ``n_pulses`` accumulates so the
    feature pipeline can tell a triple-tap from a single long touch. The inactive
    gaps between the merged touches are not materialised as samples (the
    segmenter only records while active) — ``n_pulses`` carries that information
    instead. Returns ``None`` if ``segments`` is empty."""
    segs = sorted((s for s in segments if s is not None),
                  key=lambda s: s.start_ms)
    if not segs:
        return None
    merged = TouchSegment(start_ms=segs[0].start_ms, end_ms=segs[-1].end_ms)
    for s in segs:
        merged.mags.extend(s.mags)
        merged.acts.extend(s.acts)
        merged.times_ms.extend(s.times_ms)
    merged.n_pulses = sum(s.n_pulses for s in segs)
    return merged


class TouchSegmenter:
    """Feeds magnet samples in, emits :class:`TouchSegment` on release.

    Use :meth:`feed` per message (live or replay); a segment is returned when a
    touch ends. :meth:`segment_stream` is a convenience for a full recording.
    """

    def __init__(self) -> None:
        self._active = False
        self._cur: TouchSegment | None = None
        self._last_active: set[int] = set()

    @staticmethod
    def _act_set(msg: dict) -> set[int]:
        raw = msg.get("act") or []
        out: set[int] = set()
        for v in raw:
            try:
                out.add(int(v))
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _mag_vec(msg: dict) -> list[float]:
        raw = msg.get("mag") or []
        out: list[float] = []
        for v in raw:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                out.append(0.0)
        return out

    def feed(self, msg: dict, t_ms: float) -> TouchSegment | None:
        """Process one ``magnet`` message at time ``t_ms``.

        Returns a finished :class:`TouchSegment` when this message ends a touch
        (act set becomes empty after being non-empty), else None."""
        act = self._act_set(msg)

        if act and not self._active:                 # touch begins
            self._active = True
            self._cur = TouchSegment(start_ms=t_ms, end_ms=t_ms)

        if self._active and self._cur is not None:
            self._cur.mags.append(self._mag_vec(msg))
            self._cur.acts.append(act)
            self._cur.times_ms.append(t_ms)
            self._cur.end_ms = t_ms

        if not act and self._active:                 # touch ends
            self._active = False
            seg, self._cur = self._cur, None
            return seg
        return None

    def segment_stream(self, samples) -> list[TouchSegment]:
        """Segment a whole stream of ``(msg, t_ms)`` pairs. A touch still open
        at the end is flushed as a final segment."""
        out: list[TouchSegment] = []
        for msg, t_ms in samples:
            seg = self.feed(msg, t_ms)
            if seg is not None:
                out.append(seg)
        if self._active and self._cur is not None:   # flush trailing touch
            out.append(self._cur)
            self._active = False
            self._cur = None
        return out
