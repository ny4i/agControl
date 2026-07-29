#!/usr/bin/env python3
"""Select an Antenna Genius antenna from N1MM+ {auxantsel} macro presses.

Listens for N1MM RadioInfo UDP broadcasts. When a packet carries a non-blank
<AuxAntSelectedName>, the matching AG antenna (matched by name) is selected on
the AG port corresponding to <RadioNr>.

Defaults to DRY RUN: it reports what it would do and does not touch the AG.
Pass --live to actually switch antennas.

Design notes
------------
Why match on the NAME and not <AuxAntSelected>: verified on this station, a
press reporting antenna "OCF" sends AuxAntSelected=1, while OCF is antenna 4 on
the AG. The number is N1MM's own aux-antenna index and does not correspond to
AG antenna IDs; matching on it would silently select the wrong antenna.

Why not <Antenna>: it reports 0 on this station because the N1MM Configurer
"Antennas" tab is empty, so it carries no selection. If that tab is ever filled
in, <Antenna> becomes the better primary trigger because it repeats in every
10-second heartbeat and is therefore self-correcting, whereas AuxAntSelectedName
is sent in exactly one packet and is lost forever if that packet is dropped.

Why no re-assertion logic: the AG remembers the antenna selected for each band
and restores it when that band comes back. Verified with nothing but a
read-only poller running -- selecting HF_Beam on 15m and OCF on 10m, then
toggling between them, made the AG alternate antennas on its own. A selection
made here therefore persists across band changes with no further help, and a
script that re-asserted it would only risk fighting changes made by hand at
the AG utility.

Safety: hot-switching the AG's relays under RF damages the contacts. A switch
is only made when N1MM reports IsTransmitting=False. A macro pressed during a
transmission is QUEUED and applied the moment transmit ends, rather than being
dropped -- N1MM emits a packet as soon as IsTransmitting changes, so the delay
is milliseconds. The port's own tx= flag is also checked, but note it only
reports meaningfully on stations that wire PTT into the AG; where they do not,
it reads 0 permanently and proves nothing.
"""

import argparse
import os
import socket
import sys
import time

from ag_list_antennas import (
   AGClient,
   AGError,
   decode_band_mask,
   parse_kv_message,
)
from n1mm_listen import parse_radio_info

# Override per station without editing the source: set AG_HOST, or pass --ag-host.
DEFAULT_AG_HOST = os.environ.get("AG_HOST", "192.168.73.193")
DEFAULT_UDP_PORT = 12060

# A RadioInfo <RadioNr> maps onto an AG radio port (A=1, B=2).
RADIO_TO_PORT = {"1": 1, "2": 2}

# Re-read the antenna table at most this often; it only changes when the user
# renames antennas, and an 'antenna reload' status would be the proper trigger.
ANTENNA_CACHE_SECONDS = 60.0

# How long a request queued during transmit stays valid. Long enough for any
# realistic over, short enough that a forgotten request does not fire much
# later and surprise the operator.
PENDING_TIMEOUT_SECONDS = 60.0


