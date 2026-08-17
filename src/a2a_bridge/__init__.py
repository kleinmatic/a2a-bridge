"""a2a-bridge — connect any OpenAI-compatible chat client to any A2A agent."""

__version__ = "0.1.0"

from .config import AgentConfig, BridgeConfig, CallerAuth  # noqa: F401
from .mapping import A2AProtocolError  # noqa: F401
