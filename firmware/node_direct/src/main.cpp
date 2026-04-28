/**
 * SoftEdIBO — node_direct firmware
 *
 * 3-chamber air controller with onboard pumps. Valves through ULN2803A
 * (logic-level), pumps through DRV3297 (PWM). See pins.h for details.
 *
 * Build envs:
 *   pio run             -> release
 *   pio run -e debug    -> debug (Serial logs + "debug" command)
 *
 * Module breakdown:
 *   pins.h       — GPIO assignments
 *   pressure.h   — XGZP6847A ADC -> kPa conversion
 *   units.h      — kPa <-> percent helpers
 *   chambers.h   — per-chamber state machine + valve/pump control
 *   cmd_queue.h  — lock-free SPSC command ring buffer
 *   commands.h   — command parsing + processing + status broadcasts
 *   dbg.h        — DBG_PRINT macros
 *
 * Protocol: ESP-NOW JSON commands, 500 ms status broadcasts.
 *   {"cmd":"inflate|deflate|set_pressure|set_max_pressure|hold","chamber":N,...}
 *   {"cmd":"ping"} -> {"type":"pong"}
 *   {"type":"status","chamber":N,"pressure":pct}
 */

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>

#include "pins.h"
#include "pressure.h"
#include "chambers.h"
#include "cmd_queue.h"
#include "commands.h"
#include "dbg.h"

constexpr uint32_t PRESSURE_CHECK_MS = 200;
constexpr uint32_t STATUS_REPORT_MS  = 500;

static uint32_t lastPressureMs = 0;
static uint32_t lastStatusMs   = 0;

// ---------------------------------------------------------------------------
// ESP-NOW callbacks
// ---------------------------------------------------------------------------

#ifdef DEBUG_BUILD
static void onSent(const uint8_t*, esp_now_send_status_t status) {
    if (status == ESP_NOW_SEND_SUCCESS) commands::sendOk++;
    else                                commands::sendFail++;
}
#endif

static void onReceived(const uint8_t* mac_addr, const uint8_t* data, int len) {
    if (!commands::gatewayKnown) {
        memcpy(commands::gatewayMac, mac_addr, 6);
        commands::gatewayKnown = true;
        esp_now_peer_info_t peer{};
        memcpy(peer.peer_addr, commands::gatewayMac, 6);
        peer.channel = 0;
        peer.encrypt = false;
        esp_now_add_peer(&peer);
    }
    commands::parseAndQueue(data, len);
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------

void setup() {
#ifdef DEBUG_BUILD
    Serial.begin(115200);
#endif

    chambers::hardware_init();

    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    if (esp_now_init() != ESP_OK) {
        DBG_PRINTLN(F("{\"error\":\"esp_now_init_failed\"}"));
        return;
    }
#ifdef DEBUG_BUILD
    esp_now_register_send_cb(onSent);
#endif
    esp_now_register_recv_cb(onReceived);

    for (int i = 0; i < NUM_CHAMBERS; i++)
        chambers::cachedKpa[i] = pressure::readKpa(PSENSOR_PINS[i]);

    DBG_PRINTLN(F("{\"status\":\"node_direct_ready\"}"));
}

void loop() {
    uint32_t now = millis();

    // ---- Process queued commands ----
    cmd_queue::Cmd c;
    while (cmd_queue::pop(c))
        commands::process(c);

    // ---- Valve settle transitions ----
    for (int i = 0; i < NUM_CHAMBERS; i++) {
        auto& ch = chambers::state[i];
        if (ch.state == chambers::PRE_INFLATE &&
            now - ch.settle_ts >= chambers::VALVE_SETTLE_MS) {
            ch.state = chambers::INFLATING;
            chambers::setValve(i, 0, true);
            chambers::recalcPumps();
        }
        if (ch.state == chambers::PRE_DEFLATE &&
            now - ch.settle_ts >= chambers::VALVE_SETTLE_MS) {
            ch.state = chambers::DEFLATING;
            chambers::setValve(i, 1, true);
            chambers::recalcPumps();
        }
    }

    // ---- Pressure read + safety stop ----
    if (now - lastPressureMs >= PRESSURE_CHECK_MS) {
        lastPressureMs = now;
        for (int i = 0; i < NUM_CHAMBERS; i++) {
            chambers::cachedKpa[i] = pressure::readKpa(PSENSOR_PINS[i]);
            float kpa = chambers::cachedKpa[i];
            auto& ch  = chambers::state[i];
            if (ch.state == chambers::INFLATING &&
                (kpa >= ch.target_kpa || kpa >= ch.max_kpa)) {
                chambers::stop(i);
                chambers::recalcPumps();
            }
            if (ch.state == chambers::DEFLATING && kpa <= ch.target_kpa) {
                chambers::stop(i);
                chambers::recalcPumps();
            }
        }
    }

    // ---- Status broadcast ----
    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        for (int i = 0; i < NUM_CHAMBERS; i++)
            commands::sendStatus(i, chambers::cachedKpa[i]);
    }
}