class AntennaController:
   """Applies antenna selections to the AG, with validation and safety gates.

   Holds a lazily-opened AG connection. The AG closes idle sockets and the
   station network can drop, so every operation is prepared to reconnect once
   rather than assuming the socket from the last packet is still usable.
   """

   def __init__(self, host, live=False, timeout=5.0):
      self.host = host
      self.live = live
      self.timeout = timeout
      self._client = None
      self._antennas = None
      self._antennas_read_at = 0.0

      # Requests received while transmitting, held until it is safe to apply.
      # port_id -> (antenna_name, band_at_request, monotonic_timestamp).
      # One entry per port: a newer press replaces an older one, since the
      # operator's latest instruction is the one they meant.
      self._pending = {}

   def close(self):
      if self._client is not None:
         self._client.close()
         self._client = None

   def _connect(self):
      if self._client is None:
         self._client = AGClient(self.host, timeout=self.timeout)
         self._client.connect()
      return self._client

   def _command(self, command, multipart=None):
      """Run a command, transparently reconnecting once on a dropped socket."""
      try:
         return self._connect().send_command(command, multipart=multipart)
      except (OSError, AGError):
         # Drop the (possibly half-open) socket and retry exactly once. A
         # second failure is a real fault and propagates to the caller.
         self.close()
         return self._connect().send_command(command, multipart=multipart)

   def antennas(self, force=False):
      """Return {lowercased name: (id, fields)}, cached briefly."""
      now = time.monotonic()
      stale = now - self._antennas_read_at > ANTENNA_CACHE_SECONDS
      if self._antennas is None or stale or force:
         table = {}
         for message in self._command("antenna list"):
            antenna_id, fields = parse_kv_message(message, "antenna")
            table[fields.get("name", "").lower()] = (antenna_id, fields)
         self._antennas = table
         self._antennas_read_at = now
      return self._antennas

   def port_state(self, port_id):
      message = self._command("port get %d" % port_id)[0]
      _, fields = parse_kv_message(message, "port")
      return fields

   def select(self, port_id, antenna_name, n1mm_transmitting):
      """Select antenna_name on port_id. Returns a human-readable outcome.

      If the station is transmitting the request is queued rather than
      dropped, and applied by service_pending() once transmit ends. Pressing
      the macro mid-transmission should mean "switch as soon as it is safe",
      not "nothing happened".

      Refuses rather than guesses whenever anything does not line up: unknown
      name, or an antenna not legal on the port's current band.
      """
      if n1mm_transmitting:
         # port get is read-only and safe while transmitting. Record the band
         # the operator was on, so a queued request is not applied to a
         # different band later (see service_pending).
         band = int(self.port_state(port_id).get("band", "0"))
         self._pending[port_id] = (antenna_name, band, time.monotonic())
         return "QUEUED %r for port %d (transmitting; band %d)" % (
            antenna_name, port_id, band,
         )

      table = self.antennas()
      entry = table.get(antenna_name.lower())
      if entry is None:
         # Refresh once in case the antenna was renamed since we cached.
         entry = self.antennas(force=True).get(antenna_name.lower())
      if entry is None:
         return "REFUSED: no AG antenna named %r (known: %s)" % (
            antenna_name,
            ", ".join(sorted(f.get("name", "") for _, f in table.values())),
         )

      antenna_id, fields = entry
      state = self.port_state(port_id)

      # Re-check transmit state at the last possible moment before writing.
      if state.get("tx") != "0":
         return "REFUSED: AG port %d reports tx=%s" % (port_id, state.get("tx"))

      band = int(state.get("band", "0"))
      allowed = decode_band_mask(fields.get("tx", "0"))
      if band not in allowed:
         return "REFUSED: %s (antenna %s) is not valid on band %d (valid: %s)" % (
            fields.get("name"), antenna_id, band, allowed or "none",
         )

      if state.get("txant") == antenna_id and state.get("rxant") == antenna_id:
         return "no change: port %d already on %s (antenna %s)" % (
            port_id, fields.get("name"), antenna_id,
         )

      return self._apply(port_id, antenna_id, fields.get("name"), state.get("txant"))

   def service_pending(self, port_id, n1mm_transmitting):
      """Apply a queued selection once transmit ends. Returns a message or None.

      Call on every packet. N1MM sends a packet whenever IsTransmitting
      changes, so the end of a transmission is normally seen within
      milliseconds rather than at the next 10-second heartbeat.

      A queued request is discarded rather than applied if the operator has
      since changed band -- "give me the OCF" was said about the band they
      were on, and the AG keeps a separate antenna per band -- or if it has
      simply gone stale.
      """
      pending = self._pending.get(port_id)
      if pending is None or n1mm_transmitting:
         return None

      antenna_name, requested_band, requested_at = pending

      if time.monotonic() - requested_at > PENDING_TIMEOUT_SECONDS:
         del self._pending[port_id]
         return "dropped queued %r for port %d (stale)" % (antenna_name, port_id)

      current_band = int(self.port_state(port_id).get("band", "0"))
      if current_band != requested_band:
         del self._pending[port_id]
         return "dropped queued %r for port %d (band moved %d -> %d)" % (
            antenna_name, port_id, requested_band, current_band,
         )

      del self._pending[port_id]
      return "applying queued request: %s" % self.select(port_id, antenna_name, False)

   def _apply(self, port_id, antenna_id, antenna_name, previous_txant):
      """Write the selection and verify it took. Caller has validated already."""
      if not self.live:
         return "DRY RUN: would set port %d to %s (antenna %s), currently txant=%s" % (
            port_id, antenna_name, antenna_id, previous_txant,
         )

      self._command("port set %d rxant=%s txant=%s" % (port_id, antenna_id, antenna_id))
      after = self.port_state(port_id)
      if after.get("txant") != antenna_id:
         return "WARNING: set %s but port %d reports txant=%s" % (
            antenna_id, port_id, after.get("txant"),
         )
      return "OK: port %d -> %s (antenna %s)" % (port_id, antenna_name, antenna_id)


def main():
   parser = argparse.ArgumentParser(description=__doc__)
   parser.add_argument("--ag-host", default=DEFAULT_AG_HOST, help="Antenna Genius IP")
   parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT, help="RadioInfo port")
   parser.add_argument("--bind", default="0.0.0.0", help="local address to bind")
   parser.add_argument(
      "--live", action="store_true",
      help="actually switch antennas (default is a dry run that only reports)",
   )
   parser.add_argument(
      "--station", default=None,
      help="only act on packets with this StationName (default: any)",
   )
   args = parser.parse_args()

   sys.stdout.reconfigure(line_buffering=True)

   sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
   sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
   sock.bind((args.bind, args.udp_port))

   controller = AntennaController(args.ag_host, live=args.live)

   print("AG %s | RadioInfo %s:%d | %s"
         % (args.ag_host, args.bind, args.udp_port,
            "LIVE - will switch antennas" if args.live else "DRY RUN - no changes"))
   print("Waiting for {auxantsel} macro presses. Ctrl-C to stop.\n")

   try:
      while True:
         payload, sender = sock.recvfrom(65535)
         info = parse_radio_info(payload)
         if info is None:
            continue

         if args.station and info.get("StationName") != args.station:
            continue

         radio_nr = info.get("RadioNr", "")
         port_id = RADIO_TO_PORT.get(radio_nr)
         if port_id is None:
            continue

         transmitting = info.get("IsTransmitting", "").lower() == "true"
         name = info.get("AuxAntSelectedName", "")

         if name:
            print("[%s] %s radio %s requests %r"
                  % (sender[0], info.get("StationName", "?"), radio_nr, name))
            try:
               outcome = controller.select(port_id, name, transmitting)
            except (OSError, AGError) as error:
               outcome = "ERROR talking to AG: %s" % error
            print("    %s" % outcome)
            continue

         # Heartbeat, or a packet after the one-shot reverted to blank. No
         # selection to make -- the AG remembers the antenna for each band on
         # its own -- but this is where a request queued during transmit gets
         # applied, since N1MM sends a packet the moment IsTransmitting drops.
         try:
            outcome = controller.service_pending(port_id, transmitting)
         except (OSError, AGError) as error:
            outcome = "ERROR talking to AG: %s" % error
         if outcome:
            print("[%s] %s" % (sender[0], outcome))

   except KeyboardInterrupt:
      print("\nStopped.")
   finally:
      controller.close()
      sock.close()

   return 0


if __name__ == "__main__":
   raise SystemExit(main())
