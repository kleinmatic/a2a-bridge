"""Minimal A2A client: card discovery and blocking `message/send`.

Only what a chat turn needs. No Task lifecycle management, no polling, no
push-notification config — an agent that needs those is not yet a fit for a
synchronous chat UI anyway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import AgentConfig
from .mapping import TaskResult, build_message_send, parse_task

log = logging.getLogger("a2a_bridge.a2a")

WELL_KNOWN = "/.well-known/agent-card.json"


class RateLimited(RuntimeError):
    """Upstream throttled us.

    Deliberately distinct from a protocol error. Rate limiting frequently sits in
    middleware *above* the JSON-RPC app, which means it arrives as a bare HTTP
    status with an empty body — no envelope, no request id, nothing to parse. Code
    that only inspects JSON-RPC errors will mistake it for a malformed response.
    """

    def __init__(self, retry_after: float | None = None):
        super().__init__("rate limited by upstream agent")
        self.retry_after = retry_after


class PayloadTooLarge(RuntimeError):
    pass


@dataclass
class AgentCard:
    url: str
    name: str | None = None
    protocol_version: str | None = None
    streaming: bool = False
    raw: dict[str, Any] | None = None

    @classmethod
    def parse(cls, data: dict[str, Any], *, fallback_url: str) -> AgentCard:
        caps = data.get("capabilities") or {}
        return cls(
            url=data.get("url") or fallback_url,
            name=data.get("name"),
            protocol_version=data.get("protocolVersion"),
            streaming=bool(caps.get("streaming")),
            raw=data,
        )


class A2AClient:
    def __init__(self, agent: AgentConfig, *, client: httpx.AsyncClient | None = None):
        self.agent = agent
        self._client = client or httpx.AsyncClient(timeout=agent.timeout_s, follow_redirects=False)
        self._card: AgentCard | None = None

    async def close(self) -> None:
        await self._client.aclose()

    # ── discovery ────────────────────────────────────────────────────────────
    async def card(self, *, refresh: bool = False) -> AgentCard:
        if self._card and not refresh:
            return self._card

        url = self.agent.card_url
        candidates = [url] if url.endswith(WELL_KNOWN) else [url.rstrip("/") + WELL_KNOWN, url]

        last_err: Exception | None = None
        for candidate in candidates:
            try:
                resp = await self._client.get(candidate)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and ("url" in data or "protocolVersion" in data):
                    self._card = AgentCard.parse(data, fallback_url=self.agent.card_url)
                    log.info(
                        "agent %s: card ok (name=%s protocol=%s streaming=%s endpoint=%s)",
                        self.agent.id, self._card.name, self._card.protocol_version,
                        self._card.streaming, self.endpoint,
                    )
                    return self._card
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                last_err = exc

        raise RuntimeError(f"agent {self.agent.id}: no agent card at {candidates}") from last_err

    @property
    def endpoint(self) -> str:
        """Where JSON-RPC actually goes.

        Explicit config wins, then the card's own `url`, then the card location.
        """
        if self.agent.endpoint_url:
            return self.agent.endpoint_url
        if self._card:
            return self._card.url
        return self.agent.card_url

    # ── the one call we make ─────────────────────────────────────────────────
    async def send(
        self,
        text: str,
        *,
        context_id: str | None,
        caller_id: str | None = None,
    ) -> TaskResult:
        headers = {"Content-Type": "application/json", **self.agent.static_headers}
        headers.update(self.agent.caller.headers_for(caller_id))

        payload = build_message_send(text, context_id=context_id)
        resp = await self._client.post(self.endpoint, json=payload, headers=headers)

        # Transport-level rejections first: these carry no JSON-RPC envelope.
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            raise RateLimited(float(retry_after) if retry_after else None)
        if resp.status_code == 413:
            raise PayloadTooLarge("upstream rejected the request body as too large")
        if resp.status_code in (307, 308) or (300 <= resp.status_code < 400):
            # Redirects are not followed on purpose. A POST redirected to the
            # canonical path is silently dropped by most clients, so surface it
            # as the configuration error it is (very often a missing trailing
            # slash on the endpoint URL).
            raise RuntimeError(
                f"agent {self.agent.id}: endpoint redirected ({resp.status_code}) to "
                f"{resp.headers.get('Location')!r}. Set endpoint_url to the exact form, "
                "including any trailing slash."
            )
        resp.raise_for_status()

        return parse_task(resp.json(), artifact_join=self.agent.artifact_join)
