"""
radiomcp - Internet radio MCP server + TUI player

pip install radiomcp
  radiomcp       → MCP server (Claude Desktop / HTTP API / CLI)
  radio          → Interactive TUI player (terminal)

24,000+ stations from 197 countries
Powered by RadioGraph API
"""

try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("radiomcp")
except Exception:
    __version__ = "1.2.7"
__author__ = "meshpop"

from .server import main, mcp

__all__ = ["main", "mcp", "__version__"]
