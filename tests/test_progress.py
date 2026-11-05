import json

from headway.progress import Headway, Settings


def make_headway(tmp_path, mode="advisory", warmup=0, **overrides):
    session_id = overrides.pop("session_id", "default")
    values = dict(warn_after=2, intervene_after=3, mode=mode,
                  warmup_calls=warmup, evidence_window=20, tried_limit=5)
    values.update(overrides)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return Headway(workspace, tmp_path / "state", settings=Settings(**values), session_id=session_id)


def test_varied_terminal_commands_with_no_output_or_change_intervene(tmp_path):
    h = make_headway(tmp_path)
    h.set_goal("Fix the parser without changing its public API")
    commands = ["pwd", "ls -la", "git status", "printf ''"]
    results = []
    for command in commands:
        results.append(h.execute("terminal", {"command": command}, lambda: ""))
    assert [r.decision for r in results] == ["no_progress"] * 4
    assert [r.streak for r in results] == [1, 2, 3, 4]
    assert results[1].notice is not None
    packet = json.loads(results[2].notice.splitlines()[-1])
    assert packet["plugin"] == "headway"
    assert packet["intervention"] == "warn"
    assert "Fix the parser" in packet["goal_hint"]
    assert packet["unchanged"]
    assert len(packet["tried"]) == 3
    assert "materially different hypothesis" in packet["require"]
    assert packet["reason"]


def test_real_write_resets_streak_and_evidence_window(tmp_path):
    h = make_headway(tmp_path)
    h.execute("terminal", {"command": "seed"}, lambda: "same output")
    for command in ["one", "two", "three"]:
        h.execute("terminal", {"command": command}, lambda: "same output")

    def write():
        (tmp_path / "workspace" / "fixed.py").write_text("fixed\n")
        return "created fixed.py"

    result = h.execute("write_file", {"path": "fixed.py"}, write)
    assert result.decision == "state_changed"
    assert result.streak == 0
    assert result.notice is None
    assert h.status()["streak"] == 0


def test_novel_error_output_is_evidence_and_resets_streak(tmp_path):
    h = make_headway(tmp_path)
    h.execute("terminal", {"command": "test one"}, lambda: "same output")
    result = h.execute("terminal", {"command": "test two"}, lambda: "TypeError: bad input")
    assert result.decision == "new_evidence"
    assert result.streak == 0


def test_timestamp_only_output_does_not_reset_streak(tmp_path):
    h = make_headway(tmp_path)
    h.execute("terminal", {"command": "watch"}, lambda: "waiting at 2026-11-05T22:00:00Z pid 1")
    result = h.execute("terminal", {"command": "watch again"}, lambda: "waiting at 2026-11-06T23:00:00Z pid 2")
    assert result.decision == "no_progress"
    assert result.streak == 2


def test_read_coverage_expands_once_then_repeated_read_is_no_progress(tmp_path):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "app.py").write_text("".join(f"line {i}\n" for i in range(1, 30)))
    h = make_headway(tmp_path)
    args = {"path": "app.py", "offset": 1, "limit": 10}
    first = h.execute("read_file", args, lambda: "first read")
    assert first.decision == "coverage_expand"
    repeated = h.execute("read_file", args, lambda: "first read")
    assert repeated.decision == "no_progress"
    later = h.execute("read_file", {"path": "app.py", "offset": 11, "limit": 10}, lambda: "later read")
    assert later.decision == "coverage_expand"


def test_warmup_tools_are_recorded_but_do_not_count(tmp_path):
    h = make_headway(tmp_path, warmup=2)
    for command in ["a", "b", "c"]:
        result = h.execute("terminal", {"command": command}, lambda: "same")
    assert result.streak == 1
    assert h.status()["observed_calls"] == 3


def test_enforce_blocks_same_class_but_allows_recovery_and_reset(tmp_path):
    h = make_headway(tmp_path, mode="enforce")
    h.execute("terminal", {"command": "seed"}, lambda: "same")
    for command in ["a", "b", "c"]:
        h.execute("terminal", {"command": command}, lambda: "same")

    called = False
    def must_not_run():
        nonlocal called
        called = True
        return "should not run"
    blocked = h.execute("terminal", {"command": "different but still thrashing"}, must_not_run)
    assert blocked.blocked
    assert not called
    assert "Headway circuit breaker" in blocked.result
    assert json.loads(blocked.packet.to_json())["intervention"] == "block"

    delegated = h.execute("delegate_task", {"task": "review parser hypothesis"}, lambda: "new reviewer result")
    assert not delegated.blocked
    assert delegated.decision == "new_evidence"
    assert h.status()["streak"] == 0


def test_enforce_blocks_repeated_reads_until_new_coverage(tmp_path):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "a.txt").write_text("a\n")
    h = make_headway(tmp_path, mode="enforce")
    for _ in range(4):
        h.execute("read_file", {"path": "a.txt"}, lambda: "")
    assert h.status()["streak"] == 3
    blocked = h.execute("read_file", {"path": "a.txt"}, lambda: "a\n")
    assert blocked.blocked


