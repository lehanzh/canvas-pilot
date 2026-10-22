"""`canvas-pilot check`: verify the configuration and print a sample of what the tools will see."""

from __future__ import annotations

import asyncio

from .canvas import CanvasError
from .config import ConfigError, config_file


async def _check() -> int:
    from . import server

    try:
        c = server.client()
        me = await c.get("/users/self", cache=False)
        print(f"OK: authenticated to {c.settings.base_url} as {me.get('name')} (timezone {c.settings.timezone})")
        print(f"Token read from: {c.settings.token_source}")
        print(f"Settings file: {config_file()}" + ("" if config_file().exists() else " (not created; using environment variables)") + "\n")
        print(await server.canvas_list_courses())
        print("\n" + await server.canvas_get_daily_todo(days_ahead=7, max_items=5))
    except (ConfigError, CanvasError) as e:
        print(f"FAILED: {e}")
        return 1
    finally:
        if server._client:
            await server._client.aclose()
    return 0


def run_check() -> int:
    return asyncio.run(_check())
