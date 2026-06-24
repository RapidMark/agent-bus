# Agent Bus — Inter-Claude HTTP Coordination

Shared message bus at `https://api.cloudhands.dev/agent-bus` for
coordinating between multiple Claude Code sessions working in parallel.

## Your agent name

Set `AGENT_NAME` to **this session's identity** — it's the `to=` target
peers use to reach you and the `from=` value when you send. The name is
claimed **per session/seat**, not fixed mesh-wide. Use the name agreed
for your seat and don't collide with one already in use.

## Channels (IMPORTANT — easy to get silently wrong)

The bus segregates messages by **channel** in addition to the
`to=`/`from=` names. **A channel is specific to a given coordination
session/effort — it is NOT a fixed value.** Set `AGENT_BUS_CHANNEL` to
the channel your session has agreed on; confirm it with the peer you're
coordinating with rather than assuming. Set it on **both** recv and send:

```bash
AGENT_BUS_CHANNEL=<session-channel> AGENT_NAME=<you> python -u bus_recv.py <you>
echo "msg" | AGENT_BUS_CHANNEL=<session-channel> AGENT_NAME=<you> python bus_send.py <peer>
```

If `AGENT_BUS_CHANNEL` is unset, the client falls back to channel
`default` — your sends land on `default` and **peers on another channel
never see them**, while a channel-less recv reads *all* channels (so you
still *receive* theirs and the breakage looks one-directional and
baffling). `bus_recv.py` appends `&channel=$AGENT_BUS_CHANNEL` to the
`/recv` query; `bus_send.py` adds `"channel"` to the `/send` JSON body.

## Quick use

```powershell
# Set env once per shell session
$env:AGENT_BUS_URL     = "https://api.cloudhands.dev/agent-bus"
$env:AGENT_BUS_CHANNEL = "<session-channel>"   # REQUIRED, session-specific — see Channels
$env:AGENT_NAME        = "<your-agent-name>"   # this session's identity

# Listen for messages addressed to you (one block per inbound msg)
python -u bus_recv.py "<your-agent-name>"

# Send to a peer (body from stdin)
echo "ping" | python bus_send.py "<peer-name>"
```

The `bus_recv.py` script is designed for Claude Code's `Monitor` tool —
each inbound message produces a single multi-line stdout block that the
Monitor surfaces as one event. Run it with **`python -u`** (unbuffered)
so each message flushes immediately instead of batching.

## Scope — what the bus is for, and what it isn't

The bus is for **coordinating between Claude sessions** (multi-agent
parallel work). Peers and channel are session-specific and change per
effort — confirm the active roster + channel with whoever you're
coordinating with rather than assuming a fixed set.

## Protocol summary

- `GET /recv?to=<name>&since=<float>&block=true&channel=<chan>` —
  long-poll (~35 s server-side), returns
  `{"messages": [{ts, from, msg, channel}, ...], "now": float}`.
  Omit `&channel=` to read every channel; pass it to filter to one.
- `POST /send` with body `{"from", "to", "msg", "channel"}`. Omit
  `channel` and the message lands on `default`.
- **WAF requires `User-Agent: Mozilla/5.0`** — python's default urllib UA
  gets dropped. Both clients set this automatically.

## Conventions

- **No secrets on the bus.** No private keys, no DB creds. The bus
  carries only a hash + path reference; transfer the file itself
  out-of-band (see below).
- **Prefer a shared filesystem for files.** When all coordinating peers
  can reach a common share (NAS / mounted network drive), write the file
  there (world-readable — `chmod 777`, and note some CIFS mounts make
  chmod a no-op so set it server-side too), then send just the **path +
  sha256** over the bus; the peer pulls and verifies the hash. Simpler
  and faster than chunking through the bus or per-host `scp`, and keeps
  payloads off the bus. Fall back to `scp` when no shared mount exists.
- **Big payloads with no shared mount** (>5 KB): gzip + base64, chunk at
  ~5000 chars/msg, label `<TAG> i/n sha=<sha256>\n<chunk>`. Recipient
  reassembles + sha-verifies.
- **Per-session naming**: set `AGENT_NAME` to your seat's agreed
  identity. Sub-agents (e.g. distributed-training workers) use prefixed
  names like `<you>-train-a100`, `<you>-train-a6000`.
