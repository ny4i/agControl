#!/usr/bin/env python3
"""List the antennas configured on a 4O3A Antenna Genius switch.

Exercises the minimum useful slice of the AG v4 TCP/IP API:
connect -> read prologue -> issue 'antenna list' -> collect the multi-part
response -> parse rows.

Protocol reference: docs/Antenna-Genius-TCPIP-API.md and
docs/Antenna-Genius-TCPIP-antenna.md
"""

import argparse
import os
import socket
import sys

# Override per station without editing the source: set AG_HOST, or pass --host.
DEFAULT_HOST = os.environ.get("AG_HOST", "192.168.73.193")
DEFAULT_PORT = 9007

# The API docs specify 0x0D (CR) as the command terminator, but that is WRONG
# for firmware 4.1.16: verified against a live device, a bare CR produces no
# response at all -- the device buffers the line and never dispatches it. Both
# LF and CRLF work, and the device's own replies are LF-terminated.
#
# CRLF is sent rather than bare LF so the command is also correctly framed by
# any firmware that really does parse on CR, as documented.
COMMAND_TERMINATOR = b"\x0d\x0a"

# From docs/Known-API-responses.md
RESPONSE_CODES = {
   0x000: "OK",
   0x001: "Invalid command format",
   0x010: "Unknown command",
   0x020: "Invalid command parameters",
   0x030: "Invalid subscription object",
   0x0FF: "Client not authorized",
}


class AGError(Exception):
   """A command was rejected by the device, or the device misbehaved."""


class AGClient:
   """Minimal synchronous client for the Antenna Genius TCP/IP API.

   Ownership/lifetime: the caller owns the instance and must call close(),
   or use it as a context manager. One command is in flight at a time; this
   is deliberately not thread-safe.

   Invariant: self._seq is the sequence number of the last command sent, and
   is echoed by the device on every response belonging to that command.
   """

   def __init__(self, host, port=DEFAULT_PORT, timeout=5.0):
      self.host = host
      self.port = port
      self.timeout = timeout
      self._sock = None
      self._buffer = b""
      self._seq = 0
      self.prologue = None

   def __enter__(self):
      self.connect()
      return self

   def __exit__(self, exc_type, exc_value, traceback):
      self.close()
      return False

   def connect(self):
      """Open the socket and consume the mandatory prologue line."""
      self._sock = socket.create_connection((self.host, self.port), self.timeout)
      self._sock.settimeout(self.timeout)

      # The device sends 'V<a.b.c> AG[ AUTH]' immediately on connect, every
      # time. It must be consumed before any command, or it will be mistaken
      # for a response.
      self.prologue = self.read_line()
      if not self.prologue.startswith("V"):
         raise AGError("Unexpected prologue from device: %r" % self.prologue)

      if self.prologue.endswith("AUTH"):
         # WAN-originated connection: an 'auth code=<code>' command is
         # required before the device will accept anything else.
         raise AGError(
            "Device requires authentication (prologue: %r). This script only "
            "supports LAN connections." % self.prologue
         )

   def close(self):
      if self._sock is not None:
         self._sock.close()
         self._sock = None

   def next_seq(self):
      """Sequence numbers are documented as 1-255, so wrap rather than grow."""
      self._seq = self._seq % 255 + 1
      return self._seq

   def read_line(self):
      """Read one device line, blocking until a terminator arrives.

      Handles CR, LF, and CRLF framing because the device-to-client
      terminator is not specified in the API documentation.
      """
      while True:
         index = min(
            (i for i in (self._buffer.find(b"\r"), self._buffer.find(b"\n")) if i >= 0),
            default=-1,
         )
         if index >= 0:
            line = self._buffer[:index]
            rest = self._buffer[index + 1:]
            # Collapse CRLF / LFCR so the next read does not see a blank line.
            if rest[:1] in (b"\r", b"\n") and rest[:1] != self._buffer[index:index + 1]:
               rest = rest[1:]
            self._buffer = rest
            return line.decode("ascii", errors="replace")

         chunk = self._sock.recv(4096)
         if not chunk:
            raise AGError("Connection closed by device while awaiting a response")
         self._buffer += chunk

   def send_command(self, command, multipart=None):
      """Send a command and return its response message(s) as a list.

      There are two response shapes, and the client MUST know which to expect
      because the device gives no other end-of-response signal (verified on
      firmware 4.1.16):

      * 'list' commands  -> zero or more rows, then a response with an empty
                            message that terminates the sequence.
      * everything else  -> exactly ONE response. For 'get' it carries the
                            payload; for set/ping/reboot the message is empty.

      Waiting for an empty terminator after a 'get' hangs until the socket
      times out, since none is ever sent.

      multipart=None auto-detects from the command verb; pass True/False
      explicitly for commands this heuristic does not cover.

      Asynchronous 'S0|' status lines may arrive at any time and are skipped
      here; a subscribing client would need to route them instead.
      """
      if multipart is None:
         multipart = command.split()[-1:] == ["list"] or " list " in command

      seq = self.next_seq()
      wire = "C%d|%s" % (seq, command)
      self._sock.sendall(wire.encode("ascii") + COMMAND_TERMINATOR)

      messages = []
      while True:
         line = self.read_line()

         if not line:
            continue

         if line.startswith("S"):
            # Unsolicited status. We never subscribe, so just ignore it.
            continue

         if not line.startswith("R"):
            raise AGError("Unrecognized line from device: %r" % line)

         parts = line[1:].split("|", 2)
         if len(parts) != 3:
            raise AGError("Malformed response from device: %r" % line)

         reply_seq, hex_code, message = parts

         if reply_seq != str(seq):
            # Belongs to a different command. With one command in flight this
            # should not happen; surface it rather than silently misattribute.
            raise AGError(
               "Response sequence %s does not match command sequence %d: %r"
               % (reply_seq, seq, line)
            )

         # Decisions are made on the hex code, never the message text.
         code = int(hex_code, 16)
         if code != 0:
            raise AGError(
               "Command %r failed: 0x%03X (%s)"
               % (command, code, RESPONSE_CODES.get(code, "unknown error code"))
            )

         if not multipart:
            # Exactly one response; an empty message means "OK, no payload".
            return [message] if message else []

         if message == "":
            return messages

         messages.append(message)


