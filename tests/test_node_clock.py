"""NodeClock: node sample stamps -> PC time, and dropped-frame counting."""

import pytest

from src.hardware.node_clock import NodeClock


def test_sample_time_ignores_transport_jitter():
    clock = NodeClock()
    offset = 50_000.0                     # PC clock - node clock
    latencies = [12.0, 3.0, 40.0, 3.0, 25.0, 60.0]
    placed = [clock.observe(node_ms, node_ms + offset + latency)
              for node_ms, latency in zip(range(0, 600, 100), latencies)]
    # Once a fast frame (3 ms) was seen, samples sit 100 ms apart regardless
    # of how late each frame arrived.
    steps = [b - a for a, b in zip(placed[1:], placed[2:])]
    assert steps == pytest.approx([100.0] * 4)
    assert placed[1] == pytest.approx(100.0 + offset + 3.0)


def test_sample_is_never_placed_after_its_arrival():
    clock = NodeClock()
    clock.observe(0.0, 1000.0)
    assert clock.observe(100.0, 1050.0) == pytest.approx(1050.0)


def test_counter_gaps_count_lost_frames():
    clock = NodeClock()
    for count in (0, 1, 2, 5, 6, 9):
        clock.observe(count * 100.0, count * 100.0 + 10.0, count)
    assert clock.frames_lost == 4


def test_node_reboot_restarts_the_time_base_without_counting_a_gap():
    clock = NodeClock()
    clock.observe(900_000.0, 1_000_000.0, 500)
    placed = clock.observe(100.0, 1_005_000.0, 0)     # millis() restarted
    assert placed == pytest.approx(1_005_000.0)
    assert clock.frames_lost == 0


def test_old_offsets_are_forgotten_so_clock_drift_is_followed():
    clock = NodeClock(window=3)
    clock.observe(0.0, 10.0)               # offset 10
    for node_ms in (100.0, 200.0, 300.0):
        placed = clock.observe(node_ms, node_ms + 30.0)   # offset now 30
    assert placed == pytest.approx(330.0)
