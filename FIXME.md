# FIXME - Actuator (pumps + valves) logic

Status tracker for the actuator chain (PC -> gateway -> node -> pumps/valves).
The layer-by-layer dissection this file used to hold described the
pre-redesign code and is obsolete: the control logic was redesigned around the
shared **coupled-fill supervisor** (`firmware/common/coupled_fill.h`, both
boards) with progressive close. The current design, its diagnostics and its
safety caps are documented in `docs/COUPLED_FILL_CONTROL.md` and
`docs/PRESSURE_AND_FILL_SAFETY.md`; this file now only tracks what is still
open.

## STATUS (2026-07-05)

The 2026-06-26 PC-side fixes (see Resolved) did not make the chain work on
their own - the real faults were per-chamber closed-loop control over
**coupled** sensors (shared manifolds, no check valves) and a `millis()`
underflow that force-stopped rounds the instant they opened. Both are fixed by
the engine redesign (see the docs above). What remains is hardware validation
plus the known gaps below.

## Open

- [ ] **Bench-validate the progressive-close firmware on hardware, both
      boards.** Confirm the boot marker in the log after flashing -
      `"fw":"estop-1-sw02"` (direct) / `"fw":"estop-1"` (multiplexed) (OTA
      sends the prebuilt
      `firmware/node_actuator/firmware-*.bin`; rebuild them with
      `scripts/build-firmware.sh` - building into `.pio/` alone is not enough).
- [ ] **`duty` is not honoured on the engine path.** Both boards parse and
      store the optional pump `duty` on inflate/deflate/set_pressure, but
      `recalcPumps()` never uses it (full duty, except the `hold_duty` servo
      and the below-floor deflate pull) (an earlier duty-aware version
      regressed the pump and was reverted). The PC's gentle-fill plumbing
      (`set_pressure(period_ms=...)` -> `duty_for_period` / `DutyModel`) therefore
      has no firmware effect today; only the direct board's bench `test_run`
      honours `duty` (which is what the duty-curve calibration sweep uses).
      Decide: re-add duty-aware pumps safely, or drop the runtime duty path.
- [ ] **The multiplexed board lags the direct board's command set:** no
      `test_run`/`test_stop` (continuous bench run), no `status_rate` fast
      telemetry (`FastTelemetry`), no `pumps`/`seq` frames, and no seq-less
      `stop`/`resume`/`tare` acks (it does ack `set_max`/`set_min` with a
      `seq`, like the direct board). Continuous fill / duty-curve /
      deflate-curve calibration is direct-only until ported, so the GUI
      disables Calibrate Fill for `node_multiplexed` chambers.
- [ ] **Manual-vs-engine pump arbitration on the direct board.** Mostly
      resolved (`valve_manual` drops the chamber from the engines, the bench
      test owns the hardware exclusively, pumps follow the ACTUAL valve
      mirror), but a `pump_manual` ON is still silently overwritten by the next
      engine `recalcPumps()`. The multiplexed board suspends autonomous control
      under a `manualActive` latch; the direct board has no equivalent.
- [ ] **Command ACK reliability.** `set_max/min_pressure` are confirmed
      end-to-end on BOTH boards (`seq` + `ackConfirmable` ->
      `se::node::sendAck`; PC `CommandConfirmer` retransmits). Still TODO:
      seq-confirmed `stop`/`resume`/`configure` (the direct board acks
      stop/resume without a `seq`, the multiplexed board not at all) and the
      blanket Tier-1 gateway retry. The actuation commands stay
      fire-and-forget by design; the pre-actuation limit re-push
      (`Skin._push_limits`) remains as a backstop - `docs/ACK_RELIABILITY.md`.

## Resolved (kept for the record)

| What | Resolution |
|---|---|
| `firmware/common/fill_control.h` orphaned since the engine redesign (dead `MAX_FILL_MS`/`fillTimeTick`, lost idle leak top-up `maintainTick`/`hold_kpa`) | Header deleted. Leak maintenance was reimplemented engine-aware as the `hold_duty` regulated hold (`firmware/common/hold_duty.h`, both boards; auto-hold after every inflate/deflate). |
| Whole-skin vent alternated `valve_manual` sides (the boards only keep one side open at a time) | Fill calibration, Skin Config -> Zero Sensors and the Test Actuators Vent button now send the firmware `vent` command (both valves open, pumps kept off). |
| STOP ALL didn't stop (the dialog auto-`resume`d, discarding the firmware latch; a lost `stop` frame left the actuator running) | `_stop_all` latches (sends `stop` x3, no auto-resume) and re-arms lazily; the firmware `stop`/`resume` latch on BOTH boards re-asserts all-off every loop while latched. App-wide: the "0" panic key + the menubar E-stop button latch every robot (`src/gui/main_window.py`). |
| Test dialog / fill calibrator sent `value=255`, which the firmware ignores (`inflate` reads `delta`, default 10 %) | Both now send `delta=100`; the test dialog documents the field choice inline. |
| Stale `pins.h` pump comment (claimed PUMP1=IO33) | Comment fixed and bench-verified: `PUMP_PINS={32,33}`, PUMP1=IO32 (inflate), PUMP2=IO33 (vacuum). Pumps confirmed physically working - which also closes the DRV3297-enable question. |
| Pressure-based inflate self-cancelling on a bad/laggy sensor reading | Superseded by the engine: median-of-3 reads, per-chamber cutoff debounce, min-round spike gate; the hard kPa caps moved to +/-100 (effectively uncapped) so the unreliable gauge cannot gate fills - over-pressure is time-bounded instead (`capMs`/`chamber_max_ms`/`seq_max_ms`/watchdog). |
| The old section 11 "time-based calibrated control" redesign plan | Superseded by what actually shipped: measured fill curves (`FillProfile`, per-combination `fill_profiles`, per-type templates `fill_profiles_by_type`), falling deflate curves (`DeflateProfile` + floor-aware time budgets), duty sweeps (`DutyModel`), and the engine's per-chamber `capMs`. The planned idle resync is moot: the PC recomputes fill % from the live `kpa` on every status frame (`src/hardware/units.py` `kpa_to_pct`) instead of dead-reckoning elapsed time. |
