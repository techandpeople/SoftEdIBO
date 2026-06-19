"""StreamRecorder — captures all gateway messages of a session to JSONL.

The study records the **raw sensor streams** (not video) so touch/organ data can
be analysed and used to train a touch-gesture model later. This recorder taps
the single firehose — `ESPNowGateway.on_message` — so it captures every node
message (`magnet`, `organ`, `status`, boot announces, …) with a PC-side receive
timestamp, one JSON object per line.

Responsibility is narrow: subscribe, timestamp, write, unsubscribe. It owns the
file and nothing else; persistence policy (when to record, where) is the
SessionPanel's job.

Two gateway contracts matter (`src/hardware/espnow_gateway.py`):
- `on_message` stores a ``weakref.WeakMethod`` of the callback, so we must
  register a **bound method** (`self.handle_message`) and stay alive while
  recording — the owner keeps a reference.
- messages are delivered on the gateway's daemon read thread, so writes are
  guarded by a lock.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class StreamRecorder:
    """Records every gateway message of a session to a JSONL file.

    Args:
        gateway: The shared ``ESPNowGateway`` to tap.
        path: Destination ``.jsonl`` file (parent dirs are created).
        session_id: Stored in the header line for traceability.
    """

    def __init__(self, path: str | Path, session_id: str = "",
                 gateway: Any = None):
        self._gateway = gateway
        self._path = Path(path)
        self._session_id = session_id
        self._lock = threading.Lock()
        self._file = None
        self._count = 0
        # Simulated touch sources (SimulatedMagnetSensor) tapped in addition to
        # the gateway, so recording works in simulation too.
        self._magnet_sources: list[Any] = []

    @property
    def is_recording(self) -> bool:
        return self._file is not None

    @property
    def message_count(self) -> int:
        """Number of messages written so far."""
        return self._count

    def start(self) -> None:
        """Open the file, write the header, and subscribe to the gateway."""
        if self.is_recording:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "w", encoding="utf-8")
        header = {
            "schema": SCHEMA_VERSION,
            "session_id": self._session_id,
            "started": datetime.now().isoformat(timespec="milliseconds"),
        }
        self._file.write(json.dumps(header) + "\n")
        self._file.flush()
        # Bound method so the gateway's WeakMethod stays valid while we're alive.
        if self._gateway is not None:
            self._gateway.on_message(self.handle_message)
        logger.info("StreamRecorder started → %s", self._path)

    def attach_magnet(self, controller: Any) -> None:
        """Also record ``on_magnet`` events from a touch controller — used in
        simulation, where touches come from SimulatedMagnetSensor rather than
        through the gateway. No-op if the controller has no ``on_magnet``."""
        on_magnet = getattr(controller, "on_magnet", None)
        if on_magnet is None:
            return
        on_magnet(self.handle_message)
        self._magnet_sources.append(controller)

    def handle_message(self, data: dict[str, Any]) -> None:
        """Write one message line. Runs on the gateway read thread."""
        with self._lock:
            if self._file is None:
                return
            line = json.dumps({
                "t": datetime.now().isoformat(timespec="milliseconds"),
                "msg": data,
            })
            self._file.write(line + "\n")
            self._count += 1

    def stop(self) -> None:
        """Unsubscribe and close the file. Safe to call more than once."""
        remove = getattr(self._gateway, "remove_message_callback", None)
        if remove is not None:
            remove(self.handle_message)
        with self._lock:
            if self._file is not None:
                self._file.flush()
                self._file.close()
                self._file = None
        logger.info("StreamRecorder stopped (%d messages) → %s",
                    self._count, self._path)
