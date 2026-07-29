# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Documentation only — a mirror of the 4O3A `genius-api-docs` GitHub wiki describing the
**Antenna Genius (AG) v4 Ethernet API**. There is no source code, build system, test suite,
or package manifest. All content lives in `docs/` as flat Markdown files named after their
wiki page (`Antenna-Genius-TCPIP-<command>.md`); inter-page links are bare wiki links
(`[antenna](Antenna-Genius-TCPIP-antenna)`) with no `.md` extension — preserve that form when
editing so the pages still resolve if republished to the wiki.

`docs/Home.md` is the entry point; `docs/Antenna-Genius-TCPIP-API.md` is the hub page that
indexes every command and status page. Adding a new command page means adding it to that
hub's command list too.

The project name (`antennaGeniusControl`) implies a client will be written against this API.
Anything about that client must come from the user or actual code — do not infer it from these docs.

## Protocol architecture (the part worth knowing before writing a client)

Two transports, both on port 9007:

- **UDP 9007 — discovery.** Every AG broadcasts a whitespace-delimited `AG key=value ...`
  line once per second (ip, port, v, serial, name, ports, antennas, mode, uptime). Serial is
  derived from the last three MAC octets and is the device's stable identity — it is also what
  `stack` and `flex` bindings key on.
- **TCP 9007 — command/status.** On connect the device sends a prologue `V<a.b.c> AG[ AUTH]`.
  The trailing ` AUTH` appears only for WAN-originated connections and means the client must
  send `auth code=<code>` (configured via `network set auth=`) before it may control the device.

Three message shapes on the TCP channel, all ASCII, `\r` (0x0D) terminated, always `.` as the
decimal separator regardless of locale:

| Direction | Form | Notes |
|---|---|---|
| client → device | `C<seq>\|<command>` | seq 1–255 |
| device → client | `R<seq>\|<hex_response>\|<message>` | seq echoed from the command |
| device → client | `S0\|<message>` | asynchronous status; `0` reserved |

Key consequences for a client implementation:

- **Two response shapes, and the client must know which to expect** — there is no other
  end-of-response signal. `list` commands (`antenna list`, `band list`, `group list`,
  `output list`, `stack list`, `flex list`) emit one `R<seq>|0|<row>` per row **then a
  terminating `R<seq>|0|` with an empty message**. Every other command — including all
  `get` commands — sends **exactly one response and no terminator**. Waiting for an empty
  terminator after `info get` or `port get 1` hangs until the socket times out.
- **Dispatch on seq, not order.** Async `S0|` statuses can interleave with `R` responses at any time.
- **Decide on the hex code, not the text.** `0` = success; anything else is failure. Codes are
  defined in `docs/Known-API-responses.md`: `0x001` invalid format, `0x010` unknown command,
  `0x020` invalid parameters, `0x030` invalid subscription object, `0x0FF` not authorized.
- **Statuses require subscription.** Nothing is pushed until `sub <object> <id|all>`
  (`port`, `relay`, `antenna`, `group`, `output`); `unsub` reverses it.
- **`antenna reload` / `output reload` statuses carry no data** — they are invalidation signals.
  On receipt the client must re-issue the corresponding `list` command(s) to refresh its cache.
- **Keepalive is opt-in and strict.** After `keepalive enable` the device expects a `ping` every
  second; 5 seconds of silence closes the socket.
- **Some commands reboot the device** (~20 s unresponsive, all connections dropped): `reboot`,
  `conf init`, and `network set` when DHCP changes or a static address changes.

## Domain model

- **Radio ports** — 1 = A, 2 = B. Each has a band source (`auto`/`source`), a detected `band`,
  and selected `rxant`/`txant`.
- **Bands** — fixed slots 0–15; slot 0 is reserved for "None" and cannot be set. Band detection
  is by frequency range (`freq_start`/`freq_stop`) against a frequency source (FlexRadio, LAN).
- **Antennas** — carry three 16-bit hex bitmasks (`tx`, `rx`, `inband`) where bit *n* means
  "available on band slot *n*". Masks are transmitted as bare hex without `0x` (e.g. `0007`).
- **Groups / outputs** — outputs belong to a group; a group activates in `ANT` mode (a specific
  antenna selected) or `BAND` mode (any band in its `bands` mask active). `output init` is
  mandatory before configuring groups or outputs. Output `state` is a hex relay bitmap, echoed
  back live in the `S0|relay tx= rx= state=` status.
- **Stack** — two AGs paired by serial; `mode` 0 = A/B switch, 1 = using ports 7 & 8. Changing
  stack mode changes the antenna list, so it emits an `antenna reload` status.

