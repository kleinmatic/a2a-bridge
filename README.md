# a2a-bridge

[![CI](https://github.com/kleinmatic/a2a-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/kleinmatic/a2a-bridge/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/a2a-bridge)](https://pypi.org/project/a2a-bridge/)

**Connect LibreChat, or any OpenAI-compatible chat client, to any [A2A](https://a2a-protocol.org) agent.**

A2A agents speak JSON-RPC. Chat clients speak the OpenAI chat-completions API. The bridge
translates between them. Each configured A2A agent appears as a selectable model, and the
agent's answer reaches the screen unchanged.

```
┌─────────────┐   POST /v1/chat/completions   ┌────────────┐   JSON-RPC message/send   ┌───────────┐
│ chat client │ ────────────────────────────► │ a2a-bridge │ ────────────────────────► │ A2A agent │
│ (LibreChat) │ ◄──────────────────────────── │            │ ◄──────────────────────── │           │
└─────────────┘        assistant message      └────────────┘        Task + artifacts    └───────────┘
```

To add an agent, add a block to the config file. No code change is needed.

## Why not MCP?

MCP exposes an agent as a *tool*. That puts a model between the agent and the user, and the
model paraphrases whatever the agent returns. This is fine for data lookups. It destroys
anything that depends on the agent's own voice, formatting, or cross-agent attribution: a
multi-agent response that labels which agent said what comes back flattened into a summary.

A2A treats the far side as a peer rather than a function. The bridge keeps that property:
there is **no model in the path**. The user's text goes to the agent, and the agent's text is
what renders.

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

Then send a **second** request with the same `X-Conversation-Id` and a follow-up that depends
on the first answer. If the agent remembers, sessions work. Sessions are the part most likely
to break in a way you do not see, and the part hardest to notice later.

### Docker

```bash
docker build -t a2a-bridge .
docker run -p 8600:8600 \
  -v "$PWD/agents.yml:/app/agents.yml:ro" \
  -v a2a_data:/data \
  a2a-bridge
```

Keep `/data` on a volume. It holds the map from conversation id to `contextId`. Lose it and
every user starts over, and the agent forgets whatever it had decided about them.

---

## Configure

A working config is three lines:

```yaml
agents:
  - id: myagent                                    # becomes the model name
    card_url: https://agent.example.org/api/agent/
```

The bridge reads the rest from the agent card: the JSON-RPC endpoint, the protocol version,
and whether the agent can stream.

Two settings you will want early:

```yaml
store: "sqlite:///data/context.db"     # memory:// | sqlite:///path | mongodb://...
api_keys_env: A2A_BRIDGE_API_KEYS      # the NAME of an env var holding comma-separated keys

agents:
  - id: myagent
    card_url: https://agent.example.org/api/agent/
    conversation_id_header: X-Conversation-Id      # see "Sessions" below
```

`store` decides whether conversations survive a restart. `api_keys_env` holds the name of an
environment variable, not the keys themselves, so no secret is written in the file. If you
leave it out, or if the named variable is not exported, the bridge answers anyone who can
reach the port. It logs a warning at startup when it starts open.

Every option, each with the reason to set it, is in
[`examples/agents.example.yml`](examples/agents.example.yml).

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

**→ Full guide, including the `@mention` setup and the traps that fail silently:
[docs/librechat.md](docs/librechat.md).**

Read the traps section before you debug anything. Several traps produce no error: a wrong
trailing slash, a title model that does not exist, a config file whose inode changed. Each one
shows symptoms that point at a different cause.

---

## What the agent needs to support

Minimum for a working integration:

- An **agent card**, at `/.well-known/agent-card.json` or served from the endpoint itself.
- **`message/send`** (JSON-RPC 2.0), returning a Task whose text lives in
  `result.artifacts[].parts[].text`.
- A **server-minted `contextId`** returned on the first response and honoured on later ones.

Optional, and worth having:

- **`message/stream`**: mainly for working-state notes, which turn a long blank wait into
  visible progress. Streaming does not imply incremental text; many agents send a whole
  artifact at once.
- **Working-state `status.message`** copy ("Searching…", "Handing off to X…"), forwarded to
  the user as it arrives.

Not used: Task lifecycle management, polling, push notifications. An agent needing those is
not yet a fit for a synchronous chat UI.

---

## Sessions

The single most important thing to get right.

The bridge omits `contextId` on the first turn, lets the server mint one, stores it against
the client's conversation id, and echoes it afterwards. Agents commonly bind session state to
that value: history, entitlement, subscription. A rotated `contextId` can silently send a user
back to the beginning.

That is why `conversation_id_header` matters. Without it, the bridge looks for a
`conversation_id` or `user` field in the request body. If neither is present, it hashes the
newest user message. That hash changes on every turn, so the fallback cannot hold a
multi-turn session together.

---

## Design notes

Behaviours that took real debugging to establish, in case they look arbitrary:

**Only the newest user turn is sent.** A2A agents are stateful per `contextId` and keep their
own transcript. Replaying the client's history would duplicate their context every turn and
inflate their token spend.

**Parts within an artifact concatenate with nothing between them. Separate artifacts get the
separator.** A streaming agent emits one part per chunk of a single string. A separator
between parts splits words and breaks markdown mid-token.

**Two failure layers.** JSON-RPC errors arrive as HTTP 200 with an `error` object. Rate
limiting arrives as a bare HTTP 429 with an **empty body**, sent by middleware above the
JSON-RPC app: no envelope, nothing to parse. Code that only inspects JSON-RPC errors mistakes
one for the other.

**429 is surfaced, never retried.** A throttled request is information the operator wants. A
retry loop hides it and adds load.

**Redirects are not followed.** A `307` on a POST loses its body in most clients. The usual
cause is a missing trailing slash, so the bridge reports the redirect as a configuration
error.

**Streaming is always available to the client.** Chat clients request `stream: true` by
default and break on a plain JSON body, so a blocking agent's answer is emitted as a single
delta.

**Failures render in-chat by default.** A non-2xx becomes a contextless red banner in most
chat UIs. Set `on_error: http_error` per agent for programmatic callers.

**Per-turn ids are recorded even though nothing reads them yet.** The agent's task id is
emitted once and cannot be reconstructed later. Without it, "which answer was this about?" has
no answer for feedback, cost, or audit.

---

## Forwarding caller identity

Agents that rate-limit per IP see every user of a server-side bridge as one caller, so one
busy user throttles everyone. If the agent supports it, forward a stable per-user id:

```yaml
    caller:
      id_header: X-Caller-Id
      auth_header: X-Caller-Auth
      secret_env: MY_SHARED_SECRET
```

The bridge sends the id plus an HMAC-SHA256 of it under a shared secret, so the agent can
verify the id instead of trusting it. **An unsigned identity header lets any caller claim any
id and escape the rate limit.** The secret must stay server-side.

Use a secret scoped to this purpose alone. If the agent's operator offers a key that also
signs sessions or authorises billing, ask for a separate one. Proving "this caller id came
from me" needs far less authority than that.

---

## Development

```bash
pip install -e '.[dev]'
pytest
ruff check src tests
```

Contract tests live in `tests/`. Recorded wire responses go in `tests/fixtures/`; see the
README there. Record them instead of writing them by hand: the tests should fail when a peer
changes its wire shape, and that only works if the fixtures came off the wire.

The shipped fixtures came off the wire from a live multi-agent publisher. They cover a paywall
gate, a cross-agent handoff, both JSON-RPC error shapes, and streaming with progress notes.
The envelopes are untouched. The prose inside them was rewritten to a fictional publisher, so
nothing here reproduces a real organization's copy.

---

### Releasing

**The git tag is the version.** There is no version number to edit in any file.

1. Pick the number. Semver, still `0.x`: bump the **middle** number for a breaking
   change or a new feature, the **last** for a fix. `0.1.0` → `0.2.0` → `0.2.1`.
2. Publish a GitHub Release tagged `vX.Y.Z`.

The release workflow does the rest: it builds, runs the tests, refuses to continue
if the built version and the tag disagree, and uploads to PyPI over OIDC. There
is no PyPI token in the repo or in anyone's shell.

A `1.0.0` release will be a promise that `agents.yml` and the caller headers are
stable. They are not stable yet.

## Status

Early and deliberately small. Blocking `message/send`, optional `message/stream`, card
discovery, one request/response turn. No Task lifecycle management, no push notifications.

## License

MIT
