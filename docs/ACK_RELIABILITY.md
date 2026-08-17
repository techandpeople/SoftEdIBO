# Confirmed command delivery (Tier 2 ACK plan)

Captures the agreed plan to make a small set of **safety-critical, set-once**
commands reliably *applied* by the node, rather than fire-and-forget over ESP-NOW.

> **Status.** The **safety limits `set_max_pressure` / `set_min_pressure` are
> implemented** end-to-end (both actuator boards + PC) - the highest-priority
> case, since a silently-lost `set_max` caused the 20->50 kPa over-inflation. The
> node ACKs each *after applying* it
> (`{"type":"ack","cmd":...,"seq":N,"chamber":C,"ok":...}`, a shared `se::node::sendAck`
> in [firmware/common/se_espnow.h]), and `CommandConfirmer`
> ([src/hardware/command_confirmer.py]) retransmits the same `seq` until the ack
> lands. The session applies limits this way at skin construction
> (`Skin._push_pressure_limits`, off-thread), keeping the fire-and-forget
> pre-actuation re-push as a backstop. **Still planned:** confirming
> `stop`/`resume`/`configure`, and the blanket Tier 1 gateway retry below.

## Why

ESP-NOW PC->node commands are fire-and-forget. A dropped `set_max_pressure` leaves
the node clamping to a stale limit (observed: a 20 kPa chamber inflated to ~50 kPa
via repeated `+`; `HARD_MAX_KPA = 100`). A dropped `stop` silently defeats the
emergency stop. The session already re-pushes limits before each actuation
(self-healing band-aid, see [PRESSURE_AND_FILL_SAFETY.md]), but that does not
cover `stop`/`configure` and never reports a truly unreachable node.

Two reliability levels were considered:

- **Tier 1** - gateway uses the ESP-NOW TX-status callback and retries on
  `ESP_NOW_SEND_FAIL`. Confirms the frame reached the node's *radio*. Cheap,
  blanket, no PC changes. Does **not** confirm the app applied the command.
- **Tier 2 (this doc)** - the node ACKs *after applying* a command; the PC waits,
  retries, and surfaces failure. Confirms **application**, end-to-end. More work,
  scoped to a few critical commands.

Tier 2 is the right guarantee for safety limits and the emergency stop. The two
tiers are complementary; Tier 1 can be added later as blanket coverage.

## What exists today (build on, don't reinvent)

- `commands::sendAck(cmd)` -> `{"type":"ack","cmd":"<cmd>"}` - **direct node only**,
  no seq, no chamber. Still sent for `stop`/`resume`/`test_run`/`test_stop`/
  `status_rate` ([firmware/node_actuator/src/direct/commands.h]); left as-is.
- The seq-carrying limit ack now lives in `common/` as `se::node::sendAck(cmd,
  seq, chamber, ok, err)` ([firmware/common/se_espnow.h]), wired into **both**
  boards' command handlers for `set_max`/`set_min` (the `ackConfirmable` helper).
- The **PC consumes** seq-carrying `type:ack` via `CommandConfirmer`
  ([src/hardware/command_confirmer.py]); the old seq-less stop/resume acks and
  `ota_ack` are still handled separately.
- The gateway **paces** its ESP-NOW sends (`se::sendPaced` waits for the TX-done
  callback before the next frame, fixing burst drops) - pacing only, **not** a
  retry, so Tier 1 remains unimplemented.
- OTA already implements a full ack+seq+timeout+retry exchange on the PC
  ([src/hardware/node_ota_updater.py], `_handle` + `_wait_for`) - the reference
  pattern: a read-thread handler records acks under a lock and sets a
  `threading.Event`; a worker loops with a deadline.
- `cmd_queue::Cmd` is **shared** across both nodes ([firmware/common/cmd_queue.h]);
  adding a `seq` field there covers both. 16-deep SPSC ring.
- Nodes already track `se::txOk` / `se::txFail` (shared ESP-NOW send callback) -
  relevant to Tier 1, not needed here.

## Scope: which commands get confirmed

Confirm (set-once, **idempotent**, safety/critical):

| command            | why confirm                              |
|--------------------|------------------------------------------|
| `stop` / `resume`  | emergency stop must not silently fail    |
| `set_max_pressure` | safety ceiling; stale value over-inflates |
| `set_min_pressure` | safety floor (vacuum)                    |
| `configure`        | set-once node config (chambers/organ channels) |

Do **not** confirm (stay fire-and-forget):

- `inflate` / `deflate` - **non-idempotent** (relative steps); must never be
  auto-retried.
- `set_pressure` - driven per-tick by activities; confirming adds round-trip
  latency. Relies on the re-push band-aid + the firmware clamp.
- `set_led`, `ping`, `valve_manual`, `pump_manual`, and all node->PC streams.

Idempotency is the deciding rule: only commands safe to apply twice are retried.

## Wire protocol changes

PC->node (only for confirmable commands): add an optional `seq` (monotonic
`uint16` per node).

```json
{"target":"AA:..","cmd":"set_max_pressure","chamber":2,"value":20.0,"seq":41}
```

node->PC ACK, sent **after the command is applied** (so it means "applied", not
"received"):

```json
{"type":"ack","cmd":"set_max_pressure","seq":41,"chamber":2,"ok":true}
```

- `seq` is echoed so the PC matches the exact attempt (disambiguates retries and
  late/duplicate acks - `cmd`+`chamber` alone is ambiguous).
- `ok:false` + `err:"bad_chamber"` is a **NACK** for a command the node rejected
  (invalid chamber, out-of-range value) so the PC fails fast instead of retrying.
