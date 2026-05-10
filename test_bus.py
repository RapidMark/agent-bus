#!/usr/bin/env python3
"""End-to-end smoke test: spin up bus_server.py, send a message, receive it.

Run: python test_bus.py
Exits 0 if all checks pass, non-zero otherwise.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request


def http(method: str, url: str, body: dict | None = None, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, method=method, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "test_bus.py"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> int:
    port = int(os.environ.get("BUS_PORT", "18988"))
    base = f"http://127.0.0.1:{port}"
    here = os.path.dirname(os.path.abspath(__file__))
    server = subprocess.Popen(
        [sys.executable, os.path.join(here, "bus_server.py"), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for ready
        for _ in range(30):
            try:
                r = http("GET", f"{base}/healthz")
                if r.get("ok"):
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            print("server failed to start"); return 1

        # 1. Send a message
        r = http("POST", f"{base}/send", body={"from": "alice", "to": "bob", "msg": "hello bob"})
        assert r["ok"] is True, f"send failed: {r}"
        assert r["ts"] > 0
        print(f"[1] /send ok, ts={r['ts']}")

        # 2. Recv as bob
        r = http("GET", f"{base}/recv?to=bob&since=0")
        assert len(r["messages"]) == 1, f"expected 1 msg, got {len(r['messages'])}"
        m = r["messages"][0]
        assert m["from"] == "alice" and m["msg"] == "hello bob"
        print(f"[2] /recv returns the message")

        # 3. Recv as bob with since=now should be empty
        r2 = http("GET", f"{base}/recv?to=bob&since={r['now']}")
        assert len(r2["messages"]) == 0, "expected 0 msgs after cursor"
        print(f"[3] /recv with advanced cursor is empty")

        # 4. Send 5 more, batch recv
        for i in range(5):
            http("POST", f"{base}/send", body={"from": "alice", "to": "bob", "msg": f"msg {i}"})
        r3 = http("GET", f"{base}/recv?to=bob&since={r['now']}")
        assert len(r3["messages"]) == 5
        print(f"[4] batch of 5 received")

        # 5. /inbox shows counts
        r4 = http("GET", f"{base}/inbox")
        assert "bob" in r4["recipients"]
        print(f"[5] /inbox: {r4['recipients']}")

        # 6. Bad request rejected
        try:
            http("POST", f"{base}/send", body={"from": "alice"})  # missing to+msg
        except urllib.error.HTTPError as e:
            assert e.code == 400
            print(f"[6] /send rejects malformed body with 400")
        else:
            print("expected 400, got success"); return 1

        print("\nALL TESTS PASS")
        return 0
    finally:
        server.terminate()
        server.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
