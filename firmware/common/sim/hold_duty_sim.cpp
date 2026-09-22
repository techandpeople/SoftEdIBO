// Native simulation of the leak-compensating hold regulator (hold_duty.h)
// against a simple chamber + diaphragm pump plant. Dev-only: checks the maths
// (continuous duty, no valve chatter, convergence) before a board reflash.
//
//   g++ -std=c++17 -O2 -I stub -I .. hold_duty_sim.cpp -o /tmp/hold_sim && /tmp/hold_sim
//
// Plant (up to 3 chambers on one line, either side):
//   dp/dt = (pump delivery / open chambers - loss) / compliance
//   delivery = G * (u - U0)+ while the pump spins (it starts at >= START_U,
//              keeps spinning down to STALL_U), zero otherwise
//   loss     = kClosed * p (valve closed)  |  (kClosed + kOpen) * p (open)
//   reading  = p + head * delivery^2 (flow head on the gauge tee) + noise
//   opening the valve equalises the chamber into the empty manifold
//   (p *= 1 - manifold).
// Every scenario prints: valve toggles after settling, RMS error, max
// overshoot, the duty range - and FAIL when a scenario violates its target.
#include <cstdio>
#include <cstdlib>
#include <cmath>
#include <vector>
#include "hold_duty.h"

uint32_t sim_now_ms = 0;

struct Plant {
    int   n = 1;                    // chambers on the line
    int   dir = 0;                  // 0 pressure side, 1 vacuum side
    float p[3] = {0, 0, 0};
    float u = 0, dlv = 0;           // dlv: delivery after the motor's inertia lag
    bool  open[3] = {false, false, false};
    bool  spinning = false;
    float G = 0.10f, U0 = 60, START_U = 180, STALL_U = 40;
    float kClosed = 0, kOpen = 0.1f, head = 0.01f, manifold = 0.12f, noise = 0.05f;
    float sgn() const { return dir ? -1.0f : 1.0f; }
    int   nOpen() const { int k = 0; for (int i = 0; i < n; i++) if (open[i]) k++; return k; }
    float delivery(int i) const { int k = nOpen(); return (open[i] && k) ? dlv / k : 0; }
    void step(float dtS) {
        if (u <= 0) spinning = false;
        else if (u >= START_U) spinning = true;
        else if (u < STALL_U) spinning = false;
        float want = spinning ? G * (u - U0) : 0;
        if (want < 0) want = 0;
        dlv += (want - dlv) * dtS / 0.06f;    // ~60 ms motor / flow lag
        for (int i = 0; i < n; i++) {
            float loss = kClosed * p[i] + (open[i] ? kOpen * p[i] : 0);   // toward ambient
            p[i] += (sgn() * delivery(i) - loss) * dtS;
            if (sgn() * p[i] < 0) p[i] = 0;
        }
    }
    float reading(int i) const {
        float d = delivery(i);
        float nz = noise * ((rand() % 2001) - 1000) / 1000.0f;
        return p[i] + sgn() * head * d * d + nz;
    }
    void setValve(int i, bool o) {
        if (o && !open[i]) p[i] *= (1 - manifold);
        open[i] = o;
    }
};

struct Scenario {
    const char* name;
    Plant plant;
    float target;
    uint8_t seed;
    float durationS;
    int   maxTogglesAfterSettle;   // -1 = no check
    float maxRms;
};

static bool run(const Scenario& sc) {
    Plant pl = sc.plant;
    const int side = pl.dir;
    const float gaugeFloor = -100.0f;   // a vacuum-capable gauge: every target visible
    for (int i = 0; i < pl.n; i++)
        pl.p[i] = sc.target * 0.92f;    // arrives just under target (the fill's flow head)
    hold_duty::Engine<3> eng;
    eng.ctrlPeriodMs = 20;
    sim_now_ms = 1000;
    for (int i = 0; i < pl.n; i++)
        eng.request(i, (uint8_t)side, sc.target, sc.seed, false, gaugeFloor, [](int, uint8_t) {});
    int toggles = 0, togglesLate = 0;
    float se = 0; int n = 0; float maxOver = 0;
    int minDuty = 255, maxDutyLate = 0; long dutySum = 0; int dutyN = 0;
    const float settleS = 6.0f;
    bool lastOpen[3] = {false, false, false};
    const float dtS = 0.001f;
    for (uint32_t t = 0; t < (uint32_t)(sc.durationS * 1000); t++) {
        sim_now_ms = 1000 + t;
        if (t % 2000 == 0)
            for (int i = 0; i < pl.n; i++)
                eng.request(i, (uint8_t)side, sc.target, 0, false, gaugeFloor, [](int, uint8_t) {});   // keepalive
        hold_duty::Duties d = eng.tick(
            sim_now_ms, false,
            [&](int i, uint8_t) { pl.setValve(i, true); },
            [&](int i, uint8_t) { pl.setValve(i, false); },
            [&](int i) -> float { return pl.reading(i); });
        pl.u = pl.nOpen() ? (side ? d.deflate : d.inflate) : 0;   // board rule: no open valve -> pump off
        pl.step(dtS);
        for (int i = 0; i < pl.n; i++)
            if (pl.open[i] != lastOpen[i]) { toggles++; if (t > settleS * 1000) togglesLate++; lastOpen[i] = pl.open[i]; }
        if (t > settleS * 1000) {
            for (int i = 0; i < pl.n; i++) {
                float e = pl.sgn() * (pl.p[i] - sc.target);
                se += e * e; n++;
                if (e > maxOver) maxOver = e;
            }
            if (pl.u > 0) { if (pl.u < minDuty) minDuty = (int)pl.u; if (pl.u > maxDutyLate) maxDutyLate = (int)pl.u; dutySum += (int)pl.u; dutyN++; }
        }
        if (((t % 500 == 0) || (getenv("FINE") && t < 2500 && t % 40 == 0)) && getenv("TRACE"))
            printf("  t=%5.1f p=%6.2f r=%6.2f u=%3d open=%d ff=%6.1f floor=%3d valid=%d root=%6.1f loss=%.3f\n",
                   t / 1000.0, pl.p[0], pl.reading(0), (int)pl.u, pl.open[0], eng.side[side].ff,
                   eng.side[side].runFloor, eng.side[side].valid, eng.side[side].root, eng.closedLoss(0));
    }
    float rms = n ? sqrtf(se / n) : 0;
    bool ok = rms <= sc.maxRms &&
              (sc.maxTogglesAfterSettle < 0 || togglesLate <= sc.maxTogglesAfterSettle);
    printf("%-34s toggles=%3d late=%3d rms=%.3f over=%.2f duty[%d..%d] mean=%.0f floor=%d  %s\n",
           sc.name, toggles, togglesLate, rms, maxOver,
           dutyN ? minDuty : 0, maxDutyLate, dutyN ? (double)dutySum / dutyN : 0.0,
           eng.side[side].runFloor, ok ? "ok" : "FAIL");
    return ok;
}

