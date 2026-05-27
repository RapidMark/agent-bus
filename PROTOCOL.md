# Agent-bus protocol

Wire-format spec for the message bus that `bus_recv.py`, `bus_send.py`, and
`bus_listener.sh` implement against. Any conforming server allows multiple
collaborating agents (humans, scripts, or LLM-driven assistants) to coordinate
asynchronously over HTTP.

## Endpoints

### `POST /send`

Enqueue a single message addressed to a named recipient.

Request:
```http
POST /send HTTP/1.1
Content-Type: application/json

{
  "channel": "default",
  "from":    "spark",
  "to":      "dell",
  "msg":     "T1 verify done — 4/4 hits are honeypots."
}
```

Response (200 OK):
```json
{ "ok": true, "ts": 1778431629.594, "channel": "default" }
```

`ts` is the server's clock at the moment the message was accepted; clients use
this as the cursor reference if they want to know "when was this delivered."

The `channel` field (optional, default `"default"`) isolates traffic on the
same bus — two messages with the same `to` but different `channel` values are
delivered to different receivers. Servers that pre-date this field MUST treat
its absence as `"default"`, which keeps every legacy client routing correctly.

### `GET /recv?to=<name>&since=<seconds>&block=<bool>&channel=<channel>`

Long-poll for messages addressed to `<name>` with `ts > <since>` on the
specified channel.

Query parameters:
- `to` (required) — agent name to receive for.
- `since` (required) — float seconds; only messages with `ts > since` are
  returned. The client maintains this cursor by remembering the largest `ts`
  it has seen.
- `block` (optional, default `false`) — if `true`, the server holds the
  request open up to ~30 s waiting for new messages, then returns whatever
  has accumulated. If `false`, returns immediately with whatever's queued.
- `max` (optional) — soft cap on the number of messages returned per call.
- `channel` (optional, default `"default"`) — only messages posted to this
  channel are returned. Use to keep unrelated agent conversations on the same
  bus from cross-contaminating.

Response (200 OK):
```json
{
  "messages": [
    { "ts": 1778431650.1, "from": "dell", "msg": "ack" },
    { "ts": 1778431655.4, "from": "cube", "msg": "ready" }
  ],
  "now": 1778431659.7
}
```

`now` is the server's clock at response time; clients should advance their
`since` cursor to `max(max(messages.ts), now)` so they don't re-receive
their own batch.

### `GET /inbox` (optional)

Returns counts of pending unread messages, nested by channel then recipient
(no message contents). Useful for status dashboards. Optional — not all
server implementations support this.

Response (200 OK):
```json
{
  "recipients": {
    "default":    { "dell": 3, "cube": 1 },
    "cloudhands": { "backend": 7 }
  },
  "now": 1778431659.7
}
```

## Semantics

- **At-least-once delivery** is fine; clients are responsible for cursor
  management. There is no per-recipient ack protocol.
- **Ordering:** messages within a single `recv` response are ordered by
  `ts` ascending. There is no global ordering across recipients.
- **Persistence:** server-implementation-dependent. Reference server below
  uses an in-memory deque per recipient with a configurable max size.
- **No authentication in this spec.** Production deployments should put
  the bus behind HTTPS + WAF + per-agent auth tokens.
- **Message size:** server-implementation-dependent. The reference server
  caps at 64 KB per message.

## Privacy notes (carry over from README)

- Treat bus traffic as visible to whoever runs the receiving listener.
  When `bus_recv.py` is used under Claude Code's Monitor tool, every
  message renders in the operator's chat — *including any sensitive
  payload an upstream sender included*. Do not put priv keys, recovered
  addresses, balances, or recipe+seed pairs in messages. Use hash + path
  + count for sensitive batches; pass the actual data via SSH or shared
  filesystem.
- Address fingerprints (first 6-8 chars of the addr, or
  `hash(addr) & 0xffffffff`) are anonymized enough for inter-agent
  coordination without exposing the underlying priv key.
