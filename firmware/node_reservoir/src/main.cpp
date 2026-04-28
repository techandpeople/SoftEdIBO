#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <esp_now.h>

#include "chambers.h"
#include "cmd_queue.h"
#include "config.h"
#include "dbg.h"
#include "mux.h"
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

uint8_t gatewayMac[6] = {};
bool gatewayKnown = false;
bool configured = false;

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
    int pct = units::kpaToPctOf(kpa, chambers::state[chamber].max_kpa);
    char buf[56];
    int len = snprintf(buf, sizeof(buf),
                       "{\"type\":\"status\",\"chamber\":%d,\"pressure\":%d}",
                       chamber, pct);
    esp_now_send(gatewayMac, reinterpret_cast<const uint8_t*>(buf), len);
}

void sendTankStatus(const char* kind, float kpa, float ref_kpa) {
    if (!gatewayKnown) return;
    int pct = units::kpaToPctOf(kpa, ref_kpa);
    char buf[72];
    int len = snprintf(buf, sizeof(buf),
                       "{\"type\":\"tank_status\",\"kind\":\"%s\",\"pressure\":%d}",
                       kind, pct);
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
    } else if (strcmp(cmd, "hold") == 0) {
        c.type = cmd_queue::CMD_HOLD;
        c.chamber = doc["chamber"] | -1;
    } else if (strcmp(cmd, "set_tank_pressure") == 0) {
        c.type = cmd_queue::CMD_SET_TANK_PRESSURE;
        const char* kind = doc["kind"] | "pressure";
        c.chamber = (strcmp(kind, "vacuum") == 0) ? 1 : 0;
        c.param_kpa = doc["value"] | 0.0f;
    } else if (strcmp(cmd, "configure") == 0) {
        c.type = cmd_queue::CMD_CONFIGURE;
        c.cfg_chambers = doc["num_chambers"] | config::state.num_chambers;
        c.cfg_p_max = doc["tank_pressure_max_kpa"] | config::state.tank_pressure_max_kpa;
        c.cfg_v_max = doc["tank_vacuum_max_kpa"] | config::state.tank_vacuum_max_kpa;

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
#ifdef DEBUG_BUILD
    } else if (strcmp(cmd, "debug") == 0) {
        c.type = cmd_queue::CMD_DEBUG;
#endif
    } else {
        return;
    }

    cmd_queue::push(c);
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
        config::state.num_chambers = max(1, min((int)c.cfg_chambers, MAX_CHAMBERS));
        config::state.tank_pressure_max_kpa = constrain(c.cfg_p_max, 1.0f, config::HARD_TANK_MAX_KPA);
        config::state.tank_vacuum_max_kpa = constrain(c.cfg_v_max, 1.0f, config::HARD_TANK_MAX_KPA);
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

    if (c.type == CMD_SET_TANK_PRESSURE) {
        if (c.chamber == 0) {
            config::state.tank_pressure_target_kpa = constrain(c.param_kpa, 0.0f, config::state.tank_pressure_max_kpa);
        } else {
            config::state.tank_vacuum_target_kpa = constrain(c.param_kpa, 0.0f, config::state.tank_vacuum_max_kpa);
        }
        return;
    }

    int n = c.chamber;
    if (n < 0 || n >= config::state.num_chambers || n >= MAX_CHAMBERS) {
        return;
    }

    switch (c.type) {
    case CMD_INFLATE: {
        float delta = units::pctToKpaOf(constrain(c.param, 0, 100), chambers::state[n].max_kpa);
        float target = min(chambers::cachedKpa[n] + delta, chambers::state[n].max_kpa);
        chambers::beginInflate(n, target);
        break;
    }
    case CMD_DEFLATE: {
        float delta = units::pctToKpaOf(constrain(c.param, 0, 100), chambers::state[n].max_kpa);
        float target = max(chambers::cachedKpa[n] - delta, 0.0f);
        chambers::beginDeflate(n, target);
        break;
    }
    case CMD_SET_PRESSURE: {
        float target = units::pctToKpaOf(constrain(c.param, 0, 100), chambers::state[n].max_kpa);
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
        chambers::state[n].max_kpa = constrain(c.param_kpa, 0.1f, config::HARD_CHAMBER_MAX_KPA);
        if (chambers::state[n].state == chambers::INFLATING &&
            chambers::cachedKpa[n] >= chambers::state[n].max_kpa) {
            chambers::stop(n);
        }
        break;
    }
    case CMD_HOLD:
        chambers::stop(n);
        break;
    default:
        break;
    }
}

