#pragma once

// ---------------------------------------------------------------------------
// SoftEdIBO Multiplexed Reservoir Node — Pin definitions
// Update these to match your PCB revision.
//
// This node manages NUM_TANKS independent air tanks (e.g. pressure + vacuum).
// Each tank i has:
//   - Inflate pump relay: bit(i*2)   on the 74HC595 shift register
//   - Deflate pump relay: bit(i*2+1) on the 74HC595 shift register
//   - Pressure sensor:    channel i  on the 74HC4051 analog mux
//
// Pump relays are solid-state or mechanical relays driven from the shift
// register outputs (logic-level on/off). For PWM pump control, use the
// node_reservoir firmware (direct GPIO + LEDC) instead.
//
// Sensor mux: 74HC4051 — S0/S1/S2 select channel, SIG read via one ADC1 pin.
// Shift register: 74HC595 chain — relay bits for all tanks.
// ---------------------------------------------------------------------------

// Shift register (74HC595) — pump relay control
constexpr int SR_DATA_PIN  = 23;   // SER   (data in)
constexpr int SR_CLK_PIN   = 22;   // SRCLK (shift clock)
constexpr int SR_LATCH_PIN = 21;   // RCLK  (storage clock / latch)

// Sensor analog mux (74HC4051) — one pressure sensor per tank
constexpr int SMUX_S0_PIN  = 13;   // select bit 0
constexpr int SMUX_S1_PIN  = 12;   // select bit 1
constexpr int SMUX_S2_PIN  = 14;   // select bit 2
constexpr int SMUX_SIG_PIN = 34;   // signal output → ADC1 (WiFi-safe)
