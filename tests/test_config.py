"""Token lookup order: environment / .env first, then the system keychain."""

from __future__ import annotations

import pytest

from canvas_pilot import config


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    # Point the settings file at an empty temp dir so the real one doesn't leak in.
    monkeypatch.setenv("CANVAS_PILOT_CONFIG_DIR", str(tmp_path))
    for var in ("CANVAS_API_TOKEN", "CANVAS_BASE_URL", "CANVAS_TIMEZONE", "CANVAS_EXCLUDE_COURSE_IDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CANVAS_BASE_URL", "https://canvas.test")
    return tmp_path


def test_keychain_used_when_env_unset(isolated, monkeypatch):
    monkeypatch.setattr(config, "keychain_token", lambda: "from-keychain")
    s = config.load_settings()
    assert s.token == "from-keychain" and "keychain" in s.token_source


def test_env_var_overrides_keychain(isolated, monkeypatch):
    monkeypatch.setattr(config, "keychain_token", lambda: "from-keychain")
    monkeypatch.setenv("CANVAS_API_TOKEN", "from-env")
    assert config.load_settings().token == "from-env"


def test_missing_token_points_to_setup(isolated, monkeypatch):
    monkeypatch.setattr(config, "keychain_token", lambda: None)
    with pytest.raises(config.ConfigError, match="canvas-pilot setup"):
        config.load_settings()


def test_settings_file_is_read(isolated, monkeypatch):
    monkeypatch.delenv("CANVAS_BASE_URL")
    monkeypatch.setattr(config, "keychain_token", lambda: "t")
    (isolated / "config.env").write_text("CANVAS_BASE_URL=myschool.instructure.com/\nCANVAS_EXCLUDE_COURSE_IDS=1, 2\n")
    s = config.load_settings()
    assert s.base_url == "https://myschool.instructure.com" and s.exclude_course_ids == {1, 2}
    monkeypatch.delenv("CANVAS_BASE_URL")
    monkeypatch.delenv("CANVAS_EXCLUDE_COURSE_IDS")


def test_write_config_values_keeps_other_lines(tmp_path):
    from canvas_pilot.cli import write_config_values

    f = tmp_path / "config.env"
    f.write_text("# comment\nCANVAS_EXCLUDE_COURSE_IDS=5\n# CANVAS_TIMEZONE=UTC\n")
    write_config_values(f, {"CANVAS_BASE_URL": "https://a.test", "CANVAS_TIMEZONE": "America/Chicago"})
    text = f.read_text()
    assert "# comment" in text and "CANVAS_EXCLUDE_COURSE_IDS=5" in text
    assert "CANVAS_TIMEZONE=America/Chicago" in text and "CANVAS_BASE_URL=https://a.test" in text


def test_client_config_from_checkout(capsys):
    from canvas_pilot.cli import print_client_config

    print_client_config()
    out = capsys.readouterr().out
    assert "claude mcp add canvas" in out and "[mcp_servers.canvas]" in out and '"mcpServers"' in out


def test_repr_hides_token(isolated, monkeypatch):
    monkeypatch.setattr(config, "keychain_token", lambda: "secret-value")
    assert "secret-value" not in repr(config.load_settings())


class FakeSecurityTool:
    """Stands in for /usr/bin/security so tests never touch the real Keychain."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}
        self.calls: list[list[str]] = []

    def __call__(self, args, input=None, **kwargs):
        import re
        import subprocess

        self.calls.append(args)
        cmd = args[1]
        if cmd == "-i":
            m = re.search(r'-s (\S+) -a "([^"]+)" .*-T /usr/bin/security -w "([^"]+)"', input)
            self.store[(m[1], m[2])] = m[3]
            return subprocess.CompletedProcess(args, 0, "", "")
        key = (args[args.index("-s") + 1], args[args.index("-a") + 1])
        if cmd == "find-generic-password":
            found = key in self.store
            return subprocess.CompletedProcess(args, 0 if found else 44, self.store.get(key, "") + "\n", "")
        if cmd == "delete-generic-password":
            return subprocess.CompletedProcess(args, 0 if self.store.pop(key, None) else 44, "", "")
        raise AssertionError(args)


@pytest.fixture
def fake_security(monkeypatch):
    fake = FakeSecurityTool()
    monkeypatch.setattr(config, "_use_security_tool", lambda: True)
    monkeypatch.setattr(config.subprocess, "run", fake)
    return fake


def test_macos_token_round_trip_via_security_tool(fake_security):
    config.store_token("1234~abcDEF")
    assert config.keychain_token() == "1234~abcDEF"
    # The token goes to `security -i` on stdin, never on a command line (visible in `ps`).
    assert not any("1234~abcDEF" in " ".join(c) for c in fake_security.calls)
    assert config.delete_token() and config.keychain_token() is None and not config.delete_token()


def test_macos_rejects_tokens_with_quotes(fake_security):
    with pytest.raises(config.KeychainError):
        config.store_token('abc" -T /bin/sh')
    assert fake_security.store == {}


def test_connect_claude_desktop_merges_and_backs_up(monkeypatch, tmp_path):
    import json

    from canvas_pilot import cli

    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"preferences": {"x": 1}, "mcpServers": {"other": {"command": "foo"}}}))
    monkeypatch.setattr(cli, "app_config_path", lambda app: cfg)
    monkeypatch.setattr(cli, "launch_command", lambda: ("/bin/canvas-pilot", []))
    monkeypatch.setattr(cli, "claude_desktop_running", lambda: True)
    assert cli.connect_app("claude-desktop") == 1  # refuses while the app could overwrite the file
    monkeypatch.setattr(cli, "claude_desktop_running", lambda: False)
    assert cli.connect_app("claude-desktop") == 0
    data = json.loads(cfg.read_text())
    assert data["preferences"] == {"x": 1} and data["mcpServers"]["other"] == {"command": "foo"}
    assert data["mcpServers"]["canvas"] == {"command": "/bin/canvas-pilot"}
    assert len(list(tmp_path.glob("claude_desktop_config.json.backup-*"))) == 1


def test_connect_refuses_broken_json(monkeypatch, tmp_path):
    from canvas_pilot import cli

    cfg = tmp_path / "mcp.json"
    cfg.write_text("{ not json")
    monkeypatch.setattr(cli, "app_config_path", lambda app: cfg)
    assert cli.connect_app("cursor") == 1
    assert cfg.read_text() == "{ not json"


def test_hidden_store_live_reload_and_one_time_legacy_import(tmp_path):
    from canvas_pilot.hidden import HiddenCourses

    cfg = tmp_path / "config.env"
    cfg.write_text("CANVAS_BASE_URL=https://a.test\nCANVAS_EXCLUDE_COURSE_IDS=5,6\n")
    f = tmp_path / "hidden_courses.json"
    a = HiddenCourses(f, legacy_ids=frozenset({5, 6}), legacy_file=cfg)
    assert a.ids() == {5, 6}
    assert "CANVAS_EXCLUDE_COURSE_IDS" not in cfg.read_text() and "CANVAS_BASE_URL=https://a.test" in cfg.read_text()

    b = HiddenCourses(f)  # another running copy of the server (e.g. Claude Code next to Claude Desktop)
    a.hide(7, "Club")
    assert 7 in b  # picked up without a restart
    b.unhide(5)
    assert a.ids() == {6, 7}
    # The old setting may live on somewhere we can't edit; an unhide still sticks.
    assert 5 not in HiddenCourses(f, legacy_ids=frozenset({5, 6}))
    a.fill_names({6: "Orientation"})
    assert b.items()[6] == "Orientation"
    assert b.unhide_all() == 2 and a.ids() == set()
