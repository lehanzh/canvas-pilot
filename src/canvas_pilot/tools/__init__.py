"""MCP tools, one module per area. Importing this package registers them all on the server."""

from . import assignments, content, courses, grades, planner  # noqa: F401
