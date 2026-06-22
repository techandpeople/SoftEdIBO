"""Diagnostic watchdog for GUI-thread stalls.

When the Qt event loop is blocked (a synchronous serial open, a slow query, a
layout feedback loop, a repaint storm…) the window stops repainting and the OS
shows the "busy" cursor. This watchdog pinpoints *what* is blocking it.

How it works: a QTimer on the GUI thread updates a heartbeat every ``poll_ms``.
A separate daemon thread checks that heartbeat; if it goes stale for longer than
``stall_ms`` the GUI thread is stuck, so the watchdog dumps the traceback of
every thread (via :mod:`faulthandler`) — including the main thread, showing the
exact line it's stuck on.

Off by default. Enable by launching with ``SOFTEDIBO_WATCHDOG=1`` (optionally
``SOFTEDIBO_WATCHDOG_MS=<stall threshold>``). Output goes to stderr.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time

from PySide6.QtCore import QTimer


def install_loop_watchdog(
    app,
    *,
    stall_ms: int = 400,
    poll_ms: int = 100,
) -> None:
    """Install the GUI-thread stall watchdog on ``app`` (no-op unless enabled).

    Enabled when the ``SOFTEDIBO_WATCHDOG`` environment variable is truthy.
    ``SOFTEDIBO_WATCHDOG_MS`` overrides ``stall_ms`` if set.
    """
    if os.environ.get("SOFTEDIBO_WATCHDOG", "").lower() not in ("1", "true", "yes", "on"):
        return

    stall_ms = int(os.environ.get("SOFTEDIBO_WATCHDOG_MS", stall_ms))
    stall_s = stall_ms / 1000.0

    # Heartbeat shared between the GUI thread (writer) and the watchdog (reader).
    # A plain assignment is atomic enough for this purpose. Starts as None so the
    # watchdog ignores the one-time startup window (MainWindow construction runs
    # before app.exec(), i.e. before the event loop — and the heartbeat — start);
    # measuring only begins once the first tick fires inside the running loop.
    heartbeat = {"t": None}

    beat_timer = QTimer(app)
    beat_timer.setInterval(poll_ms)
    beat_timer.timeout.connect(lambda: heartbeat.__setitem__("t", time.monotonic()))
    beat_timer.start()

    def _watch() -> None:
        reported = False
        while True:
            time.sleep(poll_ms / 2000.0)
            last = heartbeat["t"]
            if last is None:
                continue  # event loop not running yet — don't measure startup
            stalled_for = time.monotonic() - last
            if stalled_for > stall_s:
                if not reported:
                    print(
                        f"\n=== GUI-THREAD STALL: event loop blocked "
                        f"{stalled_for * 1000:.0f} ms (threshold {stall_ms} ms) ===",
                        file=sys.stderr,
                        flush=True,
                    )
                    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                    print("=== end stall dump ===\n", file=sys.stderr, flush=True)
                    reported = True
            else:
                reported = False  # loop recovered — re-arm for the next stall

    threading.Thread(target=_watch, name="loop-watchdog", daemon=True).start()
    print(
        f"[loop-watchdog] enabled (stall threshold {stall_ms} ms) — "
        "stack dumps go to stderr",
        file=sys.stderr,
        flush=True,
    )
