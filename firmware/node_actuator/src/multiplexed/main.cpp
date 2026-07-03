#include <Arduino.h>
#include <ArduinoJson.h>
#include <esp_ota_ops.h>

#include "se_espnow.h"
#include "se_ota.h"
#include "chambers.h"
#include "cmd_queue.h"
#include "config.h"
#include "dbg.h"
#include "leds.h"
#include "mux.h"
#include "organ.h"
#include "pca_valves.h"
#include "pins.h"
#include "pumps.h"
#include "units.h"

namespace {

constexpr uint32_t PRESSURE_CHECK_MS = 200;
constexpr uint32_t STATUS_REPORT_MS  = 500;

// The closed-loop chamber cutoff runs MUCH tighter than tank control / telemetry.
// A chamber's inflate pump runs at full duty the whole time it is INFLATING, so
// the achieved pressure overshoots the target by however much is delivered between
// two checks: at 200 ms a single "+" step (e.g. +10 % of range) blew past to ~30 %
// before the cutoff looked at the sensor. 20 ms shrinks that window ~10x so the
// measured level settles on the commanded target (mirrors node_direct). Tank
// control stays on the slow PRESSURE_CHECK_MS cadence — its pumps must not toggle
// fast, and the slow mux scan is fine for them.
constexpr uint32_t CHAMBER_CHECK_MS  = 20;

uint32_t lastPressureMs = 0;
uint32_t lastChamberMs  = 0;
uint32_t lastStatusMs = 0;

// Gateway MAC tracking lives in the shared ESP-NOW layer.
using se::node::gatewayMac;
using se::node::gatewayKnown;
bool configured = false;

// Emergency stop: when latched, loop() forces pumps off + valves closed and
// skips all autonomous control until a "resume" command clears it.
bool emergencyStopped = false;

// Organ channels arrive inside `configure` but are applied from loop() (not
// the ESP-NOW receive task) so organ::tick never races a reconfiguration.
int pendingOrganCh[organ::MAX_ORGANS] = {};
volatile int pendingOrganCount = -1;   // -1 = nothing pending

void sendRaw(const char* payload) {
    if (!gatewayKnown) return;
    esp_now_send(gatewayMac, reinterpret_cast<const uint8_t*>(payload), strlen(payload));
}

void sendError(const char* reason) {
    char buf[96];
    int len = snprintf(buf, sizeof(buf), "{\"type\":\"error\",\"reason\":\"%s\"}", reason);
    if (gatewayKnown) {
        esp_now_send(gatewayMac, reinterpret_cast<const uint8_t*>(buf), len);
    }
}

void sendPong() {
    // "kpa_min" self-reports the gauge floor (sensor low end) so the PC knows
    // below which pressure a deflate needs a time budget instead of the sensor.
    char pong[96];
    snprintf(pong, sizeof(pong),
             "{\"type\":\"pong\",\"rgbw\":" LED_RGBW_JSON ",\"kpa_min\":%.0f}",
             (double)pressure::FLOOR_KPA);
    sendRaw(pong);
}

void sendStatus(int chamber, float kpa) {
    if (!gatewayKnown) return;
    auto& ch = chambers::state[chamber];
    int pct = units::kpaToPct(kpa, ch.min_kpa, ch.max_kpa);
    // "st" is the real actuation state (0 idle, 1 inflating, 2 deflating) so the
    // PC reflects whether a chamber is actually being driven rather than
    // inferring it from pressure-vs-target (which never settles with pumps off).
    // "vi"/"vd" are the ACTUAL inflate/deflate valve outputs (pca_valves mirror),
    // so the PC shows a valve as open whoever opened it — manual toggle, an
    // inflate/deflate, or the firmware closing it again.
    char buf[96];
    int len = snprintf(buf, sizeof(buf),
                       "{\"type\":\"status\",\"chamber\":%d,\"pressure\":%d,\"kpa\":%.2f,\"st\":%d,\"vi\":%d,\"vd\":%d}",
                       chamber, pct, kpa, (int)ch.state,
                       pca_valves::isOpen(chamber, 0) ? 1 : 0,
                       pca_valves::isOpen(chamber, 1) ? 1 : 0);
    esp_now_send(gatewayMac, reinterpret_cast<const uint8_t*>(buf), len);
}

#ifdef DEBUG_BUILD
// Which valves are open the instant an actuation command lands (the info the user
// wants kept), streamed over ESP-NOW so it lands in the PC log without a cable.
void sendRxOpen(const char* cmd, int chamber) {
    if (!gatewayKnown) return;
    uint16_t inf = 0, def = 0;
    for (int i = 0; i < MAX_CHAMBERS; i++) {
        if (pca_valves::isOpen(i, 0)) inf |= (uint16_t)(1u << i);
        if (pca_valves::isOpen(i, 1)) def |= (uint16_t)(1u << i);
    }
    char buf[112];
    int len = snprintf(buf, sizeof(buf),
        "{\"type\":\"dbg\",\"ev\":\"rx\",\"cmd\":\"%s\",\"ch\":%d,\"open_inf\":%d,\"open_def\":%d}",
        cmd, chamber, inf, def);
    esp_now_send(gatewayMac, reinterpret_cast<const uint8_t*>(buf), len);
}

// Engine round/measure trace (coupled_fill::Event), registered as chambers::dbgHook.
void sendEngEvent(uint8_t ev, uint16_t mask, uint8_t dir) {
    if (!gatewayKnown) return;
    char buf[80];
    int len = snprintf(buf, sizeof(buf),
        "{\"type\":\"dbg\",\"ev\":\"eng\",\"dir\":%d,\"code\":%d,\"mask\":%d}", dir, ev, mask);
    esp_now_send(gatewayMac, reinterpret_cast<const uint8_t*>(buf), len);
}

// Pump-vs-valve sanity: report a pump role running with NO open valve (dead-head,
// the "running dry / forcing" failure) OR more pumps running than the open valves
// should need (forcing the manifold). recalcPumps() should make both impossible —
// this only fires on a regression.
void checkDryPumps() {
    if (!gatewayKnown) return;
    int openInf = 0, openDef = 0;
    for (int i = 0; i < MAX_CHAMBERS; i++) {
        if (pca_valves::isOpen(i, 0)) openInf++;
        if (pca_valves::isOpen(i, 1)) openDef++;
    }
    int runP = 0, runV = 0;
    for (int i = 0; i < NUM_PUMPS; i++) {
        if (ledcRead(i) == 0) continue;
        if (pumps::roles[i] == pumps::ROLE_PRESSURE) runP++;
        else if (pumps::roles[i] == pumps::ROLE_VACUUM) runV++;
    }
    const int VPP = chambers::VALVES_PER_PUMP;
    int wantP = openInf > 0 ? (openInf + VPP - 1) / VPP : 0;
    int wantV = openDef > 0 ? (openDef + VPP - 1) / VPP : 0;
    if (runP <= wantP && runV <= wantV) return;
    char buf[136];
    int len = snprintf(buf, sizeof(buf),
        "{\"type\":\"dbg\",\"ev\":\"dry\",\"runP\":%d,\"wantP\":%d,\"openInf\":%d,\"runV\":%d,\"wantV\":%d,\"openDef\":%d}",
        runP, wantP, openInf, runV, wantV, openDef);
    esp_now_send(gatewayMac, reinterpret_cast<const uint8_t*>(buf), len);
}

void installDebugHook() { chambers::dbgHook = &sendEngEvent; }
#endif

bool isDisconnectedRail(int raw) {
    return raw < 40 || raw > 4055;
}

void detectSensors(int valid_channels[], int& valid_count, int tank_candidates[], int& tank_count) {
    valid_count = 0;
    tank_count = 0;

    for (int ch = 0; ch < mux::MUX_CHANNELS; ch++) {
        int raw = mux::readRaw(ch);
        if (isDisconnectedRail(raw)) {
            continue;
        }

        float r0 = mux::readKpa(ch);
        float r1 = mux::readKpa(ch);
        float r2 = mux::readKpa(ch);
        float lo = min(r0, min(r1, r2));
        float hi = max(r0, max(r1, r2));
        if ((hi - lo) > 1.0f) {
            continue;
        }

        valid_channels[valid_count++] = ch;
    }

    config::state.num_chambers = min(valid_count, MAX_CHAMBERS);
    for (int i = 0; i < config::state.num_chambers; i++) {
        config::state.chamber_mux_ch[i] = valid_channels[i];
    }
    for (int i = config::state.num_chambers; i < MAX_CHAMBERS; i++) {
        config::state.chamber_mux_ch[i] = -1;
    }

    for (int i = config::state.num_chambers; i < valid_count; i++) {
        tank_candidates[tank_count++] = valid_channels[i];
    }

    char channels[96] = {0};
    int pos = 0;
    for (int i = 0; i < valid_count && pos < static_cast<int>(sizeof(channels)) - 8; i++) {
        pos += snprintf(channels + pos, sizeof(channels) - pos, "%s%d", i == 0 ? "" : ",", valid_channels[i]);
    }
    LOG("TODO: valid sensors detected at mux channels: %s — confirm\n", channels);
}

// No reservoir tanks: the pumps push directly into the chambers, so their role
// (pressure = inflate, vacuum = deflate) can't be discovered by watching a tank
// sensor. Split the pumps evenly by default — first half PRESSURE, second half
// VACUUM (e.g. 3 + 3 of NUM_PUMPS = 6) — which the gateway's `configure`
// (pump_groups / pump_inflate_count / pump_deflate_count) overrides with the real
// wiring. There are no tank sensors to assign.
void assignDefaultPumpRoles() {
    config::state.pressure_tank_mux_ch = -1;
    config::state.vacuum_tank_mux_ch = -1;
    for (int i = 0; i < NUM_PUMPS; i++) {
        pumps::roles[i] = (i < NUM_PUMPS / 2) ? pumps::ROLE_PRESSURE : pumps::ROLE_VACUUM;
    }
    LOG("Default pump roles: PUMP1..%d pressure, rest vacuum (override via configure)\n",
        NUM_PUMPS / 2);
}

void applyPumpGroups(uint8_t pressure_mask, uint8_t vacuum_mask) {
    for (int i = 0; i < NUM_PUMPS; i++) {
        bool in_pressure = (pressure_mask & (1u << i)) != 0;
        bool in_vacuum = (vacuum_mask & (1u << i)) != 0;
        if (in_pressure && !in_vacuum) {
            pumps::roles[i] = pumps::ROLE_PRESSURE;
        } else if (in_vacuum && !in_pressure) {
            pumps::roles[i] = pumps::ROLE_VACUUM;
        }
    }
}

void parseAndQueue(const uint8_t* data, int len) {
    JsonDocument doc;
    if (deserializeJson(doc, data, len) != DeserializationError::Ok) {
        return;
    }

    const char* cmd = doc["cmd"] | "";
    cmd_queue::Cmd c{};

    if (strcmp(cmd, "ping") == 0) {
        c.type = cmd_queue::CMD_PING;
        c.chamber = -1;
    } else if (strcmp(cmd, "inflate") == 0) {
        c.type = cmd_queue::CMD_INFLATE;
        c.chamber = doc["chamber"] | -1;
        c.param = doc["delta"] | 10;
        c.fill_ms = doc["ms"] | 0;
        c.duty = doc["duty"] | 0;
    } else if (strcmp(cmd, "deflate") == 0) {
        c.type = cmd_queue::CMD_DEFLATE;
        c.chamber = doc["chamber"] | -1;
        c.param = doc["delta"] | 10;
        c.fill_ms = doc["ms"] | 0;
        c.duty = doc["duty"] | 0;
    } else if (strcmp(cmd, "set_pressure") == 0) {
        c.type = cmd_queue::CMD_SET_PRESSURE;
        c.chamber = doc["chamber"] | -1;
        c.param = doc["value"] | 0;
        c.duty = doc["duty"] | 0;
    } else if (strcmp(cmd, "set_max_pressure") == 0) {
        c.type = cmd_queue::CMD_SET_MAX;
        c.chamber = doc["chamber"] | -1;
        c.param_kpa = doc["value"] | config::DEFAULT_CHAMBER_MAX_KPA;
    } else if (strcmp(cmd, "set_min_pressure") == 0) {
        c.type = cmd_queue::CMD_SET_MIN;
        c.chamber = doc["chamber"] | -1;
        c.param_kpa = doc["value"] | config::DEFAULT_CHAMBER_MIN_KPA;
    } else if (strcmp(cmd, "hold") == 0) {
        c.type = cmd_queue::CMD_HOLD;
        c.chamber = doc["chamber"] | -1;
    } else if (strcmp(cmd, "stop") == 0) {
        c.type = cmd_queue::CMD_STOP;
    } else if (strcmp(cmd, "resume") == 0) {
        c.type = cmd_queue::CMD_RESUME;
    } else if (strcmp(cmd, "valve_manual") == 0) {
        c.type = cmd_queue::CMD_VALVE_MANUAL;
        c.chamber = doc["chamber"] | -1;
        c.param = doc["side"] | 0;     // 0=inflate, 1=deflate
        c.cfg_chambers = doc["open"] | 0;
    } else if (strcmp(cmd, "pump_manual") == 0) {
        c.type = cmd_queue::CMD_PUMP_MANUAL;
        c.param = doc["pump"] | 0;     // 0=pressure, 1=vacuum
        c.cfg_chambers = doc["on"] | 0;
    } else if (strcmp(cmd, "configure") == 0) {
        c.type = cmd_queue::CMD_CONFIGURE;
        c.cfg_chambers = doc["num_chambers"] | config::state.num_chambers;
        c.cfg_p_min    = doc["tank_pressure_min_kpa"]    | config::state.tank_pressure_min_kpa;
        c.cfg_p_max    = doc["tank_pressure_max_kpa"]    | config::state.tank_pressure_max_kpa;
        c.cfg_v_min    = doc["tank_vacuum_min_kpa"]      | config::state.tank_vacuum_min_kpa;
        c.cfg_v_max    = doc["tank_vacuum_max_kpa"]      | config::state.tank_vacuum_max_kpa;
        c.cfg_p_target = doc["tank_pressure_target_kpa"] | config::state.tank_pressure_target_kpa;
        c.cfg_v_target = doc["tank_vacuum_target_kpa"]   | config::state.tank_vacuum_target_kpa;

        int inflate_count = constrain((int)(doc["pump_inflate_count"] | 0), 0, NUM_PUMPS);
        int deflate_count = constrain((int)(doc["pump_deflate_count"] | 0), 0, NUM_PUMPS);

        c.cfg_pressure_mask = 0;
        c.cfg_vacuum_mask = 0;

        JsonObject groups = doc["pump_groups"].as<JsonObject>();
        if (!groups.isNull()) {
            JsonArray pressure = groups["pressure"].as<JsonArray>();
            JsonArray vacuum = groups["vacuum"].as<JsonArray>();
            for (JsonVariant v : pressure) {
                int p = v.as<int>();
                if (p >= 1 && p <= NUM_PUMPS) c.cfg_pressure_mask |= (1u << (p - 1));
            }
            for (JsonVariant v : vacuum) {
                int p = v.as<int>();
                if (p >= 1 && p <= NUM_PUMPS) c.cfg_vacuum_mask |= (1u << (p - 1));
            }
        }

        if (c.cfg_pressure_mask == 0 && c.cfg_vacuum_mask == 0) {
            for (int i = 0; i < inflate_count; i++) c.cfg_pressure_mask |= (1u << i);
            for (int i = inflate_count; i < inflate_count + deflate_count && i < NUM_PUMPS; i++) {
                c.cfg_vacuum_mask |= (1u << i);
            }
        }

        // Organ circuits: list of mux channels; index in the list = slot in
        // the organ broadcasts. Staged here, applied from loop().
        JsonArray organ_channels = doc["organ_channels"].as<JsonArray>();
        if (!organ_channels.isNull()) {
            int count = 0;
            for (JsonVariant v : organ_channels) {
                int ch = v.as<int>();
                if (count < organ::MAX_ORGANS && ch >= 0 && ch < mux::MUX_CHANNELS) {
                    pendingOrganCh[count++] = ch;
                }
            }
            pendingOrganCount = count;
        }
#ifdef DEBUG_BUILD
    } else if (strcmp(cmd, "debug") == 0) {
        c.type = cmd_queue::CMD_DEBUG;
#endif
    } else if (strcmp(cmd, "set_led") == 0) {
        // Handled inline (not queued): just stores each ring's target LED state,
        // which loop()'s leds::update() renders. "ring" (0..3) selects one of the
        // four rings; omitted / -1 addresses all four at once.
        // {"cmd":"set_led","ring":0..3,"color":"#RRGGBB",
        //  "pattern":"off|solid|blink|pulse|comet","period_ms":N,"count":N,
        //  "fade_ms":N,"index":N}
        const char* col = doc["color"]     | "#000000";
        const char* pat = doc["pattern"]   | "solid";
        uint32_t period = doc["period_ms"] | 0;
        int32_t  count  = doc["count"]     | 0;
        uint32_t fade   = doc["fade_ms"]   | leds::DEFAULT_FADE_MS;
        float    offset = ((float)(doc["angle"] | 0.0f)) / 360.0f;   // split/comet rotation
        int      ring   = doc["ring"]      | -1;
        uint8_t r, g, b;
        leds::parseHexColor(col, r, g, b);
        if (strcmp(pat, "off") == 0) { r = g = b = 0; }   // "off" = dark, any colour
        int idx = doc["index"] | -1;
        if (idx >= 0) leds::setPixel(ring, idx, r, g, b, fade);   // single pixel (test panel)
        else          leds::set(ring, r, g, b, leds::patternFromStr(pat), period, count, fade, offset);
        return;
    } else if (strcmp(cmd, "set_led_halves") == 0) {
        // Split ring(s) into len(colors) equal arcs (the purple/yellow look) in
        // ONE frame. Replaces the PC's old per-pixel burst — 24 set_led frames
        // that reset the node by calling show() once per pixel in the recv task.
        // {"cmd":"set_led_halves","ring":0..3,"colors":["#RRGGBB",...],"pattern":...,
        //  "fade_ms":N}  pattern "comet" paints one comet per colour.
        const char* pat = doc["pattern"]   | "solid";
        uint32_t period = doc["period_ms"] | 0;
        int32_t  count  = doc["count"]     | 0;
        uint32_t fade   = doc["fade_ms"]   | leds::DEFAULT_FADE_MS;
        float    offset = ((float)(doc["angle"] | 0.0f)) / 360.0f;   // rotate the split
        int      ring   = doc["ring"]      | -1;
        uint8_t r[leds::MAX_SEGMENTS], g[leds::MAX_SEGMENTS], b[leds::MAX_SEGMENTS];
        int k = 0;
        for (JsonVariant v : doc["colors"].as<JsonArray>()) {
            if (k >= leds::MAX_SEGMENTS) break;
            leds::parseHexColor(v.as<const char*>(), r[k], g[k], b[k]);
            k++;
        }
        if (k > 0) leds::setSegments(ring, r, g, b, k, leds::patternFromStr(pat), period, count, fade, offset);
        return;
    } else {
        return;
    }

    cmd_queue::push(c);
}

// ---------------------------------------------------------------------------
// Manual (dev/test) override. The pumps normally run autonomously to maintain
// the tanks, so a manual command would fight that loop. While a manual command
// is active we SUSPEND the autonomous tank/chamber control and drive the
// requested actuator directly. Safety nets (enforced in loop()):
//   1. Dead-man: the override auto-clears after MANUAL_MAX_ON_MS and autonomous
//      control resumes, so a lost "off" command can't leave a pump running.
//   2. HARD limits: pumps/valves are cut if a tank or chamber hits its hard cap.
// At most one valve per chamber is held open at a time (inflate XOR deflate).
// Dev/teacher tool only — never exposed to children.
// ---------------------------------------------------------------------------

constexpr uint32_t MANUAL_MAX_ON_MS = 5000;

bool     manualActive               = false;
uint32_t manualTs                   = 0;
bool     manualPumpOn[2]            = {false, false};   // [0]=pressure, [1]=vacuum
bool     manualValveOpen[MAX_CHAMBERS][2] = {};         // [chamber][0=inflate,1=deflate]

void applyManualPump(int role01, bool on) {
    if (role01 < 0 || role01 > 1) return;
    manualPumpOn[role01] = on;
    pumps::Role role = (role01 == 0) ? pumps::ROLE_PRESSURE : pumps::ROLE_VACUUM;
    pumps::setRoleDuty(role, on ? pumps::PUMP_DEFAULT_DUTY : 0);
}

void applyManualValve(int chamber, int side, bool open) {
    if (chamber < 0 || chamber >= MAX_CHAMBERS || side < 0 || side > 1) return;
    if (open) manualValveOpen[chamber][1 - side] = false;   // single side open
    manualValveOpen[chamber][side] = open;
    pca_valves::setChamberValve(chamber,
        manualValveOpen[chamber][0], manualValveOpen[chamber][1]);
}

// Turn every manual actuator off and hand control back to the autonomous loops.
void manualClearAll() {
    pumps::setRoleDuty(pumps::ROLE_PRESSURE, 0);
    pumps::setRoleDuty(pumps::ROLE_VACUUM, 0);
    manualPumpOn[0] = manualPumpOn[1] = false;
    for (int chmbr = 0; chmbr < MAX_CHAMBERS; chmbr++) {
        if (manualValveOpen[chmbr][0] || manualValveOpen[chmbr][1])
            pca_valves::setChamberValve(chmbr, false, false);
        manualValveOpen[chmbr][0] = manualValveOpen[chmbr][1] = false;
    }
    manualActive = false;
}

// Emergency stop — slam everything off immediately. loop() keeps it that way
// (and skips all control) while emergencyStopped is set.
void emergencyStopAll() {
    chambers::abortSequences();   // drop any in-progress coupled-fill sequence
    pumps::stopAll();
    pca_valves::closeAllValves();
    manualClearAll();
    for (int i = 0; i < MAX_CHAMBERS; i++) chambers::stop(i);
}

// Actuation commands that a ``chamber == -1`` target fans out to every chamber
// (so "Inflate All" / "Deflate All" is one frame, not one per chamber). Limit
// commands (set_max/set_min — each chamber has its own) and the manual bench
// controls are deliberately excluded; they stay single-target.
bool isFanOut(cmd_queue::CmdType t) {
    using namespace cmd_queue;
    return t == CMD_INFLATE || t == CMD_DEFLATE
        || t == CMD_SET_PRESSURE || t == CMD_HOLD;
}

// Apply one already-parsed command to a single (assumed valid) chamber ``n``.
// Split out of processCommand() so a ``chamber == -1`` actuation can fan out to
// every chamber in one pass.
void applyChamberCmd(int n, const cmd_queue::Cmd& c) {
    using namespace cmd_queue;
    auto& ch = chambers::state[n];

    // duty 0 (unset) -> full speed. ``fill_ms`` is the optional per-chamber time
    // budget: targeting is pressure-based via the engine, but a target the gauge
    // can't see (deflate below the sensor floor) closes on this calibrated time
    // instead; 0 keeps the engine's chamber_max_ms backstop.
    uint8_t duty = c.duty ? c.duty : chambers::DEFAULT_DUTY;

    switch (c.type) {
    case CMD_INFLATE: {
        float delta  = (ch.max_kpa - ch.min_kpa) * constrain(c.param, 0, 100) / 100.0f;
        float target = min(chambers::cachedKpa[n] + delta, ch.max_kpa);
        // Only actuate if actually below target (else a repeated "+" at the cap
        // would creep past max one round at a time).
        if (chambers::cachedKpa[n] < target)
            chambers::requestInflate(n, target, duty, c.fill_ms);
        break;
    }
    case CMD_DEFLATE: {
        float delta  = (ch.max_kpa - ch.min_kpa) * constrain(c.param, 0, 100) / 100.0f;
        float target = max(chambers::cachedKpa[n] - delta, ch.min_kpa);
        if (chambers::cachedKpa[n] > target)
            chambers::requestDeflate(n, target, duty, c.fill_ms);
        break;
    }
    case CMD_SET_PRESSURE: {
        float target = units::pctToKpa(constrain(c.param, 0, 100), ch.min_kpa, ch.max_kpa);
        if      (chambers::cachedKpa[n] < target) chambers::requestInflate(n, target, duty);
        else if (chambers::cachedKpa[n] > target) chambers::requestDeflate(n, target, duty);
        else chambers::holdChamber(n);
        break;
    }
    case CMD_SET_MAX: {
        ch.max_kpa = constrain(c.param_kpa, ch.min_kpa + 0.1f, config::HARD_CHAMBER_MAX_KPA);
        if (ch.state == chambers::INFLATING && chambers::cachedKpa[n] >= ch.max_kpa)
            chambers::holdChamber(n);
        break;
    }
    case CMD_SET_MIN: {
        ch.min_kpa = constrain(c.param_kpa, config::HARD_CHAMBER_MIN_KPA, ch.max_kpa - 0.1f);
        if (ch.state == chambers::DEFLATING && chambers::cachedKpa[n] <= ch.min_kpa)
            chambers::holdChamber(n);
        break;
    }
    case CMD_HOLD:
        chambers::holdChamber(n);
        break;
    case CMD_VALVE_MANUAL: {
        // chamber = chamber, param = side (0=inflate, 1=deflate), cfg_chambers = open (0/1)
        // Enter manual override: drop the engine sequences + close everything first
        // so the manual action starts from a clean slate; auto-cleared by dead-man.
        if (!manualActive) { chambers::abortSequences(); chambers::closeAll(); }
        manualActive = true;
        manualTs     = millis();
        applyManualValve(n, c.param, c.cfg_chambers != 0);
        break;
    }
    case CMD_PUMP_MANUAL: {
        // param = pump role (0=pressure, 1=vacuum), cfg_chambers = on (0/1)
        if (!manualActive) { chambers::abortSequences(); chambers::closeAll(); }
        manualActive = true;
        manualTs     = millis();
        applyManualPump(c.param, c.cfg_chambers != 0);
        break;
    }
    default:
        break;
    }
}

void processCommand(const cmd_queue::Cmd& c) {
    using namespace cmd_queue;

    if (c.type == CMD_PING) {
        sendPong();
        return;
    }

    // Emergency stop / re-arm — works regardless of configured/error state.
    if (c.type == CMD_STOP)   { emergencyStopped = true;  emergencyStopAll(); return; }
    if (c.type == CMD_RESUME) { emergencyStopped = false; return; }

#ifdef DEBUG_BUILD
    if (c.type == CMD_DEBUG) {
        char buf[192];
        int len = snprintf(buf, sizeof(buf),
                           "{\"type\":\"debug\",\"ready\":%d,\"configured\":%d,\"num_chambers\":%d,\"p_tank\":%d,\"v_tank\":%d}",
                           config::state.ready ? 1 : 0,
                           configured ? 1 : 0,
                           config::state.num_chambers,
                           config::state.pressure_tank_mux_ch,
                           config::state.vacuum_tank_mux_ch);
        if (gatewayKnown) {
            esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
        }
        return;
    }
#endif

    if (config::state.error) {
        sendError("pca9685_address_conflict");
        return;
    }

    if (c.type == CMD_CONFIGURE) {
        config::state.num_chambers          = max(1, min((int)c.cfg_chambers, MAX_CHAMBERS));
        config::state.tank_pressure_min_kpa = constrain(c.cfg_p_min, config::HARD_TANK_MIN_KPA, config::HARD_TANK_MAX_KPA);
        config::state.tank_pressure_max_kpa = constrain(c.cfg_p_max, config::state.tank_pressure_min_kpa + 0.1f, config::HARD_TANK_MAX_KPA);
        config::state.tank_vacuum_min_kpa   = constrain(c.cfg_v_min, config::HARD_TANK_MIN_KPA, config::HARD_TANK_MAX_KPA);
        config::state.tank_vacuum_max_kpa   = constrain(c.cfg_v_max, config::state.tank_vacuum_min_kpa + 0.1f, config::HARD_TANK_MAX_KPA);
        config::state.tank_pressure_target_kpa = constrain(c.cfg_p_target,
            config::state.tank_pressure_min_kpa, config::state.tank_pressure_max_kpa);
        config::state.tank_vacuum_target_kpa   = constrain(c.cfg_v_target,
            config::state.tank_vacuum_min_kpa,   config::state.tank_vacuum_max_kpa);
        if (c.cfg_pressure_mask || c.cfg_vacuum_mask) {
            applyPumpGroups(c.cfg_pressure_mask, c.cfg_vacuum_mask);
        }
        configured = true;
        return;
    }

    if (!configured) {
        sendError("not_configured");
        return;
    }

#ifdef DEBUG_BUILD
    if (c.type == CMD_INFLATE || c.type == CMD_DEFLATE
        || c.type == CMD_SET_PRESSURE || c.type == CMD_HOLD) {
        const char* name = c.type == CMD_INFLATE      ? "inflate"
                         : c.type == CMD_DEFLATE      ? "deflate"
                         : c.type == CMD_SET_PRESSURE ? "set_pressure" : "hold";
        sendRxOpen(name, c.chamber);
    }
#endif

    // chamber == -1 fans an actuation out to EVERY chamber, so the PC's
    // Inflate/Deflate-All travels as ONE ESP-NOW frame instead of one per
    // chamber. With no command-level retry a single dropped per-chamber frame
    // used to leave most chambers un-actuated (the "only one inflates" bug).
    int n = c.chamber;
    int limit = min(config::state.num_chambers, (int)MAX_CHAMBERS);
    if (n == -1 && isFanOut(c.type)) {
        for (int i = 0; i < limit; i++) applyChamberCmd(i, c);
        return;
    }
    if (n < 0 || n >= limit) return;
    applyChamberCmd(n, c);
}

// Enforce hard limits while in manual override (called at the pressure cadence
// with freshly-read chamber pressures). Cuts any manual valve that would push a
// chamber past its hard cap. Dead-man timeout is handled in loop(). (No tank
// checks: the pumps push straight into chambers, so the per-chamber caps are the
// limit that matters.)
void manualPressureSafety() {
    for (int i = 0; i < config::state.num_chambers; i++) {
        float k = chambers::cachedKpa[i];
        if (manualValveOpen[i][0] && k >= config::HARD_CHAMBER_MAX_KPA) applyManualValve(i, 0, false);
        if (manualValveOpen[i][1] && k <= config::HARD_CHAMBER_MIN_KPA) applyManualValve(i, 1, false);
    }
}

// Apply a staged organ-channel configuration: hand the channels to the organ
// module and scrub them from any chamber/tank assignment the boot autodetect
// may have claimed (organ circuits read mid-range when the cover is on, so
// they can masquerade as pressure sensors during the scan).
void applyPendingOrganChannels() {
    int count = pendingOrganCount;
    if (count < 0) return;
    pendingOrganCount = -1;
    organ::setChannels(pendingOrganCh, count);
    for (int i = 0; i < count; i++) {
        int ch = pendingOrganCh[i];
        for (int c = 0; c < MAX_CHAMBERS; c++) {
            if (config::state.chamber_mux_ch[c] == ch) config::state.chamber_mux_ch[c] = -1;
        }
        if (config::state.pressure_tank_mux_ch == ch) config::state.pressure_tank_mux_ch = -1;
        if (config::state.vacuum_tank_mux_ch == ch) config::state.vacuum_tank_mux_ch = -1;
    }
    LOG("TODO: organ circuits on %d mux channel(s) — confirm wiring\n", count);
}

void onReceived(const uint8_t* mac_addr, const uint8_t* data, int len) {
    DBG_PRINT("RX %02X:%02X:%02X:%02X:%02X:%02X (%d) ",
              mac_addr[0], mac_addr[1], mac_addr[2],
              mac_addr[3], mac_addr[4], mac_addr[5], len);
    for (int i = 0; i < len; i++) DBG_PRINT("%c", (char)data[i]);
    DBG_PRINTLN("");

    se::node::learnGateway(mac_addr);   // remember gateway + add peer on first msg
    if (se::ota::tryHandle(data, len)) return;   // firmware update over ESP-NOW
    parseAndQueue(data, len);
}

void autodetect() {
    int valid_channels[16] = {};
    int valid_count = 0;
    int tank_candidates[4] = {};
    int tank_count = 0;

    detectSensors(valid_channels, valid_count, tank_candidates, tank_count);
    assignDefaultPumpRoles();

    config::state.ready = true;
}

}  // namespace

