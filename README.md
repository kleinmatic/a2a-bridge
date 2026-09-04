# a2a-bridge

[![CI](https://github.com/kleinmatic/a2a-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/kleinmatic/a2a-bridge/actions/workflows/ci.yml)

**Connect LibreChat — or any OpenAI-compatible chat client — to any [A2A](https://a2a-protocol.org) agent.**

A2A agents speak JSON-RPC. Chat clients speak the OpenAI chat-completions API. This is the
adapter in between: each configured A2A agent shows up as a selectable model, and the agent's
answer lands on screen unchanged.

```
┌─────────────┐   POST /v1/chat/completions   ┌────────────┐   JSON-RPC message/send   ┌───────────┐
│ chat client │ ────────────────────────────► │ a2a-bridge │ ────────────────────────► │ A2A agent │
│ (LibreChat) │ ◄──────────────────────────── │            │ ◄──────────────────────── │           │
└─────────────┘        assistant message      └────────────┘        Task + artifacts    └───────────┘
```

Adding an agent is a config block, not a code change.

## Why not MCP?

MCP exposes an agent as a *tool*, which means a model sits between the agent and the user and
paraphrases whatever comes back. Fine for data lookups, destructive for anything where the
agent's own voice, formatting, or cross-agent attribution matters — multi-agent responses that
label which agent said what get flattened into a summary.

A2A treats the far side as a peer, not a function call. This bridge keeps that property: there
is **no model in the path**. The user's text goes to the agent, and the agent's text is what
renders.

---

## Quickstart

```bash
pip install a2a-bridge                 # or from a checkout: pip install -e .
cp examples/agents.example.yml agents.yml
$EDITOR agents.yml                     # set card_url to your agent
A2A_BRIDGE_CONFIG=agents.yml python -m a2a_bridge.server
```

Verify without a chat client in the loop:

```bash
curl -s localhost:8600/healthz
curl -s localhost:8600/v1/models

curl -s localhost:8600/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Conversation-Id: test-1' \
  -d '{"model":"myagent","messages":[{"role":"user","content":"hello"}]}'
```

Then send a **second** request with the same `X-Conversation-Id` and a follow-up that depends on
the first answer. If the agent remembers, sessions work — that is the part most likely to be
subtly broken, and the part hardest to notice later.

### Docker

```bash
docker build -t a2a-bridge .
docker run -p 8600:8600 \
  -v "$PWD/agents.yml:/app/agents.yml:ro" \
  -v a2a_data:/data \
  a2a-bridge
```

Keep `/data` on a volume. It holds the conversation → contextId map, and losing it means every
user starts over — including losing whatever the agent had decided about them.

---

## Configure

```yaml
store: "sqlite:///data/context.db"     # memory:// | sqlite:///path | mongodb://...
api_keys_env: A2A_BRIDGE_API_KEYS      # comma-separated; omit to leave the bridge open

agents:
  - id: myagent                                    # becomes the model name
    card_url: https://agent.example.org/api/agent/
    conversation_id_header: X-Conversation-Id      # see "Sessions" below
```

Everything else — the JSON-RPC endpoint, protocol version, streaming support — is read from the
agent card. See [`examples/agents.example.yml`](examples/agents.example.yml) for every option.

| Route | Purpose |
|---|---|
| `POST /v1/chat/completions` | blocking and streaming |
| `GET /v1/models` | one entry per configured agent, so clients self-populate |
| `GET /healthz` | liveness |

---

## Using it with LibreChat

Short version:

```yaml
endpoints:
  custom:
    - name: "My Agent"
      apiKey: "${A2A_BRIDGE_API_KEY}"
      baseURL: "http://a2a-bridge:8600/v1"
      models: { default: ["myagent"], fetch: false }
      headers:
        X-Conversation-Id: "{{LIBRECHAT_BODY_CONVERSATIONID}}"
      titleEndpoint: "bedrock"     # anything BUT this endpoint
      maxContextTokens: 200000
```

**→ Full guide, including the `@mention` setup and a list of traps that do not announce
themselves: [docs/librechat.md](docs/librechat.md).**

Read the traps section before debugging anything. Several of them fail silently — a wrong trailing
slash, a title model that does not exist, a config file whose inode changed — and each one looks
like a different problem than it is.

---

## What the agent needs to support

Minimum for a working integration:

- An **agent card**, at `/.well-known/agent-card.json` or served from the endpoint itself.
- **`message/send`** (JSON-RPC 2.0), returning a Task whose text lives in
  `result.artifacts[].parts[].text`.
- A **server-minted `contextId`** returned on the first response and honoured on later ones.

Optional, and worth having:

- **`message/stream`** — mainly for working-state notes, which turn a long blank wait into visible
  progress. Streaming does not imply incremental text; many agents send a whole artifact at once.
- **Working-state `status.message`** copy — "Searching…", "Handing off to X…" — forwarded to the
  user as it arrives.

Not used: Task lifecycle management, polling, push notifications. An agent needing those is not
yet a fit for a synchronous chat UI.

---

## Sessions

The single most important thing to get right.

The bridge omits `contextId` on the first turn, lets the server mint one, stores it against the
client's conversation id, and echoes it afterwards. Agents commonly bind session state to that
value — history, entitlement, subscription — so a rotated `contextId` can silently send a user
back to the beginning.

That is why `conversation_id_header` matters. Without it the bridge falls back to hashing the
first user message, which breaks the moment anyone edits or regenerates it.

---

## Design notes

Behaviours that took real debugging to establish, in case they look arbitrary:

**Only the newest user turn is sent.** A2A agents are stateful per `contextId` and keep their own
transcript. Replaying the client's history would duplicate their context every turn and inflate
their token spend.

**Parts within an artifact concatenate with nothing between them; separate artifacts get the
separator.** A streaming agent emits one part per chunk of a single string. Using a separator for
both splits words and breaks markdown mid-token.

**Two failure layers.** JSON-RPC errors arrive as HTTP 200 with an `error` object. Rate limiting
arrives as a bare HTTP 429 with an **empty body**, from middleware above the JSON-RPC app — no
envelope, nothing to parse. Code that only inspects JSON-RPC errors mistakes one for the other.

**429 is surfaced, never retried.** A throttled request is information the operator wants, not
something to hide in a retry loop.

**Redirects are not followed.** A `307` on a POST loses its body in most clients. Usually a missing
trailing slash, so it is reported as the configuration error it is.

**Streaming is always available to the client.** Chat clients request `stream: true` by default and
break on a plain JSON body, so a blocking agent's answer is emitted as a single delta.

**Failures render in-chat by default.** A non-2xx becomes a contextless red banner in most chat
UIs. Set `on_error: http_error` per agent for programmatic callers.

**Per-turn ids are recorded even though nothing reads them.** The agent's task id is emitted once
and cannot be reconstructed later; without it, "which answer was this about?" is unanswerable for
feedback, cost or audit.

---

## Forwarding caller identity

Agents that rate-limit per IP see every user of a server-side bridge as one caller, so one busy
user throttles everyone. If the agent supports it, forward a stable per-user id:

```yaml
    caller:
      id_header: X-Caller-Id
      auth_header: X-Caller-Auth
      secret_env: MY_SHARED_SECRET
```

The bridge sends the id plus an HMAC-SHA256 of it under a shared secret, so the agent can verify
rather than trust. **An unsigned identity header is a rate-limit bypass** waiting to be found, and
the secret must stay server-side.

Use a secret scoped to *this purpose*. If the agent's operator offers you a key that also signs
sessions or authorises billing, ask for a separate one — proving "this caller id came from me"
needs far less authority than that.

---

## Development

```bash
pip install -e '.[dev]'
pytest
ruff check src tests
```

Contract tests live in `tests/`. Recorded wire responses go in `tests/fixtures/` — see the README
there. Recording rather than hand-writing them is the point: the tests should fail when a peer
changes its wire shape, which only works if the fixtures came off the wire.

The fixtures shipped here came off the wire from a live multi-agent publisher, and cover a
paywall gate, a cross-agent handoff, both JSON-RPC error shapes, and streaming with progress
notes. The envelopes are untouched; the prose inside them was rewritten to a fictional
publisher, so nothing here reproduces a real organization's copy.

---

## Status

Early, and deliberately small. Blocking `message/send`, optional `message/stream`, card discovery,
one request/response turn. No Task lifecycle management, no push notifications.

## License

MIT
