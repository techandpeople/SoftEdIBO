#pragma once

// ---------------------------------------------------------------------------
// SoftEdIBO Mux Chamber Node — Pin definitions
// Update these to match your PCB revision.
//
// Valve control: 74HC595 shift-register chain
//   Each chamber uses 2 bits: bit(ch*2)   = inflate valve
//                              bit(ch*2+1) = deflate valve
//   Daisy-chain more 74HC595 chips for more than 4 chambers per chip.
//
// Sensor mux: 74HC4051 8-channel analog multiplexer
//   S0/S1/S2 select the channel; SIG is read by one ADC1 pin.
//   Supports up to 8 chambers. For more, add a second 74HC4051 on a
//   separate ADC1 pin with shared S0/S1/S2.
// ---------------------------------------------------------------------------

// Shift register (74HC595) — valve control
constexpr int SR_DATA_PIN  = 23;   // SER   (data in)
constexpr int SR_CLK_PIN   = 22;   // SRCLK (shift clock)
constexpr int SR_LATCH_PIN = 21;   // RCLK  (storage clock / latch)

// Sensor analog mux (74HC4051) — up to 8 pressure sensors on one ADC pin
constexpr int SMUX_S0_PIN  = 13;   // select bit 0
constexpr int SMUX_S1_PIN  = 12;   // select bit 1
constexpr int SMUX_S2_PIN  = 14;   // select bit 2
constexpr int SMUX_SIG_PIN = 34;   // signal output → ADC1 (WiFi-safe)

// Pumps — DRV8833, one PWM pin per channel
// Not present / not initialised in RESERVOIR_MODE.
#ifndef RESERVOIR_MODE
constexpr int PUMP1_PIN = 25;      // inflate pump (DRV8833 channel A)
constexpr int PUMP2_PIN = 26;      // deflate pump (DRV8833 channel B)
#endif