## Verified against real hardware (firmware 4.1.16)

These were confirmed on a live device and contradict the docs. Trust these over `docs/`:

- **The command terminator is LF (0x0A), not the documented CR (0x0D).** A command ending in a
  bare `\r` gets *no response whatsoever* — the device buffers it and never dispatches, so the
  client just times out. `\n` and `\r\n` both work. Device replies are LF-terminated.
  `ag_list_antennas.py` sends CRLF to satisfy both readings.
- **`antenna list` rows include an undocumented `hotkey=` field**, e.g.
  `antenna 1 name=HF_Beam tx=03E0 rx=03E0 inband=0000 hotkey=0`. Parse key=value pairs
  permissively rather than by fixed position or a fixed field set.
- **`get` commands send no empty terminator** (see above) — the docs' "at least one response
  will be sent" wording misleads here.
- **`conf get` returns an undocumented `bandcheck=1` field.**
- **Valid `port set source=` values are `AUTO`, `MANUAL`, `BCD`, `FLEX`, `LAN`** — the docs never
  enumerate them. **The firmware accepts any other string, returns success (`0`), and silently
  falls back to `AUTO`.** A typo such as `BDC` therefore reports success while leaving the port
  in `AUTO`. Validate the value client-side *and* read back with `port get` to confirm; the
  response code alone proves nothing here.
- Setting `source=` also changes `auto` as a side effect (setting `source=LAN` left `auto=0`),
  so pass `auto=` explicitly whenever it matters.
- **Antenna selection sticks while the port stays in automatic band mode.** Writing
  `port set 1 rxant=N txant=N` with `auto=1 source=BCD` left untouched was accepted and still
  held 4 s later — the docs' `auto=0 source=MANUAL` example is not required just to choose an
  antenna. This means the AG can keep owning band detection while a client owns only the
  antenna-within-band choice. (Not yet tested: whether a BCD *band change* resets the antenna
  to that band's default.)
- LAN connections get the prologue `V4.1.16 AG` with no ` AUTH` suffix, as documented.
- `keepalive enable` did **not** persist across connections: an idle connection with no `ping`
  survived well past the documented 5-second cutoff. Treat keepalive as per-connection opt-in.

Reference station used for verification (re-query your own; don't trust this):
hardware 4.0, 2 radio ports / 8 antenna ports, stacking disabled, no groups or outputs
configured, a FlexRadio visible to `flex list`.

## N1MM+ RadioInfo integration (in progress)

`n1mm_listen.py` is a read-only capture tool for the logger's UDP `RadioInfo` broadcasts.
Confirmed live on this station:

- Broadcasts arrive on **UDP 12060**, sourced from the station PC's LAN address rather than
  loopback, roughly every 10 s plus on change — matching the documented behavior.
- N1MM also binds **12080** and **13010** exclusively for its own use; those cannot be co-bound,
  and are unrelated to the outbound broadcast.
- **`<Antenna>` reports `0`** — the N1MM Configurer "Antennas" tab has no entries, so this field
  carries no usable selection. Any design keyed on `<Antenna>` is inert until that tab is filled in.
- `<Freq>`/`<TXFreq>` are in tens of Hz (`1404080` = 14.040800 MHz) and are reliable; they map
  cleanly onto the AG `band list` ranges (14.0408 falls in band 5, matching the port's `band=5`).

Hardware safety rule for any control path: never issue `port set` unless N1MM reports
`IsTransmitting=False` **and** a fresh `port get` reports `tx=0`. Hot-switching the AG's relays
under RF damages the contacts, and the AG cannot detect a keyed amplifier on its own.

## Known documentation defects

Copy these forward only if a fix is requested — but do not treat them as protocol truth:

- `docs/Antenna-Genius-TCPIP-keepalive.md` and `-conf.md` reuse a wrong or copy-pasted
  description line (keepalive is described as "Authorizes the client…").
- `docs/Antenna-Genius-TCPIP-port.md` and `-conf.md` show responses beginning with `C` where
  the response prefix must be `R`.
- `docs/Antenna-Genius-TCPIP-stack.md` SET synopsis omits `set` in its example.
- `docs/Antenna-Genius-TCPIP-output.md` SET example passes `mask=01`, a parameter not in the
  documented synopsis.
- `docs/Antenna-Genius-TCPIP-sub.md` says "the following response will be generated" for a
  missing object but shows nothing; the code is `0x030` per `Known-API-responses.md`.
- `sub`/`unsub` list `antenna`, `group`, and `output` as objects but only `port` and `relay`
  have documented subscribe examples.
- `port get` documented output omits `inband=`, which the `PORT` status message includes.
