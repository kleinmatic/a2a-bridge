# LibreChat + a2a-bridge, start to finish

A working chat UI talking to your A2A agent, on your own machine. The typing takes
about five minutes; the first container pull takes longer.

This stack is **LibreChat, Mongo, and the bridge**, nothing else. Upstream
LibreChat also ships Meilisearch, pgvector, and a RAG API. None of them take part
in talking to an A2A agent, so they are left out.

This file is the short path. For the reasoning behind each configuration line, and
for the traps that fail silently, read
[`../../docs/librechat.md`](../../docs/librechat.md).

---

## Before you start

You need Docker, and an A2A agent that already answers. Check the agent first.
Time spent debugging LibreChat against a broken agent is wasted:

```bash
curl -s https://agent.example.org/.well-known/agent-card.json | head
```

Substitute your own agent's URL. If that returns a card, continue.

## 1. Configure

From the **repo root**:

```bash
make demo-config
```

That writes two gitignored files from their examples:

- `examples/librechat/agents.yml`: the bridge's config
- `examples/librechat/.env`: LibreChat's config, with fresh secrets generated for you

Now edit `examples/librechat/agents.yml` and set `card_url` to your agent.

**If your agent runs on this machine but outside Docker, use
`host.docker.internal`, not `localhost`.** Inside the container, localhost is the
container itself.

If your agent's id is not `myagent`, change it in `agents.yml` and in the three
places it appears in `librechat.yaml`: `models.default`, `modelSpecs.list[].name`,
and `preset.model`.

## 2. Start

```bash
make demo-up
```

The first run pulls LibreChat and Mongo and builds the bridge, so give it a few
minutes. Then:

- **LibreChat** → <http://localhost:3080>
- **the bridge** → <http://localhost:8600/v1/models>

Register an account on first visit. It is local and unverified; any email works.

## 3. Talk to the agent

In LibreChat, type `@myagent` and ask something.

Then ask a **follow-up that depends on the first answer**, such as "tell me more
about the second one". This is the most important check. If the agent has lost the
thread, the conversation-id header is not arriving, and every later symptom will
look like a different bug. See the traps doc.

---

## Everyday commands

All from the repo root:

| Command | What it does |
|---|---|
| `make demo-up` | Build and start everything |
| `make demo-check` | Ask the bridge directly, no chat client in the loop |
| `make demo-logs` | Follow the bridge's logs, where wiring problems surface |
| `make demo-restart` | Restart the bridge after editing `agents.yml` |
| `make demo-down` | Stop, keeping conversations and sessions |
| `make demo-reset` | Stop and **delete all data**, including accounts |

**`agents.yml` and `librechat.yaml` are read once, at startup.** Editing either
one does nothing until that container restarts. `docker compose up -d` will not
notice the edit, because the service definition has not changed. Use `make
demo-restart` for the bridge, and `make demo-down && make demo-up` for LibreChat.

## When something is wrong

Start at the agent and work toward the UI.

```bash
make demo-check          # is the bridge up and configured?
make demo-logs           # what did the bridge see?
```

The bridge reports configuration errors in plain language. A `307` means a
missing trailing slash on `card_url`. A bare `429` means the agent is
rate-limiting you; the bridge never retries it, because a throttle is information
you want.

If answers arrive but the agent forgets the conversation between turns, the
conversation-id header is the cause. Check that it is spelled the same way in
`librechat.yaml` and `agents.yml`.

If your other models vanish from the picker after you add the spec, the cause is
`interface.modelSelect`, which is already set correctly in the shipped
`librechat.yaml`. Both this and the header trap are explained in
[`../../docs/librechat.md`](../../docs/librechat.md).

## Making it yours

Adding a second agent takes a config block in `agents.yml` plus a `modelSpecs`
entry in `librechat.yaml`. No code change, no rebuild of the bridge image: run
`make demo-restart`.
