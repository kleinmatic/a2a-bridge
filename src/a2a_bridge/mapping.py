"""Pure translation between the OpenAI chat-completions shape and A2A JSON-RPC.

Deliberately free of I/O so the wire contract can be tested against recorded
fixtures without a network or a running agent.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class TaskResult:
    """One completed A2A turn.

    `task_id` is the agent's own per-turn identifier and the only durable way to
    say WHICH answer something refers to. `context_id` identifies the whole
    conversation, so it cannot attribute anything to a single turn — the
    distinction matters for feedback, billing disputes, and any after-the-fact
    analysis. It is free to capture at request time and impossible to
    reconstruct later, so it is captured whether or not anything consumes it yet.
    """

    text: str
    context_id: str | None
    state: str
    task_id: str | None = None


# States that carry text meant for the user. `input-required` is a legitimate
# question back to them, so it renders like any other answer.
TERMINAL_OK_STATES = {"completed", "input-required"}
FAILED_STATES = {"failed", "rejected", "canceled", "cancelled", "unknown"}


def parse_task(response: dict[str, Any], *, artifact_join: str = "\n\n") -> TaskResult:
    """Turn an A2A `message/send` response into a TaskResult.

    The payload lives in `result.artifacts[].parts[].text`, NOT `result.message`
    — a distinction that cost us a round of debugging when first verified.
    """
    if response.get("error"):
        err = response["error"]
        raise A2AProtocolError(
            f"JSON-RPC error {err.get('code')}: {err.get('message')}", payload=err
        )

    result = response.get("result")
    if not isinstance(result, dict):
        raise A2AProtocolError("response has no result object", payload=response)

    context_id = result.get("contextId")
    task_id = result.get("id")
    state = (result.get("status") or {}).get("state", "unknown")

    # Two different joins, and conflating them corrupts the text.
    #
    # Parts WITHIN one artifact are fragments of a single document — a streaming
    # agent emits one part per chunk, so "[", "THE RIDGE", "LINE GAZETTE]" must
    # concatenate with nothing between them. Any separator here splits words and
    # breaks markdown mid-token.
    #
    # Separate artifacts ARE separate documents, so they keep `artifact_join`.
    documents: list[str] = []
    for artifact in result.get("artifacts") or []:
        fragments = [
            part["text"]
            for part in artifact.get("parts") or []
            if part.get("kind") == "text" and part.get("text")
        ]
        if fragments:
            documents.append("".join(fragments))

    text = artifact_join.join(documents)

    if not text and state in FAILED_STATES:
        raise A2AProtocolError(f"agent returned state {state!r} with no text", state=state, payload=result)

    return TaskResult(text=text, context_id=context_id, state=state, task_id=task_id)


@dataclass(frozen=True)
class StreamEvent:
    """One decoded frame of an A2A `message/stream` response.

    Three shapes matter: a working-state note meant for the user to read while
    waiting, a chunk of the answer, and the terminal marker.
    """

    kind: str                      # "progress" | "artifact" | "final"
    text: str = ""
    context_id: str | None = None
    task_id: str | None = None
    state: str | None = None


def parse_stream_event(payload: dict[str, Any]) -> StreamEvent | None:
    """Decode one SSE frame. Returns None for frames carrying nothing useful.

    Progress copy lives on the task STATUS, not in an artifact, which is what
    keeps it out of the answer text — a status note is never mistaken for content.
    """
    if payload.get("error"):
        err = payload["error"]
        raise A2AProtocolError(
            f"JSON-RPC error {err.get('code')}: {err.get('message')}", payload=err
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        return None

    common = {"context_id": result.get("contextId"), "task_id": result.get("taskId") or result.get("id")}
    kind = result.get("kind")

    if kind == "artifact-update":
        artifact = result.get("artifact") or {}
        text = "".join(
            part.get("text", "")
            for part in artifact.get("parts") or []
            if part.get("kind") == "text"
        )
        return StreamEvent(kind="artifact", text=text, **common)

    if kind == "status-update":
        status = result.get("status") or {}
        state = status.get("state")
        if result.get("final"):
            return StreamEvent(kind="final", state=state, **common)
        message = status.get("message") or {}
        note = " ".join(
            part.get("text", "")
            for part in message.get("parts") or []
            if part.get("kind") == "text"
        ).strip()
        if not note:
            return None
        return StreamEvent(kind="progress", text=note, state=state, **common)

    return None


def sse_frame(delta: dict[str, Any], *, model: str, completion_id: str, created: int,
              finish: str | None = None) -> str:
    """One `chat.completion.chunk` frame."""
    import json

    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


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
