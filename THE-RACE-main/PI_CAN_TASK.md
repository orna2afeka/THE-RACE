# Pi task — get MMS and BMS running together on two CAN channels

**Read this whole file before running anything.** Work through the tasks in
order and stop at the first one that fails; each later task assumes the earlier
ones passed.

Run every command from the git root on the Pi:

```bash
cd ~/Desktop/THE-RACE-main
```

⚠️ **Path gotcha:** the repo root contains a directory *also* called
`THE-RACE-main`. So the service file is at `THE-RACE-main/deploy/can-up.service`,
and the app is at `THE-RACE-main/SolarRace_OS/main.py`. `deploy/can-up.service`
does **not** exist and will fail with "No such file or directory".

---

## Situation

The car has two CAN channels because its devices do not agree on a bitrate.
A single CAN wire carries exactly one bitrate — two nodes clocked differently
corrupt each other's frames — so they cannot share one.

| Channel | Configured rate | Device believed to be on it | IDs |
|---------|-----------------|------------------------------|-----|
| `can0`  | 1 Mbit/s        | MMS (SiliXcon LYNX)          | `0x600`–`0x628` (11-bit) |
| `can1`  | 500 kbit/s      | BMS (JBD, query/response)    | `0x100`–`0x110` (11-bit) |
| ?       | ?               | J1939 battery-temp module    | `0x1839F3xx` (29-bit) |

The temp module's channel is **unknown**. It is often fixed at 250 kbit/s, in
which case it cannot share either wire above and needs a third interface.

Bitrates live in one place: `CAN_BITRATES` in
`THE-RACE-main/SolarRace_OS/config.py`. For SocketCAN, python-can **ignores**
the `bitrate=` argument entirely — the real rate is whatever `ip link` set. So
`config.py` and `deploy/can-up.service` must be kept in step by hand.

Reading is channel-agnostic by design: frames from every open bus are merged and
dispatched by CAN ID alone, and the three ID ranges do not overlap. Nothing
downstream cares which wire a frame arrived on, so **moving a device between
channels is a wiring change, not a code change.**

---

## Known facts — measured on this Pi, do not re-derive

Last `ip -details link show`:

```
can0: <NO-CARRIER,NOARP,UP,ECHO> ... state DOWN
      can state BUS-OFF restart-ms 0
can1: <NOARP,UP,LOWER_UP,ECHO> ... state UP
      can state ERROR-ACTIVE restart-ms 0
```

* `can1` is **healthy**. `ERROR-ACTIVE` is the normal working state of a CAN
  node, not an error.
* `can0` is **BUS-OFF**: its transmit-error counter passed 255 and the
  controller removed itself from the bus. With `restart-ms 0` (the kernel
  default) it **never** comes back — it keeps the `UP` flag, carries zero
  frames, and makes `ip link set can0 up` fail with "Device or resource busy".
  That last error is a *symptom*, not the problem.

**Most likely cause of the bus-off.** Every CAN frame needs an ACK from some
other node on the wire. A transmitter with no peer retries forever, adding 8 to
its error counter each attempt, until it hits 255. `_poll_bms()` in `main.py`
transmits BMS queries on **every open bus**. So a channel with nothing live on
it is driven to bus-off by our own polling. The comment in `_poll_bms` claiming
the copy on the wrong bus is "harmless" is **wrong** and should not be trusted.

Corollary worth testing: if the MMS really were live on `can0` at 1 Mbit/s, it
would have ACKed those frames and `can0` would never have gone bus-off. So
**`can0` may not be carrying the MMS at all**, or not at 1 Mbit/s.

---

## Task 1 — check the power supply first

The Pi has been showing **"Low voltage warning — please check your power
supply."** Do not skip this. Under-volting corrupts the SPI link the MCP2515
CAN controller sits on, and one of its signatures is exactly the failure being
chased here: error bursts that push a channel to bus-off.

```bash
vcgencmd get_throttled
```

* `throttled=0x0` — clean, continue.
* Any other value — **under-voltage has occurred.** Bit 0 set means it is
  happening *now*; bit 16 means it happened since boot. Fix the supply (a proper
  5 V/3 A PSU, not a phone charger or a long thin USB cable) before drawing any
  conclusion about the CAN wiring, or you will be debugging two faults at once.

Report the value either way.

---

## Task 2 — install the systemd unit and revive `can0`

