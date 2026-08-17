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
// RGBW/RGB LED rings (SK6812 / WS2812). Four independent rings - one 24-LED +
// three 16-LED - each on its OWN data pin, driven as four separate strips (see
// leds.h). The "set_led" command's "ring" field (0..3) selects one.
//
// Pins were chosen by the hardware owner; the multiplexed board is not yet
// soldered. The 16-LED rings are NOT on GPIO34/35/36: those are input-only on
// the classic ESP32 and physically cannot output a NeoPixel data signal.
//
// WARNING - provisional wiring: ring 1 (IO17) and ring 2 (IO16) overlap the
// sensor-mux select lines SMUX_S1 (17) and SMUX_S0 (16) above. While shared,
// the mux select and the LED data line fight over the same pins at runtime, so
// chamber pressure sensing on this board will be wrong until the mux select
// lines are moved to other free GPIOs (the board is unsoldered, so that move is
// still open).
// ---------------------------------------------------------------------------
constexpr int NUM_RINGS = 4;
constexpr int LED_PINS[NUM_RINGS]  = {23, 17, 16, 4};
//                                    24-LED ring  -> IO23
//                                    16-LED ring1 -> IO17  (overlaps SMUX_S1!)
//                                    16-LED ring2 -> IO16  (overlaps SMUX_S0!)
//                                    16-LED ring3 -> IO4
constexpr int RING_LEDS[NUM_RINGS] = {24, 16, 16, 16};
