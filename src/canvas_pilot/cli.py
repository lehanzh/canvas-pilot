"""Command line: `canvas-pilot [serve|setup|check|connect|hide|unhide|hidden|set-token|delete-token|client-config]`."""

from __future__ import annotations

import argparse
import getpass
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import write_config_values  # noqa: F401  (re-exported for callers)

LEGACY_FLAGS = {"--check": "check", "--set-token": "set-token", "--delete-token": "delete-token"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = [LEGACY_FLAGS.get(a, a) for a in argv]
    parser = argparse.ArgumentParser(prog="canvas-pilot", description="Canvas LMS academic-assistant MCP server.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Run the MCP server over stdio (default; this is what AI clients launch).")
    sub.add_parser("setup", help="Interactive first-time setup: Canvas URL, token (to keychain), timezone.")
    sub.add_parser("check", help="Verify the configuration and print a sample of your data.")
    sub.add_parser("set-token", help="Save or replace the Canvas token in the system keychain.")
    sub.add_parser("delete-token", help="Remove the Canvas token from the system keychain.")
    sub.add_parser("client-config", help="Print config snippets for Claude, Cursor, Codex, VS Code, Gemini.")
    hide = sub.add_parser("hide", help="Leave a course out of to-do lists, deadlines and other summaries.")
    hide.add_argument("course", nargs="+", help="Course name, code or ID, e.g. 'Husky 101'.")
    unhide = sub.add_parser("unhide", help="Bring a hidden course back into summaries ('all' for every one).")
    unhide.add_argument("course", nargs="+", help="Hidden course's name, code or ID, or 'all'.")
    sub.add_parser("hidden", help="List hidden courses.")
    connect = sub.add_parser("connect", help="Add canvas-pilot to an AI app's settings for you.")
    connect.add_argument("app", choices=CONNECTABLE_APPS, help="Which app to connect.")
    args = parser.parse_args(argv)

    match args.command:
        case None | "serve":
            from .server import mcp

            mcp.run()  # stdio transport
            return 0
        case "setup":
            return setup()
        case "check":
            from .check import run_check

            return run_check()
        case "set-token":
            return set_token()
        case "delete-token":
            return delete_token()
        case "client-config":
            print_client_config()
            return 0
        case "connect":
            return connect_app(args.app)
        case "hide" | "unhide" | "hidden":
            return hide_command(args.command, " ".join(getattr(args, "course", []) or []))
    return 2


# -- token ----------------------------------------------------------------------


def set_token(base_url: str | None = None) -> int:
    from .config import KEYCHAIN_SERVICE, KeychainError, keychain_account, store_token

    if base_url:
        print(f"Create a token at {base_url}/profile/settings  ->  '+ New Access Token' (set an expiry date).")
    # getpass doesn't echo, so the token never appears on screen or in shell history.
    token = getpass.getpass("Paste your Canvas access token (input hidden): ").strip()
    if not token:
        print("No token entered; nothing changed.")
        return 1
    try:
        store_token(token)
    except KeychainError as e:
        print(f"Could not save to the system keychain: {e}")
        print("Fallback: set CANVAS_API_TOKEN in your MCP client's \"env\" block instead.")
        return 1
    print(f"Saved to the system keychain (service {KEYCHAIN_SERVICE!r}, account {keychain_account()!r}).")
    return 0


def delete_token() -> int:
    from .config import delete_token as remove

    if not remove():
        print("No Canvas token was stored in the keychain.")
        return 1
    print("Removed the Canvas token from the system keychain.")
    return 0


# -- setup ------------------------------------------------------------------------


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def setup() -> int:
    from dotenv import dotenv_values

    from .config import config_file, keychain_token, local_timezone, normalize_base_url

    cfg = config_file()
    current = dotenv_values(cfg) if cfg.exists() else {}
    print(f"canvas-pilot setup  (settings file: {cfg})\n")

    url = normalize_base_url(
        _ask("Canvas address, as in your browser (e.g. https://myschool.instructure.com)", current.get("CANVAS_BASE_URL"))
    )
    if not url:
        print("A Canvas address is required.")
        return 1

    if keychain_token():
        replace = _ask("A token is already in your keychain. Replace it? (y/N)", "N").lower().startswith("y")
        if replace and set_token(url):
            return 1
    elif set_token(url):
        return 1

    tz_default = current.get("CANVAS_TIMEZONE") or f"auto: {local_timezone()}"
    tz = _ask("Timezone for displayed times (IANA name, or Enter for automatic)", tz_default)
    tz_value = None if tz.startswith("auto") else tz

    write_config_values(cfg, {"CANVAS_BASE_URL": url, "CANVAS_TIMEZONE": tz_value})
    print(f"\nSaved settings to {cfg}\n\nChecking the connection...\n")
    from .check import run_check

    if run_check():
        return 1
    print("\nNext: connect an AI app, e.g. `canvas-pilot connect claude-desktop`")
    print("(or `canvas-pilot client-config` to see settings for other apps).")
    return 0


# -- client config ------------------------------------------------------------------


def launch_command() -> tuple[str, list[str]]:
    """How an MCP client should start this server on this machine."""
    root = Path(__file__).resolve().parents[2]
    in_checkout = (root / "pyproject.toml").exists() and Path(sys.prefix).resolve() == (root / ".venv").resolve()
    if in_checkout:  # running from a source checkout via `uv run`
        return shutil.which("uv") or "uv", ["--directory", str(root), "run", "canvas-pilot"]
    return shutil.which("canvas-pilot") or str(Path(sys.argv[0]).resolve()), []


def print_client_config() -> None:
    cmd, args = launch_command()
    entry = {"command": cmd, "args": args} if args else {"command": cmd}
    as_json = json.dumps({"mcpServers": {"canvas": entry}}, indent=2)
    shell = " ".join(shlex.quote(x) for x in [cmd, *args])
    toml_args = ", ".join(json.dumps(a) for a in args)
    print(f"""Launch command on this machine: {shell}

Claude Code (terminal):
  claude mcp add canvas --scope user -- {shell}

Claude Desktop  ~/Library/Application Support/Claude/claude_desktop_config.json  (Windows: %APPDATA%\\Claude\\...)
Cursor          ~/.cursor/mcp.json
Windsurf        ~/.codeium/windsurf/mcp_config.json
Gemini CLI      ~/.gemini/settings.json
  merge into the file:
{as_json}

OpenAI Codex CLI  ~/.codex/config.toml
  [mcp_servers.canvas]
  command = {json.dumps(cmd)}
  args = [{toml_args}]

VS Code (Copilot agent mode)  run "MCP: Open User Configuration" and add:
{json.dumps({"servers": {"canvas": {"type": "stdio", **entry}}}, indent=2)}

Restart the client afterwards.""")


# -- connect ----------------------------------------------------------------------

CONNECTABLE_APPS = ["claude-desktop", "claude-code", "cursor"]
APP_NAMES = {"claude-desktop": "Claude Desktop", "claude-code": "Claude Code", "cursor": "Cursor"}


def app_config_path(app: str) -> Path:
    home = Path.home()
    if app == "cursor":
        return home / ".cursor" / "mcp.json"
    # claude-desktop
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        import os

        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        standard = appdata / "Claude" / "claude_desktop_config.json"
        if not standard.parent.exists():  # Microsoft Store install keeps its files in a package folder
            local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
            packaged = sorted(local.glob("Packages/Claude_*/LocalCache/Roaming/Claude"))
            if packaged:
                return packaged[0] / "claude_desktop_config.json"
        return standard
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def claude_desktop_running() -> bool:
    """Claude Desktop keeps its settings in memory and writes them back when it quits or saves
    preferences, wiping edits made while it was open, so we only edit the file while it's closed."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True, timeout=10).stdout
            # Case-sensitive: Claude Code ships a lowercase helper at .../claude.app/Contents/MacOS/claude.
            return any(line.strip().endswith("/Claude.app/Contents/MacOS/Claude") for line in out.splitlines())
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Claude.exe", "/NH"], capture_output=True, text=True, timeout=10
            ).stdout
            return "claude.exe" in out.lower()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def connect_app(app: str) -> int:
    cmd, args = launch_command()
    name = APP_NAMES[app]
    if app == "claude-code":
        claude = shutil.which("claude")
        if not claude:
            print("Claude Code's `claude` command wasn't found. Install Claude Code first: https://claude.com/claude-code")
            return 1
        subprocess.run([claude, "mcp", "remove", "canvas", "-s", "user"], capture_output=True, text=True)
        r = subprocess.run([claude, "mcp", "add", "canvas", "--scope", "user", "--", cmd, *args], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Claude Code couldn't add the server:\n{r.stderr or r.stdout}")
            return 1
        print("Connected to Claude Code (all projects). Start a new Claude Code session to use it.")
        return 0

    if app == "claude-desktop" and claude_desktop_running():
        print("Claude Desktop is open. It would overwrite this change when it closes, so:")
        print("  1. Quit Claude Desktop completely (Mac: Command+Q; Windows: right-click the tray icon > Quit).")
        print("  2. Run this command again.")
        print("  3. Open Claude Desktop.")
        return 1
    path = app_config_path(app)
    data: dict = {}
    if path.exists() and path.read_text().strip():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"{path} isn't valid JSON (line {e.lineno}), so it was left untouched.")
            print("Fix or empty that file, then run this command again.")
            return 1
        backup = path.with_name(f"{path.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"Backed up your current settings to {backup}")
    elif app == "claude-desktop" and not path.parent.exists():
        print(f"Claude Desktop doesn't seem to be installed (no {path.parent}).")
        print("Install it from https://claude.ai/download, open it once, then run this command again.")
        return 1
    servers = data.setdefault("mcpServers", {})
    servers["canvas"] = {"command": cmd, "args": args} if args else {"command": cmd}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    if app == "claude-desktop":
        print("Connected to Claude Desktop. Open it now; the Canvas tools load when it starts.")
    else:
        print(f"Connected to {name}. Fully quit {name} and open it again to load the Canvas tools.")
    return 0


# -- hide / unhide ----------------------------------------------------------------


def hide_command(command: str, course: str) -> int:
    import asyncio

    from mcp.server.mcpserver.exceptions import ToolError

    from . import server
    from .tools import courses
    from .canvas import CanvasError
    from .config import ConfigError

    async def run() -> str:
        try:
            if command == "hide":
                return await courses.canvas_hide_course(course)
            if command == "unhide":
                return await courses.canvas_unhide_course(course)
            hidden = server.client().hidden.items()
            if not hidden:
                return "No hidden courses."
            rows = [f"  {name or 'Unknown course'}  (course_id={cid})" for cid, name in sorted(hidden.items(), key=lambda kv: kv[1])]
            return "Hidden courses (left out of summaries; still available by name):\n" + "\n".join(rows)
        finally:
            if server._client:
                await server._client.aclose()

    try:
        print(asyncio.run(run()).replace("**", ""))
    except (CanvasError, ConfigError, ToolError) as e:
        print(f"Couldn't {command}: {e}")
        return 1
    return 0
