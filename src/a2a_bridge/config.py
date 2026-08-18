"""Configuration: one block per A2A agent, no code changes to add another.

Nothing here is specific to any particular agent. Vendor-shaped details — header
names, secrets, endpoint URLs — arrive as config, which is what keeps the bridge
reusable rather than one integration with the serial numbers filed off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = os.environ.get("A2A_BRIDGE_CONFIG", "agents.yml")


@dataclass
class CallerAuth:
    """Optional caller identity forwarded upstream.

    Agents that rate-limit per IP will see every user of a server-side bridge as
    one caller. Forwarding a stable per-user id lets them key their bucket on the
    user instead. The signature exists because an unsigned identity header is
    just a rate-limit bypass waiting to be discovered: it must be verifiable and
    the secret must never reach a browser.
    """

    id_header: str | None = None
    auth_header: str | None = None
    secret_env: str | None = None
    digest_bytes: int = 8  # rendered hex, so 8 bytes -> 16 chars

    @property
    def secret(self) -> str | None:
        return os.environ.get(self.secret_env) if self.secret_env else None

    def headers_for(self, caller_id: str | None) -> dict[str, str]:
        if not (self.id_header and caller_id):
            return {}
        out = {self.id_header: caller_id}
        secret = self.secret
        if self.auth_header and secret:
            import hashlib
            import hmac

            mac = hmac.new(secret.encode(), caller_id.encode(), hashlib.sha256)
            out[self.auth_header] = mac.hexdigest()[: self.digest_bytes * 2]
        return out


@dataclass
class AgentConfig:
    id: str
    """Doubles as the model name an OpenAI-compatible client will ask for."""

    card_url: str
    """Agent card, or the endpoint itself — some agents serve the card at both."""

    endpoint_url: str | None = None
    """Overrides the card's `url`. Set only when the card is wrong."""

    conversation_id_header: str | None = None
    """Header carrying the client's conversation id, used to key contextId."""

    message_id_header: str | None = None
    """Header carrying the client's id for THIS user turn.

    Recorded, never used for routing. It is what makes a turn identifiable after
    the fact: chat clients typically link a reply to the message that prompted it,
    so the inbound id resolves to the reply, and from there to anything attached
    to that reply — a rating, a cost, an audit note. Without it the best available
    join is ordinal or timestamp matching, which breaks the moment a user edits or
    regenerates a turn.
    """

    caller: CallerAuth = field(default_factory=CallerAuth)
    static_headers: dict[str, str] = field(default_factory=dict)
    artifact_join: str = "\n\n"
    on_error: str = "message"  # "message" renders errors in-chat; "http_error" propagates
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        if self.on_error not in {"message", "http_error"}:
            raise ValueError(f"{self.id}: on_error must be 'message' or 'http_error'")


@dataclass
class BridgeConfig:
    agents: dict[str, AgentConfig]
    store_url: str = "memory://"
    api_keys: list[str] = field(default_factory=list)
    """If set, callers must present one as `Authorization: Bearer`."""

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> BridgeConfig:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        agents: dict[str, AgentConfig] = {}
        for entry in raw.get("agents") or []:
            caller = CallerAuth(**(entry.pop("caller", None) or {}))
            agent = AgentConfig(caller=caller, **entry)
            agents[agent.id] = agent
        if not agents:
            raise ValueError(f"{path}: no agents configured")

        keys_env = raw.get("api_keys_env")
        api_keys = raw.get("api_keys") or []
        if keys_env and os.environ.get(keys_env):
            api_keys = [k.strip() for k in os.environ[keys_env].split(",") if k.strip()]

        return cls(
            agents=agents,
            store_url=os.environ.get("A2A_BRIDGE_STORE", raw.get("store", "memory://")),
            api_keys=api_keys,
        )
