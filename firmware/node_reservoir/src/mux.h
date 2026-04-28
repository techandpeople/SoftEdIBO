#pragma once
#include <Arduino.h>
#include "pins.h"
#include "pressure.h"

// 74HC4067 16-channel analog mux. S0..S3 select the channel; SIG is read by
// a single ADC1 pin (input-only). SMUX_SETTLE_US is conservative (10 µs is
// enough for the chip; we use 50 µs to also let the sensor opamp settle).

namespace mux {

constexpr uint32_t SMUX_SETTLE_US = 50;
constexpr int      MUX_CHANNELS   = 16;

inline void selectChannel(int ch) {
    digitalWrite(SMUX_S0, (ch & 1) ? HIGH : LOW);
    digitalWrite(SMUX_S1, (ch & 2) ? HIGH : LOW);
    digitalWrite(SMUX_S2, (ch & 4) ? HIGH : LOW);
    digitalWrite(SMUX_S3, (ch & 8) ? HIGH : LOW);
    delayMicroseconds(SMUX_SETTLE_US);
}

inline float readKpa(int ch) {
    selectChannel(ch);
    return pressure::voltageToPressure(
        pressure::adcToVoltage(pressure::readRawAdc(SMUX_SIG))
    );
}

inline int readRaw(int ch) {
    selectChannel(ch);
    return pressure::readRawAdc(SMUX_SIG);
}

inline void hardware_init() {
    pinMode(SMUX_S0, OUTPUT);
    pinMode(SMUX_S1, OUTPUT);
    pinMode(SMUX_S2, OUTPUT);
    pinMode(SMUX_S3, OUTPUT);
    digitalWrite(SMUX_S0, LOW);
    digitalWrite(SMUX_S1, LOW);
    digitalWrite(SMUX_S2, LOW);
    digitalWrite(SMUX_S3, LOW);
}

}  // namespace mux
