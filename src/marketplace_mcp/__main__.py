"""`python -m marketplace_mcp`: serve over stdio."""

import logging
import sys

from marketplace_mcp.config import ConfigError, Settings
from marketplace_mcp.server import build_server


def main() -> int:
    # stdout carries the protocol; anything human-readable goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"marketplace-mcp: {exc}", file=sys.stderr)
        return 2
    server = build_server(settings)
    mode = "read+write" if settings.writes_enabled else "read-only"
    print(f"marketplace-mcp: {settings.base_url} ({mode})", file=sys.stderr)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
