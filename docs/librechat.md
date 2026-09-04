# Wiring a2a-bridge into LibreChat

End-to-end setup: an A2A agent appearing in LibreChat as a selectable model, with
working sessions. Written against **LibreChat v0.8.5–v0.8.7**; differences noted.

If you read only one section, read [Traps](#traps). Every item there cost real
debugging time, and none of them produce a clear error.

---

## 1. Point the bridge at your agent

`agents.yml`:

```yaml
store: "sqlite:///data/context.db"
api_keys_env: A2A_BRIDGE_API_KEYS

agents:
  - id: myagent                                    # becomes the model name
    card_url: https://agent.example.org/api/agent/
    conversation_id_header: X-Conversation-Id
    message_id_header: X-Message-Id
```

Check it before touching LibreChat:

```bash
curl -s localhost:8600/healthz
curl -s localhost:8600/v1/models
curl -s localhost:8600/v1/chat/completions -H 'Content-Type: application/json' \
  -H 'X-Conversation-Id: test-1' \
  -d '{"model":"myagent","messages":[{"role":"user","content":"hello"}]}'
```

If that returns your agent's answer, the A2A half is done. Everything after this
is LibreChat configuration.

## 2. Add the endpoint

In `librechat.yaml`:

```yaml
endpoints:
  custom:
    - name: "My Agent"                       # display name; referenced by modelSpecs
      apiKey: "${A2A_BRIDGE_API_KEY}"
      baseURL: "http://a2a-bridge:8600/v1"   # container name, or host:port
      models:
        default: ["myagent"]                 # must match the agent `id`
        fetch: false
      headers:
        X-Conversation-Id: "{{LIBRECHAT_BODY_CONVERSATIONID}}"
        X-Message-Id: "{{LIBRECHAT_BODY_MESSAGEID}}"
      titleConvo: true
      titleEndpoint: "bedrock"               # anything BUT this endpoint
      maxContextTokens: 200000
```

**`X-Conversation-Id` is what makes multi-turn work.** Without it the bridge falls
back to hashing the newest user message. That hash changes on every turn, so the
agent gets a new session on every turn. With A2A that means the agent forgets the
user, including any entitlement it had granted them.

**`X-Message-Id` is optional and costs nothing to add.** It lets a rating, cost,
or audit note on a reply be traced back to the exact A2A turn that produced it.
That link cannot be reconstructed afterwards.

## 3. Add the `@` mention

Only `modelSpecs` gives a one-step mention (`@myagent` selects it immediately).
An `endpoint` mention is two-step, and an `agents` record wraps your agent in
another model that will paraphrase its output.

```yaml
modelSpecs:
  enforce: false
  prioritize: false            # defaults to TRUE upstream; see Traps
  list:
    - name: myagent            # what you type after @
      label: "My Agent"
      description: "..."
      preset:
        endpoint: "My Agent"   # must match the custom endpoint `name`
        model: myagent
```

## 4. Verify

```
@myagent <question>
```

Then confirm the session is real. Ask a **follow-up that depends on the first
answer** ("tell me more about the second one"). If the agent has lost context,
the conversation-id header is not arriving. See Traps.

---

## Traps

### The endpoint's trailing slash is significant

Many A2A servers are ASGI apps that redirect `/path` → `/path/` with a **307**.
Most HTTP clients will not replay a POST body across a redirect, so the request
silently arrives empty or not at all.

The bridge refuses to follow redirects and reports them instead. If you see
`endpoint redirected (307)`, copy the exact URL, slash included, into
`endpoint_url`.

### "Join the parts" is ambiguous, and the wrong reading corrupts every answer

An A2A response may carry several text parts. **Parts within one artifact are
fragments of a single string.** A streaming agent emits one per chunk, split
wherever the token stream happened to break. They concatenate with *nothing*
between them. **Separate artifacts are separate documents** and take your
configured separator.

Get this backwards and `["[", "THE AGENT]", " said"]` renders as three paragraphs
with the label split across lines. The bridge handles it. The trap is documented
because anyone writing their own client hits it.

### Rate limiting arrives as a bare HTTP status, not a JSON-RPC error

Rate limiters usually sit *above* the JSON-RPC app, so a 429 has **no envelope,
no id echo, and an empty body**. Code that only inspects JSON-RPC errors sees a
malformed response instead of a throttle. Same for 413 on oversized bodies.

Meanwhile genuine JSON-RPC errors arrive as **HTTP 200** with an `error` object.
Two failure layers; branch on both.

The bridge surfaces a 429 as an in-chat message and **never retries**. A
throttled request is information you want. A retry loop would hide it.

### `interface.modelSelect` silently hides every other model

LibreChat's interface schema marks each field optional and applies defaults at
the **object** level. So if `interface:` exists at all, even with only
`artifacts: true`, then `modelSelect` is `undefined`, not `true`. Once
`modelSpecs` is present, that suppresses every ephemeral endpoint and the picker
shows *only* your specs.

If your other models vanish after adding a spec, set it explicitly:

```yaml
interface:
  artifacts: true
  modelSelect: true     # required once modelSpecs exists
```

The reverse also works: `modelSelect: false` locks a deployment to one agent,
and it reduces the `@` menu to your specs alone. `modelSpecs.enforce` looks like
the setting for this but does not appear in v0.8.7's selector code.

### Route title generation away from the agent

Without `titleEndpoint` (or `titleConvo: false`), every new conversation fires an
extra "summarize this in a few words" request **into your A2A agent**. That is
real traffic and real cost, and a topical guard may refuse it and produce a junk
title.

Point it at a cheap endpoint you already run. Make sure that endpoint's
`titleModel` is one it can actually invoke: an invalid model id fails
fire-and-forget, so conversations silently stay "New Chat" with the error only in
the container log.

### `maxContextTokens` is required

A2A carries no token counts, so the bridge reports zero. LibreChat's own defaults
are small enough to truncate a long conversation without explanation.

### Deploying with a file bind-mount

If you mount `agents.yml` as a single **file** and your deployment tool writes it
via temp-file-and-rename (Ansible's `template`, many others), the inode changes
and the container keeps reading the deleted original. `docker compose up -d` will
not notice, because the service definition is unchanged.

Mount the *directory*, or force-recreate the container when the config changes.
The config is read once at startup regardless, so a restart is needed either way.

### Streaming means progress notes, not incremental text

`capabilities.streaming: true` on an agent card means the **method exists**, not
that text arrives word by word. Some agents stream a whole artifact in one frame.

The value is in working-state notes. An agent that reports "Searching…" while it
works shows the user progress instead of a blank pane. The bridge forwards those
notes (`show_progress`), and they become part of the saved message: the
chat-completions format has a single content channel, so nothing streamed can be
retracted later.

Chat clients request `stream: true` by default and break on a plain JSON body, so
the bridge always answers with SSE even when the agent is blocking.

---

## Version notes

**v0.8.5 through v0.8.7** all support the body-field header placeholders the
bridge relies on, but by different routes:

- **v0.8.7+**: `resolveConfigHeaders` (`packages/api/src/utils/headers.ts`) runs
  at request time specifically so body placeholders resolve.
- **v0.8.5 / v0.8.6**: no `headers.ts`, but `packages/api/src/agents/run.ts`
  passes the request body when resolving custom-endpoint headers inline.

Either way `{{LIBRECHAT_BODY_CONVERSATIONID}}` reaches your headers. Note that
`packages/api/src/endpoints/openai/initialize.ts` resolves headers *without* the
body. That is an earlier, user-only pass, and reading only that file leads to the
wrong conclusion that body placeholders do not work.

Available placeholders: `{{LIBRECHAT_BODY_*}}` for `conversationId`,
`parentMessageId`, `messageId`; `{{LIBRECHAT_USER_*}}` for `id`, `name`,
`username`, `email`, `role`, and other user fields.
