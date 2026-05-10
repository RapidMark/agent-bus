#!/usr/bin/env python3
"""Send a message to another agent on the HTTP message bus.

Configuration (env vars):
  AGENT_BUS_URL    base URL of the bus (required)
  AGENT_BUS_UA     User-Agent header (default: "Mozilla/5.0")
  AGENT_NAME       this agent's name as appears in the `from` field
                   (default: "agent")

Usage:
  echo "hello dell" | AGENT_BUS_URL=... AGENT_NAME=spark bus_send.py dell

  AGENT_BUS_URL=... AGENT_NAME=spark bus_send.py dell <<'MSG'
  multi-line
  message
  MSG

Returns the bus's POST /send JSON ack on stdout.
"""
import json
import os
import sys
import urllib.request


def main(to: str) -> None:
    url_base = os.environ.get("AGENT_BUS_URL")
    if not url_base:
        print("AGENT_BUS_URL env var required", file=sys.stderr)
        sys.exit(2)
    ua = os.environ.get("AGENT_BUS_UA", "Mozilla/5.0")
    sender = os.environ.get("AGENT_NAME", "agent")
    send_url = f"{url_base.rstrip('/')}/send"

    body = sys.stdin.read()
    payload = {"from": sender, "to": to, "msg": body}
    req = urllib.request.Request(
        send_url,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"User-Agent": ua, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        print(r.read().decode())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: AGENT_BUS_URL=... AGENT_NAME=foo bus_send.py <to-agent>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
