"""Deterministic proof-of-progress circuit breaker (no Hermes imports)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .fingerprint import WorkspaceFingerprinter, evidence_fingerprint

MUTATING_TOOLS = frozenset({
    "write_file", "patch", "file_edit", "edit_file", "str_replace_editor",
    "create_file", "delete_file", "move_file", "apply_patch",
})
READ_TOOLS = frozenset({"read_file", "search_files", "grep", "glob", "list_files"})
TERMINAL_TOOLS = frozenset({"terminal", "execute_code", "run_command", "shell"})
RECOVERY_TOOLS = frozenset({"delegate_task", "ask_user", "web_search", "search_web"})
TRACKED_TOOLS = MUTATING_TOOLS | READ_TOOLS | TERMINAL_TOOLS
REQUIRE = [
    "materially different hypothesis",
    "verification step",
    "delegate_task",
    "ask user",
]


@dataclass(frozen=True)
class Settings:
    warn_after: int = 4
    intervene_after: int = 6
    mode: str = "advisory"
    warmup_calls: int = 2
    evidence_window: int = 40
    tried_limit: int = 8
    max_file_bytes: int = 1_000_000
    max_total_bytes: int = 20_000_000
    max_files: int = 10_000
    goal_limit: int = 240

    def __post_init__(self):
        if self.warn_after < 1 or self.intervene_after < self.warn_after:
            raise ValueError("require 1 <= warn_after <= intervene_after")
        if self.mode not in {"advisory", "enforce"}:
            raise ValueError("mode must be advisory or enforce")


@dataclass
class RecoveryPacket:
    goal_hint: str
    unchanged: str
    tried: list[str]
    reason: str
    streak: int
    intervention: str
    require: list[str] = field(default_factory=lambda: list(REQUIRE))
    plugin: str = "headway"
    schema_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "schema_version": self.schema_version,
            "intervention": self.intervention,
            "goal_hint": self.goal_hint,
            "unchanged": self.unchanged,
            "tried": self.tried,
            "require": self.require,
            "reason": self.reason,
            "streak": self.streak,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))

    def render(self) -> str:
        label = "Headway circuit breaker" if self.intervention == "block" else "Headway proof-of-progress notice"
        return (f"{label}: {self.reason}\n"
                f"Next: {' | '.join(self.require)}. Use /headway reset after a new hypothesis.\n"
                + self.to_json())


@dataclass
class Outcome:
    result: Any
    decision: str
    streak: int
    notice: str | None = None
    blocked: bool = False
    packet: RecoveryPacket | None = None


@dataclass
class _Session:
    streak: int = 0
    class_streaks: dict[str, int] = field(default_factory=dict)
    observed_calls: int = 0
    goal_hint: str = ""
    unchanged: str = ""
    last_reason: str = "No tool calls observed yet."
    evidence: deque[str] = field(default_factory=deque)
    coverage: set[str] = field(default_factory=set)
    tried: deque[str] = field(default_factory=deque)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "streak": self.streak,
            "class_streaks": self.class_streaks,
            "observed_calls": self.observed_calls,
            "goal_hint": self.goal_hint,
            "unchanged": self.unchanged,
            "last_reason": self.last_reason,
            "evidence": list(self.evidence),
            "coverage": sorted(self.coverage),
            "tried": list(self.tried),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], evidence_window: int, tried_limit: int) -> "_Session":
        if raw.get("schema_version") != 1:
            return cls()
        return cls(
            streak=max(0, int(raw.get("streak", 0))),
            class_streaks={str(k): max(0, int(v)) for k, v in raw.get("class_streaks", {}).items()},
            observed_calls=max(0, int(raw.get("observed_calls", 0))),
            goal_hint=str(raw.get("goal_hint", ""))[:500],
            unchanged=str(raw.get("unchanged", ""))[:64],
            last_reason=str(raw.get("last_reason", ""))[:1000],
            evidence=deque((str(x) for x in raw.get("evidence", []) if isinstance(x, str)), maxlen=evidence_window),
            coverage={str(x) for x in raw.get("coverage", []) if isinstance(x, str)},
            tried=deque((str(x) for x in raw.get("tried", []) if isinstance(x, str)), maxlen=tried_limit),
        )


def tool_signature(tool_name: str, args: dict[str, Any]) -> str:
    """Compact stable signature for the packet, not a similarity classifier."""
    parts = []
    for key in sorted(args):
        if key in {"content", "new_string", "replacement", "text"}:
            value = f"<{len(str(args[key]))} content chars>"
        else:
            value = str(args[key])
        parts.append(f"{key}={re.sub(r'\s+', ' ', value)[:120]}")
    return f"{tool_name}(" + ", ".join(parts) + ")"[:400]


def read_identity(tool_name: str, args: dict[str, Any]) -> tuple[str, int | None, int | None] | None:
    if tool_name not in READ_TOOLS:
        return None
    path = str(args.get("path") or args.get("file_path") or args.get("pattern") or args.get("query") or "")
    offset = args.get("offset", args.get("start_line"))
    limit = args.get("limit", args.get("max_lines"))
    if offset is None and "line_range" in args:
        match = re.match(r"\s*(\d+)\s*[-:]\s*(\d+)", str(args["line_range"]))
        if match:
            offset, limit = int(match.group(1)), int(match.group(2)) - int(match.group(1)) + 1
    return path, _as_int(offset), _as_int(limit)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _range_covered(start, end, covered: list[tuple[int | None, int | None]]) -> bool:
    # A missing bound is an opaque runtime-default window: only an identical
    # identity is considered covered. Numeric windows are integer ranges.
    if start is None or end is None:
        return (None, None) in covered
    points = sorted((a, b) for a, b in covered if a is not None and b is not None)
    for a, b in points:
        if a > start:
            break
        if a <= start and end <= b:
            return True
        if a <= start <= b + 1:
            start = a
    return False


def _result_error(value) -> bool:
    if isinstance(value, dict) and value.get("error"):
        return True
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
            return isinstance(parsed, dict) and bool(parsed.get("error"))
        except ValueError:
            return False
    return False


def _tool_class(tool_name: str) -> str:
    if tool_name in TERMINAL_TOOLS:
        return "terminal"
    if tool_name in MUTATING_TOOLS:
        return "mutation"
    if tool_name in READ_TOOLS:
        return "read"
    return "recovery"


class Headway:
    """Session-isolated engine; middleware delegates to :meth:`tool_execution`.

    Fingerprint failures fail open (the tool always runs). A full before/after
    comparison is used only for local workspaces. Remote terminals are skipped
    because local hashes cannot establish their workspace state.
    """

    def __init__(self, workspace: str | Path, data_dir: str | Path, *,
                 settings: Settings | None = None, session_id: str = "default"):
        self.workspace = Path(workspace).expanduser().resolve()
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.settings = settings or Settings()
        self.session_id = session_id or "default"
        self._session = self._load()
        self._session.evidence = deque(self._session.evidence, maxlen=self.settings.evidence_window)
        self._session.tried = deque(self._session.tried, maxlen=self.settings.tried_limit)

    @property
    def _state_path(self) -> Path:
        scope = self.session_id + "\0" + str(self.workspace)
        sid = hashlib.sha256(scope.encode("utf-8", "replace")).hexdigest()[:24]
        return self.data_dir / f"{sid}.json"

    def _load(self) -> _Session:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return _Session()
            return _Session.from_dict(raw, self.settings.evidence_window, self.settings.tried_limit)
        except (OSError, ValueError, TypeError, AttributeError):
            return _Session()

    def _save(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".headway-", suffix=".tmp", dir=self.data_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(self._session.as_dict(), stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_name, self._state_path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        except OSError:
            # In-memory progress still works if plugin storage is read-only.
            pass

    def set_goal(self, text: str) -> None:
        if text and not self._session.goal_hint:
            self._session.goal_hint = text.strip()[:self.settings.goal_limit]
            self._save()

    def user_message(self, text: str) -> None:
        """Explicit new user input is a hypothesis/goal boundary."""
        self.set_goal(text)
        self.reset()

    def reset(self) -> None:
        self._session.streak = 0
        self._session.class_streaks.clear()
        self._session.last_reason = "Reset by user or new hypothesis."
        self._save()

    def set_mode(self, mode: str) -> None:
        if mode not in {"advisory", "enforce"}:
            raise ValueError("mode must be advisory or enforce")
        self.settings = Settings(**{**self.settings.__dict__, "mode": mode})

    def status(self) -> dict[str, Any]:
        return {
            "plugin": "headway",
            "session_id": self.session_id,
            "mode": self.settings.mode,
            "streak": self._session.streak,
            "class_streaks": dict(self._session.class_streaks),
            "warn_after": self.settings.warn_after,
            "intervene_after": self.settings.intervene_after,
            "observed_calls": self._session.observed_calls,
            "unchanged": self._session.unchanged,
            "last_reason": self._session.last_reason,
            "tried": list(self._session.tried),
            "enforcing": self.settings.mode == "enforce" and self._session.streak >= self.settings.intervene_after,
        }

    def _packet(self, intervention: str, reason: str | None = None) -> RecoveryPacket:
        return RecoveryPacket(
            goal_hint=self._session.goal_hint or "No goal captured; state the intended observable outcome.",
            unchanged=self._session.unchanged or "not-yet-captured",
            tried=list(self._session.tried),
            reason=reason or self._session.last_reason,
            streak=self._session.streak,
            intervention=intervention,
        )

    def _fingerprint(self):
        return WorkspaceFingerprinter(
            self.workspace,
            max_file_bytes=self.settings.max_file_bytes,
            max_total_bytes=self.settings.max_total_bytes,
            max_files=self.settings.max_files,
        ).capture()

    def tool_execution(self, tool_name=None, args=None, next_call=None, **kwargs):
        sid = kwargs.get("session_id") or kwargs.get("task_id") or self.session_id
        if sid != self.session_id:
            # The adapter maintains one engine per session. The core can still
            # be used directly; switching intentionally reloads isolated state.
            self.session_id = sid
            self._session = self._load()
        goal = kwargs.get("first_user_message") or kwargs.get("goal_hint")
        if goal:
            self.set_goal(goal)
        return self.execute(tool_name or "unknown", args or {}, next_call or (lambda: ""))

    def execute(self, tool_name: str, args: dict[str, Any], next_call: Callable[[], Any]) -> Outcome:
        tool_name = str(tool_name or "unknown")
        args = args if isinstance(args, dict) else {}
        signature = tool_signature(tool_name, args)
        identity = read_identity(tool_name, args)
        if identity and self._is_new_coverage(identity):
            # Reading an unseen path/range is a legitimate recovery action.
            # It is evaluated normally and can release the breaker on success.
            pass
        elif (self.settings.mode == "enforce"
              and self._session.class_streaks.get(_tool_class(tool_name), 0) >= self.settings.intervene_after
              and tool_name in TRACKED_TOOLS and tool_name not in RECOVERY_TOOLS):
            packet = self._packet("block", "No new observable evidence after the intervention threshold; same-class call blocked before execution.")
            self._session.last_reason = packet.reason
            self._save()
            return Outcome(packet.render(), "no_progress", self._session.streak,
                           notice=packet.render(), blocked=True, packet=packet)

        # Exempt recovery calls are executed and their real output evaluated;
        # they are not an unconditional reset button.
        backend = args.get("terminal_env") or os.environ.get("TERMINAL_ENV") or "local"
        remote = tool_name in TERMINAL_TOOLS and str(backend).lower() not in {"", "local", "host", "none"}
        if remote:
            result = next_call()
            self._session.tried.append(signature)
            self._save()
            return Outcome(result, "skipped", self._session.streak)

        try:
            before = self._fingerprint() if tool_name in TRACKED_TOOLS or tool_name in RECOVERY_TOOLS else None
        except (OSError, ValueError):
            before = None
        result = next_call()
        try:
            after = self._fingerprint() if before is not None else None
        except (OSError, ValueError):
            after = None
        evidence = evidence_fingerprint(result, self.workspace)

        state_changed = before is not None and after is not None and before.digest != after.digest
        novel = bool(evidence.normalized) and not evidence.timestamp_only and evidence.digest not in self._session.evidence
        read_ok = not _result_error(result)
        coverage_expand = identity is not None and read_ok and self._is_new_coverage(identity)
        tracked = tool_name in TRACKED_TOOLS or tool_name in RECOVERY_TOOLS

        self._session.observed_calls += 1
        self._session.tried.append(signature)
        if evidence.normalized and not evidence.timestamp_only:
            self._session.evidence.append(evidence.digest)
        if identity and read_ok:
            self._record_coverage(identity)
        if after is not None:
            self._session.unchanged = after.short_id
        elif before is not None:
            self._session.unchanged = before.short_id

        if not tracked:
            decision = "skipped"
            reason = f"{tool_name} is outside Headway's observed tool classes."
        elif state_changed:
            decision = "state_changed"
            reason = "Workspace content fingerprint changed after the call."
            self._reset_progress(reason)
        elif coverage_expand:
            decision = "coverage_expand"
            reason = "Read/search covers a new path or line range."
            self._reset_progress(reason)
        elif novel:
            decision = "new_evidence"
            reason = "Tool result contains materially new normalized evidence."
            self._reset_progress(reason)
        else:
            decision = "no_progress"
            if evidence.timestamp_only:
                reason = "Only polling timestamps/PIDs/durations changed; workspace and substantive evidence did not."
            else:
                reason = "No workspace state change, new evidence, or expanded read coverage."
            if self._session.observed_calls > self.settings.warmup_calls:
                self._session.streak += 1
                tool_class = _tool_class(tool_name)
                self._session.class_streaks[tool_class] = self._session.class_streaks.get(tool_class, 0) + 1
            self._session.last_reason = reason

        notice = None
        packet = None
        if decision == "no_progress" and self._session.streak >= self.settings.intervene_after:
            packet = self._packet("warn", f"No observable progress for {self._session.streak} consecutive calls (intervene_after={self.settings.intervene_after}).")
            notice = packet.render()
        elif decision == "no_progress" and self._session.streak >= self.settings.warn_after:
            packet = self._packet("notice", f"No observable progress for {self._session.streak} consecutive calls (warn_after={self.settings.warn_after}).")
            notice = packet.render()
        self._save()
        return Outcome(result, decision, self._session.streak, notice=notice, packet=packet)

    def _reset_progress(self, reason: str) -> None:
        self._session.streak = 0
        self._session.class_streaks.clear()
        self._session.last_reason = reason

    @staticmethod
    def _coverage_key(identity) -> str:
        path, start, end = identity
        return f"{path}:{start}:{end}"

    def _is_new_coverage(self, identity) -> bool:
        path, start, length = identity
        end = start + length - 1 if start is not None and length is not None else None
        covered = []
        for raw in self._session.coverage:
            try:
                old_path, old_start, old_end = raw.rsplit(":", 2)
                if old_path != path:
                    continue
                old_start = None if old_start == "None" else int(old_start)
                old_end = None if old_end == "None" else int(old_end)
                covered.append((old_start, old_end))
            except (ValueError, AttributeError):
                continue
        return not _range_covered(start, end, covered)

    def _record_coverage(self, identity) -> None:
        path, start, length = identity
        end = start + length - 1 if start is not None and length is not None else None
        self._session.coverage.add(self._coverage_key((path, start, end)))
        if len(self._session.coverage) > 5000:
            self._session.coverage = set(sorted(self._session.coverage)[-5000:])
