#!/usr/bin/env python3
"""Listen for N1MM+/TR4W RadioInfo UDP broadcasts and dump the useful fields.

Read-only: this does NOT touch the Antenna Genius. Its purpose is to confirm
what the logger actually sends -- which port, which fields are populated, and
how AuxAntSelectedName behaves -- before any control logic is built on it.

In N1MM+: Config > Configure Ports... > Broadcast Data, tick "Radio" and set
the destination address/port. The default is 127.0.0.1:12060, which only
reaches software on the same PC; to reach another machine use that machine's
address or a broadcast address.
"""

import argparse
import socket
import sys
import time
import xml.etree.ElementTree as ElementTree

DEFAULT_PORT = 12060

# Fields worth watching for antenna-switching purposes.
FIELDS_OF_INTEREST = (
   "app",
   "StationName",
   "RadioNr",
   "Freq",
   "TXFreq",
   "Mode",
   "IsRunning",
   "IsTransmitting",
   "IsSplit",
   "Antenna",
   "AuxAntSelected",
   "AuxAntSelectedName",
   "ActiveRadioNr",
   "IsConnected",
)


def parse_radio_info(payload):
   """Parse a RadioInfo datagram into a dict, or return None if it is not one.

   Loggers broadcast several packet types (contactinfo, score, etc.) to the
   same port, so a non-RadioInfo document is expected and not an error.
   """
   try:
      root = ElementTree.fromstring(payload.decode("utf-8", errors="replace"))
   except ElementTree.ParseError:
      return None

   if root.tag != "RadioInfo":
      return None

   # .text is None for an empty element such as <Rotors></Rotors>.
   return {child.tag: (child.text or "").strip() for child in root}


def timestamp():
   """Wall-clock time to the millisecond.

   Resolution matters when measuring how far apart a logger spaces repeated
   packets: a burst arriving within a few milliseconds shares one loss event
   and buys much less redundancy than spaced retries.
   """
   now = time.time()
   return "%s.%03d" % (time.strftime("%H:%M:%S", time.localtime(now)), (now % 1) * 1000)


def format_frequency(raw_hz_tens):
   """RadioInfo frequencies are in tens of Hz with no delimiter.

   '352211' -> 3.522110 MHz. Returns the raw string if it is not numeric.
   """
   try:
      return "%.6f MHz" % (int(raw_hz_tens) * 10 / 1_000_000)
   except (TypeError, ValueError):
      return raw_hz_tens


def main():
   parser = argparse.ArgumentParser(description=__doc__)
   parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="UDP port to listen on")
   parser.add_argument(
      "--bind", default="0.0.0.0", help="local address to bind (0.0.0.0 = all interfaces)"
   )
   parser.add_argument("--raw", action="store_true", help="dump the raw XML of every datagram")
   args = parser.parse_args()

   # Python block-buffers stdout when it is redirected to a file or pipe, so a
   # long-running capture shows nothing until it exits -- and a force-kill
   # discards the buffer entirely. Line buffering keeps a redirected capture
   # readable while it runs, which is the normal way this tool gets used.
   sys.stdout.reconfigure(line_buffering=True)

   sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
   # Allow coexisting with other listeners (e.g. N1MM's own tools) on this port.
   sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
   sock.bind((args.bind, args.port))

   print("Listening for RadioInfo on %s:%d -- Ctrl-C to stop\n" % (args.bind, args.port))

   try:
      while True:
         payload, sender = sock.recvfrom(65535)

         if args.raw:
            print("--- %s  %s:%d ---" % (timestamp(), sender[0], sender[1]))
            print(payload.decode("utf-8", errors="replace"))

         info = parse_radio_info(payload)
         if info is None:
            if not args.raw:
               print("[%s:%d] non-RadioInfo datagram (%d bytes)" % (sender[0], sender[1], len(payload)))
            continue

         parts = []
         for field in FIELDS_OF_INTEREST:
            if field not in info:
               continue
            value = info[field]
            if field in ("Freq", "TXFreq"):
               value = format_frequency(value)
            parts.append("%s=%s" % (field, value if value != "" else "-"))

         print("[%s %s] %s" % (timestamp(), sender[0], "  ".join(parts)))

         # The field that drives name-based antenna selection. N1MM populates
         # it for exactly one packet per {auxantsel} keypress; a logger may be
         # configured to repeat the macro for redundancy over UDP.
         if info.get("AuxAntSelectedName"):
            print("   >>> AuxAntSelectedName = %r" % info["AuxAntSelectedName"])

   except KeyboardInterrupt:
      print("\nStopped.")
   finally:
      sock.close()

   return 0


if __name__ == "__main__":
   raise SystemExit(main())
