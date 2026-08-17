"""Skin condition - the silicone skin set an activity is written for.

The study dresses each robot in one of three skin conditions, and the activity
to run is picked by the CONDITION, not by the robot - robot-specific parts of
a behaviour live in ``if robot is ...`` blocks inside the spec:

  * ``natural``  - plain smooth skins.
  * ``wrinkles`` - wrinkled skins (the vacuum look).
  * ``organs``   - organ-bearing skins: each Thymio carries one organ shape
    (rectangle / triangle / ellipse spread across the three robots), each Tree
    branch carries one pluggable organ, and the Turtle's central square
    carries three.

An activity declares its condition in its spec ``target`` (``{"skin": ...}``,
see :mod:`src.activities.catalog`). The session setup uses it to pre-select
the matching activity and to warn when a chosen robot's configured skin
variants don't match the condition. Pure data + helpers - Qt-free.
"""

from __future__ import annotations

from typing import Any, Iterable

NATURAL = "natural"
WRINKLES = "wrinkles"
ORGANS = "organs"
CONDITIONS: tuple[str, ...] = (NATURAL, WRINKLES, ORGANS)

_LABELS: dict[str, str] = {
    NATURAL: "Natural", WRINKLES: "Wrinkles", ORGANS: "Organs"}

# Organ-bearing variant(s) each skin TYPE is cast in (mirrors
# skin_geometry.VARIANTS_BY_TYPE). A type absent here has no organ cast -
# e.g. the Turtle sides - so it is EXEMPT from the organs-condition check.
_ORGAN_VARIANTS_BY_TYPE: dict[str, frozenset[str]] = {
    "thymio": frozenset({"organ_rectangle", "organ_triangle", "organ_ellipse"}),
    "tree_round": frozenset({"organ"}),
    "turtle_square": frozenset({"three_organ"}),
}


def is_condition(value: Any) -> bool:
    """Whether ``value`` is a known skin condition."""
    return value in CONDITIONS


def label(condition: Any) -> str:
    """Human label for a condition (the raw value if unknown)."""
    return _LABELS.get(condition, str(condition or ""))


def allowed_variants(condition: Any, skin_type: str | None
                     ) -> frozenset[str] | None:
    """Silicone variants that satisfy ``condition`` for a ``skin_type`` skin.

    ``None`` means the type is exempt (it has no cast matching the condition,
    e.g. Turtle sides during the organs condition), so any variant is fine.
    """
    if condition == ORGANS:
        return _ORGAN_VARIANTS_BY_TYPE.get(skin_type or "")
    if condition in (NATURAL, WRINKLES):
        return frozenset({condition})
    return None


def skin_mismatches(condition: Any, skins: Iterable[Any]) -> list[str]:
    """Human-readable lines for skins whose variant doesn't fit ``condition``.

    ``skins`` are duck-typed objects with ``skin_id`` / ``skin_type`` /
    ``skin_variant`` (a robot's ``skins.values()``). Empty list = all good.
    """
    from src.hardware.skin_geometry import variant_label

    out: list[str] = []
    for skin in skins:
        allowed = allowed_variants(condition, getattr(skin, "skin_type", ""))
        if allowed is None:
            continue
        variant = getattr(skin, "skin_variant", "") or ""
        if variant not in allowed:
            wanted = " / ".join(variant_label(v) for v in sorted(allowed))
            got = variant_label(variant) if variant else "no variant set"
            out.append(f"{getattr(skin, 'skin_id', '?')}: {got} (needs {wanted})")
    return out
