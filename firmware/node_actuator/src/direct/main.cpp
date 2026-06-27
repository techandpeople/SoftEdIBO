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
#include "organ.h"
#include "magnet.h"
#include "cmd_queue.h"
#include "commands.h"
#include "dbg.h"

// How often the closed-loop pressure cutoff (loop() below) samples each gauge
// and stops a chamber that has reached its target. This is the control loop, NOT
// telemetry — keep it tight. The inflate pump runs at full duty the whole time a
// chamber is INFLATING, so the achieved pressure overshoots the target by however
// much the pump delivers between two checks: at the old 200 ms a single "+" step
// (e.g. +10 % of range) blew past to ~30 % before the cutoff ever looked at the
// sensor. At 20 ms that overshoot window is ~10x smaller, so the measured level
// settles on the commanded target instead of sailing past it. A read is cheap
// (3 dedicated ADC pins, 4 samples each ≈ 100 µs); telemetry stays at 500 ms.
constexpr uint32_t PRESSURE_CHECK_MS = 20;
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
    organ::hardware_init();
    magnet::hardware_init();   // optional MLX90393 touch board (auto-detected)

    if (!se::begin(onReceived)) {
        LOG("{\"error\":\"esp_now_init_failed\"}\n");
        return;
    }

    // Firmware update support: halt all actuators before a WiFi OTA takes the
    // node off the air, and announce a WiFi OTA that just completed (see se_ota.h).
    se::ota::beforeWifiHook = &chambers::emergencyStopAll;
    se::ota::checkBootDone();

    for (int i = 0; i < NUM_CHAMBERS; i++)
        chambers::cachedKpa[i] = pressure::readKpa(PSENSOR_PINS[i]);

    // Broadcast the ready message so the gateway can forward it to the PC
    // even before the node has received its first command (and therefore
    // doesn't yet know the gateway's MAC).
    static const char ready_msg[] = "{\"status\":\"node_direct_ready\"}";
    se::broadcast(ready_msg);

    LOG("%s\n", ready_msg);

    // Magnet board ready — broadcast only now that ESP-NOW is up (announce() is a
    // no-op if no MLX90393 sensors are wired). Doing this inside magnet's
    // hardware_init(), before se::begin(), is what made the node disappear
    // whenever the sensors were connected.
    magnet::announce();
}

void loop() {
    // ---- Run a pending WiFi OTA (from the main task, not the recv callback) ----
    se::ota::pollWifi();   // never returns if an update starts (node reboots)

    uint32_t now = millis();

    // ---- Animate the LED ring (non-blocking, throttled) ----
    leds::update();

    // ---- Process queued commands ----
    cmd_queue::Cmd c;
    while (cmd_queue::pop(c))
        commands::process(c);

    // ---- Emergency stop: keep everything off, skip all actuation control ----
    // Commands (incl. "resume") are still processed above, so re-arming works.
    if (chambers::stopped) {
        chambers::emergencyStopAll();
        if (now - lastStatusMs >= STATUS_REPORT_MS) {
            lastStatusMs = now;
            for (int i = 0; i < NUM_CHAMBERS; i++)
                commands::sendStatus(i, chambers::cachedKpa[i]);
            commands::sendPumps();   // live pump state (debug: stop-latency hunt)
        }
        return;
    }

    // ---- Continuous bench test: hold one pump + its valves open, skip control ----
    // testRun() already asserted the hardware; we only refresh pressure telemetry
    // (so the dialog can watch the live, possibly bad, sensor) and bail before any
    // pressure / dead-man / watchdog tick can stop the run. New test_stop/test_run
    // commands are still processed above, so the latch can always be exited.
    if (chambers::testDir >= 0) {
        // Dead-man: this run bypasses every other safety, so it must not outlive
        // the PC link. The dialog re-sends `test_run` ~1 Hz as a keepalive; if none
        // arrives in time (dialog gone, USB/ESP-NOW link dropped) end the run and
        // fall through to normal (now-idle) control rather than inflate forever.
        if (now - chambers::testHeartbeatMs >= chambers::TEST_RUN_TIMEOUT_MS) {
            DBG_PRINT("TEST_RUN dead-man fired (no keepalive) — stopping\n");
            chambers::testStop();
        } else {
            if (now - lastStatusMs >= STATUS_REPORT_MS) {
                lastStatusMs = now;
                for (int i = 0; i < NUM_CHAMBERS; i++) {
                    chambers::cachedKpa[i] = pressure::readKpa(PSENSOR_PINS[i]);
                    commands::sendStatus(i, chambers::cachedKpa[i]);
                }
                commands::sendPumps();
            }
            return;
        }
    }

    // ---- Pressure read + safety stop ----
    if (now - lastPressureMs >= PRESSURE_CHECK_MS) {
        lastPressureMs = now;
        for (int i = 0; i < NUM_CHAMBERS; i++) {
            chambers::cachedKpa[i] = pressure::readKpa(PSENSOR_PINS[i]);
            float kpa = chambers::cachedKpa[i];
            auto& ch  = chambers::state[i];
            if (ch.state == chambers::INFLATING) {
                // A time-based fill (fill_until_ms set) deliberately ignores the
                // per-chamber pressure target — that is the whole point of timing
                // the fill when the gauge sensor is laggy or reads high. Only the
                // absolute HARD_MAX backstops it; fillTimeTick() ends it on time.
                // A closed-loop fill still stops at its sensor target as before.
                float ceiling = (ch.fill_until_ms != 0)
                                ? chambers::HARD_MAX_KPA : ch.target_kpa;
                if (kpa >= ceiling) {
                    chambers::stop(i);
                    ch.hold_kpa = chambers::cachedKpa[i];   // maintain the achieved level
                    chambers::recalcPumps();
                }
            }
            if (ch.state == chambers::DEFLATING && kpa <= ch.target_kpa) {
                chambers::stop(i);
                chambers::recalcPumps();
            }
        }
    }

    // ---- Time-based fill cutoff (calibrated fill_time; checked every loop) ----
    chambers::fillTimeTick(now);
    chambers::deflateTimeTick(now);

    // ---- Idle leak maintenance: top up a drooping held chamber (self-throttled) ----
    chambers::maintainTick(now);

    // ---- Manual (dev) actuation safety: dead-man auto-off + HARD_MAX cutoff ----
    chambers::manualSafetyTick(now);

    // ---- Child-safety watchdog: stop runaway actuations (sensor failure) ----
    chambers::actuationWatchdog(now);

    // ---- Organ + cover sensing (broadcasts on change + heartbeat) ----
    organ::tick(now);

    // ---- Magnet/touch sensing (streams ~28 Hz; no-op if no sensors) ----
    magnet::tick(now);

    // ---- Status broadcast ----
    if (now - lastStatusMs >= STATUS_REPORT_MS) {
        lastStatusMs = now;
        for (int i = 0; i < NUM_CHAMBERS; i++)
            commands::sendStatus(i, chambers::cachedKpa[i]);
        commands::sendPumps();   // live pump state (debug: stop-latency hunt)
    }
}
