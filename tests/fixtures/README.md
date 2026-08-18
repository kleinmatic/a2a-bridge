# Recorded fixtures

Real `message/send` responses, captured from live agents and used by the contract
tests. Recording them rather than hand-writing them is the point: the mapping
tests should fail when a peer changes its wire shape, which only works if the
fixtures came off the wire.

To add one: capture the full JSON-RPC response body, redact anything sensitive
(ids may be rewritten as long as they stay internally consistent), and save it as
`<agent>-<scenario>.json`.

Wanted:
- `*-answer.json`  — an ordinary successful turn
- `*-gate.json`    — a terminal task whose artifact text is a paywall/regwall pitch
- `*-error.json`   — a JSON-RPC error, or a failed task state

## Provenance

`01`–`07` were captured by the Data Tribune side on 2026-08-17 against their local
stack (web door on `:8081`) and copied from
`agentic-newsroom/fixtures/a2a/`. Subscribe tickets are sanitized; no bearer
tokens appear in any capture.

They are illustrative, not golden — model wording varies run to run. Assert on the
envelope: task state, artifact structure, `[LABEL]` blocks, and error shapes.

Two shapes worth noting, because they are easy to conflate:

- **JSON-RPC errors arrive as HTTP 200** with an `error` object (`06`, `07`).
- **Rate limiting is HTTP 429 with a zero-byte body**, from middleware above the
  JSON-RPC layer — no envelope, no id echo. `413` behaves the same over 64 KiB.
  Neither can be detected by inspecting JSON.
