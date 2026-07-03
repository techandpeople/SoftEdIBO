"""Tests for the skin-condition domain (the skin set an activity targets)."""

from types import SimpleNamespace

from src.activities import skin_condition as sc


def _skin(skin_id, skin_type, skin_variant):
    return SimpleNamespace(skin_id=skin_id, skin_type=skin_type,
                           skin_variant=skin_variant)


def test_conditions_and_labels():
    assert sc.CONDITIONS == ("natural", "wrinkles", "organs")
    assert sc.is_condition("organs") and not sc.is_condition("furry")
    assert sc.label("wrinkles") == "Wrinkles"
    assert sc.label("weird") == "weird"


def test_allowed_variants_natural_and_wrinkles():
    assert sc.allowed_variants("natural", "thymio") == frozenset({"natural"})
    assert sc.allowed_variants("wrinkles", "tree_round") == frozenset({"wrinkles"})


def test_allowed_variants_organs_per_type():
    assert sc.allowed_variants("organs", "thymio") == frozenset(
        {"organ_rectangle", "organ_triangle", "organ_ellipse"})
    assert sc.allowed_variants("organs", "tree_round") == frozenset({"organ"})
    assert sc.allowed_variants("organs", "turtle_square") == frozenset(
        {"three_organ"})
    # Types with no organ cast (e.g. turtle sides) are exempt.
    assert sc.allowed_variants("organs", "turtle_side") is None
    assert sc.allowed_variants("nope", "thymio") is None


def test_skin_mismatches_flags_wrong_variants():
    skins = [
        _skin("square", "turtle_square", "three_organ"),   # ok for organs
        _skin("side-l", "turtle_side", "natural"),         # exempt for organs
        _skin("tri", "turtle_triangle", "wrinkles"),       # exempt for organs
    ]
    assert sc.skin_mismatches("organs", skins) == []
    # Natural condition: the three_organ square is flagged.
    bad = sc.skin_mismatches("natural", skins)
    assert len(bad) == 2                                   # square + tri
    assert any("square" in line for line in bad)


def test_skin_mismatches_reports_missing_variant():
    lines = sc.skin_mismatches("organs", [_skin("s", "thymio", "")])
    assert len(lines) == 1
    assert "no variant set" in lines[0]
