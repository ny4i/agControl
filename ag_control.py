#!/usr/bin/env python3
"""Command-line control for the 4O3A Antenna Genius -- reference implementation.

Built on the AGClient in ag_list_antennas.py.

    ag_control.py status                  show both radio ports
    ag_control.py status --port 1         show one port
    ag_control.py antennas                list antennas with their band masks
    ag_control.py bands                   list band slots and frequency ranges

    ag_control.py select 1 OCF            select antenna by name (case-insensitive)
    ag_control.py select 1 4              ...or by number -- same antenna
    ag_control.py select 1 OCF --rx-only  set only the RX antenna
    ag_control.py select 1 none           clear the selection (antenna 0)

    ag_control.py mode 1 BCD              band source: AUTO MANUAL BCD FLEX LAN
    ag_control.py mode 1 MANUAL --band 5  MANUAL needs a band to be useful
    ag_control.py mode 1 BCD --auto 1     set the auto flag explicitly

Global options: --host, --dry-run, --force, --timeout.

Two firmware behaviors this script defends against, both verified on 4.1.16:

1. 'port set <n> source=<x>' accepts ANY string and returns success, silently
   falling back to AUTO for anything it does not recognize. A typo such as
   'BDC' would report success while leaving the port in AUTO. Every source is
   therefore checked against VALID_SOURCES here, and every write is read back
   and verified.

2. Selecting an antenna does NOT require leaving automatic band mode -- but a
   band change under source=BCD makes the AG revert to its own per-band
   default, discarding the selection. That is expected, not a failure.
"""

import argparse
import os
import sys

from ag_list_antennas import (
   AGClient,
   AGError,
   decode_band_mask,
   format_band_slots,
   format_table,
   parse_kv_message,
)

# Override per station without editing the source: set AG_HOST, or pass --host.
DEFAULT_HOST = os.environ.get("AG_HOST", "192.168.73.193")

# Verified by probing the device: anything outside this set is accepted by the
# firmware but silently becomes AUTO.
VALID_SOURCES = ("AUTO", "MANUAL", "BCD", "FLEX", "LAN")

# Antenna 0 means "no antenna selected" per the API docs.
NO_ANTENNA = "0"


def fetch_antennas(client):
   """Return [(id, fields)] for every antenna."""
   return [
      parse_kv_message(message, "antenna")
      for message in client.send_command("antenna list")
   ]


def fetch_bands(client):
   return [
      parse_kv_message(message, "band")
      for message in client.send_command("band list")
   ]


def fetch_port(client, port_id):
   message = client.send_command("port get %d" % port_id)[0]
   _, fields = parse_kv_message(message, "port")
   return fields


def resolve_antenna(client, token):
   """Resolve a user-supplied antenna name or number to (id, fields).

   Numbers are matched first so a purely numeric argument is unambiguous;
   names are matched case-insensitively. Raises AGError with the available
   choices when nothing matches, rather than guessing.
   """
   antennas = fetch_antennas(client)

   if token.lower() in ("none", "0"):
      return NO_ANTENNA, {"name": "(none)", "tx": "FFFF", "rx": "FFFF"}

   for antenna_id, fields in antennas:
      if token == antenna_id:
         return antenna_id, fields

   matches = [
      (antenna_id, fields)
      for antenna_id, fields in antennas
      if fields.get("name", "").lower() == token.lower()
   ]
   if len(matches) == 1:
      return matches[0]
   if len(matches) > 1:
      raise AGError(
         "%r is ambiguous: matches antennas %s"
         % (token, ", ".join(antenna_id for antenna_id, _ in matches))
      )

   raise AGError(
      "no antenna named or numbered %r. Available: %s"
      % (token, ", ".join("%s=%s" % (i, f.get("name")) for i, f in antennas))
   )


