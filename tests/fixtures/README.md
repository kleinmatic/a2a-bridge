# Recorded fixtures

Real `message/send` responses, captured from live agents and used by the contract
tests. Recording them rather than hand-writing them is the point: the mapping
tests should fail when a peer changes its wire shape, which only works if the
fixtures came off the wire.

To add one: capture the full JSON-RPC response body, replace any identifying
names in the message text, redact anything sensitive (ids may be rewritten as
long as they stay internally consistent), and save it as
`<agent>-<scenario>.json`.

Wanted:
- `*-answer.json`  — an ordinary successful turn
- `*-gate.json`    — a terminal task whose artifact text is a paywall/regwall pitch
- `*-error.json`   — a JSON-RPC error, or a failed task state

## Provenance

`01`–`08` came off the wire from a live multi-agent publisher on 2026-08-17.

**The envelopes are unedited.** Task state, artifact and part structure, the
streaming frame sequence and the error objects are exactly as received. That is
what the tests assert on, and the only part of a fixture you should trust.

**The message text was rewritten.** Every publisher, place and institution named
in the prose is invented — the Ridgeline Gazette and its partner agents do not
exist — so nothing here reproduces a real organization's copy. Ids stay
internally consistent, subscribe tickets are sanitized, and no bearer token
appears in any capture.

They are illustrative, not golden: model wording varies run to run. Assert on the
envelope — task state, artifact structure, `[LABEL]` blocks, and error shapes.

Two shapes worth noting, because they are easy to conflate:

- **JSON-RPC errors arrive as HTTP 200** with an `error` object (`06`, `07`).
- **Rate limiting is HTTP 429 with a zero-byte body**, from middleware above the
  JSON-RPC layer — no envelope, no id echo. `413` behaves the same over 64 KiB.
  Neither can be detected by inspecting JSON.
