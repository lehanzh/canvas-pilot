"""Settings: environment variables, then the per-user config file; the token comes from the system keychain."""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

KEYCHAIN_SERVICE = "canvas-pilot"


def config_dir() -> Path:
    """Per-user config folder, independent of where the code is installed.

    macOS/Linux: ~/.config/canvas-pilot (or $XDG_CONFIG_HOME/canvas-pilot); Windows: %APPDATA%\\canvas-pilot.
    """
    if override := os.environ.get("CANVAS_PILOT_CONFIG_DIR"):
        return Path(override).expanduser()
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "canvas-pilot"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "canvas-pilot"


def config_file() -> Path:
    return config_dir() / "config.env"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    base_url: str
    token: str
    timezone: tzinfo
    exclude_course_ids: frozenset[int] = field(default_factory=frozenset)
    allow_writes: bool = True
    token_source: str = "unknown"
    hidden_courses_file: Path | None = None  # default: hidden_courses.json in config_dir()

    def __repr__(self) -> str:  # never print the token
        return f"Settings(base_url={self.base_url!r}, token=<hidden>, token_source={self.token_source!r})"

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/api/v1"


def local_timezone() -> tzinfo:
    # A named zone (America/New_York), not a fixed offset, so dates across a DST change stay right.
    from tzlocal import get_localzone

    return get_localzone()


# On macOS the token is saved and read through Apple's own `security` tool rather than from Python.
# The Keychain trusts an item's creating program; uv's Python builds are ad-hoc signed and change path
# whenever Python is upgraded, which made macOS ask for the login password on every launch.
# /usr/bin/security is Apple-signed and never moves, so one approval lasts.
SECURITY_TOOL = "/usr/bin/security"


class KeychainError(RuntimeError):
    pass


def _use_security_tool() -> bool:
    return sys.platform == "darwin" and Path(SECURITY_TOOL).exists()


def keychain_token() -> str | None:
    """The token from the OS credential store (macOS Keychain, Windows Credential Locker, Secret Service)."""
    if _use_security_tool():
        try:
            r = subprocess.run(
                [SECURITY_TOOL, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", keychain_account(), "-w"],
                capture_output=True,
                text=True,
                timeout=120,  # long enough for the user to answer a one-time Keychain prompt
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (r.stdout.strip() or None) if r.returncode == 0 else None

    import keyring
    from keyring.errors import KeyringError

    try:
        return keyring.get_password(KEYCHAIN_SERVICE, keychain_account())
    except KeyringError:
        return None


def store_token(token: str) -> None:
    """Save (or replace) the token in the OS credential store. Raises KeychainError."""
    if _use_security_tool():
        if not re.fullmatch(r"[!#-\[\]-~]+", token):  # printable, no spaces, quotes or backslashes
            raise KeychainError("That doesn't look like a Canvas token (unexpected spaces or quote characters).")
        delete_token()
        # Commands go to `security -i` on stdin so the token never appears in the process list.
        # -T makes /usr/bin/security the program trusted to read it back without a prompt.
        command = (
            f'add-generic-password -s {KEYCHAIN_SERVICE} -a "{keychain_account()}" '
            f'-l "Canvas API token (canvas-pilot)" -T {SECURITY_TOOL} -w "{token}"\n'
        )
        subprocess.run([SECURITY_TOOL, "-i"], input=command, capture_output=True, text=True, timeout=60)
        if keychain_token() != token:
            raise KeychainError("macOS Keychain did not accept the token.")
        return

    import keyring
    from keyring.errors import KeyringError

    try:
        keyring.set_password(KEYCHAIN_SERVICE, keychain_account(), token)
    except KeyringError as e:
        raise KeychainError(str(e)) from e


def delete_token() -> bool:
    """Remove the stored token. Returns False if there was none."""
    if _use_security_tool():
        r = subprocess.run(
            [SECURITY_TOOL, "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", keychain_account()],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode == 0

    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(KEYCHAIN_SERVICE, keychain_account())
    except PasswordDeleteError:
        return False
    return True


def keychain_account() -> str:
    return os.environ.get("CANVAS_KEYCHAIN_ACCOUNT") or getpass.getuser()


def normalize_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and not url.startswith("https://"):
        url = "https://" + url.removeprefix("http://")
    return url.removesuffix("/api/v1")


def load_settings() -> Settings:
    # Real environment variables (e.g. an MCP client's "env" block) win over the config file.
    load_dotenv(config_file(), override=False)

    token = os.environ.get("CANVAS_API_TOKEN", "").strip()
    token_source = "CANVAS_API_TOKEN environment variable"
    if not token:
        token = (keychain_token() or "").strip()
        token_source = f"system keychain (service {KEYCHAIN_SERVICE!r})"
    if not token:
        raise ConfigError("No Canvas token found. Run `canvas-pilot setup` (or `canvas-pilot set-token`) in a terminal.")
    base_url = normalize_base_url(os.environ.get("CANVAS_BASE_URL", ""))
    if not base_url:
        raise ConfigError(f"CANVAS_BASE_URL is not set. Run `canvas-pilot setup`, or add it to {config_file()}.")

    tz_name = os.environ.get("CANVAS_TIMEZONE", "").strip()
    try:
        tz = ZoneInfo(tz_name) if tz_name else local_timezone()
    except Exception as e:  # ZoneInfoNotFoundError and friends
        raise ConfigError(f"CANVAS_TIMEZONE={tz_name!r} is not a valid IANA zone (e.g. America/New_York).") from e

    excluded = frozenset(
        int(x) for x in os.environ.get("CANVAS_EXCLUDE_COURSE_IDS", "").replace(" ", "").split(",") if x.isdigit()
    )
    allow_writes = os.environ.get("CANVAS_ALLOW_PLANNER_WRITES", "true").strip().lower() not in {"0", "false", "no"}

    return Settings(
        base_url=base_url,
        token=token,
        timezone=tz,
        exclude_course_ids=excluded,
        allow_writes=allow_writes,
        token_source=token_source,
    )


def write_config_values(path: Path, values: dict[str, str | None]) -> None:
    """Set KEY=value lines in a dotenv file, keeping other lines and comments. None removes a key."""
    lines = path.read_text().splitlines() if path.exists() else ["# canvas-pilot settings (no secrets here; the token is in the keychain)"]
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip().removeprefix("#").strip() if "=" in line else None
        if key in remaining:
            value = remaining.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
            continue
        out.append(line)
    out += [f"{k}={v}" for k, v in remaining.items() if v is not None]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")
