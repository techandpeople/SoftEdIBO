#pragma once

// ---------------------------------------------------------------------------
// SoftEdIBO node_direct — Pin definitions
//
// 3 inflatable air chambers, each with its own inflate/deflate solenoid valve.
// Valves are driven through a ULN2803A Darlington array (logic-level inputs,
// behaves identically to direct GPIO from the firmware's point of view).
// 2 pumps share air across all 3 chambers, driven via a DRV3297 motor driver.
//
// Pin assignments are verified against the schematic netlist.
// ---------------------------------------------------------------------------

// Pressure sensors (XGZP6847A) on input-only ADC pins
constexpr int PSENSOR_PINS[3] = {39, 34, 35};
//                              PSENSOR1=IO39 (SENSOR_VN, J2_4)
//                              PSENSOR2=IO34 (J2_5)
//                              PSENSOR3=IO35 (J2_6)

// Pumps via DRV3297 PWM inputs
constexpr int PUMP_PINS[2] = {32, 33};
//                           PUMP1=IO32 (J2_7), PUMP2=IO33 (J2_8)

// 8 valves wired through ULN2803A (U5). Only 6 are used (3 chambers × 2 valves).
// Mapping: chamber i → inflate=VALVE_PINS[i*2], deflate=VALVE_PINS[i*2+1]
constexpr int VALVE_PINS[6] = {25, 4, 16, 17, 18, 19};
//                             VALVE1=IO25 (ch0 inf, J2_9)
//                             VALVE2=IO4  (ch0 def, J3_13)
//                             VALVE3=IO16 (ch1 inf, J3_12)
//                             VALVE4=IO17 (ch1 def, J3_11)
//                             VALVE5=IO18 (ch2 inf, J3_9)
//                             VALVE6=IO19 (ch2 def, J3_8)
// VALVE7 (IO26) and VALVE8 (IO27) are spare on the PCB and unused by firmware.

constexpr int NUM_CHAMBERS = 3;
