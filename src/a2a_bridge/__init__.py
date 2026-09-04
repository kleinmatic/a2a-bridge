"""a2a-bridge: connect an OpenAI-compatible chat client to an A2A agent."""

from importlib.metadata import PackageNotFoundError, version

from .config import AgentConfig, BridgeConfig, CallerAuth
from .mapping import A2AProtocolError

try:
    __version__ = version("a2a-bridge")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"

# These four are the embedding surface: anyone driving the bridge from their own
# process imports them from here. They predate __version__ and must keep working.
__all__ = ["A2AProtocolError", "AgentConfig", "BridgeConfig", "CallerAuth", "__version__"]
