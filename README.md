# Agent-bus client tools

Three small utilities for coordinating multiple Claude Code agents (or any
collaborating processes) over an HTTP message bus. Used in this project to
coordinate work across three machines on three different networks (a
GB10 host with the address index, a dell with an A100, and a cube with an
A6000), each running a Claude Code session — `wallet-spark`, `dell`, and
`cube` agents on the bus.

## Files

| File | Purpose |
|---|---|
| `bus_recv.py`     | Long-polling listener — prints messages to stdout, one block per message. Use with Claude Code's Monitor tool. |
| `bus_send.py`     | Send a single message (stdin → bus). One-shot. |
| `bus_listener.sh` | Persistent 30-second-poll daemon — appends to disk logs. Use for unattended background tailing. |
| `bus_server.py`   | Reference HTTP server implementing the protocol — single file, stdlib only. Run locally for dev or testing. |
| `test_bus.py`     | End-to-end smoke test — spins up the reference server, sends + receives, asserts. |
| `PROTOCOL.md`     | Wire-format spec (request/response shapes, semantics, ordering). |
| `pyproject.toml`  | Python packaging metadata; allows `pip install .` from this directory. |

## Wire protocol

The clients assume a bus that exposes:

- `GET /recv?to=<name>&since=<float_seconds>&block=true` — long-poll for messages addressed to `<name>`.
  Response: `{"messages": [{"ts": float, "from": str, "msg": str}, ...], "now": float}`.
- `POST /send` with JSON body `{"from": str, "to": str, "msg": str}` — enqueue a message.

Any bus implementation matching this contract works. Reference implementations
exist in several open-source pub-sub libraries; for our deployment we used a
lightweight HTTPS endpoint behind a WAF (the `Mozilla/5.0` User-Agent default
exists to avoid common WAF rules that drop script-style UAs).

## Configuration

All clients read these environment variables:

| Variable | Required? | Description |
|---|---|---|
| `AGENT_BUS_URL` | yes | Base URL of the bus (no trailing slash). |
| `AGENT_NAME`    | for `bus_send.py` + `bus_listener.sh` | This agent's name. `bus_recv.py` takes the name as argv[1] instead. |
| `AGENT_BUS_UA`  | no | User-Agent header. Defaults to `Mozilla/5.0`. |

## Examples

Listen for messages addressed to `spark`, surfacing each in your terminal:

```bash
AGENT_BUS_URL=https://bus.example.com python bus_recv.py spark
```

Send a message from `spark` to `dell`:

```bash
AGENT_BUS_URL=https://bus.example.com AGENT_NAME=spark \
  python bus_send.py dell <<'EOF'
T1 verify done — 4/4 hits are honeypots. Standing down A100.
EOF
```

Run a persistent background listener that writes to disk:

```bash
AGENT_BUS_URL=https://bus.example.com AGENT_NAME=spark \
  nohup ./bus_listener.sh > /tmp/bus_listener_main.log 2>&1 &
tail -f /tmp/bus_listener_spark.log
```

Run the reference server locally for development:

```bash
python bus_server.py --host 127.0.0.1 --port 8080
# in another terminal:
AGENT_BUS_URL=http://127.0.0.1:8080 AGENT_NAME=alice python bus_send.py bob <<<"hello"
AGENT_BUS_URL=http://127.0.0.1:8080 python bus_recv.py bob
```

Run the smoke test:

```bash
python test_bus.py
# expected: ALL TESTS PASS
```

## With Claude Code's Monitor tool

`bus_recv.py` is designed so each emitted message becomes one Monitor event
(via the trailing `---` separator and explicit `flush`). `bus_monitor.py` does
the same with its own per-line `flush`. The Monitor tool turns **each stdout
line into one push notification in chat**, so the agent reacts to peer messages
in-line.

```
Monitor command: PYTHONIOENCODING=utf-8 AGENT_BUS_URL=$AGENT_BUS_URL \
  AGENT_BUS_CHANNEL=servers python -u bus_recv.py <your-name>
Monitor description: agent-bus inbox — <your-name>
Monitor persistent: true     # runs the whole session; stop it with TaskStop
```

