import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeCtx:
    def __init__(self, workspace, data_dir):
        self.workspace = str(workspace)
        self.data_dir = str(data_dir)
        self.middleware = {}
        self.hooks = {}
        self.commands = {}
        self.cli = {}
        self.config = {"warn_after": 2, "intervene_after": 3, "warmup_calls": 0}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_middleware(self, kind, callback):
        self.middleware[kind] = callback

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, callback, **kwargs):
        self.commands[name] = callback

    def register_cli_command(self, name, **kwargs):
        self.cli[name] = kwargs

    def session_cwd(self):
        return self.workspace


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADWAY_DATA_DIR", str(tmp_path / "plugin-data"))
    spec = importlib.util.spec_from_file_location(
        "headway_plugin_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = FakeCtx(workspace, tmp_path / "plugin-data")
    mod.register(ctx)
    return mod, ctx, workspace


def seed_repeated_result(ctx):
    call = ctx.middleware["tool_execution"]
    for command in ["seed", "one", "two", "three"]:
        result = call(tool_name="terminal", args={"command": command},
                      next_call=lambda _args: "same", session_id="s1")
    return result


def test_register_uses_public_surfaces(plugin):
    mod, ctx, _ = plugin
    assert set(ctx.middleware) == {"tool_execution"}
    assert set(ctx.hooks) == {"pre_llm_call"}
    assert set(ctx.commands) == {"headway"}
    assert "headway" in ctx.cli


def test_middleware_passes_args_and_appends_notice(plugin):
    _, ctx, _ = plugin
    result = seed_repeated_result(ctx)
    assert result.startswith("same\n\n")
    assert '"intervention":"warn"' in result
    assert '"plugin":"headway"' in result


def test_enforce_returns_packet_without_calling_next(plugin):
    mod, ctx, _ = plugin
    ctx.commands["headway"]("mode enforce", session_id="s1")
    seed_repeated_result(ctx)
    called = False
    def next_call(_args):
        nonlocal called
        called = True
        return "bad"
    result = ctx.middleware["tool_execution"](
        tool_name="terminal", args={"command": "again"}, next_call=next_call, session_id="s1")
    assert "Headway circuit breaker" in result
    assert not called


def test_non_string_tool_result_is_not_corrupted(plugin):
    _, ctx, _ = plugin
    original = {"content": "structured"}
    result = ctx.middleware["tool_execution"](
        tool_name="terminal", args={"command": "json"}, next_call=lambda _args: original)
    assert result is original


def test_slash_commands_status_reset_mode_and_validation(plugin):
    _, ctx, _ = plugin
    seed_repeated_result(ctx)
    status = json.loads(ctx.commands["headway"]("", session_id="s1"))
    assert status["streak"] == 3
    assert status["mode"] == "advisory"
    assert "streak=0" in ctx.commands["headway"]("reset", session_id="s1")
    assert "mode=enforce" in ctx.commands["headway"]("mode enforce", session_id="s1")
    assert "Usage" in ctx.commands["headway"]("mode invalid", session_id="s1")


def test_pre_llm_captures_first_goal_and_resets_on_later_user_message(plugin):
    _, ctx, _ = plugin
    assert ctx.hooks["pre_llm_call"](session_id="s1", user_message="Fix the parser") is None
    seed_repeated_result(ctx)
    ctx.hooks["pre_llm_call"](session_id="s1", user_message="Try the lexer instead")
    assert json.loads(ctx.commands["headway"]("status", session_id="s1"))["streak"] == 0


def test_per_session_workspace_and_state_are_isolated(plugin, tmp_path):
    mod, ctx, workspace = plugin
    seed_repeated_result(ctx)
    other = tmp_path / "other"
    other.mkdir()
    ctx.workspace = str(other)
    result = ctx.middleware["tool_execution"](
        tool_name="terminal", args={"command": "seed"}, next_call=lambda _args: "same", session_id="s2")
    assert "Headway" not in result
    assert json.loads(ctx.commands["headway"]("status", session_id="s2"))["streak"] == 0
    assert json.loads(ctx.commands["headway"]("status", session_id="s1"))["streak"] == 3


def test_cli_registration_and_status_handler(plugin, capsys):
    _, ctx, _ = plugin
    seed_repeated_result(ctx)
    ctx.cli["headway"]["handler_fn"](type("Args", (), {"headway_cmd": "status", "session": "s1"})())
    assert json.loads(capsys.readouterr().out)["streak"] == 3
    parser = __import__("argparse").ArgumentParser()
    ctx.cli["headway"]["setup_fn"](parser)
    args = parser.parse_args(["status", "s1"])
    assert args.headway_cmd == "status"
    assert args.session == "s1"


def test_dashboard_health_and_status(plugin):
    mod, ctx, _ = plugin
    dashboard = mod.dashboard_api
    assert dashboard.health()["plugin"] == "headway"
    seed_repeated_result(ctx)
    assert dashboard.status("s1")["streak"] == 3


def test_absolute_write_fingerprints_entire_session_workspace(plugin):
    _, ctx, workspace = plugin
    nested = workspace / "nested"
    nested.mkdir()
    (workspace / "existing.txt").write_text("before")
    def write(_args):
        (workspace / "existing.txt").write_text("after")
        return "changed existing file"
    result = ctx.middleware["tool_execution"](
        tool_name="write_file", args={"path": str(nested / "other.txt")},
        next_call=write, session_id="s1")
    status = json.loads(ctx.commands["headway"]("status", session_id="s1"))
    assert status["last_reason"] == "Workspace content fingerprint changed after the call."


def test_cli_status_after_middleware_does_not_reset_workspace(plugin):
    _, ctx, _ = plugin
    seed_repeated_result(ctx)
    ctx.cli["headway"]["handler_fn"](type("Args", (), {"headway_cmd": "status", "session": "s1"})())
    assert json.loads(ctx.commands["headway"]("status", session_id="s1"))["streak"] == 3
