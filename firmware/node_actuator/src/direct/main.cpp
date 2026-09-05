/**
 * SoftEdIBO - node_direct firmware
 *
 * 3-chamber air controller with onboard pumps. Valves through ULN2803A
 * (logic-level), pumps through DRV3297 (PWM). See pins.h for details.
 *
 * Build envs:
 *   pio run             -> release
 *   pio run -e debug    -> debug (Serial logs + "debug" command)
 *
 * Module breakdown:
 *   pins.h       - GPIO assignments
 *   pressure.h   - XGZP6847A ADC -> kPa conversion
 *   units.h      - kPa <-> percent helpers
 *   chambers.h   - per-chamber state machine + valve/pump control
 *   cmd_queue.h  - lock-free SPSC command ring buffer
 *   commands.h   - command parsing + processing + status broadcasts
 *   dbg.h        - DBG_PRINT macros
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

// How often the gauges are refreshed for telemetry and the command-time
// "below/above target?" guards. The actual closed-loop cutoff lives in the
// coupled-fill engine (chambers::controlTick), which does its own fresh
// median-filtered reads while a round is filling/measuring - so this cadence only
// keeps cachedKpa warm for idle telemetry and is cheap (3 dedicated ADC pins).
constexpr uint32_t PRESSURE_CHECK_MS = 20;
// The status broadcast cadence is runtime-adjustable (commands::statusReportMs):
// normally 500 ms, temporarily faster during a `status_rate` window.

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
    chambers::loadTare();      // per-chamber ambient zero (NVS) before any read
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

#ifdef DEBUG_BUILD
    commands::installDebugHook();   // stream the engine round/measure trace over ESP-NOW
#endif

    for (int i = 0; i < NUM_CHAMBERS; i++)
        chambers::cachedKpa[i] = chambers::readKpaMedian(i);   // ambient-zeroed

    // Broadcast the ready message so the gateway can forward it to the PC
    // even before the node has received its first command (and therefore
    // doesn't yet know the gateway's MAC).
    // The "fw" tag is a build marker: it lets us confirm from the PC log which
    // firmware actually booted, so a flash that silently didn't take can't be
    // mistaken for the new code. Bump it whenever the actuator logic changes.
    // "kpa_min" self-reports the gauge floor (sensor low end) so the PC knows
    // below which pressure a deflate needs a time budget instead of the sensor.
    char ready_msg[160];
    snprintf(ready_msg, sizeof(ready_msg),
             "{\"status\":\"node_direct_ready\",\"fw\":\"vac-floor-1-sw02\",\"rgbw\":" LED_RGBW_JSON ",\"kpa_min\":%.0f}",
             (double)pressure::FLOOR_KPA);
    se::broadcast(ready_msg);

    LOG("%s\n", ready_msg);

    // Magnet board ready - broadcast only now that ESP-NOW is up (announce() is a
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
        if (now - lastStatusMs >= commands::statusReportMs(now)) {
            lastStatusMs = now;
            commands::sendStatusAll();
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
        // SIGNED diff: testHeartbeatMs is set (millis()) in the command drain, after
        // loop() cached `now`, so it can be a hair ahead - unsigned would underflow
        // and kill the run the instant it started.
        if ((int32_t)(now - chambers::testHeartbeatMs) >= (int32_t)chambers::TEST_RUN_TIMEOUT_MS) {
            DBG_PRINT("TEST_RUN dead-man fired (no keepalive) - stopping\n");
            chambers::testStop();
        } else {
            if (now - lastStatusMs >= commands::statusReportMs(now)) {
                lastStatusMs = now;
                for (int i = 0; i < NUM_CHAMBERS; i++)
                    chambers::cachedKpa[i] = chambers::readKpaMedian(i);   // zeroed
                commands::sendStatusAll();
                commands::sendPumps();
            }
            return;
        }
    }

    // ---- Refresh gauges for telemetry + command-time guards ----
    // The actual closed-loop cutoff is the coupled-fill engine below (it does its
    // own fresh median reads while filling/measuring); this just keeps cachedKpa
    // warm for idle telemetry and the "below/above target?" command guards.
    if (now - lastPressureMs >= PRESSURE_CHECK_MS) {
        lastPressureMs = now;
        for (int i = 0; i < NUM_CHAMBERS; i++)
            chambers::cachedKpa[i] = chambers::readKpaMedian(i);   // ambient-zeroed
    }

    // ---- Coupled-fill engines: open the group together -> fill to the lowest open
    //      target -> close -> settle -> measure each isolated -> repeat. Drives single
    //      and multi-chamber inflate/deflate uniformly (the manifold is shared). ----
    chambers::controlTick(now);

    // ---- Leak-compensating continuous holds (valve open + equilibrium duty) ----
    chambers::holdTick(now);

    // ---- Manual (dev) actuation safety: dead-man auto-off + HARD_MAX cutoff ----
    chambers::manualSafetyTick(now);

    // ---- Child-safety watchdog: stop runaway actuations (sensor failure) ----
    chambers::actuationWatchdog(now);

    // ---- Organ + cover sensing (broadcasts on change + heartbeat) ----
    organ::tick(now);

    // ---- Magnet/touch sensing (streams ~28 Hz; no-op if no sensors) ----
    magnet::tick(now);

    // ---- High-resolution actuation trace -------------------------------------
    // Emit a status/pump frame the INSTANT a chamber's actuation state or a pump
    // duty changes, on top of the 500 ms heartbeat. The heartbeat is far too
    // coarse to see an Inflate-All: each chamber can open AND cut between two
    // heartbeats, so the PC only ever samples st:0 / pump:0 and the fill looks
    // like it never ran. On-change frames are sparse (only at transitions), so
    // they add little traffic but expose the real valve/pump timeline.
    {
        // Signature per chamber = actuation state + BOTH valve outputs, so a frame
        // is emitted the instant ANY of them changes - including a valve that opens
        // and closes without the chamber state settling differently, and a manual
        // valve toggle. This is what makes vi/vd precise enough to see a brief pulse.
        static uint8_t  prevSig[NUM_CHAMBERS] = {0xFF, 0xFF, 0xFF};
        static uint32_t prevInf = 0xFFFFFFFF, prevDef = 0xFFFFFFFF;
        bool sigChanged = false;
        for (int i = 0; i < NUM_CHAMBERS; i++) {
            uint8_t sig = (uint8_t)(chambers::state[i].state << 2)
                        | (chambers::valveOpen[i * 2 + 0] ? 2 : 0)
                        | (chambers::valveOpen[i * 2 + 1] ? 1 : 0);
            if (sig != prevSig[i]) {
                prevSig[i] = sig;
                sigChanged = true;
            }
        }
        // One batched frame the instant ANY chamber's state/valves change (all chambers'
        // fresh state rides along - harmless, and captures a co-changing group in one go).
        if (sigChanged) commands::sendStatusAll();
        uint32_t inf = ledcRead(chambers::PUMP1_LEDC_CH);
        uint32_t def = ledcRead(chambers::PUMP2_LEDC_CH);
        if (inf != prevInf || def != prevDef) {
            prevInf = inf; prevDef = def;
            commands::sendPumps();
        }
    }

    // ---- Status broadcast ----
    if (now - lastStatusMs >= commands::statusReportMs(now)) {
        lastStatusMs = now;
        commands::sendStatusAll();
        commands::sendPumps();   // live pump state (debug: stop-latency hunt)
#ifdef DEBUG_BUILD
        commands::checkDryPumps();   // warn if a pump spins with no open valve
#endif
    }
}
