#pragma once

// ---------------------------------------------------------------------------
// SoftEdIBO Air Chamber Node — Pin definitions
// Update these to match your PCB revision.
// ---------------------------------------------------------------------------

// Valves: [chamber][0] = inflate, [chamber][1] = deflate
// HIGH = valve open, LOW = valve closed
constexpr int VALVE_PINS[3][2] = {
    {22, 23},   // chamber 0: inflate, deflate
    {21, 13},   // chamber 1
    {14, 33},   // chamber 2
};

// Pumps (DRV8833 — single PWM pin per channel, other pin tied on PCB)
constexpr int PUMP1_PIN = 25;   // inflate pump  (DRV8833 channel A)
constexpr int PUMP2_PIN = 26;   // deflate pump  (DRV8833 channel B)

// Pressure sensors — XGZP6847A analog output (ADC1 pins only)
constexpr int PSENSOR_PINS[3] = {34, 35, 32};
