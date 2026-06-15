#!/usr/bin/env python3
"""Stateful agent-bus consumer for Backend Claude.

Polls the bus for messages addressed to `backend` on the `servers` channel,
ACCUMULATES per-server state in C:/tmp/bus_state/{server}.json, and prints ONLY
notify-worthy lines to stdout (each becomes a Claude Code Monitor notification):

  DEPLOY  {server}  v=<old> -> v=<new>     (heartbeat version changed)
  SILENT  {server}  no heartbeat for <n>m  (>10 min gap; emitted once)
  WAKE    {server}  heartbeat resumed        (after a SILENT)
  E       {json}                            (new exception signature, or count
                                             crossing a threshold: 1/10/100/500/1k/5k/10k)
  CHAT    [hhmmZ] from {agent}: {msg}        (message from another Claude)

Why a durable file (not an inline Monitor command): the inline consumer was lost
when the session restarted, which silently stopped deploy/exception surfacing.
This script resumes from the existing state files so restarts don't re-alert on
history or lose accumulated counts.

Run under Monitor:
  PYTHONIOENCODING=utf-8 AGENT_BUS_URL=https://your-bus.example.com \
    AGENT_BUS_CHANNEL=servers python -u bus_monitor.py backend

Heartbeat/event envelope (from the prod tailer daemon, see Backend tailer.py):
  H {"server","ts","version","uptime_s","err_24h","spool"}
  E {"server","ts","sig","level","ex_type","message","exception","traceid"} (or
    a suppressed-summary variant with "n_suppressed")
Other messages (no E/H prefix) are Claude<->Claude chat.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

STATE_DIR = os.environ.get("AGENT_BUS_STATE_DIR", "C:/tmp/bus_state")
SILENCE_SECS = 600          # >10 min with no heartbeat = SILENT
THRESHOLDS = [1, 10, 100, 500, 1000, 5000, 10000, 50000]
MAX_LINE = 4096             # 4KB chat cap per emitted line

_state = {}                 # server -> dict (mirrors the on-disk json)


def emit(line):
    if len(line) > MAX_LINE:
        line = line[:MAX_LINE - 1] + "…"
    print(line, flush=True)


def load_state():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        for fn in os.listdir(STATE_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(STATE_DIR, fn), encoding="utf-8") as f:
                    s = json.load(f)
                if s.get("server"):
                    _state[s["server"]] = s
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        pass


def save_state(server):
    s = _state.get(server)
    if not s:
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, f"{server}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def server_state(server, now):
    s = _state.get(server)
    if s is None:
        s = {
            "server": server, "first_seen_ts": now, "last_hb_ts": now,
            "last_version": None, "last_spool": 0, "is_silent": False,
            "signatures": {},
        }
        _state[server] = s
    return s


def crossed_threshold(prev_count, new_count, last_emit):
    """Return the highest threshold newly crossed (and not already emitted), else None."""
    hit = None
    for t in THRESHOLDS:
        if new_count >= t > last_emit:
            hit = t
    return hit


def handle_heartbeat(rec, now):
    server = rec.get("server")
    if not server:
        return
    s = server_state(server, now)
    s["last_hb_ts"] = int(rec.get("ts") or now)
    s["last_spool"] = rec.get("spool", s.get("last_spool", 0))
    new_version = rec.get("version")
    old_version = s.get("last_version")
    if new_version and new_version != old_version:
        if old_version is not None:
            emit(f"DEPLOY  {server}  v={old_version} -> v={new_version}")
        s["last_version"] = new_version
    if s.get("is_silent"):
        emit(f"WAKE    {server}  heartbeat resumed (v={new_version}, spool={s['last_spool']})")
        s["is_silent"] = False
    save_state(server)


def handle_event(rec, now):
    server = rec.get("server") or "unknown"
    s = server_state(server, now)
    sig = rec.get("sig") or "nosig"
    sigs = s.setdefault("signatures", {})
    cur = sigs.get(sig)

    suppressed = rec.get("n_suppressed")
    if suppressed is not None:
        # periodic suppressed-summary frame; fold the extra count in silently
        if cur:
            cur["count"] = cur.get("count", 0) + int(suppressed)
            cur["last_ts"] = int(rec.get("ts") or now)
            save_state(server)
        return

    if cur is None:
        cur = {
            "count": 0, "first_ts": int(rec.get("ts") or now),
            "last_ts": int(rec.get("ts") or now), "last_emit_threshold": 0,
            "ex_type": (rec.get("ex_type") or "")[:80],
            "head": (rec.get("message") or "")[:120],
            "level": (rec.get("level") or "").upper(),
        }
        sigs[sig] = cur
    prev = cur["count"]
    cur["count"] = prev + 1
    cur["last_ts"] = int(rec.get("ts") or now)

    hit = crossed_threshold(prev, cur["count"], cur.get("last_emit_threshold", 0))
    if hit is not None:
        cur["last_emit_threshold"] = hit
        tag = "NEW" if hit == 1 else f"x{hit}"
        emit("E " + json.dumps({
            "server": server, "sig": sig, "tag": tag, "count": cur["count"],
            "level": cur["level"], "ex_type": cur["ex_type"],
            "message": (rec.get("message") or cur["head"])[:400],
            "traceid": rec.get("traceid"),
        }))
    save_state(server)


def check_silence(now):
    for server, s in _state.items():
        if s.get("is_silent"):
            continue
        last = s.get("last_hb_ts") or 0
        if last and now - last > SILENCE_SECS:
            mins = int((now - last) / 60)
            emit(f"SILENT  {server}  no heartbeat for {mins}m (last v={s.get('last_version')})")
            s["is_silent"] = True
            save_state(server)


def main(name):
    url_base = os.environ.get("AGENT_BUS_URL")
    if not url_base:
        print("AGENT_BUS_URL env var required", file=sys.stderr)
        sys.exit(2)
    ua = os.environ.get("AGENT_BUS_UA", "Mozilla/5.0")
    channel = os.environ.get("AGENT_BUS_CHANNEL", "servers")
    ignore = {s.strip() for s in os.environ.get("AGENT_BUS_IGNORE", "").split(",") if s.strip()}
    recv_url = f"{url_base.rstrip('/')}/recv"

    load_state()
    emit(f"bus_monitor up: name={name} channel={channel} tracking={len(_state)} server(s)")

    since = time.time()
    while True:
        now = time.time()
        try:
            req = urllib.request.Request(
                f"{recv_url}?to={name}&since={since}&block=true&channel={channel}",
                headers={"User-Agent": ua},
            )
            with urllib.request.urlopen(req, timeout=35) as r:
                data = json.loads(r.read())
            for m in data.get("messages") or []:
                ts = m.get("ts", now)
                since = max(since, ts)
                frm = m.get("from") or "?"
                if frm in ignore:
                    continue
                body = m.get("msg", "") or ""
                head = body[:2]
                if head == "H ":
                    try:
                        handle_heartbeat(json.loads(body[2:]), now)
                    except json.JSONDecodeError:
                        pass
                elif head == "E ":
                    try:
                        handle_event(json.loads(body[2:]), now)
                    except json.JSONDecodeError:
                        pass
                else:
                    hh = time.strftime("%H:%M:%S", time.gmtime(ts))
                    emit(f"CHAT    [{hh}Z] from {frm}: {body}")
            if data.get("now"):
                since = max(since, data["now"])
        except (json.JSONDecodeError, urllib.error.URLError, TimeoutError) as e:
            print(f"# transient: {type(e).__name__}", file=sys.stderr, flush=True)
            time.sleep(2)
        except Exception as e:
            print(f"# unexpected: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(2)
        check_silence(time.time())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: AGENT_BUS_URL=... bus_monitor.py <agent-name>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
