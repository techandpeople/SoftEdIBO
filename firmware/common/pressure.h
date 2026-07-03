#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// XGZP6847A pressure sensor — voltage-to-kPa conversion
// Datasheet transfer function (ratiometric, 3.3 V supply):
//   V_out = V_supply * (0.05 + 0.9 * (P - P_min) / (P_max - P_min))
// Solving for P:
//   P = ((V_out / V_supply) - 0.05) * (P_max - P_min) / 0.9 + P_min
// ---------------------------------------------------------------------------

namespace pressure {

constexpr float V_SUPPLY = 3.3f;

// Sensor range (kPa, gauge). Overridable per build so swapping the 0..100 kPa
// XGZP6847A for the upcoming -40..+40 kPa vacuum-capable variant is just a
// platformio.ini flag (-DSENSOR_KPA_MIN=-40 -DSENSOR_KPA_MAX=40) — everything
// downstream (deflate closed loop below ambient, the PC floor decision) keys off
// these, so no code changes are needed when the new sensors arrive.
#ifndef SENSOR_KPA_MIN
#define SENSOR_KPA_MIN 0.0f
#endif
#ifndef SENSOR_KPA_MAX
#define SENSOR_KPA_MAX 100.0f
#endif
constexpr float P_MIN = SENSOR_KPA_MIN;   // kPa — sensor low end (gauge floor)
constexpr float P_MAX = SENSOR_KPA_MAX;   // kPa — sensor full-scale

// The lowest pressure this sensor can SEE. Anything at/below it reads as the
// floor, so a target below it can only be closed by time, never by pressure.
// Reported to the PC in ready/pong ("kpa_min") so the runtime knows when to
// attach a time budget to a deflate.
constexpr float FLOOR_KPA = P_MIN;

constexpr int   ADC_RESOLUTION = 4095;   // 12-bit ESP32 ADC
constexpr int   ADC_SAMPLES    = 4;      // multi-sample averaging

inline int readRawAdc(int pin) {
    int sum = 0;
    for (int i = 0; i < ADC_SAMPLES; i++) sum += analogRead(pin);
    return sum / ADC_SAMPLES;
}

inline float adcToVoltage(int adc) {
    return static_cast<float>(adc) / ADC_RESOLUTION * V_SUPPLY;
}

inline float voltageToPressure(float voltage) {
    float p = ((voltage / V_SUPPLY) - 0.05f) * (P_MAX - P_MIN) / 0.9f + P_MIN;
    // Clamp to the sensor's PHYSICAL range — never to zero: a vacuum-capable
    // sensor legitimately reads negative gauge pressure.
    if (p < P_MIN) return P_MIN;
    if (p > P_MAX) return P_MAX;
    return p;
}

inline float readKpa(int pin) {
    return voltageToPressure(adcToVoltage(readRawAdc(pin)));
}

}  // namespace pressure
