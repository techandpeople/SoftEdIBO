#pragma once

// ---------------------------------------------------------------------------
// SoftEdIBO node_multiplexed - Pin definitions
//
// Up to 12 chambers, with optional shared positive/vacuum reservoir tanks.
// Sensor reading
// goes through a 74HC4067 16-channel analog mux. Valves are driven by 2x
// PCA9685 I^2C PWM expanders feeding 3x ULN2803A Darlington arrays (24 outputs
// total, labelled UNL1..UNL24 on the schematic). Pumps (6x) go through 3x
// DRV3297 motor drivers.
//
// All pin assignments verified from the schematic netlist.
// ---------------------------------------------------------------------------

// 74HC4067 16-channel sensor mux (U22)
constexpr int SMUX_S0  = 16;   // IO16 -> 74HC4067 pin 10
constexpr int SMUX_S1  = 17;   // IO17 -> pin 11
constexpr int SMUX_S2  = 18;   // IO18 -> pin 14
constexpr int SMUX_S3  = 19;   // IO19 -> pin 13
constexpr int SMUX_SIG = 39;   // SENSOR_VN -> COM (pin 24), ADC1 input-only

// Mux input mapping (verified):
//   I0..I11 -> PSENSOR1..PSENSOR12 (chamber sensors)
//   I12..I15 -> external connectors J47..J50 (intended for tank sensors + spare)

// I^2C bus -> 2x PCA9685 PWM expanders -> 3x ULN2803A -> 24 valve outputs
constexpr int I2C_SDA = 21;
constexpr int I2C_SCL = 22;

// PCA9685 address pins (A0..A5) are FLOATING in the schematic - final
// addresses depend on PCB-level jumpers/solder bridges. Firmware auto-detects
// on boot (I2C scan in 0x40..0x4F range) and uses the two lowest responders
// as PCA #1 (driving UNL1..UNL16 via U6+U8) and PCA #2 (driving UNL17..UNL24
// via U20).

// 3x DRV3297 -> 6 pump PWM inputs (verified)
constexpr int PUMP_PINS[6] = {32, 33, 25, 26, 27, 13};
//                           PUMP1=IO32, PUMP2=IO33, PUMP3=IO25,
//                           PUMP4=IO26, PUMP5=IO27, PUMP6=IO13

constexpr int NUM_PUMPS = 6;

// Maximum chambers supported by the hardware (12 sensor channels x 2 valves).
constexpr int MAX_CHAMBERS = 12;

// ---------------------------------------------------------------------------
// RGBW/RGB LED rings (SK6812 / WS2812). Up to three independent 24-LED rings -
// each on its OWN data pin, driven as separate strips (see leds.h). The
// "set_led" command's "ring" field (0..2) selects one.
//
// The Turtle and Tree PCBs are identical builds of this board; they differ only
// in how many rings are populated: Turtle solders 1 (ring 0), Tree solders 3
// (one per branch skin). The firmware always defines all three - an unpopulated
// ring's data pin just drives nothing - and the PC only exposes the rings the
// robot's config declares.
//
// TODO(hardware): pin map is provisional - the final Turtle/Tree PCB routing is
// not decided yet. The old map put rings on IO17/IO16, which overlap the
// sensor-mux select lines SMUX_S1 (17) and SMUX_S0 (16) above; ring 1 below
// still does, so pressure sensing fights ring 1's data line until the PCB
// assigns free GPIOs. Rings are NOT on GPIO34/35/36: those are input-only on
// the classic ESP32 and physically cannot output a NeoPixel data signal.
// ---------------------------------------------------------------------------
constexpr int NUM_RINGS = 3;
constexpr int LED_PINS[NUM_RINGS]  = {23, 17, 4};
//                                    ring 0 -> IO23 (the only ring on Turtle)
//                                    ring 1 -> IO17 (overlaps SMUX_S1! provisional)
//                                    ring 2 -> IO4
constexpr int RING_LEDS[NUM_RINGS] = {24, 24, 24};
