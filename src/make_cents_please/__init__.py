"""make-cents-please: a local MCP server for budgeting platforms."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version in pyproject.toml.
    __version__ = version("make-cents-please")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"
