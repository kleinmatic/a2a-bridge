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
    caller:
      id_header: X-Caller-Id
      auth_header: X-Caller-Auth
      secret_env: TEST_SECRET
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
    assert [m["id"] for m in body["data"]] == ["publisher"]


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


def test_streaming_response(client):
    client.respx.post(AGENT_URL).mock(
        return_value=httpx.Response(200, json=a2a_task("streamed", context_id="ctx-1"))
    )
    r = client.post("/v1/chat/completions",
                    headers={"X-Conversation-Id": "conv-s"},
                    json={"model": "publisher", "stream": True,
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
