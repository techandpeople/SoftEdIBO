#pragma once

// ---------------------------------------------------------------------------
// SoftEdIBO Reservoir Node — Pin definitions
// Update these to match your PCB revision.
//
// This node manages a single central air reservoir (pressure or vacuum).
// It drives up to two pump pairs and monitors one pressure sensor.
//
// Pumps are driven via DRV8833 H-bridge, one PWM pin per channel.
// ---------------------------------------------------------------------------

// Inflate pump — DRV8833 channel A (PWM)
constexpr int PUMP_INF_PIN = 25;

// Deflate pump — DRV8833 channel B (PWM)
constexpr int PUMP_DEF_PIN = 26;

// Pressure sensor — XGZP6847A analog output (ADC1, input-only pin)
constexpr int SENSOR_PIN = 34;
