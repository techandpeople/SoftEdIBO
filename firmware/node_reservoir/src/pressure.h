#pragma once
#include <Arduino.h>

// XGZP6847A pressure sensor — voltage-to-kPa conversion.
// Same transfer function as node_direct (kept duplicated to keep firmware
// directories independent of each other).

namespace pressure {

constexpr float V_SUPPLY = 3.3f;
constexpr float P_MIN    = 0.0f;
constexpr float P_MAX    = 100.0f;
constexpr int   ADC_RESOLUTION = 4095;
constexpr int   ADC_SAMPLES    = 4;

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

}  // namespace pressure
