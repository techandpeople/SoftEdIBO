// Native check of the coupled_fill deflate TAIL ("vac_ms"): a pull to the
// bottom runs closed-loop down to the gauge floor, then exactly tail_ms blind,
// then closes - and a round cap mid-pull must not restart or skip the tail.
// Dev-only, like hold_duty_sim:
//
//   g++ -std=c++17 -O2 -I stub -I .. vac_tail_sim.cpp -o /tmp/vac_tail && /tmp/vac_tail
#include <cstdio>
#include "coupled_fill.h"

uint32_t sim_now_ms = 0;

namespace {

constexpr float FLOOR = -0.3f;    // gauge floor (P_MIN minus the tare zero)

struct Plant {
    float p = 10.0f;              // chamber kPa (true)
    bool  open = false;
    uint32_t openedMs = 0, closedMs = 0, floorMs = 0;
    float reading() const { return p < FLOOR ? FLOOR : p; }
};

int run(const char* name, float startKpa, uint32_t roundMax, uint32_t tail,
        uint32_t expectMinMs, uint32_t expectMaxMs) {
    Plant pl;
    pl.p = startKpa;
    sim_now_ms = 0;
    coupled_fill::Tuning t{10, 40, 120, 100, roundMax, 30000, 5000, 2, 0.10f, 100.0f};
    coupled_fill::Engine<3> eng;
    eng.begin(1, t);
    eng.count = 1;
    auto ops = coupled_fill::ops(
        [&](int) { return pl.reading(); },
        [&](int, uint8_t) { if (!pl.open) pl.openedMs = sim_now_ms; pl.open = true; },
        [&](int) { if (pl.open) pl.closedMs = sim_now_ms; pl.open = false; },
        [] {}, [](uint8_t, uint16_t) {});
    eng.request(0, FLOOR - 1.0f, 30.0f, 0, false, FLOOR, tail);
    uint32_t openTotal = 0, afterFloor = 0;   // afterFloor: inside the latch band
    for (; sim_now_ms < 30000 && eng.active(); sim_now_ms += 10) {
        eng.tick(sim_now_ms, ops);
        if (pl.open) {
            openTotal += 10;
            pl.p -= 0.02f;        // 2 kPa/s pull
            if (pl.reading() <= FLOOR + coupled_fill::FLOOR_BAND_KPA) afterFloor += 10;
        }
    }
    bool ok = !eng.active() && afterFloor >= expectMinMs && afterFloor <= expectMaxMs;
    printf("%-34s open %5u ms, after floor %5u ms  %s\n", name, (unsigned)openTotal,
           (unsigned)afterFloor, ok ? "ok" : "FAIL");
    return ok ? 0 : 1;
}

}  // namespace

int main() {
    int fails = 0;
    // From 10 kPa (5 s to the floor) with a 1 s tail.
    fails += run("tail 1000 from 10 kPa", 10.0f, 8000, 1000, 950, 1100);
    // Already at ambient: the tail alone.
    fails += run("tail 1000 from ambient", 0.0f, 8000, 1000, 950, 1250);
    // A round cap that ends rounds mid-tail must neither restart nor skip it.
    fails += run("tail 2000, round cap 1500", 1.0f, 1500, 2000, 1950, 2150);
    return fails ? 1 : 0;
}