**Read this before you debug a "dead" Monitor — the two things that bite:**

1. **Monitor is a harness capability, not a slash command and not
   `run_in_background`.** The agent calls the `Monitor` tool directly. In some
   builds it's a *deferred* tool — listed by name in a system-reminder with no
   schema until you fetch it (e.g. a tool-search `select:Monitor`) before the
   first call. **If your session doesn't list `Monitor` even as deferred, your
   build simply doesn't expose it** — there's no way to summon it, and nothing
   you typed is wrong. Ask whoever provisions your Claude Code env to enable it.

2. **Per-line flushing is mandatory.** Use `python -u` (unbuffered). If you pipe
   through a filter to drop heartbeat noise, the filter must also be
   line-buffered, e.g.:
   ```
   python -u bus_recv.py <your-name> 2>&1 \
     | grep -vE --line-buffered 'H \{|transient|TimeoutError|HTTPError|^[[:space:]]*$'
   ```
   (The `^[[:space:]]*$` clause drops the blank lines `bus_recv.py` prints
   between message blocks, so each notification is just the message.)
   Without per-line flushing on **every** stage, notifications batch/stall and
   the Monitor looks dead. (`bus_monitor.py` already filters `H`/`E`/heartbeat
   noise itself, so it needs no grep — just `-u`.)

**No Monitor tool in your session?** There's no native per-message push. The
closest fallback is `run_in_background` on the listener, then periodically
read its output file — that's polling, not push, and latency depends on how
often you check. Functionality is otherwise identical.

## Operational notes

- Each `recv` call blocks up to ~35 seconds server-side waiting for messages.
  This is intentional — long-polling keeps notification latency low without
  burning CPU on tight polling.
- The `since` cursor advances client-side based on `data["now"]`, so the bus
  doesn't need server-side per-client cursors.
- Errors during a poll (network blip, JSON decode error) are caught and the
  loop sleeps 2 seconds before retrying. The cursor is preserved across
  retries — no message loss.
- `bus_listener.sh` is the lighter-weight alternative for hosts where
  Python is heavier than necessary.

## Privacy & security considerations

- Treat bus traffic as transmitted-in-the-clear unless you've ensured the
  bus enforces transport security and access control. For private research
  workflows we put the bus behind HTTPS + WAF + per-agent authentication
  tokens; the example clients here omit auth for clarity.
- Don't put secrets (priv keys, DB passwords) in messages. The bus is a
  coordination channel, not a vault. Reference shared resources by
  out-of-band paths.
- The `AGENT_NAME` is purely a routing label; the bus should authenticate
  the actual sender by other means (mutual TLS, signed JWT, or similar)
  if you can't trust every client on the bus.

### Important: bus messages can render to user-visible chat

When `bus_recv.py` runs under Claude Code's Monitor tool, every bus message
your agent receives appears in the operator's chat as a `<task-notification>`.
That means **anything you send over the bus that includes sensitive content
(private keys, recovered wallet addresses, leaked balances, recipe+seed
tuples) leaks into chat** — not just to other agents.

For multi-agent research workflows that handle sensitive data, the protocol
is:

1. Persist hits / sensitive batches to a `chmod 0600` file on the producing
   host.
2. Compute `sha256` of the canonical-form file.
3. Send a bus message with the **hash + path + count + per-bucket
   breakdown**, no raw values:
   ```
   T1_v2 done: cross_list_unique=8 sha256=<hex> at /home/.../hits.csv
   ```
4. The consumer pulls the file via SSH / GCS / shared filesystem and
   verifies the hash matches.

Address fingerprints (first 6-8 chars or `hash(addr) & 0xffffffff`) are
generally fine in bus messages — they identify entities without exposing
the spendable priv key.

## Why this exists in this repo

The paper accompanying this codebase describes the multi-agent coordination
methodology used during the research. These three files are the concrete
implementation. They are deliberately small enough to be readable end-to-end
and don't require any framework dependencies beyond the stdlib (Python) or
`curl + jq` (shell variant).

Originally implemented in `wallet-spark` agent's local toolbox; generalized
here so peer agents (`dell`, `cube`) can pull and use the same versions.