The unit was updated: per-channel bitrates, `ExecStartPre` link resets so a
restart cannot fail on a busy interface, and `restart-ms 100` for automatic
bus-off recovery.

```bash
sudo cp THE-RACE-main/deploy/can-up.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart can-up.service
systemctl status can-up.service --no-pager -l
```

Then confirm both links:

```bash
ip -details link show can0 | grep -E "state|bitrate|restart"
ip -details link show can1 | grep -E "state|bitrate|restart"
```

**Expected:** `can0` bitrate 1000000, `can1` bitrate 500000, both
`ERROR-ACTIVE`, both `restart-ms 100`.

If `can0` returns to `BUS-OFF` within seconds, that is real: something on that
wire is wrong. Continue to Task 3 — do not paper over it.

### Check the overlay pins - likely the can0 fault

The car uses a **Waveshare 2-CH CAN HAT**: two independent MCP2515 controllers
with SN65HVD230 transceivers. Correct `/boot/firmware/config.txt` for that board:

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=23
dtoverlay=mcp2515-can1,oscillator=16000000,interrupt=25
```

**can0 = GPIO23, can1 = GPIO25.** This repo's README used to say `can0 ...
interrupt=25` with no can1 line - those are the settings for the SINGLE-channel
RS485 CAN HAT, a different board. If config.txt on this Pi still has can0 on
interrupt 25, it is bound to can1's interrupt line, which would explain can0
behaving erratically and reaching BUS-OFF.

```bash
grep -n "mcp2515\|spi" /boot/firmware/config.txt || grep -n "mcp2515\|spi" /boot/config.txt
```

**Report what those lines say.** If can0 is on interrupt 25, correct it to 23,
make sure the can1 line exists, and reboot.

### Check termination - the other prime suspect

Each channel has a switchable **120 ohm jumper**. A CAN bus needs exactly two
terminators, one at each physical end. Missing or extra termination causes
reflections and error frames, and drives a channel to BUS-OFF - exactly what
can0 did.

With the bus **unpowered**, measure across CANH/CANL on each channel:

* **~60 ohm** - correct (two 120 ohm in parallel)
* **~120 ohm** - a terminator is MISSING, set the HAT jumper ON
* **~40 ohm** - one terminator too many, set the HAT jumper OFF

Report both readings.

---

## Task 3 — find what is actually on each wire (the important one)

Do not guess the bitrates. Measure them.

`listen-only` mode never transmits and never ACKs, so it **cannot** go bus-off
and **cannot** disturb a live bus. It is the only safe way to probe.

```bash
for ch in can0 can1; do
  for r in 1000000 500000 250000 125000; do
    sudo ip link set $ch down 2>/dev/null
    sudo ip link set $ch up type can bitrate $r listen-only on 2>/dev/null || continue
    echo "===== $ch @ $r ====="
    timeout 3 candump -n 8 $ch
  done
  sudo ip link set $ch down 2>/dev/null