- A command sent **without** `seq` behaves exactly as today (no ack expected) -
  backward compatible. Old firmware ignores `seq` and never acks -> the PC times
  out and falls back to fire-and-forget with a warning, so an un-reflashed node
  still works (just unconfirmed).

## PC side

A small confirmer (new helper, or folded into `ESP32Controller`):

- Per-node monotonic `uint16` seq.
- `send_confirmed(cmd, *, timeout=0.2, retries=3, **kwargs) -> Future/bool`:
  1. assign `seq`; register pending `(mac, seq)` -> `Event` + result slot (under a
     lock).
  2. `gateway.send(mac, cmd, seq=seq, **kwargs)`.
  3. wait up to `timeout` for the matching ack.
  4. timeout -> re-send the **same** seq (idempotent), up to `retries`.
  5. `ok:true` -> success; `ok:false` -> fail now (no retry); retries exhausted ->
     fail and surface to the user.
- One `gateway.on_message` handler routes `type:ack` into the pending map and sets
  the Event (mirrors OTA `_handle`). Runs on the read thread -> only touches the
  locked map, no GUI work.
- The confirm/retry loop runs **off the GUI thread** via `async_task.run_async`
  (see [GUI_ARCHITECTURE.md] / async tooling); the result is delivered back to the
  GUI with a Qt signal. The actuation path never blocks on an ack.

Timeouts/retries: per-attempt ~200 ms (ESP-NOW RTT is a few ms; covers queue +
apply + jitter), 3 retries (~800 ms worst case). On final failure:

- `set_max`/`set_min`/`configure`: log + GUI warning ("couldn't confirm on
  <mac>; node may be unreachable"); the re-push band-aid still self-heals.
- `stop`/`resume`: escalate loudly. **Open decision:** give up + alarm, or keep
  retrying while the emergency-stop is latched.

## Firmware side

- `cmd_queue.h` (shared): add `uint16_t seq` to `Cmd` (0xFFFF = none).
- `parseAndQueue` (each node): `c.seq = doc["seq"] | 0xFFFF`.
- Consolidate `sendAck` into `common/` (e.g. `se::sendAck(cmd, seq, chamber, ok,
  err)`) so **both** nodes share it; the direct node's ad-hoc copy goes away.
- In `process()` (after applying a confirmable command) call `sendAck(...)` with
  the stored `seq`, the `chamber` (for per-chamber commands), and `ok`. Emit
  `ok:false` on rejection paths (bad chamber / clamped-to-nothing).
- Add the same calls to the **multiplexed** node (none today).
- Keep acking even commands the PC may not confirm? No - only emit an ack when
  `seq != none`, to avoid extra traffic.

## Rollout (incremental, low risk)

- **Phase A - PC only, no reflash.** Build the confirmer + ack router + pending
  map. The current direct firmware already acks `stop`/`resume` (without seq), so
  match those transitionally by `(mac, cmd)` -> reliable emergency stop on direct
  nodes *today*. (Caveat: no seq means a late ack can match a new send; tolerable
  for idempotent, rare stop/resume.)
- **Phase B - firmware.** Add `seq` to `Cmd`, shared `se::sendAck(seq,chamber,
  ok)`, wired in both nodes for the confirmable commands; NACK on rejects. PC
  switches to seq-based matching. **Done for `set_max`/`set_min`** (`seq` added to
  the shared `Cmd`, `se::node::sendAck` + `ackConfirmable` on both boards, NACK on
  bad chamber, `CommandConfirmer` matching by `(mac, seq)`). `stop`/`resume`/
  `configure` not yet seq-confirmed - the direct board still acks stop/resume
  without a seq, which no confirmer consumes yet.
- **Phase C - GUI + policy.** Surface confirm failures (banner/toast); decide the
  emergency-stop "retry forever vs. alarm" policy. `confirm_limits` currently logs
  a warning on failure (no GUI banner yet).

## Testing

- PC unit tests with a fake gateway: (a) acks immediately -> success, no retry;
  (b) drops first N sends then acks -> asserts retry count then success; (c) never
  acks -> timeout/failure surfaced; (d) `inflate`/`deflate` are **never**
  auto-retried; (e) `ok:false` fails fast without retry.
- Firmware: loopback/manual - ack carries `seq`+`chamber`; invalid chamber ->
  `ok:false`.

## Decisions

1. **Emergency stop on final failure - do both.** Keep re-sending `stop` while the
   emergency stop is latched (never give up: a node that was briefly out of range
   gets caught on the next retry), **and** raise an operator alarm if not
   confirmed within ~1 s. Fail-safe direction: with no ACK, **assume not
   stopped** - keep trying + warn. Both error cases fail safe (`stop` is
   idempotent, so a lost ACK only causes harmless extra retries + a false alarm).
   The `stop` ACK is the signal that the firmware latch engaged (the node then
   holds the safe state even if the PC crashes - see [[emergency-stop]]).
2. **`seq` scope: per-node.** Each node can act differently; the PC matches acks
   by `(mac, seq)`. (Decided.)
3. **`set_pressure` stays fire-and-forget (not confirmed).** The re-push only
   sends `set_max`/`set_min` (limits) - it never opens a valve, so it does not
   worsen leak behaviour. Valve activity under leaks comes from the firmware's
   deliberate, self-throttled `maintainTick` top-up (`hold_kpa - LEAK_MARGIN_KPA`),
   independent of acks/re-push. So confirming `set_pressure` buys nothing here and
   only adds latency. (Decided.)
