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
constexpr float P_MIN    = 0.0f;     // kPa (gauge)
constexpr float P_MAX    = 100.0f;   // kPa — sensor full-scale

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
    return (p < 0.0f) ? 0.0f : p;
}

inline float readKpa(int pin) {
    return voltageToPressure(adcToVoltage(readRawAdc(pin)));
}

}  // namespace pressure
