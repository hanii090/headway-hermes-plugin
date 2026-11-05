# Headway

**Different commands are not necessarily progress.**

Headway is a proof-of-progress circuit breaker for [Hermes Agent](https://github.com/NousResearch/hermes-agent). It watches local terminal, file-mutation, and read/search calls. When superficially different calls produce no new observable evidence, no workspace change, and no new read coverage, it gives the agent a structured recovery packet.

```text
pwd                  -> no state change
ls -la               -> no state change
git status --short   -> no state change
run parser check     -> same evidence
                         HEADWAY: change the hypothesis, verify differently,
                         delegate_task, or ask the user

write_file(app.py)   -> workspace hash changed -> streak reset
```

No LLM judge. No command-similarity heuristic. No network. Version **0.1.0**, MIT.

## Why not core loop guardrails?

Hermes issue **#67889** describes terminal thrashing that exact-argument hashes miss. Exit code 0 is not progress, and terminal is excluded from `idempotent_no_progress`. Headway addresses that observable gap:

| Nearby tool | Primary signal | Headway's difference |
| --- | --- | --- |
| `degeneracy-guard` | Argument repetition -> approval | Detects *different* arguments with unchanged state/evidence |
| `escalation-governor` | Consecutive failures / identical calls | Also catches successful-looking no-progress calls |
| `progress-squash` | tqdm carriage-return collapse | Unrelated terminal rendering |
| Chronos / Rewind / undo-effects / git-autosave | Undo, checkpoints, snapshots | Headway intervenes before more thrashing; it does not restore state |
| Near-identical-loop work (PR #103180) | Fuzzy/near-identical command similarity | Observable progress + evidence novelty, not command similarity alone |

These claims are the product motivation supplied with this build; verify the upstream issue/PR before quoting them in submission copy.

## Install

Local checkout:

```bash
# Hermes discovers the root as a package; do not copy only the headway/ subfolder.
cp -R /workspace/hermes-headway ~/.hermes/plugins/headway
hermes plugins enable headway
# Restart the gateway/session after installing or enabling.
```

From a published repository (after the owner publishes it):

```bash
hermes plugins install hanii090/headway-hermes-plugin --enable
```

Configuration uses `plugins.headway.*` / the plugin's ordinary `ctx.get_config` values. Defaults:

| Key | Default | Meaning |
| --- | ---: | --- |
| `warn_after` | 4 | Append a recovery notice at this no-progress streak |
| `intervene_after` | 6 | Inject intervention; in enforce mode, subsequent same-class calls are blocked |
| `mode` | `advisory` | `advisory`: append packet; `enforce`: block subsequent same-class no-progress calls |
| `warmup_calls` | 2 | Initial observed calls exempt from no-progress streak |
| `evidence_window` | 40 | Recent normalized result hashes per session/workspace |
| `tried_limit` | 8 | Recent signatures included in packet |
| `max_file_bytes` | 1,000,000 | Per-file content sample cap |
| `max_total_bytes` | 20,000,000 | Total content bytes per fingerprint |
| `max_files` | 10,000 | Maximum paths per fingerprint |
| `goal_limit` | 240 | First-user-goal characters in recovery packet |

## Commands and API

```text
/headway                    JSON status for this session
/headway status             same
/headway reset              release streak and same-class enforcement
/headway mode advisory      inject notices only (session-local)
/headway mode enforce       block subsequent same-class thrashing
hermes headway status       JSON for default session
hermes headway status SID   JSON for a named session
```

`pre_llm_call` captures the first user message as the goal hint and treats each later user turn as an explicit hypothesis/goal boundary. It does not rewrite or inject into the user's message.

There is **no Desktop pane**. A small optional FastAPI router exposes read-only `GET /health` and `GET /status?session=...` through Hermes's dashboard plugin-API discovery when available. It imports without FastAPI for standalone tests.

## Decision algorithm

For tracked tools (`terminal`/`execute_code`/`shell`/`run_command`, `write_file`/`patch`/`file_edit` and related mutations, `read_file`/`search_files`/`grep`/`glob`/`list_files`):

1. **Before**: hash sorted relative paths, file sizes, and bounded file-content samples under the session workspace (or explicit terminal workdir). Ignore `.git`, `node_modules`, `.venv`, `__pycache__`, and `.hermes`; do not follow symlinks.
2. Call `next_call(args)` exactly once, except a deliberate enforce-mode short circuit.
3. **After**: repeat the workspace hash. Normalize result text by removing ANSI escapes, timestamps, PIDs, durations, UUIDs, absolute workspace prefixes, and excess whitespace; hash it.
4. Classify, in order:
   - `state_changed`: before/after workspace digest differs;
   - `coverage_expand`: a successful read/search covers a previously uncovered path or line interval;
   - `new_evidence`: normalized result was not seen in the bounded evidence window;
   - `no_progress`: none of those. Timestamp/PID-only poller output is not new evidence.
5. Reset the streak on the first three classes, a later user message, or `/headway reset`. Otherwise increment after warmup. Keep separate terminal/mutation/read streaks for enforce-mode blocking.

The recovery packet contains `goal_hint`, the last workspace `unchanged` short id, recent `tried` tool signatures, required recovery options (`materially different hypothesis`, `verification step`, `delegate_task`, `ask user`), and the exact `reason`. Advisory mode appends human text plus one-line JSON to string results. Enforce mode returns a blocked-result packet without invoking the tool.

A real mutation can recover a terminal streak. Recovery tools such as `delegate_task` and `ask_user` are not blocked, but they must actually produce new evidence/change state to clear the streak.

## Demo

```bash
python3 scripts/demo.py
# optional: python3 scripts/demo.py --keep /tmp/headway-demo
```

The demo runs **real** `pwd`, `ls`, `git status`, and Python terminal commands inside a temporary workspace via the actual plugin middleware. Their evidence is deliberately held constant so the test isolates progress from command novelty; exit status is ignored. It asserts an intervention packet, then performs a real `write_file` mutation and asserts a zero streak. It exits 0 only when both proofs pass.

## Tests

```bash
python3 -m pytest -q
python3 scripts/demo.py
```

Unit tests require only Python 3.10+ and pytest; no Hermes installation or LLM is used. The adapter uses the documented public middleware/hook/command/CLI contracts. The demo's terminal runner uses Python `subprocess`; the plugin engine itself never invokes a shell or git.

## Disclosure and limits

- **Registers:** one `tool_execution` middleware, one `pre_llm_call` hook, `/headway`, `hermes headway status`, and optional read-only dashboard API. No model-facing tools and no core patches.
- **Reads:** workspace paths and bounded file contents for hashing; tool results for normalized evidence. Session goal (truncated) is retained in plugin state.
- **Writes:** atomic per-session/per-workspace JSON under Hermes `plugin_data_dir("headway")`; tests/demo may set `HEADWAY_DATA_DIR`. No plugin state is written into the measured workspace. No credentials, network, self-updater, or subprocess/git use in production code.
- **Sampling:** large files use bounded head/tail samples, so a mid-file change outside those samples can be missed. Total/file-count caps also make fingerprints partial. This is deliberate bounded work, not a cryptographic whole-workspace guarantee.
- **Scope:** local filesystem only. Explicit `TERMINAL_ENV` values other than `local`/`host` are skipped because local hashes cannot prove remote state. Commands that write outside the selected workspace, send messages, mutate remote services, or only return new output cannot be observed as external state changes.
- **Result format:** string results receive recovery text. Structured non-string results are preserved; a notice is logged rather than corrupting the result type.
- **Concurrency:** atomic file replacement protects readers from partial JSON; this version does not provide multi-process session locking. Enforce mode is a circuit breaker, not an authorization sandbox.
- **CLI/dashboard:** persisted session state is available; an external CLI without a live plugin context may show a zero/unbound status rather than the gateway's in-memory workspace. Desktop is intentionally omitted.
