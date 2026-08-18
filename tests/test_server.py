"""End-to-end tests through the HTTP surface, with the A2A peer mocked.

Covers the paths that only break once assembled: contextId minting then reuse,
the streaming variant chat clients actually request, and the transport-level
rejections that carry no JSON-RPC body.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from a2a_bridge import server as server_mod
from a2a_bridge.config import BridgeConfig

AGENT_URL = "https://agent.test/api/publisher/"
CARD_URL = "https://agent.test/api/publisher/.well-known/agent-card.json"

CONFIG_YAML = f"""
store: "memory://"
agents:
  - id: publisher
    card_url: {AGENT_URL}
    conversation_id_header: X-Conversation-Id
    message_id_header: X-Message-Id
    caller:
      id_header: X-Caller-Id
      auth_header: X-Caller-Auth
      secret_env: TEST_SECRET
  - id: blocking-publisher
    card_url: {AGENT_URL}
    conversation_id_header: X-Conversation-Id
    stream_mode: blocking
"""

CARD = {
    "name": "Test Publisher",
    "url": AGENT_URL,
    "protocolVersion": "0.3.0",
    "capabilities": {"streaming": True},
}


def a2a_task(text: str, *, context_id: str, state: str = "completed") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "kind": "task",
            "contextId": context_id,
            "status": {"state": state},
            "artifacts": [{"parts": [{"kind": "text", "text": text}]}],
        },
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg_path = tmp_path / "agents.yml"
    cfg_path.write_text(CONFIG_YAML)
    monkeypatch.setenv("TEST_SECRET", "s3cret")
    server_mod.STATE["config"] = BridgeConfig.load(cfg_path)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(CARD_URL).mock(return_value=httpx.Response(200, json=CARD))
        with TestClient(server_mod.app) as c:
            c.respx = mock  # type: ignore[attr-defined]
            yield c
    server_mod.STATE.clear()


def test_healthz_and_models(client):
    assert client.get("/healthz").json()["status"] == "ok"
    body = client.get("/v1/models").json()
    assert [m["id"] for m in body["data"]] == ["blocking-publisher", "publisher"]


def test_unknown_model_is_404(client):
    r = client.post("/v1/chat/completions",
                    json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404


def test_context_id_is_minted_then_reused(client):
    """First turn omits contextId and stores what comes back; later turns echo it.

    This is the behavior that keeps a session — and anything the agent has decided
    about the caller — alive across turns.
    """
    route = client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(200, json=a2a_task("first answer", context_id="ctx-42"))
    )

    r1 = client.post(
        "/v1/chat/completions",
        headers={"X-Conversation-Id": "conv-1", "X-Caller-Id": "user-7"},
        json={"model": "publisher", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r1.status_code == 200
    assert r1.json()["choices"][0]["message"]["content"] == "first answer"

    sent = json.loads(route.calls[0].request.content)
    assert "contextId" not in sent["params"]["message"], "first turn must let the server mint"

    # The caller identity is forwarded and signed, so the peer can rate-limit per
    # user rather than per IP.
    hdrs = route.calls[0].request.headers
    assert hdrs["x-caller-id"] == "user-7"
    assert len(hdrs["x-caller-auth"]) == 16

    route.mock(return_value=httpx.Response(200, json=a2a_task("second", context_id="ctx-42")))
    client.post(
        "/v1/chat/completions",
        headers={"X-Conversation-Id": "conv-1", "X-Caller-Id": "user-7"},
        json={"model": "publisher", "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "again"},
        ]},
    )
    sent2 = json.loads(route.calls[1].request.content)
    assert sent2["params"]["message"]["contextId"] == "ctx-42", "later turns must echo it"
    assert sent2["params"]["message"]["parts"][0]["text"] == "again", "only the newest turn"


def test_separate_conversations_get_separate_contexts(client):
    client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(200, json=a2a_task("a", context_id="ctx-A"))
    )
    client.post("/v1/chat/completions", headers={"X-Conversation-Id": "conv-A"},
                json={"model": "publisher", "messages": [{"role": "user", "content": "x"}]})

    route = client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(200, json=a2a_task("b", context_id="ctx-B"))
    )
    client.post("/v1/chat/completions", headers={"X-Conversation-Id": "conv-B"},
                json={"model": "publisher", "messages": [{"role": "user", "content": "y"}]})
    sent = json.loads(route.calls[-1].request.content)
    assert "contextId" not in sent["params"]["message"], "a new conversation starts fresh"


def test_streaming_response_for_a_blocking_agent(client):
    """stream_mode: blocking still satisfies a streaming client.

    Chat clients request streaming by default and break on a plain body, so the
    finished answer is emitted as a single delta rather than refused.
    """
    client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(200, json=a2a_task("streamed", context_id="ctx-1"))
    )
    r = client.post("/v1/chat/completions",
                    headers={"X-Conversation-Id": "conv-s"},
                    json={"model": "blocking-publisher", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text.endswith("data: [DONE]\n\n")
    assert "streamed" in r.text


def test_rate_limit_becomes_an_in_chat_message_not_a_red_error(client):
    """A 429 arrives as a bare status with an empty body — no JSON-RPC envelope.

    Default behavior renders it as an assistant message so the user learns what
    happened, instead of a chat client showing a contextless failure banner.
    """
    client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "2"}, content=b"")
    )
    r = client.post("/v1/chat/completions",
                    headers={"X-Conversation-Id": "conv-r"},
                    json={"model": "publisher", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert "rate limiting" in content
    assert "2s" in content


def test_redirect_is_reported_rather_than_followed(client):
    """A POST redirected to the canonical path loses its body in most clients, so
    it surfaces as the configuration error it is."""
    client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(307, headers={"Location": "https://agent.test/api/publisher"})
    )
    r = client.post("/v1/chat/completions",
                    headers={"X-Conversation-Id": "conv-x"},
                    json={"model": "publisher", "messages": [{"role": "user", "content": "hi"}]})
    content = r.json()["choices"][0]["message"]["content"]
    assert "redirected" in content or "unreachable" in content


def test_gate_text_passes_through_verbatim(client):
    """Labelled cross-agent blocks must not be reformatted — they are the visible
    handoff, and there is no model in this path to paraphrase them."""
    gate = "[SUBSCRIPTIONS]\nSubscribe for $24/yr.\n\n[SCHOOL TOURS]\nBook after subscribing."
    client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(200, json=a2a_task(gate, context_id="ctx-g"))
    )
    r = client.post("/v1/chat/completions",
                    headers={"X-Conversation-Id": "conv-g"},
                    json={"model": "publisher", "messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["choices"][0]["message"]["content"] == gate


def test_turn_is_recorded_with_the_agents_task_id(client):
    """The agent's per-turn id must be persisted at request time.

    contextId identifies the conversation; only taskId identifies the individual
    answer. It is emitted once and cannot be reconstructed later, so failing to
    store it makes any after-the-fact question about a specific reply —
    feedback, billing, audit — permanently unanswerable.
    """
    client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(200, json={
            "jsonrpc": "2.0", "id": "1",
            "result": {
                "kind": "task", "id": "task-abc-123", "contextId": "ctx-1",
                "status": {"state": "completed"},
                "artifacts": [{"parts": [{"kind": "text", "text": "answer"}]}],
            },
        })
    )
    client.post("/v1/chat/completions",
                headers={"X-Conversation-Id": "conv-t", "X-Message-Id": "usermsg-9"},
                json={"model": "publisher", "messages": [{"role": "user", "content": "hi"}]})

    turns = server_mod.STATE["store"]._turns
    assert len(turns) == 1
    agent_id, conversation_id, context_id, task_id, completed_at, req_msg_id = turns[0]
    assert (agent_id, conversation_id, context_id, task_id) == ("publisher", "conv-t", "ctx-1", "task-abc-123")
    assert completed_at.endswith("+00:00"), "timestamp must carry an offset, not be naive"
    # The inbound message id is what later resolves to the REPLY, and from there
    # to anything attached to it. Ordinal or timestamp matching would break on an
    # edit or a regeneration; this does not.
    assert req_msg_id == "usermsg-9"


def test_a_store_failure_never_costs_the_user_their_answer(client, monkeypatch):
    """Recording is bookkeeping; the answer is what was paid for."""
    def boom(*a, **k):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(server_mod.STATE["store"], "record_turn", boom)
    client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(200, json=a2a_task("still fine", context_id="ctx-1"))
    )
    r = client.post("/v1/chat/completions",
                    headers={"X-Conversation-Id": "conv-f"},
                    json={"model": "publisher", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "still fine"


SSE_WITH_PROGRESS = (
    'data: {"jsonrpc":"2.0","result":{"kind":"status-update","contextId":"ctx-9",'
    '"taskId":"task-9","final":false,"status":{"state":"working","message":'
    '{"role":"agent","parts":[{"kind":"text","text":"Reading your question\u2026"}]}}}}\n\n'
    'data: {"jsonrpc":"2.0","result":{"kind":"status-update","contextId":"ctx-9",'
    '"taskId":"task-9","final":false,"status":{"state":"working","message":'
    '{"role":"agent","parts":[{"kind":"text","text":"Handing off to Tours\u2026"}]}}}}\n\n'
    'data: {"jsonrpc":"2.0","result":{"kind":"artifact-update","contextId":"ctx-9",'
    '"taskId":"task-9","artifact":{"parts":[{"kind":"text","text":"[TOURS]\\nBooked."}]}}}\n\n'
    'data: {"jsonrpc":"2.0","result":{"kind":"status-update","contextId":"ctx-9",'
    '"taskId":"task-9","final":true,"status":{"state":"completed"}}}\n\n'
)


def test_progress_notes_are_forwarded_then_the_answer(client):
    """Working-state notes reach the user before the answer exists.

    This is the whole reason for using the streaming method: the answer may still
    arrive whole, so the gain is movement during the wait, not a typing effect.
    """
    client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(200, text=SSE_WITH_PROGRESS,
                                    headers={"Content-Type": "text/event-stream"})
    )
    r = client.post("/v1/chat/completions",
                    headers={"X-Conversation-Id": "conv-p", "X-Message-Id": "m-1"},
                    json={"model": "publisher", "stream": True,
                          "messages": [{"role": "user", "content": "book a tour"}]})
    assert r.status_code == 200
    body = r.text
    # notes arrive, wrapped so they read as distinct from the answer
    assert "_Reading your question" in body
    assert "_Handing off to Tours" in body
    # the answer follows, verbatim -- labels intact
    assert "[TOURS]" in body and "Booked." in body
    assert body.index("Reading your question") < body.index("Booked."), "notes must precede the answer"
    assert body.endswith("data: [DONE]\n\n")

    # the turn is still recorded, with the ids the stream carried
    turns = server_mod.STATE["store"]._turns
    assert turns and turns[-1][2] == "ctx-9" and turns[-1][3] == "task-9"
    assert turns[-1][5] == "m-1"


def test_a_stream_that_dies_midway_still_tells_the_user(client):
    """A truncated stream must not look like a finished empty answer."""
    client.respx.post(AGENT_URL).mock(side_effect=httpx.ReadError("connection lost"))
    r = client.post("/v1/chat/completions",
                    headers={"X-Conversation-Id": "conv-d"},
                    json={"model": "publisher", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert "stopped mid-answer" in r.text or "rate limiting" in r.text
    assert r.text.endswith("data: [DONE]\n\n")