def parse_kv_message(message, expected_kind):
   """Parse 'antenna 3 name=Foo tx=0007 ...' into (id, {key: value}).

   Values are left as strings; the caller decides how to interpret them.
   Note that a user-defined name containing a space would break this
   whitespace split, but the device's own utility does not permit one.
   """
   tokens = message.split()
   if len(tokens) < 2 or tokens[0] != expected_kind:
      raise AGError("Expected a %r row, got: %r" % (expected_kind, message))

   fields = {}
   for token in tokens[2:]:
      if "=" not in token:
         raise AGError("Malformed field %r in: %r" % (token, message))
      key, value = token.split("=", 1)
      fields[key] = value

   return tokens[1], fields


def decode_band_mask(mask_hex):
   """Turn a 16-bit hex band mask into the list of band slots it enables.

   Bit n set means the antenna is available on band slot n (0-15).
   """
   try:
      mask = int(mask_hex, 16)
   except ValueError:
      raise AGError("Invalid band mask: %r" % mask_hex)

   return [bit for bit in range(16) if mask & (1 << bit)]


def format_band_slots(slots):
   """Render band slots compactly, collapsing runs: [1,2,3,5] -> '1-3,5'.

   Keeps the column narrow enough to stay readable when an antenna is enabled
   on most of the 16 slots.
   """
   if not slots:
      return "-"

   runs = []
   start = previous = slots[0]
   for slot in slots[1:]:
      if slot == previous + 1:
         previous = slot
         continue
      runs.append((start, previous))
      start = previous = slot
   runs.append((start, previous))

   return ",".join(
      str(low) if low == high else "%d-%d" % (low, high) for low, high in runs
   )


def format_table(headers, rows):
   """Render rows as a left-aligned table sized to the widest cell per column.

   The last column is not padded, so a long trailing value cannot introduce
   trailing whitespace.
   """
   widths = [
      max(len(str(row[column])) for row in [headers] + rows)
      for column in range(len(headers))
   ]

   lines = []
   for row in [headers] + rows:
      cells = [
         str(value).ljust(widths[column]) for column, value in enumerate(row)
      ]
      lines.append("  ".join(cells).rstrip())

   # Underline the header so the columns stay legible without box drawing.
   lines.insert(1, "  ".join("-" * width for width in widths))
   return "\n".join(lines)


def list_antennas(client):
   """Return [(id, fields)] for every antenna port on the device."""
   return [
      parse_kv_message(message, "antenna")
      for message in client.send_command("antenna list")
   ]


def main():
   parser = argparse.ArgumentParser(description=__doc__)
   parser.add_argument("--host", default=DEFAULT_HOST, help="AG IP address")
   parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="AG TCP port")
   parser.add_argument("--timeout", type=float, default=5.0, help="socket timeout, seconds")
   parser.add_argument(
      "--bands", action="store_true", help="show band slot numbers instead of raw masks"
   )
   args = parser.parse_args()

   try:
      with AGClient(args.host, args.port, args.timeout) as client:
         print("Connected to %s:%d -- %s" % (args.host, args.port, client.prologue))
         antennas = list_antennas(client)
   except (OSError, AGError) as error:
      print("Error: %s" % error, file=sys.stderr)
      return 1

   def show(fields, key):
      if args.bands:
         return format_band_slots(decode_band_mask(fields.get(key, "0")))
      return fields.get(key, "")

   rows = [
      [
         antenna_id,
         fields.get("name", ""),
         show(fields, "tx"),
         show(fields, "rx"),
         show(fields, "inband"),
      ]
      for antenna_id, fields in antennas
   ]

   print(format_table(["ID", "NAME", "TX", "RX", "INBAND"], rows))

   print("\n%d antenna(s)" % len(antennas))
   return 0


if __name__ == "__main__":
   sys.exit(main())
