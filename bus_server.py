#!/usr/bin/env python3
"""Reference HTTP message bus server.

Single-file implementation of the protocol documented in PROTOCOL.md.
Stdlib only — no external dependencies. Suitable for local dev, testing,
and small-scale multi-agent workflows on a private network.

Run:
  python bus_server.py [--host 0.0.0.0] [--port 8080] [--max-queue 1000]

Endpoints:
  POST /send                                 — enqueue message
  GET  /recv?to=<name>&since=<s>&block=<b>   — long-poll receive
  GET  /inbox                                — count pending per recipient
  GET  /healthz                              — readiness probe

NOT FOR PRODUCTION:
- No authentication.
- In-memory only (messages lost on restart).
- Single-threaded long-poll uses time.sleep polling, not condition variables;
  fine for <100 concurrent recipients but won't scale to thousands.
- No HTTPS — terminate TLS at a fronting proxy if exposed publicly.
"""
import argparse
import json
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


DEFAULT_CHANNEL = "default"


class BusState:
    def __init__(self, max_queue: int = 1000) -> None:
        self.lock = threading.RLock()
        self.max_queue = max_queue
        # Keyed by (channel, recipient) so traffic on different channels can't
        # collide even when two channels happen to use the same agent name.
        # Backward-compatible: omitting `channel` on the client side routes to
        # `DEFAULT_CHANNEL`, so existing deployments keep working unchanged.
        self.queues: dict[tuple[str, str], deque] = defaultdict(
            lambda: deque(maxlen=max_queue)
        )

    def push(self, channel: str, sender: str, recipient: str, body: str) -> float:
        ts = time.time()
        msg = {"ts": ts, "from": sender, "msg": body}
        with self.lock:
            self.queues[(channel, recipient)].append(msg)
        return ts

    def fetch(self, channel: str, recipient: str, since: float, max_count: int = 100) -> list:
        with self.lock:
            q = self.queues.get((channel, recipient))
            if not q:
                return []
            return [m for m in q if m["ts"] > since][:max_count]

    def inbox_counts(self) -> dict:
        # Nested: { channel: { recipient: count } }
        with self.lock:
            out: dict[str, dict[str, int]] = {}
            for (channel, recipient), q in self.queues.items():
                if not q:
                    continue
                out.setdefault(channel, {})[recipient] = len(q)
            return out


STATE: BusState  # set in main


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # quieter
        pass

    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/recv":
            recipient = q.get("to")
            if not recipient:
                self._json(400, {"error": "to= required"})
                return
            channel = q.get("channel", DEFAULT_CHANNEL)
            since = float(q.get("since", "0"))
            block = q.get("block", "false").lower() == "true"
            max_count = int(q.get("max", "100"))
            deadline = time.time() + 30.0
            while True:
                msgs = STATE.fetch(channel, recipient, since, max_count)
                if msgs or not block or time.time() >= deadline:
                    self._json(200, {"messages": msgs, "now": time.time()})
                    return
                time.sleep(0.5)
        elif u.path == "/inbox":
            self._json(200, {"recipients": STATE.inbox_counts(), "now": time.time()})
        elif u.path == "/healthz":
            self._json(200, {"ok": True, "now": time.time()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        u = urlparse(self.path)
        if u.path != "/send":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            self._json(413, {"error": "message too large (>64KB)"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return
        sender = body.get("from")
        recipient = body.get("to")
        msg = body.get("msg")
        channel = body.get("channel", DEFAULT_CHANNEL)
        if not (sender and recipient and isinstance(msg, str)):
            self._json(400, {"error": "from, to, msg required"})
            return
        ts = STATE.push(channel, sender, recipient, msg)
        self._json(200, {"ok": True, "ts": ts, "channel": channel})


def main() -> None:
    global STATE
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--max-queue", type=int, default=1000)
    args = p.parse_args()
    STATE = BusState(max_queue=args.max_queue)
    print(f"agent-bus reference server listening on http://{args.host}:{args.port}")
    print(f"  POST /send   -- enqueue a message")
    print(f"  GET  /recv   -- long-poll receive")
    print(f"  GET  /inbox  -- pending counts per recipient")
    print(f"  GET  /healthz")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