def require_idle(fields, port_id, force):
   """Refuse to switch a port that is transmitting.

   Hot-switching the AG's relays under RF damages the contacts, and the AG
   cannot see a keyed amplifier. --force exists for bench work only.
   """
   if fields.get("tx") == "0":
      return
   if force:
      print("WARNING: port %d reports tx=%s; proceeding because --force was given"
            % (port_id, fields.get("tx")), file=sys.stderr)
      return
   raise AGError(
      "port %d reports tx=%s (transmitting). Refusing to switch; use --force to override"
      % (port_id, fields.get("tx"))
   )


def cmd_status(client, args):
   port_ids = [args.port] if args.port else [1, 2]
   antennas = dict(fetch_antennas(client))

   rows = []
   for port_id in port_ids:
      f = fetch_port(client, port_id)

      def antenna_label(key):
         value = f.get(key, NO_ANTENNA)
         if value == NO_ANTENNA:
            return "none"
         return "%s (%s)" % (value, antennas.get(value, {}).get("name", "?"))

      rows.append([
         "%s (%s)" % (port_id, "A" if port_id == 1 else "B"),
         f.get("source", "?"),
         f.get("auto", "?"),
         f.get("band", "?"),
         antenna_label("rxant"),
         antenna_label("txant"),
         f.get("tx", "?"),
         f.get("inhibit", "?"),
      ])

   print(format_table(
      ["PORT", "SOURCE", "AUTO", "BAND", "RX ANT", "TX ANT", "TX", "INH"], rows
   ))
   return 0


def cmd_antennas(client, args):
   rows = []
   for antenna_id, f in fetch_antennas(client):
      rows.append([
         antenna_id,
         f.get("name", ""),
         format_band_slots(decode_band_mask(f.get("tx", "0"))),
         format_band_slots(decode_band_mask(f.get("rx", "0"))),
         f.get("tx", ""),
         f.get("rx", ""),
      ])
   print(format_table(["ID", "NAME", "TX BANDS", "RX BANDS", "TXMASK", "RXMASK"], rows))
   return 0


def cmd_bands(client, args):
   rows = []
   for band_id, f in fetch_bands(client):
      rows.append([
         band_id,
         f.get("name", ""),
         "%.3f" % float(f.get("freq_start", 0)),
         "%.3f" % float(f.get("freq_stop", 0)),
      ])
   print(format_table(["SLOT", "NAME", "START MHz", "STOP MHz"], rows))
   return 0


def cmd_select(client, args):
   antenna_id, fields = resolve_antenna(client, args.antenna)
   before = fetch_port(client, args.port)
   require_idle(before, args.port, args.force)

   band = int(before.get("band", "0"))

   # An antenna is only wired for the bands in its mask. Selecting one outside
   # that set is almost always a mistake, so it is refused by default.
   if antenna_id != NO_ANTENNA:
      allowed = decode_band_mask(fields.get("tx", "0"))
      if band not in allowed:
         message = (
            "%s (antenna %s) is not enabled on band %d; its TX mask covers %s"
            % (fields.get("name"), antenna_id, band, format_band_slots(allowed))
         )
         if not args.force:
            raise AGError(message + ". Use --force to override")
         print("WARNING: %s; proceeding because --force was given" % message,
               file=sys.stderr)

   assignments = []
   if not args.tx_only:
      assignments.append("rxant=%s" % antenna_id)
   if not args.rx_only:
      assignments.append("txant=%s" % antenna_id)

   command = "port set %d %s" % (args.port, " ".join(assignments))

   if args.dry_run:
      print("DRY RUN: would send %r" % command)
      print("         port %d currently rxant=%s txant=%s band=%s"
            % (args.port, before.get("rxant"), before.get("txant"), before.get("band")))
      return 0

   client.send_command(command)

   after = fetch_port(client, args.port)
   failures = []
   if not args.tx_only and after.get("rxant") != antenna_id:
      failures.append("rxant=%s" % after.get("rxant"))
   if not args.rx_only and after.get("txant") != antenna_id:
      failures.append("txant=%s" % after.get("txant"))

   if failures:
      raise AGError(
         "port %d did not take the selection (expected %s, got %s)"
         % (args.port, antenna_id, ", ".join(failures))
      )

   print("port %d -> %s (antenna %s)  rxant=%s txant=%s band=%s"
         % (args.port, fields.get("name"), antenna_id,
            after.get("rxant"), after.get("txant"), after.get("band")))
   return 0