void setup() {
    esp_ota_mark_app_valid_cancel_rollback();
    Serial.begin(115200);

    mux::hardware_init();
    pumps::hardware_init();
    pumps::stopAll();
    leds::hardware_init();
    chambers::enginesInit();

    if (!se::begin(onReceived)) {
        LOG("{\"error\":\"esp_now_init_failed\"}\n");
        config::state.error = true;
        return;
    }

    // Firmware update support: halt all actuators before a WiFi OTA takes the
    // node off the air, and announce a WiFi OTA that just completed (see se_ota.h).
    se::ota::beforeWifiHook = &emergencyStopAll;
    se::ota::checkBootDone();

#ifdef DEBUG_BUILD
    installDebugHook();   // stream the engine round/measure trace over ESP-NOW
#endif

    bool pca_ok = pca_valves::init();
    if (!pca_ok) {
        config::state.error = true;
        return;
    }

    autodetect();
    pca_valves::closeAllValves();
    pumps::stopAll();

    // Broadcast the ready message so the gateway can forward it to the PC
    // even before the node has received its first command (and therefore
    // doesn't yet know the gateway's MAC).
    char ready_msg[160];
    snprintf(ready_msg, sizeof(ready_msg),
             "{\"status\":\"node_multiplexed_ready\",\"fw\":\"progressive-close-1\",\"rgbw\":" LED_RGBW_JSON ",\"kpa_min\":%.0f}",
             (double)pressure::FLOOR_KPA);
    se::broadcast(ready_msg);

    LOG("%s\n", ready_msg);
}

