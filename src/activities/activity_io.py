"""Import / export of authored behaviours as portable JSON files.

The Activity Editor persists behaviours in the local ``declarative_activities``
table, keyed by a DB-local id (``DA001`` ...). To move a behaviour between
machines, back one up, or share it, the editor can write it to a self-contained
``.json`` file and read one back. This module is the pure, GUI-free serialiser
for that file - :mod:`src.gui.behavior_editor_panel` only drives the file
dialogs.

File shape::

    {
      "format": "softedibo.activity",
      "version": 1,
      "name": "Hospital - gentle heartbeat",
      "description": "...",
      "spec": { "initial": ..., "states": {...}, "_blockly": {...} }
    }

The DB-local id and timestamps are deliberately *not* part of the file: an
imported behaviour always becomes a fresh record on the importing machine. The
``spec`` carries the editor's ``_blockly`` workspace blob (ignored by the
runtime/validator) so an imported behaviour round-trips back into editable
blocks.
"""

from __future__ import annotations

import json
from typing import Any

from src.activities.catalog import SpecError, validate_spec

ACTIVITY_FILE_FORMAT = "softedibo.activity"
ACTIVITY_FILE_VERSION = 1


class ActivityFileError(ValueError):
    """Raised when an activity file can't be read as a behaviour."""


def serialize_activity(name: str, description: str,
                       spec: dict[str, Any]) -> str:
    """Return the JSON text for an exportable activity file.

    Validates ``spec`` first so we never write a file that won't re-import.
    Raises :class:`ActivityFileError` if there is no name and propagates
    :class:`~src.activities.catalog.SpecError` for a malformed spec.
    """
    name = (name or "").strip()
    if not name:
        raise ActivityFileError("an activity needs a name to be exported")
    validate_spec(spec)            # raises SpecError on a malformed spec
    payload = {
        "format": ACTIVITY_FILE_FORMAT,
        "version": ACTIVITY_FILE_VERSION,
        "name": name,
        "description": description or "",
        "spec": spec,
    }
    return json.dumps(payload, indent=2)


def deserialize_activity(text: str) -> tuple[str, str, dict[str, Any]]:
    """Parse activity-file ``text`` -> ``(name, description, spec)``.

    Accepts both the wrapped export format above and a bare behaviour spec (a
    dict with ``states``), so hand-written specs import too. Raises
    :class:`ActivityFileError` on anything that isn't a runnable behaviour.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActivityFileError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ActivityFileError("file must contain a JSON object")

    if "spec" in data and "states" not in data:
        # Wrapped export format.
        fmt = data.get("format")
        if fmt not in (None, ACTIVITY_FILE_FORMAT):
            raise ActivityFileError(f"unrecognised file format {fmt!r}")
        name = str(data.get("name") or "").strip()
        description = str(data.get("description") or "")
        spec = data.get("spec")
    else:
        # Bare spec (e.g. hand-authored, or an older export without a wrapper).
        name = str(data.get("name") or "").strip()
        description = str(data.get("description") or "")
        spec = data

    if not isinstance(spec, dict):
        raise ActivityFileError("file has no behaviour spec")
    try:
        validate_spec(spec)
    except SpecError as exc:
        raise ActivityFileError(f"behaviour is not runnable: {exc}") from exc
    return name, description, spec