def test_explicit_reset_clears_streak_and_enforcement(tmp_path):
    h = make_headway(tmp_path, mode="enforce")
    h.execute("terminal", {"command": "seed"}, lambda: "same")
    for command in ["a", "b", "c"]:
        h.execute("terminal", {"command": command}, lambda: "same")
    h.reset()
    result = h.execute("terminal", {"command": "next hypothesis"}, lambda: "same")
    assert not result.blocked
    assert result.streak == 1


def test_user_hypothesis_change_resets_streak(tmp_path):
    h = make_headway(tmp_path)
    h.execute("terminal", {"command": "a"}, lambda: "same")
    h.user_message("Try the tokenizer instead")
    assert h.status()["streak"] == 0


def test_state_persists_and_status_cli_is_read_only(tmp_path):
    h = make_headway(tmp_path)
    h.execute("terminal", {"command": "seed"}, lambda: "same")
    h.execute("terminal", {"command": "a"}, lambda: "same")
    loaded = make_headway(tmp_path)
    assert loaded.status()["streak"] == 1
    state_files = list((tmp_path / "state").glob("*.json"))
    assert len(state_files) == 1
    before = state_files[0].read_text()
    assert loaded.status()["plugin"] == "headway"
    assert state_files[0].read_text() == before


def test_recovery_tool_can_release_enforcement_when_it_changes_state(tmp_path):
    h = make_headway(tmp_path, mode="enforce")
    h.execute("terminal", {"command": "seed"}, lambda: "same")
    for command in ["a", "b", "c"]:
        h.execute("terminal", {"command": command}, lambda: "same")
    assert h.execute("terminal", {"command": "d"}, lambda: "unused").blocked

    def mutation():
        (tmp_path / "workspace" / "recovery.txt").write_text("verified\n")
        return "wrote verification result"
    result = h.execute("write_file", {"path": "recovery.txt"}, mutation)
    assert result.decision == "state_changed"
    assert h.status()["streak"] == 0


def test_middleware_accepts_tool_execution_context_kwargs(tmp_path):
    h = make_headway(tmp_path, session_id="s1")
    outcome = h.tool_execution("terminal", {"command": "pwd"}, lambda: "ok",
                               session_id="s1", task_id="t1")
    assert outcome.result == "ok"
    assert h.session_id == "s1"


def test_non_local_terminal_is_honestly_skipped(tmp_path):
    h = make_headway(tmp_path)
    result = h.execute("terminal", {"command": "hostname", "terminal_env": "ssh"}, lambda: "remote")
    assert result.decision == "skipped"
    assert result.streak == 0


def test_default_thresholds_and_modes_match_product_contract():
    settings = Settings()
    assert settings.warn_after == 4
    assert settings.intervene_after == 6
    assert settings.mode == "advisory"
    assert settings.warmup_calls == 2


def test_state_is_atomic_and_corrupt_state_fails_open(tmp_path):
    h = make_headway(tmp_path)
    state = h._state_path
    state.parent.mkdir(parents=True)
    state.write_text("not json")
    assert make_headway(tmp_path).status()["streak"] == 0
    h.execute("terminal", {"command": "pwd"}, lambda: "")
    assert not list(state.parent.glob("*.tmp"))
    assert json.loads(state.read_text())["streak"] == 1


def test_partially_overlapping_read_range_expands_coverage(tmp_path):
    h = make_headway(tmp_path)
    h.execute("read_file", {"path": "app.py", "offset": 1, "limit": 15}, lambda: "")
    result = h.execute("read_file", {"path": "app.py", "offset": 11, "limit": 10}, lambda: "")
    assert result.decision == "coverage_expand"


def test_failed_read_does_not_claim_new_coverage(tmp_path):
    h = make_headway(tmp_path)
    result = h.execute("read_file", {"path": "missing.py"}, lambda: '{"error": "not found"}')
    assert result.decision != "coverage_expand"
    assert "missing.py" not in h._session.coverage


def test_workspace_roots_have_isolated_persistent_state(tmp_path):
    h = make_headway(tmp_path)
    h.execute("terminal", {"command": "seed"}, lambda: "same")
    h.execute("terminal", {"command": "again"}, lambda: "same")
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = Headway(other_root, tmp_path / "state", settings=h.settings, session_id="default")
    assert other.status()["streak"] == 0
    other.execute("terminal", {"command": "seed"}, lambda: "same")
    assert make_headway(tmp_path).status()["streak"] == 1


def test_remote_environment_is_not_measured_as_local_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    h = make_headway(tmp_path)
    result = h.execute("terminal", {"command": "pwd"}, lambda: "remote output")
    assert result.decision == "skipped"
    assert result.streak == 0
