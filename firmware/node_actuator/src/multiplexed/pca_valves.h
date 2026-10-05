#pragma once
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

#include "pins.h"
#include "dbg.h"

// Two PCA9685 PWM expanders drive 3x ULN2803A -> 24 valve outputs (UNL1..24).
//
// Wiring (verified from netlist):
//   PCA #1 LED0-7  -> U6  -> UNL[i+1]  = pca1.LED[i]  for i = 0..7    (sequential)
//   PCA #1 LED8-15 -> U8  -> UNL[24-i] = pca1.LED[i]  for i = 8..15   (REVERSED)
//   PCA #2 LED0-7  -> U20 -> UNL[24-i] = pca2.LED[i]  for i = 0..7    (REVERSED)
//
// Chambers follow the physical connector layout, not UNL order: each chamber
// owns one neighbouring connector pair (see VALVE_MAP below).

namespace pca_valves {

constexpr int  PCA_FREQ_HZ      = 1000;          // PWM frequency for the chip
constexpr int  I2C_CLOCK        = 400000;        // 400 kHz fast I2C
constexpr uint8_t SCAN_RANGE_LO = 0x40;
constexpr uint8_t SCAN_RANGE_HI = 0x4F;

// Fixed addresses, set by the A0..A5 jumpers: U21 has A0 low (J63 to GND),
// U5 has A0 high (J62 to 3V3). pca1 = U5 (UNL1-16), pca2 = U21 (UNL17-24).
constexpr uint8_t PCA1_ADDR = 0x41;
constexpr uint8_t PCA2_ADDR = 0x40;

inline Adafruit_PWMServoDriver pca1(PCA1_ADDR);
inline Adafruit_PWMServoDriver pca2(PCA2_ADDR);
// Per-chamber valve outputs: {chip (0 = pca1, 1 = pca2), inflate channel,
// deflate channel}. Connector names are the PCB references.
struct ValveOutputs {
    uint8_t chip;
    uint8_t inflate_ch;
    uint8_t deflate_ch;
};

constexpr ValveOutputs VALVE_MAP[] = {
    {0, 12, 14},  // chamber 0:  J17 / J21
    {0,  9, 11},  // chamber 1:  J9  / J14
    {0,  8, 10},  // chamber 2:  J18 / J22
    {0,  2,  0},  // chamber 3:  J3  / J2
    {0,  3,  1},  // chamber 4:  J15 / J19
    {0,  6,  4},  // chamber 5:  J4  / J5
    {0,  7,  5},  // chamber 6:  J16 / J20
    {1,  5,  0},  // chamber 7:  J10 / J52
    {1,  6,  7},  // chamber 8:  J30 / J28
    {1,  1,  4},  // chamber 9:  J44 / J29
    {1,  2,  3},  // chamber 10: J53 / J51
    {0, 13, 15},  // chamber 11: J6  / J7
};
static_assert(sizeof(VALVE_MAP) / sizeof(VALVE_MAP[0]) == MAX_CHAMBERS,
              "VALVE_MAP needs one entry per chamber");

// Every valve output must belong to exactly one chamber.
constexpr bool valveMapIsUnique() {
    for (int a = 0; a < MAX_CHAMBERS * 2; a++) {
        for (int b = a + 1; b < MAX_CHAMBERS * 2; b++) {
            const ValveOutputs& va = VALVE_MAP[a / 2];
            const ValveOutputs& vb = VALVE_MAP[b / 2];
            uint8_t cha = (a % 2) ? va.deflate_ch : va.inflate_ch;
            uint8_t chb = (b % 2) ? vb.deflate_ch : vb.inflate_ch;
            if (va.chip == vb.chip && cha == chb) return false;
        }
    }
    return true;
}
static_assert(valveMapIsUnique(), "VALVE_MAP assigns a valve output twice");

inline uint8_t pca1_addr = 0;
inline uint8_t pca2_addr = 0;
inline bool    initialized = false;

// Software mirror of the actual valve outputs, kept in sync by every write path
// (setChamberValve / closeAllValves). There is no readback from the PCA9685, so
// this is the single source of truth for "is this valve open" - reported in the
// status broadcast so the PC reflects the real valve state. Index: chamber*2 +
// side, side 0 = inflate, 1 = deflate.
inline bool valveOpen[MAX_CHAMBERS * 2] = {};

inline bool isOpen(int chamber, int side) {
    if (chamber < 0 || chamber >= MAX_CHAMBERS || side < 0 || side > 1) return false;
    return valveOpen[chamber * 2 + side];
}

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
    for (int i = 0; i < n; i++) LOG("PCA9685 responder %d at 0x%02X\n", i, addrs[i]);

    bool has1 = false, has2 = false;
    for (int i = 0; i < n; i++) {
        has1 |= addrs[i] == PCA1_ADDR;
        has2 |= addrs[i] == PCA2_ADDR;
    }
    if (!has1 || !has2) {
        LOG("ERROR: PCA9685 missing - need U5 at 0x%02X and U21 at 0x%02X "
            "(check A0..A5 jumpers).\n", PCA1_ADDR, PCA2_ADDR);
        return false;
    }
    pca1_addr = PCA1_ADDR;
    pca2_addr = PCA2_ADDR;

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
// changes - caller is responsible for the settle delay between close-then-open.
inline void setChamberValve(int chamber, bool inflate_open, bool deflate_open) {
    if (!initialized) return;
    DBG_PRINT("VALVE ch=%d inflate=%s deflate=%s\n",
              chamber, inflate_open ? "OPEN" : "close",
              deflate_open ? "OPEN" : "close");
    if (chamber >= 0 && chamber < MAX_CHAMBERS) {
        valveOpen[chamber * 2 + 0] = inflate_open;
        valveOpen[chamber * 2 + 1] = deflate_open;
    }
    if (chamber < 0 || chamber >= MAX_CHAMBERS) return;
    const ValveOutputs& v = VALVE_MAP[chamber];
    Adafruit_PWMServoDriver& chip = v.chip == 0 ? pca1 : pca2;
    setBinary(chip, v.inflate_ch, inflate_open);
    setBinary(chip, v.deflate_ch, deflate_open);
}

inline void closeAllValves() {
    if (!initialized) return;
    for (int ch = 0; ch < 16; ch++) {
        setBinary(pca1, ch, false);
        setBinary(pca2, ch, false);
    }
    for (int i = 0; i < MAX_CHAMBERS * 2; i++) valveOpen[i] = false;
}

}  // namespace pca_valves
