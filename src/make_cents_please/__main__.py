"""Entry point: ``python -m make_cents_please`` (stdio transport)."""

from __future__ import annotations

import sys

from .config import ConfigError, load_config
from .logging_setup import configure_logging
from .server import build_server


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"make-cents-please: {exc}", file=sys.stderr)
        sys.exit(2)
    configure_logging(config.log_level, [config.token.reveal()] if config.token else [])
    build_server(config).run("stdio")


if __name__ == "__main__":
    main()
