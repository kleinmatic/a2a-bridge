"""OpenAI-compatible HTTP surface in front of one or more A2A agents."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .a2a import A2AClient, PayloadTooLarge, RateLimited
from .config import BridgeConfig
from .mapping import (
    A2AProtocolError,
    chat_completion,
    last_user_text,
    sse_chunks,
    sse_frame,
)
from .store import build_store

log = logging.getLogger("a2a_bridge")

STATE: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: BridgeConfig = STATE.get("config") or BridgeConfig.load()
    STATE["config"] = cfg
    STATE["store"] = build_store(cfg.store_url)
    STATE["clients"] = {aid: A2AClient(agent) for aid, agent in cfg.agents.items()}

    # No keys means anyone who can reach the port can spend the agent's tokens.
    # Fine on a laptop, rarely what someone wants on a server, and until now it
    # happened in silence -- including when `api_keys_env` names a variable that
    # was never exported, which looks configured and is not.
    if not cfg.api_keys:
        log.warning(
            "no api_keys configured: the bridge is open to anyone who can reach it. "
            "Set api_keys in the config, or api_keys_env naming an exported variable."
        )

    # Fetch cards at startup for a fast failure and a legible log line, but do
    # not die on it: an agent that is briefly unreachable should not stop the
    # bridge from serving the others, and a conference network is not a reason
    # to be in a crash loop.
    for aid, client in STATE["clients"].items():
        try:
            await client.card()
        except Exception as exc:  # noqa: BLE001
            log.warning("agent %s: card unavailable at startup (%s); will retry on demand", aid, exc)

    try:
        yield
    finally:
        for client in STATE["clients"].values():
            await client.close()
        STATE["store"].close()


app = FastAPI(title="a2a-bridge", version=__version__, lifespan=lifespan)


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    keys = STATE["config"].api_keys
    if not keys:
        return
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token not in keys:
        raise HTTPException(status_code=401, detail="invalid api key")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "agents": sorted(STATE["config"].agents)}


@app.get("/v1/models")
async def models(_: None = Depends(require_api_key)) -> dict[str, Any]:
    """Each configured agent presents as a model, so clients self-populate."""
    return {
        "object": "list",
        "data": [
            {"id": aid, "object": "model", "owned_by": "a2a-bridge"}
            for aid in sorted(STATE["config"].agents)
        ],
    }


def _conversation_id(agent, request: Request, body: dict[str, Any]) -> str:
    """Find a stable key for this conversation.

    Preference order matters. A client-supplied conversation id is the only
    option that survives the user editing an earlier message; deriving a key from
    message content does not, and since contextId carries entitlement state, a
    rotated key silently sends someone back through the gate. Falling back to a
    hash is better than failing, but it is a fallback.
    """
    if agent.conversation_id_header:
        value = request.headers.get(agent.conversation_id_header)
        if value:
            return value

    for field in ("conversation_id", "user"):
        if body.get(field):
            return str(body[field])

    import hashlib

    seed = last_user_text(body.get("messages") or [])
    log.warning(
        "agent %s: no conversation id available; falling back to a content hash. "
        "Editing the first message will start a new session.", agent.id,
    )
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()[:32]


async def _streamed(request: Request, agent_cfg, client, store, model: str,
                   text: str, conversation_id: str, caller_id: str | None,
                   context_id: str | None):
    """Serve a streaming request from the agent's own stream.

    Progress notes are forwarded as they arrive so the user sees movement instead
    of a blank pane; the answer follows. Both go down the single content channel
    the chat-completions format provides, so ordering is the only separation
    available — hence notes first, answer last.
    """
    import time
    import uuid as _uuid

    completion_id = f"chatcmpl-{_uuid.uuid4().hex[:24]}"
    created = int(time.time())
    wrap = (agent_cfg.progress_prefix, agent_cfg.progress_suffix)

    async def gen():
        answer: list[str] = []
        seen_context = context_id
        seen_task = None
        wrote_progress = False
        yield sse_frame({"role": "assistant"}, model=model, completion_id=completion_id, created=created)
        try:
            async for ev in client.stream(text, context_id=context_id, caller_id=caller_id):
                seen_context = ev.context_id or seen_context
                seen_task = ev.task_id or seen_task
                if ev.kind == "progress" and agent_cfg.show_progress:
                    wrote_progress = True
                    yield sse_frame(
                        {"content": f"{wrap[0]}{ev.text}{wrap[1]}\n"},
                        model=model, completion_id=completion_id, created=created,
                    )
                elif ev.kind == "artifact" and ev.text:
                    if wrote_progress and not answer:
                        # separate the work log from the answer
                        yield sse_frame({"content": "\n"}, model=model,
                                        completion_id=completion_id, created=created)
                    answer.append(ev.text)
                    yield sse_frame({"content": ev.text}, model=model,
                                    completion_id=completion_id, created=created)
        except RateLimited as exc:
            detail = "The agent is rate limiting requests."
            if exc.retry_after:
                detail += f" Try again in about {exc.retry_after:.0f}s."
            yield sse_frame({"content": detail}, model=model,
                            completion_id=completion_id, created=created)
        except Exception as exc:
            log.exception("agent %s: stream failed", model)
            yield sse_frame({"content": f"\n\nThe agent stopped mid-answer ({exc})."},
                            model=model, completion_id=completion_id, created=created)

        if seen_context and seen_context != context_id:
            store.put(model, conversation_id, seen_context)
        try:
            store.record_turn(
                model, conversation_id, seen_context, seen_task,
                datetime.now(UTC).isoformat(),
                request_message_id=(
                    request.headers.get(agent_cfg.message_id_header)
                    if agent_cfg.message_id_header
                    else None
                ),
            )
        except Exception:
            log.exception("agent %s: failed to record turn (answer unaffected)", model)

        log.info(
            "agent=%s conversation=%s context=%s task=%s streamed chars=%d",
            model, conversation_id, seen_context, seen_task, sum(len(a) for a in answer),
        )
        yield sse_frame({}, model=model, completion_id=completion_id, created=created, finish="stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _: None = Depends(require_api_key)):
    cfg: BridgeConfig = STATE["config"]
    body = await request.json()

    model = body.get("model")
    agent_cfg = cfg.agents.get(model)
    if agent_cfg is None:
        raise HTTPException(status_code=404, detail=f"unknown model {model!r}")

    client: A2AClient = STATE["clients"][model]
    store = STATE["store"]

    try:
        text = last_user_text(body.get("messages") or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    conversation_id = _conversation_id(agent_cfg, request, body)
    caller_id = request.headers.get(agent_cfg.caller.id_header) if agent_cfg.caller.id_header else None
    context_id = store.get(model, conversation_id)

    # Prefer the agent's own stream when it has one: it is the only way its
    # working-state notes reach the user. Falls back silently, because a missing
    # card or a non-streaming agent should degrade to a slower answer, not none.
    if body.get("stream") and agent_cfg.stream_mode == "auto":
        try:
            card = await client.card()
            streaming_supported = card.streaming
        except Exception:  # noqa: BLE001
            streaming_supported = False
        if streaming_supported:
            return await _streamed(
                request, agent_cfg, client, store, model, text,
                conversation_id, caller_id, context_id,
            )

    try:
        result = await client.send(text, context_id=context_id, caller_id=caller_id)
    except RateLimited as exc:
        # Surfaced, never retried away: a throttled request is information the
        # operator wants, and a silent retry loop turns one visible failure into
        # an invisible one plus more load.
        detail = "The agent is rate limiting requests."
        if exc.retry_after:
            detail += f" Try again in about {exc.retry_after:.0f}s."
        return _error_response(agent_cfg, model, detail, status=429)
    except PayloadTooLarge:
        return _error_response(agent_cfg, model, "That message was too large for the agent.", status=413)
    except A2AProtocolError as exc:
        log.error("agent %s: protocol error: %s", model, exc)
        return _error_response(agent_cfg, model, f"The agent could not answer ({exc}).", status=502)
    except Exception as exc:
        log.exception("agent %s: call failed", model)
        return _error_response(agent_cfg, model, f"The agent is unreachable ({exc}).", status=502)

    reply, new_context_id, state = result.text, result.context_id, result.state

    # Remember whatever the server minted. It owns the value; we only echo it.
    if new_context_id and new_context_id != context_id:
        store.put(model, conversation_id, new_context_id)

    # Record the turn. The agent's per-turn id is emitted once and cannot be
    # recovered afterwards, so it is captured even though nothing reads it yet:
    # without it, "which answer was this about?" is unanswerable for feedback,
    # billing questions or any later audit. Best-effort — a storage failure must
    # never cost the user an answer they already paid for.
    try:
        store.record_turn(
            model, conversation_id, new_context_id, result.task_id,
            datetime.now(UTC).isoformat(),
            request_message_id=(
                request.headers.get(agent_cfg.message_id_header)
                if agent_cfg.message_id_header
                else None
            ),
        )
    except Exception:
        log.exception("agent %s: failed to record turn (answer unaffected)", model)

    log.info(
        "agent=%s conversation=%s context=%s task=%s state=%s chars=%d",
        model, conversation_id, new_context_id, result.task_id, state, len(reply),
    )

    if body.get("stream"):
        return StreamingResponse(
            iter(sse_chunks(reply, model=model)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return JSONResponse(chat_completion(reply, model=model))


def _error_response(agent_cfg, model: str, detail: str, *, status: int):
    """Render a failure the way this agent is configured to.

    Default is an in-chat assistant message rather than an HTTP error: a chat UI
    turns a non-2xx into a red banner with no context, which is a poor way to
    learn that an agent is throttling you. `on_error: http_error` is there for
    programmatic callers that would rather have the status.
    """
    if agent_cfg.on_error == "http_error":
        return JSONResponse({"error": {"message": detail, "type": "upstream_error"}}, status_code=status)
    return JSONResponse(chat_completion(detail, model=model))


def main() -> None:  # pragma: no cover
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("A2A_BRIDGE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        app,
        host=os.environ.get("A2A_BRIDGE_HOST", "0.0.0.0"),
        port=int(os.environ.get("A2A_BRIDGE_PORT", "8600")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
