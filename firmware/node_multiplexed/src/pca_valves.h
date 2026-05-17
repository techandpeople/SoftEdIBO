#pragma once
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

#include "pins.h"
#include "dbg.h"

// Two PCA9685 PWM expanders drive 3× ULN2803A → 24 valve outputs (UNL1..24).
//
// Mapping (verified from netlist):
//   PCA #1 (8 chambers): UNL[i+1] = pca1.LED[i]   for i = 0..15        (sequential)
//   PCA #2 (4 chambers): UNL[24-i] = pca2.LED[i]  for i = 0..7         (REVERSED)
//
// Per-chamber valve assignment:
//   chamber c (0..11): inflate = UNL[c*2 + 1], deflate = UNL[c*2 + 2]
//
// On the firmware's PCA channels:
//   c < 8    → pca1 channels (c*2)   inflate, (c*2 + 1) deflate
//   c >= 8   → pca2 channels (23-2c) inflate, (22-2c)   deflate    (REVERSED)

namespace pca_valves {

constexpr int  PCA_FREQ_HZ      = 1000;          // PWM frequency for the chip
constexpr int  I2C_CLOCK        = 400000;        // 400 kHz fast I2C
constexpr uint8_t SCAN_RANGE_LO = 0x40;
constexpr uint8_t SCAN_RANGE_HI = 0x4F;

inline Adafruit_PWMServoDriver pca1(0x40);  // address replaced after scan
inline Adafruit_PWMServoDriver pca2(0x41);
inline uint8_t pca1_addr = 0;
inline uint8_t pca2_addr = 0;
inline bool    initialized = false;

// I2C scan: returns count of devices found in [SCAN_RANGE_LO, SCAN_RANGE_HI]
// and writes their addresses (sorted ascending) into `out`.
inline int scanI2C(uint8_t out[], int max_out) {
    int found = 0;
    for (uint8_t addr = SCAN_RANGE_LO; addr <= SCAN_RANGE_HI && found < max_out; addr++) {
        Wire.beginTransmission(addr);
        if (Wire.endTransmission() == 0) {
            out[found++] = addr;
        }
    }
    return found;
}

// Setup I2C bus + scan for PCA9685 chips. Returns true if at least 2 distinct
// addresses respond. Caller must check the return and surface an error if false.
inline bool init() {
    Wire.begin(I2C_SDA, I2C_SCL);
    Wire.setClock(I2C_CLOCK);

    uint8_t addrs[16];
    int n = scanI2C(addrs, 16);
    if (n < 2) {
        LOG("ERROR: PCA9685 address conflict — only %d chip(s) found, "
            "need 2 distinct addresses (check A0..A5 pins).\n", n);
        return false;
    }

    pca1_addr = addrs[0];
    pca2_addr = addrs[1];
    LOG("TODO: PCA9685 #1 at 0x%02X, #2 at 0x%02X — confirm against PCB\n",
        pca1_addr, pca2_addr);

    pca1 = Adafruit_PWMServoDriver(pca1_addr);
    pca2 = Adafruit_PWMServoDriver(pca2_addr);
    pca1.begin();
    pca1.setPWMFreq(PCA_FREQ_HZ);
    pca2.begin();
    pca2.setPWMFreq(PCA_FREQ_HZ);

    // Make sure all valves start CLOSED.
    for (int ch = 0; ch < 16; ch++) {
        pca1.setPWM(ch, 0, 4096);
        pca2.setPWM(ch, 0, 4096);
    }

    initialized = true;
    return true;
}

// Drive one channel of a PCA chip fully on or off (binary, no dimming).
inline void setBinary(Adafruit_PWMServoDriver& chip, int ch, bool on) {
    if (on) chip.setPWM(ch, 4096, 0);
    else    chip.setPWM(ch, 0, 4096);
}

// Per-chamber valve control. Closes both before opening one if the side
// changes — caller is responsible for the settle delay between close-then-open.
inline void setChamberValve(int chamber, bool inflate_open, bool deflate_open) {
    if (!initialized) return;
    if (chamber < 8) {
        setBinary(pca1, chamber * 2,     inflate_open);
        setBinary(pca1, chamber * 2 + 1, deflate_open);
    } else {
        // U21 inputs are wired in REVERSED order (see netlist comment above).
        int c = chamber;  // 8..11
        int inf_chan = 23 - 2 * c;       // 7, 5, 3, 1
        int def_chan = 22 - 2 * c;       // 6, 4, 2, 0
        setBinary(pca2, inf_chan, inflate_open);
        setBinary(pca2, def_chan, deflate_open);
    }
}

inline void closeAllValves() {
    if (!initialized) return;
    for (int ch = 0; ch < 16; ch++) {
        setBinary(pca1, ch, false);
        setBinary(pca2, ch, false);
    }
}

}  // namespace pca_valves
