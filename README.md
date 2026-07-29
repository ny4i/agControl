# agControl

Python control for the [4O3A Antenna Genius](https://www.4o3a.com/) antenna switch over its
Ethernet API, plus an optional bridge that lets N1MM+ macros override the antenna selection.

Thanks to N8SDR for converting the API docs to md files: https://github.com/N8SDR1/SDRLoggerPlusNo third-party dependencies — Python 3.7+ and the standard library.

Everything here was developed and verified against real hardware: **Antenna Genius hardware 4.0,
firmware 4.1.16**. Several behaviors documented below differ from the published API docs; see
[Firmware quirks](#firmware-quirks).

---

## Quick start

```bash
git clone https://github.com/ny4i/agControl.git
cd agControl

export AG_HOST=192.168.1.39        # your Antenna Genius IP
python ag_control.py status
```

Every script also accepts `--host` (or `--ag-host`), which overrides `AG_HOST`.

To find your device's address, it broadcasts a UDP discovery packet on port 9007 once per second
beginning with `AG ip=...`, or check the AG Windows utility.

---

## Scripts

| Script | Purpose |
|---|---|
| `ag_control.py` | Main CLI: query state, select antennas, set band source |
| `ag_list_antennas.py` | The reusable `AGClient` protocol class, plus a minimal antenna-list CLI |
| `n1mm_listen.py` | Read-only monitor for N1MM+ RadioInfo UDP broadcasts |
| `ag_n1mm_control.py` | Bridges N1MM+ `{auxantsel}` macros to AG antenna selection |

`ag_control.py` is the reference implementation. The other three build on the same client.

---

## `ag_control.py`

```
ag_control.py [--host HOST] [--timeout SECONDS] [--dry-run] [--force] <command>
```

### Query

```bash
python ag_control.py status              # both radio ports
python ag_control.py status --port 1     # one port
python ag_control.py antennas            # antennas with decoded band masks
python ag_control.py bands               # band slots and frequency ranges
```

```
PORT   SOURCE  AUTO  BAND  RX ANT       TX ANT       TX  INH
-----  ------  ----  ----  -----------  -----------  --  ---
1 (A)  BCD     1     5     1 (HF_Beam)  1 (HF_Beam)  0   0
2 (B)  AUTO    1     0     none         none         0   0
```

### Select an antenna

Antennas may be given by **name** (case-insensitive) or by **number** — these are equivalent:

```bash
python ag_control.py select 1 OCF
python ag_control.py select 1 4
```

```bash
python ag_control.py select 1 OCF --rx-only   # RX antenna only
python ag_control.py select 1 OCF --tx-only   # TX antenna only
python ag_control.py select 1 none            # clear the selection
```

The first argument is the radio port: `1` = A, `2` = B.

Selecting an antenna does **not** require taking the port out of automatic band mode. The AG can
keep deriving the band from BCD/FLEX/etc. while you override only which antenna that band uses.

### Set the band source

```bash
python ag_control.py mode 1 BCD
python ag_control.py mode 1 MANUAL --band 5
python ag_control.py mode 1 BCD --auto 1
```

Valid sources: **`AUTO`, `MANUAL`, `BCD`, `FLEX`, `LAN`**.

> Setting `source=` also changes the `auto` flag as a side effect. Pass `--auto` explicitly
> whenever it matters.

### Safety options

`--dry-run` prints the exact command that would be sent and changes nothing:

```
$ python ag_control.py --dry-run select 1 HF_Beam
DRY RUN: would send 'port set 1 rxant=1 txant=1'
         port 1 currently rxant=4 txant=4 band=7
```

`--force` overrides the two safety checks below. Intended for bench work.

By default the tool **refuses** to:

- switch a port that reports `tx=1` (transmitting);
- select an antenna whose band mask does not include the port's current band.

> The `tx` flag reflects a PTT line wired into the Antenna Genius. **If your station does not
> feed PTT to the AG, `tx` always reads `0`** and this check can never fire — do not rely on it
> as transmit protection. Where the logger reports transmit state, that is the meaningful gate
> (see the N1MM section).

```
$ python ag_control.py select 1 6m_Yagi
Error: 6m_Yagi (antenna 8) is not enabled on band 7; its TX mask covers 10. Use --force to override
```

Unknown antenna names fail loudly rather than guessing:

```
$ python ag_control.py select 1 Beam
Error: no antenna named or numbered 'Beam'. Available: 1=HF_Beam, 2=Antenna_2, ... 8=6m_Yagi
```

All commands exit `0` on success and `1` on failure, so they script cleanly.

---

## Using `AGClient` in your own code

```python
from ag_list_antennas import AGClient, parse_kv_message

with AGClient("192.168.1.39") as ag:
    print(ag.prologue)                                  # 'V4.1.16 AG'

    message = ag.send_command("port get 1")[0]
    _, port = parse_kv_message(message, "port")
    print(port["band"], port["txant"])

    for row in ag.send_command("antenna list"):         # multi-part, handled for you
        print(row)
```

`send_command()` returns the response message(s) as a list, raises `AGError` on any non-zero
response code, and auto-detects which of the two response framings to expect
(see [Firmware quirks](#firmware-quirks)). Pass `multipart=True`/`False` to override.

The class is deliberately single-command-at-a-time and not thread-safe. It ignores asynchronous
`S0|` status messages, since it never subscribes; driving subscriptions needs a receive loop
rather than this request/response model.

---

## N1MM+ integration

### Why

The Antenna Genius already picks an antenna per band on its own. The point of this bridge is to
**override that default for a given band** from the logger — e.g. "on 20m use the OCF, not the
beam" — without giving up the AG's own band detection.

### Setup

In N1MM+: **Config → Configure Ports… → Broadcast Data**, enable **Radio**, and point it at the
machine that will run the bridge (default UDP port 12060). The `127.0.0.1` default only reaches
software on the N1MM PC itself.

Add the `{auxantsel NN}` macro to a function key, where `NN` is N1MM's own **aux antenna number**:

```
F11 OCF,{auxantsel 01}
```

N1MM resolves that number to a *name* from its antenna table and broadcasts the name in
`<AuxAntSelectedName>`. **That name must match the AG antenna name** (case-insensitive) — it is
what this bridge matches on.

Match on the name, not the number. They are unrelated: on the reference station `{auxantsel 01}`
broadcasts `AuxAntSelected=1` for an antenna that is number **4** on the AG. A client keying on
the number selects the wrong antenna, silently.

> **Repeating the macro for redundancy does not work.** `{auxantsel 01} {auxantsel 01}
> {auxantsel 01}` on one function key produces a single UDP packet, not three — N1MM emits a
> packet on state *change*, and three identical calls are one change. Verified by capture. The
> selection is sent exactly once per keypress; press the key again if a switch does not happen.

### Watch the traffic first

```bash
python n1mm_listen.py            # decoded summary
python n1mm_listen.py --raw      # full XML
```

This is read-only and never touches the AG. Use it to confirm packets arrive and that
`AuxAntSelectedName` carries the name you expect.

### Run the bridge

```bash
python ag_n1mm_control.py                       # DRY RUN - reports only, changes nothing
python ag_n1mm_control.py --live                # actually switches antennas
python ag_n1mm_control.py --live --station STATION1  # only act on one StationName
```

```
[192.168.1.50] STATION1 radio 1 requests 'OCF'
    OK: port 1 -> OCF (antenna 4)
```

`RadioNr` 1 and 2 map to AG ports A and B. The antenna's band mask is checked against the port's
current band before any write.

**Presses during a transmission are queued, not dropped.** Switching relays under RF damages the
contacts, so a macro pressed while `IsTransmitting=True` is held and applied the moment transmit
ends. N1MM emits a packet as soon as `IsTransmitting` changes, so the delay is milliseconds
rather than a wait for the next heartbeat.

```
[192.168.1.50] STATION1 radio 1 requests 'OCF'
    QUEUED 'OCF' for port 1 (transmitting; band 5)
[192.168.1.50] applying queued request: OK: port 1 -> OCF (antenna 4)
```

A queued request is dropped rather than applied if the operator changes band before transmit ends
(the request was about the band they were on, and the AG keeps a separate antenna per band), or
if it goes stale after 60 seconds. A newer press replaces an older queued one.

### Behavior worth understanding

**Selections persist across band changes — the AG does this for you.** The Antenna Genius
remembers the antenna chosen for each band and restores it when that band comes back, so a
selection made by the macro survives band changes with no help from this script.

This was verified with nothing but a read-only poller running: HF_Beam selected on 15m and OCF on
10m, then toggling between the two bands made the AG alternate antennas by itself. The bridge
therefore contains no re-assertion logic — it would be redundant, and it would fight antenna
changes made by hand at the AG utility.

The table is held in flash and **survives a power cycle**: after cutting power at the supply, both
bands still restored their own antenna. Set your preference for a band once and it stays set.

**`{auxantsel}` is a one-shot.** N1MM sends the antenna name in exactly one packet, then reverts
the field to blank on the next heartbeat. If that datagram is lost, the switch simply does not
happen and there is no retry — press the key again. Running the bridge on the same machine as
N1MM makes loss unlikely.

**`<Antenna>` is not used.** It would be a better trigger — it repeats in every 10-second
heartbeat and is therefore self-correcting — but it reports `0` unless the N1MM Configurer
"Antennas" tab is populated.

---

## Firmware quirks

Verified on firmware 4.1.16. Each of these contradicts, or is absent from, the published API docs.

**Commands must be terminated with LF (`0x0A`), not the documented CR (`0x0D`).** A command ending
in a bare `\r` produces *no response at all* — the device buffers it and never dispatches, so the
client just times out. `\n` and `\r\n` both work; device replies are LF-terminated. These scripts
send CRLF to satisfy either reading.

**There are two response framings, and the client must know which to expect.** `list` commands
return one row per line followed by a terminating response with an empty message. Every other
command — including all `get` commands — returns *exactly one* response and no terminator. Waiting
for a terminator after `port get 1` hangs until the socket times out.

**`port set source=` silently accepts invalid values.** The firmware returns success (`0`) for any
string and falls back to `AUTO` for anything it does not recognize — so a typo like `BDC` reports
success while leaving the port in `AUTO`. The response code proves nothing here; `ag_control.py`
validates client-side *and* reads the value back to confirm.

**The `auto` flag does not survive a power cycle.** A port left at `auto=0 source=BCD` comes back
up as `auto=1 source=BCD`. `source` is persisted; `auto` is not. The per-band antenna table *is*
persisted.

**Undocumented fields:** `antenna list` rows include `hotkey=`, and `conf get` returns
`bandcheck=`. Parse key/value pairs permissively rather than by fixed position.

**Selecting an antenna does not require `MANUAL`.** The docs' example pairs manual antenna
selection with `auto=0 source=MANUAL`, but writing `rxant=`/`txant=` while the port stays in
`auto=1 source=BCD` is accepted and holds — until the next band change.

---

## Protocol reference

The Antenna Genius Ethernet API uses port 9007 for both transports: UDP for the once-per-second
discovery broadcast, TCP for commands and status. Official documentation is the
[4O3A genius-api-docs wiki](https://github.com/4o3a/genius-api-docs/wiki).

The `docs/` directory here is a convenience mirror of that wiki. It is **4O3A's documentation,
not part of this project's MIT license**, and is included for offline reference only — always
treat the upstream wiki as authoritative, and see [Firmware quirks](#firmware-quirks) for the
places where observed behavior differs from it.

`CLAUDE.md` holds the accumulated protocol findings in a form intended for AI coding assistants;
it is also a reasonable contributor's summary of how the API actually behaves.

---

## Disclaimer

Not affiliated with or endorsed by 4O3A Signature. Antenna Genius is their product. Use at your
own risk — this software drives RF switching hardware, and the safety interlocks here are
best-effort, not a substitute for correct station wiring and sequencing.

## License

MIT — see [LICENSE](LICENSE). The MIT grant covers the software only, not the mirrored 4O3A
documentation under `docs/`.
