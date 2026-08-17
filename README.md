# a2a-bridge

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
paraphrases whatever comes back. That is fine for data lookups and destructive for anything where
the agent's own voice, formatting, or cross-agent attribution matters — multi-agent responses that
label which agent said what get flattened into a summary.

A2A treats the far side as a peer, not a function call. This bridge keeps that property: there is
**no model in the path**. The user's text goes to the agent, and the agent's text is what renders.

## Install

```bash
pip install a2a-bridge          # or: pip install 'a2a-bridge[mongo]'
cp examples/agents.example.yml agents.yml
A2A_BRIDGE_CONFIG=agents.yml python -m a2a_bridge.server
```

Or with Docker:

```bash
docker build -t a2a-bridge .
docker run -p 8600:8600 -v "$PWD/agents.yml:/app/agents.yml:ro" a2a-bridge
```

## Configure

```yaml
store: "sqlite:///data/context.db"
agents:
  - id: publisher                                   # becomes the model name
    card_url: https://example.org/api/publisher/
    conversation_id_header: X-Conversation-Id
```

Everything else — the JSON-RPC endpoint, the protocol version, streaming support — is read from
the agent card. See [`examples/agents.example.yml`](examples/agents.example.yml) for every option.

### Endpoints

| Route | Purpose |
|---|---|
| `POST /v1/chat/completions` | blocking and streaming |
| `GET /v1/models` | one entry per configured agent, so clients self-populate |
| `GET /healthz` | liveness |

## Using it from LibreChat

Add a custom endpoint in `librechat.yaml`:

```yaml
endpoints:
  custom:
    - name: "Publisher"
      apiKey: "${A2A_BRIDGE_API_KEY}"
      baseURL: "http://a2a-bridge:8600/v1"
      models:
        default: ["publisher"]
        fetch: true
      headers:
        X-Conversation-Id: "{{LIBRECHAT_BODY_CONVERSATIONID}}"
        X-Caller-Id: "{{LIBRECHAT_USER_ID}}"
      titleEndpoint: "bedrock"        # keep title generation off the agent
      maxContextTokens: 200000
```

Three things worth knowing:

- **`{{LIBRECHAT_BODY_CONVERSATIONID}}` is what makes sessions work.** It gives the bridge a stable
  key per conversation. Without it the bridge falls back to hashing the first user message, which
  breaks if that message is ever edited.
- **Set `titleEndpoint` (or `titleConvo: false`).** Otherwise every new conversation fires an extra
  "summarize this in a few words" request into your agent — real traffic, real cost, and a chance of
  a guard refusing it and producing a strange title.
- **`maxContextTokens` is required.** A2A carries no token counts, so the bridge reports zero and
  LibreChat's defaults are small.

## Design notes

Behaviors that took real debugging to establish, in case they look arbitrary:

**Only the newest user turn is sent.** A2A agents are stateful per `contextId` and keep their own
transcript. Replaying the client's full history would duplicate their context every turn and
inflate their token spend.

**`contextId` is minted by the server, never by us.** The bridge omits it on the first turn, stores
whatever comes back, and echoes it afterwards. Agents commonly bind session state to that value —
including entitlement decisions — so a rotated `contextId` can silently send someone back to the
start. It is also why the bridge persists the mapping rather than holding it in memory by default.

**The payload is in `result.artifacts[].parts[].text`, not `result.message`.**

**Rate limiting usually arrives as a bare HTTP status with an empty body.** Middleware tends to sit
*above* the JSON-RPC app, so there is no envelope to parse. Code that only inspects JSON-RPC errors
will misread a 429 as a malformed response. The bridge checks the status, and **never retries** —
a throttled request is information the operator wants, not something to bury in a retry loop.

**Redirects are not followed.** A `307` on a POST loses its body in most clients. The usual cause is
a missing trailing slash, so the bridge reports it as the configuration error it is.

**Streaming is supported even for blocking agents.** Chat clients commonly send `stream: true` by
default and break on a plain JSON body. When the agent returns a whole answer at once, the bridge
emits it as a single content delta — the client gets its expected framing without any pretence of
token-by-token generation.

**Failures render in-chat by default.** A non-2xx becomes a contextless red banner in most chat UIs.
Set `on_error: http_error` per agent if you would rather have the status code.

## Forwarding caller identity

Agents that rate-limit per IP see every user of a server-side bridge as a single caller, which means
one busy user can throttle everyone. If the agent supports it, forward a stable per-user id:

```yaml
    caller:
      id_header: X-Caller-Id
      auth_header: X-Caller-Auth
      secret_env: MY_SHARED_SECRET
```

The bridge sends the id plus an HMAC-SHA256 of it under a shared secret, so the agent can verify the
identity rather than trust it. **An unsigned identity header is a rate-limit bypass** waiting to be
found, and the secret must stay server-side — never let it reach a browser.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Contract tests live in `tests/`. Recorded wire responses go in `tests/fixtures/` — see the README
there. The point of recording rather than hand-writing them is that the tests should fail when a
peer changes its wire shape, which only works if the fixtures came off the wire.

## Status

Early. The A2A surface covered is deliberately small: blocking `message/send`, card discovery, and
one request/response turn. No Task lifecycle management, no push notifications, no
`message/stream` passthrough — that last one only becomes worthwhile once agents emit incremental
artifact updates rather than one whole artifact.

## License

MIT
