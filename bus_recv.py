#!/usr/bin/env python3
"""Resilient agent-bus listener — survives transient JSON / network errors.

Reads messages addressed to this agent from an HTTP message bus and prints them
to stdout, one event-block per message. Designed to run under Claude Code's
Monitor tool (each printed block becomes a notification) or as a standalone
daemon piped to a log file.

Configuration (env vars):
  AGENT_BUS_URL      base URL of the bus (default: $AGENT_BUS_URL or "set me")
  AGENT_BUS_UA       User-Agent header (default: "Mozilla/5.0" — many bus
                     deployments sit behind a WAF that rejects script UAs;
                     the Mozilla string keeps long-poll requests open)
  AGENT_BUS_CHANNEL  channel for traffic isolation (default: "default"). Only
                     messages sent on the same channel are received. Lets the
                     same bus carry multiple isolated agent-conversations.
  AGENT_BUS_STATE_DIR  dir for the persisted `since` cursor (default:
                     "C:/tmp/bus_state"). On start the listener resumes from the
                     last-seen ts so messages sent while it was down are still
                     delivered (offline catch-up), keyed per (name, channel).
  AGENT_BUS_NO_CURSOR  set to 1 to disable cursor persistence and always start
                     at "now" (the old behavior).

Usage:
  AGENT_BUS_URL=https://your-bus.example.com python bus_recv.py my-agent-name
  AGENT_BUS_CHANNEL=cloudhands AGENT_BUS_URL=... python bus_recv.py ch-claude-1

Protocol (works against any bus implementing /recv?to=&since=&block=true):
  GET {AGENT_BUS_URL}/recv?to={name}&since={float_seconds}&block=true
       -> {"messages": [{"ts": float, "from": str, "msg": str}, ...],
           "now":      float}

  Each message has at minimum {ts, from, msg}; additional fields are passed
  through unchanged.

Stdout format (one block per message):
  [HH:MM:SSZ] from {sender}:
  {full message body}
  ---
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request


# Windows consoles default to cp1252, which raises UnicodeEncodeError on any
# non-Latin-1 character in a message body (em-dashes, arrows, emoji, CJK, …).
# Without this the listener crash-loops on the first such message because the
# `since` cursor never advances past the offending /recv batch. errors=replace
# is a deliberate downgrade: a single message with weird bytes still prints
# (with replacement chars) instead of taking the whole listener down.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def _cursor_path(name: str, channel: str) -> str:
    """Per-(name, channel) cursor file so distinct agents don't clobber."""
    state_dir = os.environ.get("AGENT_BUS_STATE_DIR", "C:/tmp/bus_state")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{name}_{channel}")
    return os.path.join(state_dir, f"cursor_{safe}.txt")


def _load_cursor(path: str):
    """Return the persisted `since` float, or None if absent/unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def _save_cursor(path: str, since: float) -> None:
    """Atomically persist `since` so a restart replays messages sent while down."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(repr(since))
        os.replace(tmp, path)
    except OSError:
        pass


def main(name: str) -> None:
    url_base = os.environ.get("AGENT_BUS_URL")
    if not url_base:
        print("AGENT_BUS_URL env var required", file=sys.stderr)
        sys.exit(2)
    ua = os.environ.get("AGENT_BUS_UA", "Mozilla/5.0")
    channel = os.environ.get("AGENT_BUS_CHANNEL", "default")
    recv_url = f"{url_base.rstrip('/')}/recv"

    # Resume from the persisted cursor so messages sent while this listener was
    # down still get delivered on the next start (offline catch-up). First run
    # (no cursor file) starts at "now". Disable with AGENT_BUS_NO_CURSOR=1.
    cursor_path = _cursor_path(name, channel)
    use_cursor = os.environ.get("AGENT_BUS_NO_CURSOR", "") not in ("1", "true", "True")
    since = (_load_cursor(cursor_path) if use_cursor else None) or time.time()
    while True:
        try:
            req = urllib.request.Request(
                f"{recv_url}?to={name}&since={since}&block=true&channel={channel}",
                headers={"User-Agent": ua},
            )
            with urllib.request.urlopen(req, timeout=35) as r:
                raw = r.read()
            data = json.loads(raw)
            for m in data.get("messages") or []:
                ts = m.get("ts", 0)
                hh = time.strftime("%H:%M:%S", time.gmtime(ts))
                print(f"[{hh}Z] from {m.get('from')}:")
                print(m.get("msg", ""))
                print("---")
                sys.stdout.flush()
                since = max(since, ts)
            if data.get("now"):
                since = max(since, data["now"])
            if use_cursor:
                _save_cursor(cursor_path, since)
        except (json.JSONDecodeError, urllib.error.URLError, TimeoutError) as e:
            print(f"# transient: {type(e).__name__}", file=sys.stderr, flush=True)
            time.sleep(2)
        except Exception as e:
            print(f"# unexpected: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(2)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: AGENT_BUS_URL=... bus_recv.py <agent-name>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
