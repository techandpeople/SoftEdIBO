"""Run blocking work off the GUI thread and deliver the result back on it.

Qt slots/callbacks run on the main thread, so any blocking call made from one
(opening a serial port, enumerating ports, training a model, parsing a big
recording) freezes the event loop — the window stops repainting and the cursor
shows the OS "busy" spinner. ``run_async`` moves that call to a worker thread
from the global :class:`QThreadPool` and emits the result via a signal, which
Qt delivers as a queued connection on the GUI thread.

Usage::

    run_async(
        self._gateway.connect,          # runs on a worker thread
        on_done=self._on_connected,     # runs on the GUI thread
        on_error=self._on_connect_failed,
        parent=self,
    )

The signals carrier is parented to ``parent`` so it stays alive until the result
is delivered and is cleaned up with the owning widget.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    done = Signal(object)
    error = Signal(Exception)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _Signals) -> None:
        super().__init__()
        self._fn = fn
        self._signals = signals

    def run(self) -> None:  # noqa: D401 — QRunnable entry point
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the worker
            self._signals.error.emit(exc)
        else:
            self._signals.done.emit(result)


def run_async(
    fn: Callable[[], Any],
    *,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
    parent: QObject | None = None,
) -> _Signals:
    """Run ``fn`` on a worker thread; invoke ``on_done``/``on_error`` on the GUI thread.

    Returns the signals carrier (mostly for tests); callers normally ignore it.
    """
    signals = _Signals(parent)
    if on_done is not None:
        signals.done.connect(on_done)
    if on_error is not None:
        signals.error.connect(on_error)
    QThreadPool.globalInstance().start(_Task(fn, signals))
    return signals
