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
