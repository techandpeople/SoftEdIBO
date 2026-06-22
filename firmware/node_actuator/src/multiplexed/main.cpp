#include <Arduino.h>
#include <ArduinoJson.h>
#include <esp_ota_ops.h>

#include "se_espnow.h"
#include "se_ota.h"
#include "chambers.h"
#include "cmd_queue.h"
#include "config.h"
#include "dbg.h"
#include "mux.h"
#include "organ.h"
#include "pca_valves.h"
#include "pins.h"
#include "pumps.h"
#include "units.h"

namespace {

constexpr uint32_t PRESSURE_CHECK_MS = 200;
constexpr uint32_t STATUS_REPORT_MS  = 500;
constexpr float DETECT_DELTA_KPA     = 0.3f;

uint32_t lastPressureMs = 0;
uint32_t lastStatusMs = 0;

// Gateway MAC tracking lives in the shared ESP-NOW layer.
using se::node::gatewayMac;
using se::node::gatewayKnown;
bool configured = false;

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
    sendRaw("{\"type\":\"pong\"}");
}

void sendStatus(int chamber, float kpa) {
    if (!gatewayKnown) return;
    auto& ch = chambers::state[chamber];
    int pct = units::kpaToPct(kpa, ch.min_kpa, ch.max_kpa);
    char buf[56];
    int len = snprintf(buf, sizeof(buf),
                       "{\"type\":\"status\",\"chamber\":%d,\"pressure\":%d}",
                       chamber, pct);
    esp_now_send(gatewayMac, reinterpret_cast<const uint8_t*>(buf), len);
}

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

