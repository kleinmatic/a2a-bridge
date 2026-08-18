"""Contract tests for the wire mapping.

These encode behaviors that were expensive to discover and are easy to regress:
the payload lives in artifacts rather than a message, only the newest user turn
is forwarded, and contextId is echoed rather than invented.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from a2a_bridge.mapping import (
    A2AProtocolError,
    build_message_send,
    chat_completion,
    last_user_text,
    parse_task,
    sse_chunks,
)

FIXTURES = Path(__file__).parent / "fixtures"


def task(*, state="completed", artifacts=None, context_id="ctx-1"):
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "kind": "task",
            "contextId": context_id,
            "status": {"state": state},
            "artifacts": artifacts if artifacts is not None else [
                {"parts": [{"kind": "text", "text": "hello"}]}
            ],
        },
    }


# ── request side ─────────────────────────────────────────────────────────────

def test_only_the_newest_user_turn_is_sent():
    """History must not be replayed: the agent keeps its own, keyed by contextId."""
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]
    assert last_user_text(messages) == "second"


def test_multipart_content_is_flattened():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "a"},
        {"type": "image_url", "image_url": {"url": "..."}},
        {"type": "text", "text": "b"},
    ]}]
    assert last_user_text(messages) == "ab"


def test_no_user_message_is_an_error():
    with pytest.raises(ValueError):
        last_user_text([{"role": "system", "content": "be helpful"}])


def test_context_id_omitted_on_first_turn():
    """The server mints contextId; sending one we made up would orphan the session."""
    env = build_message_send("hi")
    assert "contextId" not in env["params"]["message"]
    assert env["method"] == "message/send"
    assert env["jsonrpc"] == "2.0"
    assert env["params"]["message"]["parts"] == [{"kind": "text", "text": "hi"}]


def test_context_id_echoed_when_known():
    env = build_message_send("hi", context_id="ctx-9")
    assert env["params"]["message"]["contextId"] == "ctx-9"


def test_message_ids_are_unique_per_call():
    a = build_message_send("x")["params"]["message"]["messageId"]
    b = build_message_send("x")["params"]["message"]["messageId"]
    assert a != b


# ── response side ────────────────────────────────────────────────────────────

def test_text_comes_from_artifacts_not_message():
    """`result.message` is not where the payload lives, despite the name."""
    resp = task()
    resp["result"]["message"] = {"parts": [{"kind": "text", "text": "DECOY"}]}
    r = parse_task(resp); text, ctx, state = r.text, r.context_id, r.state
    assert text == "hello"
    assert ctx == "ctx-1"
    assert state == "completed"


def test_multiple_artifacts_and_parts_join_in_order():
    resp = task(artifacts=[
        {"parts": [{"kind": "text", "text": "one"}, {"kind": "data", "data": {}}]},
        {"parts": [{"kind": "text", "text": "two"}]},
    ])
    text = parse_task(resp, artifact_join="\n\n").text
    assert text == "one\n\ntwo"


def test_gate_response_is_an_ordinary_completed_task():
    """A paywall or regwall arrives as terminal `completed` text, not
    `input-required` — so it needs no special handling anywhere."""
    resp = task(artifacts=[{"parts": [{"kind": "text", "text": "Subscribe to continue."}]}])
    r = parse_task(resp); text, state = r.text, r.state
    assert state == "completed"
    assert "Subscribe" in text


def test_input_required_still_renders_its_text():
    resp = task(state="input-required",
                artifacts=[{"parts": [{"kind": "text", "text": "Which school?"}]}])
    r = parse_task(resp); text, state = r.text, r.state
    assert text == "Which school?"
    assert state == "input-required"


def test_jsonrpc_error_raises():
    with pytest.raises(A2AProtocolError):
        parse_task({"jsonrpc": "2.0", "id": "1",
                    "error": {"code": -32000, "message": "boom"}})


def test_failed_state_with_no_text_raises():
    with pytest.raises(A2AProtocolError):
        parse_task(task(state="failed", artifacts=[]))


def test_missing_result_raises():
    with pytest.raises(A2AProtocolError):
        parse_task({"jsonrpc": "2.0", "id": "1"})


# ── client-facing shape ──────────────────────────────────────────────────────

def test_chat_completion_shape():
    out = chat_completion("hi", model="agent-x")
    assert out["object"] == "chat.completion"
    assert out["model"] == "agent-x"
    assert out["choices"][0]["message"] == {"role": "assistant", "content": "hi"}
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["usage"]["total_tokens"] == 0  # A2A carries no token counts


def test_sse_stream_is_well_formed_and_terminated():
    """Clients default to stream:true and break on a plain body, so this path is
    not optional even though the upstream answer arrives whole."""
    frames = sse_chunks("hello", model="agent-x")
    assert frames[-1] == "data: [DONE]\n\n"
    assert all(f.startswith("data: ") for f in frames)

    payloads = [json.loads(f[len("data: "):]) for f in frames[:-1]]
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert payloads[1]["choices"][0]["delta"] == {"content": "hello"}
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert {p["object"] for p in payloads} == {"chat.completion.chunk"}


# ── recorded fixtures, when present ──────────────────────────────────────────

@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")))
def test_recorded_fixtures_parse(path: Path):
    """Every recorded response must still map cleanly.

    Skips silently while the fixtures directory is empty so the suite is green
    before any have been captured.
    """
    data = json.loads(path.read_text())
    if data.get("error"):
        with pytest.raises(A2AProtocolError):
            parse_task(data)
        return
    r = parse_task(data); text, context_id, state = r.text, r.context_id, r.state
    assert context_id, f"{path.name}: no contextId to persist"
    assert state, f"{path.name}: no task state"
    if state not in {"failed", "rejected", "canceled"}:
        assert text, f"{path.name}: terminal-ok task with no text"


def test_sqlite_store_migrates_a_pre_existing_table(tmp_path):
    """A store created before a column existed must gain it, not silently drop data.

    The volume outlives container rebuilds on purpose, so an old schema is the
    normal case after an upgrade, not an edge case.
    """
    import sqlite3

    from a2a_bridge.store import SqliteStore

    db = tmp_path / "old.db"
    # Simulate the previous release's schema: no request_message_id.
    old = sqlite3.connect(db)
    old.execute(
        """CREATE TABLE turns (
               id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
               conversation_id TEXT NOT NULL, context_id TEXT, task_id TEXT,
               completed_at TEXT NOT NULL, seq INTEGER NOT NULL)"""
    )
    old.commit()
    old.close()

    store = SqliteStore(str(db))
    store.record_turn("a", "c1", "ctx", "task-1", "2026-01-01T00:00:00+00:00",
                      request_message_id="msg-1")
    row = store._conn.execute(
        "SELECT task_id, request_message_id FROM turns"
    ).fetchone()
    assert row == ("task-1", "msg-1")
    store.close()


def test_fragmented_parts_concatenate_without_a_separator():
    """A streaming agent emits one part per chunk of ONE document.

    Real shape from the live agent: ['[', 'THE DATA TRIBUNE]\\n\\nSubscriber-only',
    ' just need three things:', ...]. Joining these with the artifact separator
    splits words and breaks markdown mid-token — it rendered '[', 'THE', 'DATA'
    on separate lines in a live demo.
    """
    resp = task(artifacts=[{"parts": [
        {"kind": "text", "text": "["},
        {"kind": "text", "text": "THE DATA TRIBUNE]\n\nSubscriber-only briefing. I"},
        {"kind": "text", "text": " need three things:"},
        {"kind": "text", "text": ""},
    ]}])
    text = parse_task(resp, artifact_join="\n\n").text
    assert text == "[THE DATA TRIBUNE]\n\nSubscriber-only briefing. I need three things:"
    assert "[\n\nTHE" not in text


def test_separate_artifacts_still_get_the_separator():
    """Distinct artifacts are distinct documents, unlike fragments of one."""
    resp = task(artifacts=[
        {"parts": [{"kind": "text", "text": "first"}]},
        {"parts": [{"kind": "text", "text": "second"}]},
    ])
    assert parse_task(resp, artifact_join="\n\n").text == "first\n\nsecond"
