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
#include <esp_ota_ops.h>

#include "se_espnow.h"
#include "se_ota.h"
#include "pins.h"
#include "pressure.h"
#include "chambers.h"
#include "leds.h"
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

static void onReceived(const uint8_t* mac_addr, const uint8_t* data, int len) {
    DBG_PRINT("RX %02X:%02X:%02X:%02X:%02X:%02X (%d) ",
              mac_addr[0], mac_addr[1], mac_addr[2],
              mac_addr[3], mac_addr[4], mac_addr[5], len);
    for (int i = 0; i < len; i++) DBG_PRINT("%c", (char)data[i]);
    DBG_PRINTLN("");

    se::node::learnGateway(mac_addr);   // remember gateway + add peer on first msg
    if (se::ota::tryHandle(data, len)) return;   // firmware update over ESP-NOW
    commands::parseAndQueue(data, len);
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------

void setup() {
    esp_ota_mark_app_valid_cancel_rollback();
    Serial.begin(115200);

    chambers::hardware_init();
    leds::hardware_init();

    if (!se::begin(onReceived)) {
        LOG("{\"error\":\"esp_now_init_failed\"}\n");
        return;
    }

    for (int i = 0; i < NUM_CHAMBERS; i++)
        chambers::cachedKpa[i] = pressure::readKpa(PSENSOR_PINS[i]);

    // Broadcast the ready message so the gateway can forward it to the PC
    // even before the node has received its first command (and therefore
    // doesn't yet know the gateway's MAC).
    static const char ready_msg[] = "{\"status\":\"node_direct_ready\"}";
    se::broadcast(ready_msg);

    LOG("%s\n", ready_msg);
}

void loop() {
    uint32_t now = millis();

    // ---- Animate the LED ring (non-blocking, throttled) ----
    leds::update();

    // ---- Process queued commands ----
    cmd_queue::Cmd c;
    while (cmd_queue::pop(c))
        commands::process(c);

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

    // ---- Manual (dev) actuation safety: dead-man auto-off + HARD_MAX cutoff ----
    chambers::manualSafetyTick(now);

    // ---- Status broadcast ----
    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        for (int i = 0; i < NUM_CHAMBERS; i++)
            commands::sendStatus(i, chambers::cachedKpa[i]);
    }
}