void loop() {
    // Run a pending WiFi OTA from the main task (never returns if it starts —
    // the node reboots into the new firmware).
    se::ota::pollWifi();

    // Animate the LED rings (non-blocking, throttled).
    leds::update();

    cmd_queue::Cmd c;
    while (cmd_queue::pop(c)) {
        processCommand(c);
    }

    // Emergency stop or any not-ready state: keep everything off and skip all
    // control. Commands (incl. "resume") were already drained above, so the
    // node can be re-armed.
    if (emergencyStopped || config::state.error || !config::state.ready || !configured) {
        pumps::stopAll();
        pca_valves::closeAllValves();
        delay(5);
        return;
    }

    uint32_t now = millis();

    // Manual (dev) override: dead-man auto-off every loop (cheap, no I/O). When
    // it fires, autonomous control resumes on the next pressure tick. SIGNED diff:
    // manualTs is set (millis()) in the command drain, after loop() cached `now`,
    // so an unsigned `now - manualTs` would underflow on the first tick and clear
    // the override the instant it was set (manual valve closes itself).
    if (manualActive && (int32_t)(now - manualTs) >= (int32_t)MANUAL_MAX_ON_MS) manualClearAll();

    // ---- Organ + cover sensing (per configured slot) ----
    applyPendingOrganChannels();
    organ::tick(now);

    // ---- Child-safety watchdog: stop runaway actuations (sensor failure) ----
    if (!manualActive) chambers::actuationWatchdog(now);

    if (manualActive && now - lastPressureMs >= PRESSURE_CHECK_MS) {
        lastPressureMs = now;
        // Autonomous control suspended. Refresh chamber pressures and enforce
        // hard limits on whatever the operator is driving manually.
        for (int i = 0; i < config::state.num_chambers; i++) {
            int m = config::state.chamber_mux_ch[i];
            if (m >= 0) chambers::cachedKpa[i] = mux::readKpa(m);
        }
        manualPressureSafety();
    }

    // ---- Coupled-fill engines on their own (tight) cadence: open the group
    //      together → fill to the lowest open target → close → settle → measure
    //      each isolated → repeat. Reads the mux gauges itself (median-filtered)
    //      and recalcs the count-scaled pumps when valves change. ----
    if (!manualActive && now - lastChamberMs >= CHAMBER_CHECK_MS) {
        lastChamberMs = now;
        chambers::controlTick(now);
        chambers::recalcPumps();
    }

    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        for (int i = 0; i < config::state.num_chambers; i++) {
            sendStatus(i, chambers::cachedKpa[i]);
        }
#ifdef DEBUG_BUILD
        checkDryPumps();
#endif
    }
}
