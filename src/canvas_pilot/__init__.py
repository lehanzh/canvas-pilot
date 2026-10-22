"""Canvas LMS academic-assistant MCP server."""

import logging
import sys


def main() -> None:
    # httpx logs every request URL at INFO; keep the client's stderr log readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from .cli import main as cli_main

    sys.exit(cli_main())