float readTankKpa(int mux_ch) {
    if (mux_ch < 0 || mux_ch >= mux::MUX_CHANNELS) return 0.0f;
    return mux::readKpa(mux_ch);
}

void tankControlStep() {
    float pressure_kpa = readTankKpa(config::state.pressure_tank_mux_ch);
    float vacuum_kpa = readTankKpa(config::state.vacuum_tank_mux_ch);

    if (pressure_kpa >= config::state.tank_pressure_max_kpa) {
        pumps::setRoleDuty(pumps::ROLE_PRESSURE, 0);
    } else {
        bool need_pressure = pressure_kpa < config::state.tank_pressure_target_kpa;
        pumps::setRoleDuty(pumps::ROLE_PRESSURE, need_pressure ? pumps::PUMP_DEFAULT_DUTY : 0);
    }

    if (vacuum_kpa >= config::state.tank_vacuum_max_kpa) {
        pumps::setRoleDuty(pumps::ROLE_VACUUM, 0);
    } else {
        bool need_vacuum = vacuum_kpa > config::state.tank_vacuum_target_kpa;
        pumps::setRoleDuty(pumps::ROLE_VACUUM, need_vacuum ? pumps::PUMP_DEFAULT_DUTY : 0);
    }

    sendTankStatus("pressure", pressure_kpa, config::state.tank_pressure_max_kpa);
    sendTankStatus("vacuum", vacuum_kpa, config::state.tank_vacuum_max_kpa);
}

void chamberControlStep(uint32_t now) {
    for (int i = 0; i < config::state.num_chambers; i++) {
        int mux_ch = config::state.chamber_mux_ch[i];
        if (mux_ch < 0) continue;

        chambers::cachedKpa[i] = mux::readKpa(mux_ch);

        auto& ch = chambers::state[i];
        if (ch.state == chambers::PRE_INFLATE && now - ch.settle_ts >= chambers::VALVE_SETTLE_MS) {
            ch.state = chambers::INFLATING;
            pca_valves::setChamberValve(i, true, false);
        }
        if (ch.state == chambers::PRE_DEFLATE && now - ch.settle_ts >= chambers::VALVE_SETTLE_MS) {
            ch.state = chambers::DEFLATING;
            pca_valves::setChamberValve(i, false, true);
        }

        if (ch.state == chambers::INFLATING &&
            (chambers::cachedKpa[i] >= ch.target_kpa || chambers::cachedKpa[i] >= ch.max_kpa)) {
            chambers::stop(i);
        }
        if (ch.state == chambers::DEFLATING && chambers::cachedKpa[i] <= ch.target_kpa) {
            chambers::stop(i);
        }
    }
}

void onReceived(const uint8_t* mac_addr, const uint8_t* data, int len) {
    if (!gatewayKnown) {
        memcpy(gatewayMac, mac_addr, 6);
        gatewayKnown = true;
        esp_now_peer_info_t peer{};
        memcpy(peer.peer_addr, gatewayMac, 6);
        peer.channel = 0;
        peer.encrypt = false;
        esp_now_add_peer(&peer);
    }
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
    Serial.begin(115200);

    mux::hardware_init();
    pumps::hardware_init();
    pumps::stopAll();

    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    if (esp_now_init() != ESP_OK) {
        LOG("ERROR: esp_now_init_failed\n");
        config::state.error = true;
        return;
    }
    esp_now_register_recv_cb(onReceived);

    bool pca_ok = pca_valves::init();
    if (!pca_ok) {
        config::state.error = true;
        return;
    }

    autodetect();
    pca_valves::closeAllValves();
    pumps::stopAll();
    LOG("{\"status\":\"node_reservoir_ready\"}\n");
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
    if (now - lastPressureMs >= PRESSURE_CHECK_MS) {
        lastPressureMs = now;
        tankControlStep();
        chamberControlStep(now);
    }

    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        for (int i = 0; i < config::state.num_chambers; i++) {
            sendStatus(i, chambers::cachedKpa[i]);
        }
    }
}
