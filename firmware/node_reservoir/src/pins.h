#pragma once

// ---------------------------------------------------------------------------
// SoftEdIBO node_reservoir — Pin definitions
//
// Up to 12 chambers + shared positive/vacuum reservoir tanks. Sensor reading
// goes through a 74HC4067 16-channel analog mux. Valves are driven by 2×
// PCA9685 I²C PWM expanders feeding 3× ULN2803A Darlington arrays (24 outputs
// total, labelled UNL1..UNL24 on the schematic). Pumps (6×) go through 3×
// DRV3297 motor drivers.
//
// All pin assignments verified from the schematic netlist.
// ---------------------------------------------------------------------------

// 74HC4067 16-channel sensor mux (U22)
constexpr int SMUX_S0  = 16;   // IO16 → 74HC4067 pin 10
constexpr int SMUX_S1  = 17;   // IO17 → pin 11
constexpr int SMUX_S2  = 18;   // IO18 → pin 14
constexpr int SMUX_S3  = 19;   // IO19 → pin 13
constexpr int SMUX_SIG = 39;   // SENSOR_VN → COM (pin 24), ADC1 input-only

// Mux input mapping (verified):
//   I0..I11 → PSENSOR1..PSENSOR12 (chamber sensors)
//   I12..I15 → external connectors J47..J50 (intended for tank sensors + spare)

// I²C bus → 2× PCA9685 PWM expanders → 3× ULN2803A → 24 valve outputs
constexpr int I2C_SDA = 21;
constexpr int I2C_SCL = 22;

// PCA9685 address pins (A0..A5) are FLOATING in the schematic — final
// addresses depend on PCB-level jumpers/solder bridges. Firmware auto-detects
// on boot (I2C scan in 0x40..0x4F range) and uses the two lowest responders
// as PCA #1 (driving UNL1..UNL16 via U6+U8) and PCA #2 (driving UNL17..UNL24
// via U20).

// 3× DRV3297 → 6 pump PWM inputs (verified)
constexpr int PUMP_PINS[6] = {32, 33, 25, 26, 27, 13};
//                           PUMP1=IO32, PUMP2=IO33, PUMP3=IO25,
//                           PUMP4=IO26, PUMP5=IO27, PUMP6=IO13

constexpr int NUM_PUMPS = 6;

// Maximum chambers supported by the hardware (12 sensor channels × 2 valves).
constexpr int MAX_CHAMBERS = 12;
