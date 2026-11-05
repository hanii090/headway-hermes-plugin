#!/usr/bin/env python3
"""Proof, not a simulation of Headway decisions: run real tools via middleware.

Usage: python3 scripts/demo.py [--keep /tmp/headway-demo]
No Hermes installation, LLM, network, credentials, or git is needed. Terminal
commands are executed with subprocess only inside an isolated temp workspace.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


class DemoContext:
    def __init__(self, workspace):
        self.workspace = workspace
        self.config = {"mode": "advisory", "warn_after": 2, "intervene_after": 3,
                       "warmup_calls": 0}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_middleware(self, kind, callback):
        self.middleware = callback

    def register_hook(self, name, callback):
        self.user_hook = callback

    def register_command(self, name, callback, **kwargs):
        self.command = callback

    def session_cwd(self):
        return str(self.workspace)


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "headway_demo", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=Path, help="Keep the demo workspace here")
    args = parser.parse_args()
    if args.keep:
        args.keep.mkdir(parents=True, exist_ok=True)
        workspace = args.keep / "workspace"
        data = args.keep / "headway-data"
        workspace.mkdir(exist_ok=True)
    else:
        temp = tempfile.TemporaryDirectory(prefix="headway-demo-")
        workspace = Path(temp.name) / "workspace"
        data = Path(temp.name) / "headway-data"
        workspace.mkdir()
    os.environ["HEADWAY_DATA_DIR"] = str(data)
    (workspace / "app.py").write_text("def parse(value):\n    return value\n")

    plugin = load_plugin()
    ctx = DemoContext(workspace)
    plugin.register(ctx)
    ctx.user_hook(session_id="demo", user_message="Fix parser handling without changing its API")

    print("HEADWAY 0.1.0 — Different commands are not necessarily progress.")
    print(f"Workspace: {workspace}")
    print("Running varied terminal commands through tool_execution middleware…")
    commands = [
        ["pwd"],
        ["ls", "-la"],
        ["git", "status", "--short"],
        [sys.executable, "-c", "print('parser unchanged')"],
    ]
    outcomes = []
    for argv in commands:
        def run_terminal(call_args, argv=argv):
            completed = subprocess.run(argv, cwd=workspace, text=True,
                                       capture_output=True, timeout=10, check=False)
            # The real exit code is deliberately ignored: exit 0 is not progress.
            output = completed.stdout + completed.stderr
            # Same repeated evidence even though the commands are different.
            # This isolates observable progress from command similarity.
            return "parser unchanged\n"
        result = ctx.middleware(
            tool_name="terminal", args={"command": " ".join(argv), "workdir": str(workspace)},
            next_call=run_terminal, session_id="demo")
        has_packet = '"plugin":"headway"' in result
        outcomes.append(has_packet)
        label = "INTERVENE" if has_packet else "observe"
        print(f"  {label:9} $ {' '.join(argv)}")
        if has_packet:
            packet = json.loads(result.splitlines()[-1])
            print(f"           streak={packet['streak']} unchanged={packet['unchanged']} reason={packet['reason']}")

    proof1 = outcomes[-1] and (workspace / "app.py").read_text() == "def parse(value):\n    return value\n"
    print(f"PROOF 1 {'PASS' if proof1 else 'FAIL'}: four different commands with repeated output and unchanged workspace -> intervention packet.")

    def real_write(call_args):
        (workspace / "app.py").write_text("def parse(value):\n    return str(value).strip()\n")
        return "updated app.py"

    result = ctx.middleware(
        tool_name="write_file", args={"path": str(workspace / "app.py"), "content": "updated"},
        next_call=real_write, session_id="demo")
    status = json.loads(ctx.command("status", session_id="demo"))
    proof2 = status["streak"] == 0 and status["last_reason"] == "Workspace content fingerprint changed after the call."
    print(f"PROOF 2 {'PASS' if proof2 else 'FAIL'}: real write changed the workspace -> streak reset to {status['streak']}.")
    print("RESULT:", status["last_reason"])
    if not (proof1 and proof2):
        return 1
    print("PASS: observable progress, not command novelty or exit_code 0, controls the breaker.")
    if not args.keep:
        temp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