done
```

**The rate that prints frames is the rate that wire runs at.** Silence at every
rate means nothing is transmitting on that wire — either it is unplugged, the
device is unpowered, or termination is wrong.

Identify what you find by ID:

| IDs seen | Device |
|----------|--------|
| `0x600`–`0x628` | MMS / LYNX motor controller |
| `0x1839F3xx` (29-bit) | J1939 battery-temp module |
| `0x100`–`0x110` | BMS — **will not appear here**, see below |

⚠️ The **BMS never broadcasts.** It is strictly query/response: it answers only
when the Pi sends a query frame carrying `0x5A`. In `listen-only` mode you
cannot query it, so a silent channel does **not** prove the BMS is absent.
Confirm the BMS in Task 4 instead.

**Record for each channel: which rate produced frames, and which IDs.** That
table is the answer this whole task exists to produce.

---

## Task 4 — verify both devices decode together

Restore the real (non-listen-only) config, putting each channel at the rate
Task 3 measured:

```bash
sudo systemctl restart can-up.service
```

If Task 3 found rates that differ from 1 Mbit/s on `can0` or 500 kbit/s on
`can1`, **stop and report** — do not just edit the files. Both
`THE-RACE-main/SolarRace_OS/config.py` (`CAN_BITRATES`) and
`THE-RACE-main/deploy/can-up.service` have to change together, and the right
values need confirming first.

Run the app:

```bash
python THE-RACE-main/SolarRace_OS/main.py
```

Watch the startup lines. Expect an `[can] opened socketcan:can0 @ 1000kbps` and
an `[can] opened socketcan:can1 @ 500kbps` (or whatever Task 3 established).

In a second terminal, confirm the BMS is answering — its frames only appear once
`main.py` is polling:

```bash
candump can1,100:7F0    # BMS replies, 0x100-0x110
candump can0,600:7F0    # MMS frames,  0x600-0x628
```

**Success looks like:** MMS frames streaming continuously on one channel, BMS
frames appearing at ~1 Hz on the other (the poll interval), and neither channel
going bus-off.

---

## Task 5 — stop cross-bus polling (only after Task 3 succeeds)

Once you know which channel the BMS is on, the poller should stop transmitting
on the other one. This is what drove `can0` to bus-off, and it will keep doing
so on any channel without a live peer.

`_poll_bms()` in `THE-RACE-main/SolarRace_OS/main.py` currently loops over every
open bus. The fix is to restrict it to the BMS channel — ideally via a new
setting next to `CAN_BITRATES` in `config.py` (something like
`BMS_POLL_CHANNEL = "can1"`), defaulting to polling all buses when unset so a
single-channel car still works.

**Do not do this blind.** Confirm the BMS channel in Task 4 first, then make the
change, then re-verify Task 4 still passes.

---

## Task 6 — read the LYNX controller's own configuration

This is the highest-value thing to collect at the car, and it is a *read*, not
a change.

The speedometer reads **~40 km/h when the car is doing 74**. The cause is
established and it is NOT a decoding bug:

* `0x610` bytes 4-5 is the only speed-like field on the entire bus. Every CAN ID
  seen in capture (`0x30`, `0x37`, `0x100`-`0x110`, `0x600`, `0x610`, `0x615`,
  `0x618`, `0x620`, `0x625`, `0x626`, `0x628`, `0x1839F380`) was scanned at every
  byte offset as u8 / u16-LE / s16-LE / u16-BE. **No field carries true road
  speed**, in km/h or in 0.1 km/h. The controller does not publish it.
* That field tracks motor RPM at a fixed 1.0366x, which is exactly a speed
  computed in 0.1 km/h from a 1.727 m wheel with **no gear reduction applied**.
  The LYNX's gear ratio has never been configured, so it behaves as 1:1.

There are two ways to fix this, and the first is much better:

**(a) Configure the controller (preferred).** In the LYNX's own settings, set the
gear ratio and the wheel size to the car's real values. Then `0x610` bytes 4-5
becomes true km/h, our decode collapses to `raw / 10`, and the tire and gear
constants stop affecting the speedometer at all — the number arrives correct off
the wire with no compensation anywhere in our code.

**(b) Correct `GEAR_RATIO` in `drivetrain.py` (fallback).** It is currently
`112/22 = 5.0909`; the measured value is **2.75** (a clean sprocket ratio —
110/40, 88/32, 44/16). This keeps a scale factor in our code forever and also
moves distance, the odometer and lap counting, so it is not a drive-by edit.

**What to collect at the car:**

1. The LYNX's configured **gear ratio** and **wheel size / circumference**
   (whatever the config tool calls them). Report the values as found, before
   changing anything.
2. The **motor and wheel sprocket tooth counts**, counted by hand. These are the
   physical truth and settle option (b) if (a) is not available.
3. The **rolling circumference**: mark the tire and the floor with the driver
   aboard at normal pressure, roll straight for exactly 5 wheel revolutions,
   measure the distance, divide by 5.

Do not change `drivetrain.py` or `mms_parser.py` from the Pi. Report the numbers
and the fix will be made once, on the pit side, where the pit database and the
exporter can be kept in step with it.

---

## Report back

1. `vcgencmd get_throttled` value.
2. Final `ip -details link show` for both channels (state, bitrate, restart-ms).
3. **The Task 3 table**: per channel, the rate that produced traffic and the IDs
   seen. This is the key result.
4. Whether MMS and BMS frames were both flowing in Task 4.
5. Where the J1939 temp module turned up, if anywhere.
6. **Task 6 numbers**: the controller's configured gear ratio and wheel size,
   the sprocket tooth counts, and the measured rolling circumference.
7. Anything that contradicts the "Known facts" above — those were measured
   earlier and may have changed.
