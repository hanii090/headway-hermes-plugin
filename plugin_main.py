"""Hermes registration adapter; the progress engine has no Hermes dependency."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .headway.progress import Headway, Settings

log = logging.getLogger(__name__)
_DEFAULTS = Settings()
_ENGINES: dict[tuple[str, str], Headway] = {}
_SESSION_WORKSPACES: dict[str, Path] = {}
_BASE_SETTINGS = _DEFAULTS
_BASE_DATA_DIR: Path | None = None


def _settings(ctx) -> Settings:
    values = {}
    for key in Settings.__dataclass_fields__:
        default = getattr(_DEFAULTS, key)
        try:
            value = ctx.get_config(key, default) if getattr(ctx, "get_config", None) else default
            values[key] = default if value is None else value
        except Exception:
            values[key] = default
    return Settings(**values)


def _data_dir() -> Path:
    override = os.environ.get("HEADWAY_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir("headway"))
    except ImportError:
        # Standalone unit/demo fallback only; Hermes uses plugin_data_dir above.
        return Path.home() / ".hermes" / "plugins" / "data" / "headway"


def _cwd(ctx, session_id: str = "") -> Path:
    try:
        value = ctx.session_cwd() if hasattr(ctx, "session_cwd") else ""
        if value:
            return Path(value).expanduser().resolve()
    except Exception:
        pass
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return Path(resolve_agent_cwd()).resolve()
    except ImportError:
        return Path(os.getcwd()).resolve()


def _engine(ctx, session_id: str = "", workspace: str | Path | None = None) -> Headway:
    sid = session_id or "default"
    root = Path(workspace).expanduser().resolve() if workspace else _cwd(ctx, sid)
    key = (sid, str(root))
    if key not in _ENGINES:
        _ENGINES[key] = Headway(root, _BASE_DATA_DIR or _data_dir(),
                                settings=_BASE_SETTINGS, session_id=sid)
    return _ENGINES[key]


def _workspace_for_call(ctx, sid: str, tool_name: str, args: dict) -> Path:
    """Use an explicit terminal workdir, otherwise the session workspace.

    File mutations elsewhere in this workspace still affect the before/after
    fingerprint even when their own path is nested. Explicit absolute paths
    outside the workspace are outside this version's observation scope.
    """
    default = _cwd(ctx, sid)
    raw = args.get("workdir") or args.get("cwd")
    if raw and tool_name in {"terminal", "execute_code", "shell", "run_command"}:
        candidate = Path(os.path.expanduser(str(raw)))
        if not candidate.is_absolute():
            candidate = default / candidate
        return candidate.resolve()
    return default


def _append_notice(result, notice: str | None):
    if not notice:
        return result
    if isinstance(result, str):
        return result + "\n\n" + notice
    # Public middleware permits any result. Preserve structured results rather
    # than silently stringifying them; string Hermes tool output gets the packet.
    log.warning("Headway notice for non-string result: %s", notice.splitlines()[0])
    return result


def register(ctx):
    global _BASE_SETTINGS, _BASE_DATA_DIR
    _BASE_SETTINGS = _settings(ctx)
    _BASE_DATA_DIR = _data_dir()
    _ENGINES.clear()
    _SESSION_WORKSPACES.clear()

    def tool_execution(tool_name=None, args=None, next_call=None, **kwargs):
        args = args or {}
        sid = kwargs.get("session_id") or kwargs.get("task_id") or "default"
        root = _workspace_for_call(ctx, sid, str(tool_name), args)
        _SESSION_WORKSPACES[sid] = root
        engine = _engine(ctx, sid, root)
        outcome = engine.execute(str(tool_name), args, lambda: next_call(args))
        if outcome.blocked:
            return outcome.result
        return _append_notice(outcome.result, outcome.notice)

    def pre_llm_call(session_id: str = "", user_message="", **kwargs):
        _SESSION_WORKSPACES[session_id or "default"] = _cwd(ctx, session_id)
        if not isinstance(user_message, str):
            return None
        engine = _engine(ctx, session_id)
        engine.user_message(user_message)
        return None

    def headway_command(raw_args: str = "", *, session_id: str = "", **kwargs):
        parts = (raw_args or "").strip().split()
        action = parts[0] if parts else "status"
        sid = session_id or "default"
        root = _SESSION_WORKSPACES.get(sid)
        engine = _engine(ctx, sid, root)
        if action == "status":
            return json.dumps(engine.status(), indent=2)
        if action == "reset":
            engine.reset()
            return "Headway reset: streak=0; enforcement released. Evidence and read coverage are retained."
        if action == "mode" and len(parts) == 2 and parts[1] in {"advisory", "enforce"}:
            engine.set_mode(parts[1])
            return f"Headway mode={parts[1]} for this session (configuration default is {_BASE_SETTINGS.mode})."
        return "Usage: /headway [status] | /headway reset | /headway mode advisory|enforce"

    def cli(args):
        session = getattr(args, "session", None) or "default"
        engine = _engine(ctx, session)
        print(json.dumps(engine.status(), indent=2))

    ctx.register_middleware("tool_execution", tool_execution)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_command("headway", headway_command,
                         description="Show proof-of-progress streak, reset it, or set advisory/enforce mode",
                         args_hint="[status] | reset | mode advisory|enforce")
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command(name="headway", help="Inspect Headway proof-of-progress state",
                                 setup_fn=_cli_setup, handler_fn=cli,
                                 description="hermes headway status [session]")


def _cli_setup(parser):
    sub = parser.add_subparsers(dest="headway_cmd")
    status = sub.add_parser("status", help="Show session state (default: default)")
    status.add_argument("session", nargs="?", default="default")


def status_for_session(session_id: str = "default") -> dict:
    """Shared read-only status lookup for CLI and dashboard."""
    sid = session_id or "default"
    root = _SESSION_WORKSPACES.get(sid)
    if root is None:
        # No observed workspace: a clean, persisted default-scope state if any.
        root = Path("/__headway_unbound__")
    return _engine(None, sid, root).status()