int main() {
    srand(1);
    std::vector<Scenario> S;
    Plant base;
    // A: tight chamber (the bench chamber 0): after one top-up it must stay
    //    closed - no pump at all.
    { Plant p = base; p.kClosed = 0.0f; p.kOpen = 0.05f;
      S.push_back({"A tight chamber", p, 11.0f, 0, 30, 0, 0.6f}); }
    // B: small leak, equilibrium above the run floor -> continuous, no toggles.
    { Plant p = base; p.kClosed = 0.02f; p.kOpen = 0.30f;
      S.push_back({"B leaky, u* in range", p, 11.0f, 0, 30, 0, 0.5f}); }
    // C: bigger leak -> continuous at a higher duty.
    { Plant p = base; p.kClosed = 0.05f; p.kOpen = 0.60f;
      S.push_back({"C leakier, u* ~ 130", p, 11.0f, 0, 30, 0, 0.5f}); }
    // D: leak so small the run floor over-delivers -> slow pulses at the
    //    floor (the physical limit), never at the start floor.
    { Plant p = base; p.kClosed = 0.01f; p.kOpen = 0.02f;
      S.push_back({"D leak below floor (pulses)", p, 11.0f, 0, 30, 24, 0.5f}); }
    // E: starving line (huge open loss) -> pump at the rail, no chatter.
    { Plant p = base; p.kClosed = 0.05f; p.kOpen = 3.0f;
      S.push_back({"E starving", p, 11.0f, 0, 20, 0, 99.0f}); }
    // F: calibrated seed near equilibrium.
    { Plant p = base; p.kClosed = 0.02f; p.kOpen = 0.30f;
      S.push_back({"F seeded at 100", p, 11.0f, 100, 20, 0, 0.5f}); }
    // G: pump that really stalls at 110 (above the run floor): the stall
    //    detector must learn the floor up and still hold.
    { Plant p = base; p.kClosed = 0.03f; p.kOpen = 0.40f; p.STALL_U = 110; p.U0 = 90;
      S.push_back({"G stalls under 110", p, 11.0f, 0, 40, -1, 0.8f}); }
    // H: low pressure pose, tight.
    { Plant p = base; p.kClosed = 0.0f; p.kOpen = 0.05f;
      S.push_back({"H tight at 4 kPa", p, 4.0f, 0, 20, 0, 0.5f}); }
    // I: high gain pump (small chamber).
    { Plant p = base; p.G = 0.3f; p.kClosed = 0.03f; p.kOpen = 0.8f; p.head = 0.005f;
      S.push_back({"I small chamber, high gain", p, 11.0f, 0, 30, 0, 0.6f}); }
    // J: two leaky chambers at the same level on one line: both continuous.
    { Plant p = base; p.n = 2; p.kClosed = 0.02f; p.kOpen = 0.30f; p.G = 0.2f;
      S.push_back({"J two chambers, one line", p, 11.0f, 0, 30, 0, 0.5f}); }
    // K: vacuum hold (dir 1) on a gauge that sees it: same regulator, flipped.
    { Plant p = base; p.dir = 1; p.kClosed = 0.02f; p.kOpen = 0.30f;
      S.push_back({"K vacuum hold at -11 kPa", p, -11.0f, 0, 30, 0, 0.5f}); }
    // L: tight vacuum chamber stays closed.
    { Plant p = base; p.dir = 1; p.kClosed = 0.0f; p.kOpen = 0.05f;
      S.push_back({"L tight vacuum", p, -8.0f, 0, 20, 0, 0.5f}); }
    bool all = true;
    for (auto& sc : S) all = run(sc) && all;
    printf("%s\n", all ? "ALL OK" : "SOME FAILED");
    return all ? 0 : 1;
}