def cmd_mode(client, args):
   source = args.source.upper()
   if source not in VALID_SOURCES:
      # Caught here because the device would accept it and silently use AUTO.
      raise AGError(
         "invalid band source %r. Valid: %s" % (args.source, ", ".join(VALID_SOURCES))
      )

   before = fetch_port(client, args.port)
   require_idle(before, args.port, args.force)

   assignments = ["source=%s" % source]

   if args.auto is not None:
      assignments.append("auto=%d" % args.auto)
   if args.band is not None:
      if not 0 <= args.band <= 15:
         raise AGError("band must be 0-15, got %d" % args.band)
      assignments.append("band=%d" % args.band)

   command = "port set %d %s" % (args.port, " ".join(assignments))

   if args.dry_run:
      print("DRY RUN: would send %r" % command)
      print("         port %d currently source=%s auto=%s band=%s"
            % (args.port, before.get("source"), before.get("auto"), before.get("band")))
      return 0

   client.send_command(command)

   after = fetch_port(client, args.port)
   # The firmware reports success even when it ignored the value, so the
   # read-back is the only real confirmation.
   if after.get("source") != source:
      raise AGError(
         "port %d source is %r, not %r -- the firmware silently rejected it"
         % (args.port, after.get("source"), source)
      )

   print("port %d -> source=%s auto=%s band=%s"
         % (args.port, after.get("source"), after.get("auto"), after.get("band")))

   if source == "MANUAL" and args.band is None:
      print("note: MANUAL holds band=%s until changed; the AG will no longer "
            "follow the radio" % after.get("band"), file=sys.stderr)
   return 0


def build_parser():
   parser = argparse.ArgumentParser(
      description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
   )
   parser.add_argument("--host", default=DEFAULT_HOST, help="Antenna Genius IP address")
   parser.add_argument("--timeout", type=float, default=5.0, help="socket timeout, seconds")
   parser.add_argument("--dry-run", action="store_true",
                       help="show what would be sent without changing anything")
   parser.add_argument("--force", action="store_true",
                       help="override the transmit and band-mask safety checks")

   sub = parser.add_subparsers(dest="command", required=True)

   p_status = sub.add_parser("status", help="show radio port state")
   p_status.add_argument("--port", type=int, choices=(1, 2), help="port (default: both)")
   p_status.set_defaults(func=cmd_status)

   sub.add_parser("antennas", help="list antennas").set_defaults(func=cmd_antennas)
   sub.add_parser("bands", help="list band slots").set_defaults(func=cmd_bands)

   p_select = sub.add_parser("select", help="select an antenna on a port")
   p_select.add_argument("port", type=int, choices=(1, 2), help="radio port (1=A, 2=B)")
   p_select.add_argument("antenna", help="antenna name or number, or 'none'")
   group = p_select.add_mutually_exclusive_group()
   group.add_argument("--rx-only", action="store_true", help="set only the RX antenna")
   group.add_argument("--tx-only", action="store_true", help="set only the TX antenna")
   p_select.set_defaults(func=cmd_select)

   p_mode = sub.add_parser("mode", help="set the band source for a port")
   p_mode.add_argument("port", type=int, choices=(1, 2), help="radio port (1=A, 2=B)")
   p_mode.add_argument("source", help="band source: %s" % ", ".join(VALID_SOURCES))
   p_mode.add_argument("--auto", type=int, choices=(0, 1), help="set the auto flag")
   p_mode.add_argument("--band", type=int, help="band slot 0-15 (useful with MANUAL)")
   p_mode.set_defaults(func=cmd_mode)

   return parser


def main():
   args = build_parser().parse_args()

   try:
      with AGClient(args.host, timeout=args.timeout) as client:
         return args.func(client, args)
   except (OSError, AGError) as error:
      print("Error: %s" % error, file=sys.stderr)
      return 1


if __name__ == "__main__":
   raise SystemExit(main())
