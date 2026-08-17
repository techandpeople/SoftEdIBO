"""Unit tests for src.activities.organ_resolver (per-organ inference)."""

from __future__ import annotations

import math

from src.activities.organ_matching import OrganMatcher
from src.activities.organ_resolver import (
    ABSENT, BAD, GOOD, OrganResolver, OrganSpec,
)


# Three organs whose good/bad parallel combinations are well separated.
ORGANS = [
    OrganSpec("liver", good_ohm=1500, bad_ohm=4700),
    OrganSpec("heart", good_ohm=2200, bad_ohm=5600),
    OrganSpec("lung", good_ohm=3300, bad_ohm=6800),
]


def _parallel(*values: float) -> float:
    return OrganMatcher.parallel_resistance(list(values))


# --- resolve: cover off / empty -------------------------------------------

def test_cover_off_all_absent():
    r = OrganResolver(ORGANS, tolerance_ohm=80)
    assert r.resolve(math.inf) == {"liver": ABSENT, "heart": ABSENT,
                                   "lung": ABSENT}


def test_no_organs_empty():
    assert OrganResolver([], tolerance_ohm=80).resolve(1000) == {}


# --- resolve: all good / all bad ------------------------------------------

def test_all_good():
    total = _parallel(1500, 2200, 3300)
    verdicts = OrganResolver(ORGANS, tolerance_ohm=80).resolve(total)
    assert verdicts == {"liver": GOOD, "heart": GOOD, "lung": GOOD}


def test_all_bad():
    total = _parallel(4700, 5600, 6800)
    verdicts = OrganResolver(ORGANS, tolerance_ohm=80).resolve(total)
    assert verdicts == {"liver": BAD, "heart": BAD, "lung": BAD}


# --- resolve: mixed + partial ---------------------------------------------

def test_mixed_good_bad_absent():
    # liver good, heart bad, lung absent.
    total = _parallel(1500, 5600)
    verdicts = OrganResolver(ORGANS, tolerance_ohm=80).resolve(total)
    assert verdicts == {"liver": GOOD, "heart": BAD, "lung": ABSENT}


def test_single_good_organ():
    total = _parallel(2200)  # only heart, good
    verdicts = OrganResolver(ORGANS, tolerance_ohm=80).resolve(total)
    assert verdicts == {"liver": ABSENT, "heart": GOOD, "lung": ABSENT}


# --- resolve: out of tolerance --------------------------------------------

def test_unmatched_reading_all_absent():
    # A reading nowhere near any combination -> cannot trust -> all absent.
    verdicts = OrganResolver(ORGANS, tolerance_ohm=10).resolve(123.0)
    assert verdicts == {"liver": ABSENT, "heart": ABSENT, "lung": ABSENT}


# --- helpers --------------------------------------------------------------

def test_good_count_and_any_bad():
    verdicts = {"liver": GOOD, "heart": BAD, "lung": GOOD}
    assert OrganResolver.good_count(verdicts) == 2
    assert OrganResolver.any_bad(verdicts) is True
    assert OrganResolver.any_bad({"liver": GOOD}) is False


# --- from_organ_configs ---------------------------------------------------

def test_from_organ_configs():
    cfgs = [
        {"id": "liver", "good_ohm": 1500, "bad_ohm": 4700},
        {"id": "heart", "good_ohm": 2200, "bad_ohm": 5600},
    ]
    r = OrganResolver.from_organ_configs(cfgs, tolerance_ohm=80)
    total = _parallel(1500, 5600)
    assert r.resolve(total) == {"liver": GOOD, "heart": BAD}


def test_from_organ_configs_missing_bad_ignored():
    # An organ with no bad variant only ever resolves good or absent.
    cfgs = [{"id": "x", "good_ohm": 1000, "bad_ohm": 0}]
    r = OrganResolver.from_organ_configs(cfgs, tolerance_ohm=50)
    assert r.resolve(1000) == {"x": GOOD}
    assert r.resolve(math.inf) == {"x": ABSENT}