void detectPumpToTank(const int tank_candidates[], int tank_count) {
    config::state.pressure_tank_mux_ch = -1;
    config::state.vacuum_tank_mux_ch = -1;

    for (int i = 0; i < NUM_PUMPS; i++) {
        pumps::roles[i] = pumps::ROLE_UNKNOWN;
    }

    if (tank_count <= 0) {
        LOG("TODO: no tank sensor candidates found on mux channels I12..I15 — confirm wiring\n");
        return;
    }

    float baseline[4] = {0};
    for (int i = 0; i < tank_count && i < 4; i++) {
        baseline[i] = mux::readKpa(tank_candidates[i]);
    }

    pca_valves::closeAllValves();

    for (int p = 0; p < NUM_PUMPS; p++) {
        pumps::setDuty(p, 160);
        delay(300);
        pumps::setDuty(p, 0);
        delay(120);

        float bestAbs = 0.0f;
        float bestDelta = 0.0f;
        int bestIdx = -1;

        for (int i = 0; i < tank_count && i < 4; i++) {
            float after = mux::readKpa(tank_candidates[i]);
            float delta = after - baseline[i];
            if (fabsf(delta) > bestAbs) {
                bestAbs = fabsf(delta);
                bestDelta = delta;
                bestIdx = i;
            }
            baseline[i] = after;
        }

        if (bestIdx >= 0 && bestAbs >= DETECT_DELTA_KPA) {
            int ch = tank_candidates[bestIdx];
            if (bestDelta > 0.0f) {
                pumps::roles[p] = pumps::ROLE_PRESSURE;
                if (config::state.pressure_tank_mux_ch < 0) {
                    config::state.pressure_tank_mux_ch = ch;
                }
            } else {
                pumps::roles[p] = pumps::ROLE_VACUUM;
                if (config::state.vacuum_tank_mux_ch < 0) {
                    config::state.vacuum_tank_mux_ch = ch;
                }
            }
        }

        const char* roleName = "?";
        if (pumps::roles[p] == pumps::ROLE_PRESSURE) roleName = "pressure";
        if (pumps::roles[p] == pumps::ROLE_VACUUM) roleName = "vacuum";
        LOG("TODO: pump i (PUMP%d / IO%d) -> %s tank\n", p + 1, PUMP_PINS[p], roleName);
    }

    if (config::state.pressure_tank_mux_ch < 0 && tank_count > 0) {
        config::state.pressure_tank_mux_ch = tank_candidates[0];
    }
    if (config::state.vacuum_tank_mux_ch < 0 && tank_count > 1) {
        config::state.vacuum_tank_mux_ch = tank_candidates[1];
    }

    LOG("TODO: pressure tank on mux ch %d, vacuum tank on mux ch %d\n",
        config::state.pressure_tank_mux_ch, config::state.vacuum_tank_mux_ch);
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
    } else if (strcmp(cmd, "deflate") == 0) {
        c.type = cmd_queue::CMD_DEFLATE;
        c.chamber = doc["chamber"] | -1;
        c.param = doc["delta"] | 10;
    } else if (strcmp(cmd, "set_pressure") == 0) {
        c.type = cmd_queue::CMD_SET_PRESSURE;
        c.chamber = doc["chamber"] | -1;
        c.param = doc["value"] | 0;
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

void processCommand(const cmd_queue::Cmd& c) {
    using namespace cmd_queue;

    if (c.type == CMD_PING) {
        sendPong();
        return;
    }

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

    int n = c.chamber;
    if (n < 0 || n >= config::state.num_chambers || n >= MAX_CHAMBERS) {
        return;
    }

    auto& ch = chambers::state[n];

    switch (c.type) {
    case CMD_INFLATE: {
        if (c.fill_ms > 0) {
            // Time-based fill: open for the calibrated window; max_kpa is the
            // only pressure cutoff.
            chambers::beginInflate(n, ch.max_kpa, c.fill_ms);
        } else {
            float delta  = (ch.max_kpa - ch.min_kpa) * constrain(c.param, 0, 100) / 100.0f;
            float target = min(chambers::cachedKpa[n] + delta, ch.max_kpa);
            chambers::beginInflate(n, target);
        }
        break;
    }
    case CMD_DEFLATE: {
        float delta  = (ch.max_kpa - ch.min_kpa) * constrain(c.param, 0, 100) / 100.0f;
        float target = max(chambers::cachedKpa[n] - delta, ch.min_kpa);
        chambers::beginDeflate(n, target);
        break;
    }
    case CMD_SET_PRESSURE: {
        float target = units::pctToKpa(constrain(c.param, 0, 100), ch.min_kpa, ch.max_kpa);
        if (chambers::cachedKpa[n] < target) {
            chambers::beginInflate(n, target);
        } else if (chambers::cachedKpa[n] > target) {
            chambers::beginDeflate(n, target);
        } else {
            chambers::stop(n);
        }
        break;
    }
    case CMD_SET_MAX: {
        ch.max_kpa = constrain(c.param_kpa, ch.min_kpa + 0.1f, config::HARD_CHAMBER_MAX_KPA);
        if (ch.state == chambers::INFLATING && chambers::cachedKpa[n] >= ch.max_kpa) {
            chambers::stop(n);
        }
        break;
    }
    case CMD_SET_MIN: {
        ch.min_kpa = constrain(c.param_kpa, config::HARD_CHAMBER_MIN_KPA, ch.max_kpa - 0.1f);
        if (ch.state == chambers::DEFLATING && chambers::cachedKpa[n] <= ch.min_kpa) {
            chambers::stop(n);
        }
        break;
    }
    case CMD_HOLD:
        chambers::stop(n);
        break;
    case CMD_VALVE_MANUAL: {
        // chamber = chamber, param = side (0=inflate, 1=deflate), cfg_chambers = open (0/1)
        // Enter manual override (suspends autonomous control); auto-cleared by dead-man.
        manualActive = true;
        manualTs     = millis();
        applyManualValve(n, c.param, c.cfg_chambers != 0);
        break;
    }
    case CMD_PUMP_MANUAL: {
        // param = pump role (0=pressure, 1=vacuum), cfg_chambers = on (0/1)
        manualActive = true;
        manualTs     = millis();
        applyManualPump(c.param, c.cfg_chambers != 0);
        break;
    }
    default:
        break;
    }
}

float readTankKpa(int mux_ch) {
    if (mux_ch < 0 || mux_ch >= mux::MUX_CHANNELS) return 0.0f;
    return mux::readKpa(mux_ch);
}

// Enforce hard limits while in manual override (called at the pressure cadence
// with freshly-read chamber pressures). Cuts any manual actuator that would push
// a tank or chamber past its hard cap. Dead-man timeout is handled in loop().
void manualPressureSafety() {
    float p = readTankKpa(config::state.pressure_tank_mux_ch);
    float v = readTankKpa(config::state.vacuum_tank_mux_ch);
    if (manualPumpOn[0] && p >= config::HARD_TANK_MAX_KPA) applyManualPump(0, false);
    if (manualPumpOn[1] && v <= config::HARD_TANK_MIN_KPA) applyManualPump(1, false);
    for (int i = 0; i < config::state.num_chambers; i++) {
        float k = chambers::cachedKpa[i];
        if (manualValveOpen[i][0] && k >= config::HARD_CHAMBER_MAX_KPA) applyManualValve(i, 0, false);
        if (manualValveOpen[i][1] && k <= config::HARD_CHAMBER_MIN_KPA) applyManualValve(i, 1, false);
    }
}

// True while at least one chamber is in the given state. Used to refill the
// shared tanks ONLY when no chamber is drawing from / dumping into them, so a
// chamber's calibrated fill time isn't disturbed by a concurrent tank refill
// (the PC fill-time calibration assumes a steady tank).
bool anyChamberInState(chambers::State want) {
    for (int i = 0; i < config::state.num_chambers; i++) {
        if (chambers::state[i].state == want) return true;
    }
    return false;
}

void tankControlStep() {
    float pressure_kpa = readTankKpa(config::state.pressure_tank_mux_ch);
    float vacuum_kpa   = readTankKpa(config::state.vacuum_tank_mux_ch);

    // Refill the tanks only while idle: pause the pressure pump whenever a
    // chamber is inflating (drawing from the pressure tank) and the vacuum pump
    // whenever a chamber is deflating. The tank simply droops during a fill and
    // is topped back up once the chambers settle.
    bool inflating = anyChamberInState(chambers::INFLATING);
    bool deflating = anyChamberInState(chambers::DEFLATING);

    // Pressure tank — pump fills it when below target. Stop at hard max.
    // Also stop if reading drops below min (sensor or seal failure).
    if (inflating ||
        pressure_kpa >= config::state.tank_pressure_max_kpa ||
        pressure_kpa <  config::state.tank_pressure_min_kpa) {
        pumps::setRoleDuty(pumps::ROLE_PRESSURE, 0);
    } else {
        bool need_pressure = pressure_kpa < config::state.tank_pressure_target_kpa;
        pumps::setRoleDuty(pumps::ROLE_PRESSURE, need_pressure ? pumps::PUMP_DEFAULT_DUTY : 0);
    }

    // Vacuum tank — pump evacuates (pulls pressure DOWN) when above target.
    // Stop at hard min (deepest vacuum). Also stop if above max (broken seal).
    if (deflating ||
        vacuum_kpa <= config::state.tank_vacuum_min_kpa ||
        vacuum_kpa >  config::state.tank_vacuum_max_kpa) {
        pumps::setRoleDuty(pumps::ROLE_VACUUM, 0);
    } else {
        bool need_vacuum = vacuum_kpa > config::state.tank_vacuum_target_kpa;
        pumps::setRoleDuty(pumps::ROLE_VACUUM, need_vacuum ? pumps::PUMP_DEFAULT_DUTY : 0);
    }

    // Status broadcasts: report percent over each tank's [min, max] range so the
    // UI can show 0-100 even when limits are negative (vacuum tank).
    if (gatewayKnown) {
        char buf[80];
        int p_pct = units::kpaToPct(pressure_kpa,
                                    config::state.tank_pressure_min_kpa,
                                    config::state.tank_pressure_max_kpa);
        int v_pct = units::kpaToPct(vacuum_kpa,
                                    config::state.tank_vacuum_min_kpa,
                                    config::state.tank_vacuum_max_kpa);
        int len = snprintf(buf, sizeof(buf),
                           "{\"type\":\"tank_status\",\"kind\":\"pressure\",\"pressure\":%d}", p_pct);
        esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
        len = snprintf(buf, sizeof(buf),
                       "{\"type\":\"tank_status\",\"kind\":\"vacuum\",\"pressure\":%d}", v_pct);
        esp_now_send(gatewayMac, reinterpret_cast<uint8_t*>(buf), len);
    }
}

void chamberControlStep(uint32_t now) {
    for (int i = 0; i < config::state.num_chambers; i++) {
        int mux_ch = config::state.chamber_mux_ch[i];
        if (mux_ch < 0) continue;

        chambers::cachedKpa[i] = mux::readKpa(mux_ch);

        auto& ch = chambers::state[i];
        if (ch.state == chambers::INFLATING &&
            (chambers::cachedKpa[i] >= ch.target_kpa || chambers::cachedKpa[i] >= ch.max_kpa)) {
            chambers::stop(i);
            ch.hold_kpa = chambers::cachedKpa[i];   // maintain the achieved level
        }
        if (ch.state == chambers::DEFLATING &&
            (chambers::cachedKpa[i] <= ch.target_kpa || chambers::cachedKpa[i] <= ch.min_kpa)) {
            chambers::stop(i);
        }
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
    detectPumpToTank(tank_candidates, tank_count);

    config::state.ready = true;
}

}  // namespace

void setup() {
    esp_ota_mark_app_valid_cancel_rollback();
    Serial.begin(115200);

    mux::hardware_init();
    pumps::hardware_init();
    pumps::stopAll();

    if (!se::begin(onReceived)) {
        LOG("{\"error\":\"esp_now_init_failed\"}\n");
        config::state.error = true;
        return;
    }

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
    static const char ready_msg[] = "{\"status\":\"node_multiplexed_ready\"}";
    se::broadcast(ready_msg);

    LOG("%s\n", ready_msg);
}

void loop() {
    cmd_queue::Cmd c;
    while (cmd_queue::pop(c)) {
        processCommand(c);
    }

    if (config::state.error || !config::state.ready || !configured) {
        pumps::stopAll();
        pca_valves::closeAllValves();
        delay(5);
        return;
    }

    uint32_t now = millis();

    // Manual (dev) override: dead-man auto-off every loop (cheap, no I/O). When
    // it fires, autonomous control resumes on the next pressure tick.
    if (manualActive && now - manualTs >= MANUAL_MAX_ON_MS) manualClearAll();

    // ---- Organ + cover sensing (per configured slot) ----
    applyPendingOrganChannels();
    organ::tick(now);

    // ---- Child-safety watchdog: stop runaway actuations (sensor failure) ----
    if (!manualActive) chambers::actuationWatchdog(now);

    // ---- Time-based fill cutoff (calibrated fill_time; every loop, not gated
    //      by the slow mux pressure cadence) ----
    if (!manualActive) chambers::fillTimeTick(now);

    // ---- Idle leak maintenance: top up a drooping held chamber (self-throttled) ----
    if (!manualActive) chambers::maintainTick(now);

    if (now - lastPressureMs >= PRESSURE_CHECK_MS) {
        lastPressureMs = now;
        if (manualActive) {
            // Autonomous control suspended. Refresh chamber pressures and enforce
            // hard limits on whatever the operator is driving manually.
            for (int i = 0; i < config::state.num_chambers; i++) {
                int m = config::state.chamber_mux_ch[i];
                if (m >= 0) chambers::cachedKpa[i] = mux::readKpa(m);
            }
            manualPressureSafety();
        } else {
            tankControlStep();
            chamberControlStep(now);
        }
    }

    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        for (int i = 0; i < config::state.num_chambers; i++) {
            sendStatus(i, chambers::cachedKpa[i]);
        }
    }
}
