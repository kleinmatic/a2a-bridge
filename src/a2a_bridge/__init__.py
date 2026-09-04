"""a2a-bridge: connect an OpenAI-compatible chat client to an A2A agent."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("a2a-bridge")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"

__all__ = ["__version__"]
