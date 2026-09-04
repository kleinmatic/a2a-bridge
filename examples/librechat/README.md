# LibreChat + a2a-bridge, from nothing

A working chat UI talking to your A2A agent, on your own machine, in about five
minutes of typing plus a container pull.

This stack is **LibreChat, Mongo and the bridge** — nothing else. Upstream
LibreChat also ships Meilisearch, pgvector and a RAG API; none of them take part
in talking to an A2A agent, so they are left out.

For the reasoning behind each configuration line, and the traps that fail
silently, read [`../../docs/librechat.md`](../../docs/librechat.md). This file is
the short path.

---

## Before you start

You need Docker, and an A2A agent that is already answering. Check the agent
first — every minute spent debugging LibreChat against a broken agent is wasted:

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

- `examples/librechat/agents.yml` — the bridge's config
- `examples/librechat/.env` — LibreChat's, with fresh secrets generated for you

Now edit `examples/librechat/agents.yml` and set `card_url` to your agent.

**If your agent runs on this machine but outside Docker, use
`host.docker.internal`, not `localhost`.** Inside the container, localhost is the
container.

If your agent's id is not `myagent`, change it in `agents.yml` and in the three
places it appears in `librechat.yaml` (`models.default`, `modelSpecs.list[].name`,
and `preset.model`).

## 2. Start

```bash
make demo-up
```

First run pulls LibreChat and Mongo and builds the bridge, so give it a few
minutes. Then:

- **LibreChat** → <http://localhost:3080>
- **the bridge** → <http://localhost:8600/v1/models>

Register an account on first visit. It is local and unverified; any email works.

## 3. Talk to the agent

In LibreChat, type `@myagent` and ask something.

Then ask a **follow-up that depends on the first answer** — "tell me more about
the second one". This is the check that matters. If the agent has lost the
thread, the conversation-id header is not arriving, and everything downstream of
that will look like a different bug. See the traps doc.

---

## Everyday commands

All from the repo root:

| Command | What it does |
|---|---|
| `make demo-up` | Build and start everything |
| `make demo-check` | Ask the bridge directly, no chat client in the loop |
| `make demo-logs` | Follow the bridge's logs — where wiring problems surface |
| `make demo-restart` | Restart the bridge after editing `agents.yml` |
| `make demo-down` | Stop, keeping conversations and sessions |
| `make demo-reset` | Stop and **delete all data**, including accounts |

**`agents.yml` and `librechat.yaml` are read once, at startup.** Editing either
one does nothing until that container restarts, and `docker compose up -d` will
not notice, because the service definition has not changed. Use `make
demo-restart` for the bridge; `make demo-down && make demo-up` for LibreChat.

## When something is wrong

Work outward from the agent, not inward from the UI.

```bash
make demo-check          # is the bridge up and configured?
make demo-logs           # what did the bridge see?
```

The bridge reports configuration errors in plain language: a `307` means a
missing trailing slash on `card_url`; a bare `429` is the agent rate-limiting
you, and is never retried, because a throttle is information you want.

If answers arrive but the agent has amnesia between turns, that is the
conversation-id header — check it is spelled the same way in `librechat.yaml` and
`agents.yml`.

If your other models vanish from the picker after adding the spec, that is
`interface.modelSelect`, which is already set correctly in the shipped
`librechat.yaml`. Both this and the header trap are explained in
[`../../docs/librechat.md`](../../docs/librechat.md).

## Making it yours

Adding a second agent is a config block in `agents.yml` plus a `modelSpecs` entry
in `librechat.yaml`. No code changes, no rebuild of the bridge image — just
`make demo-restart`.
