"""Pure translation between the OpenAI chat-completions shape and A2A JSON-RPC.

Deliberately free of I/O so the wire contract can be tested against recorded
fixtures without a network or a running agent.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Iterable

JSONRPC_VERSION = "2.0"


class A2AProtocolError(RuntimeError):
    """The peer answered, but not with something we can turn into a reply."""

    def __init__(self, message: str, *, state: str | None = None, payload: Any = None):
        super().__init__(message)
        self.state = state
        self.payload = payload


def last_user_text(messages: Iterable[dict[str, Any]]) -> str:
    """Extract the newest user turn.

    We send ONLY this, never the full history. A2A agents are stateful per
    contextId and keep their own transcript; replaying history would duplicate
    their context every turn and inflate their token spend. This is the single
    most important rule in the mapping.
    """
    text: str | None = None
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # OpenAI multipart content: keep the text parts, in order.
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            text = "".join(parts)
    if not text:
        raise ValueError("no user message in request")
    return text


def build_message_send(text: str, *, context_id: str | None = None, request_id: str | None = None) -> dict[str, Any]:
    """Build a JSON-RPC `message/send` envelope.

    `contextId` is omitted on the first turn so the server mints it; we then
    persist whatever it returns and echo it back on every later turn. The server
    binds subscription state to that value, so it is ours to remember, never
    ours to invent.
    """
    message: dict[str, Any] = {
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
        "messageId": str(uuid.uuid4()),
    }
    if context_id:
        message["contextId"] = context_id
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id or str(uuid.uuid4()),
        "method": "message/send",
        "params": {"message": message},
    }


# States that carry text meant for the user. `input-required` is a legitimate
# question back to them, so it renders like any other answer.
TERMINAL_OK_STATES = {"completed", "input-required"}
FAILED_STATES = {"failed", "rejected", "canceled", "cancelled", "unknown"}


def parse_task(response: dict[str, Any], *, artifact_join: str = "\n\n") -> tuple[str, str | None, str]:
    """Turn an A2A `message/send` response into (text, context_id, state).

    The payload lives in `result.artifacts[].parts[].text`, NOT `result.message`
    — a distinction that cost us a round of debugging when first verified.
    """
    if "error" in response and response["error"]:
        err = response["error"]
        raise A2AProtocolError(
            f"JSON-RPC error {err.get('code')}: {err.get('message')}", payload=err
        )

    result = response.get("result")
    if not isinstance(result, dict):
        raise A2AProtocolError("response has no result object", payload=response)

    context_id = result.get("contextId")
    state = (result.get("status") or {}).get("state", "unknown")

    chunks: list[str] = []
    for artifact in result.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("kind") == "text" and part.get("text"):
                chunks.append(part["text"])

    text = artifact_join.join(chunks)

    if not text and state in FAILED_STATES:
        raise A2AProtocolError(f"agent returned state {state!r} with no text", state=state, payload=result)

    return text, context_id, state


def chat_completion(text: str, *, model: str, completion_id: str | None = None) -> dict[str, Any]:
    """Wrap text in a non-streaming chat-completions response.

    Token counts are reported as zero: A2A carries none. Clients that need a
    context ceiling must be configured with one (in LibreChat, `maxContextTokens`
    on the endpoint).
    """
    return {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def sse_chunks(text: str, *, model: str, completion_id: str | None = None) -> list[str]:
    """Render a complete answer as a minimal SSE stream.

    Chat clients commonly request `stream: true` by default and break on a plain
    JSON body, so streaming support is not optional even when the upstream agent
    is blocking. Agents that deliver a whole artifact in one frame gain nothing
    from real streaming, so we emit one content delta and finish: the client gets
    its spinner and its expected framing without us pretending to token-stream.
    """
    cid = completion_id or f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def frame(delta: dict[str, Any], finish: str | None) -> str:
        import json

        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return [
        frame({"role": "assistant"}, None),
        frame({"content": text}, None),
        frame({}, "stop"),
        "data: [DONE]\n\n",
    ]
