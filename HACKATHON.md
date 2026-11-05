# Headway — proof of progress, not command novelty

## Problem

An agent can run `pwd`, then `ls`, then `git status`, then another check, all successfully, and still be stuck. Exact-argument loop hashes miss the varied commands. Exit code 0 says the command ran, not that the task moved. Hermes issue #67889 is the motivating terminal-thrashing case; terminal is outside `idempotent_no_progress`.

Existing catalog approaches cover argument repetition, failures/identical calls, terminal progress-bar rendering, or undo/snapshots. Near-identical-loop detection (PR #103180) covers command similarity. Headway's lane is **observable progress + evidence novelty**.

## Solution

Fingerprint the local workspace before and after terminal, mutation, and read tools. Normalize result evidence to remove timestamps, PIDs, ANSI, absolute workspace paths, and whitespace. Reset the no-progress streak only for:

1. changed workspace state;
2. new normalized evidence; or
3. newly covered read paths/ranges.

Otherwise, after configurable thresholds, inject a JSON + human recovery packet: original goal, unchanged workspace id, recent tool signatures, and required next moves—different hypothesis, verification, delegation, or ask the user. Optional enforce mode short-circuits subsequent same-class no-progress calls. A real write clears the streak.

## Proof

`python3 scripts/demo.py` executes varied real terminal commands through the public `tool_execution` adapter with repeated evidence and an unchanged workspace. It asserts intervention, then writes a file and asserts reset. `python3 -m pytest -q` tests fingerprints, evidence novelty, pollers, read coverage, same-class enforcement, persistence/isolation, commands, CLI, and the API without Hermes.

## Honest boundaries

Bounded content samples may miss changes; remote/external state is not locally provable; structured results are not rewritten; no Desktop pane; no multi-process lock. Production uses stdlib `hashlib`/`pathlib` only—no shell, git, network, credentials, core patches, or self-updater. The demo alone uses `subprocess` for real terminal commands.

**MIT · Rehan Aslam (hanii090) · v0.1.0**
