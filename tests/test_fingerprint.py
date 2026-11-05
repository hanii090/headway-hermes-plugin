from headway.fingerprint import WorkspaceFingerprinter, evidence_fingerprint, normalize_evidence


def test_workspace_hash_detects_content_change_and_ignores_internal_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('one')\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("not workspace evidence")
    fp = WorkspaceFingerprinter(tmp_path)

    before = fp.capture()
    (tmp_path / ".git" / "config").write_text("different metadata")
    assert fp.capture().digest == before.digest

    (tmp_path / "src" / "app.py").write_text("print('two')\n")
    after = fp.capture()
    assert after.digest != before.digest
    assert after.short_id != before.short_id
    assert after.files == ["src/app.py"]


def test_workspace_fingerprint_is_bounded_for_large_files(tmp_path):
    large = tmp_path / "large.bin"
    large.write_bytes(b"a" * 10000)
    fp = WorkspaceFingerprinter(tmp_path, max_file_bytes=64, max_total_bytes=128)
    before = fp.capture()
    assert before.partial
    large.write_bytes(b"a" * 5000 + b"b" + b"a" * 4999)
    sampled = fp.capture()
    assert sampled.digest == before.digest  # documented sampling limit
    assert sampled.partial
    exact = WorkspaceFingerprinter(tmp_path, max_file_bytes=20_000, max_total_bytes=20_000)
    assert exact.capture().digest != before.digest


def test_normalize_removes_noise_and_relativizes_paths(tmp_path):
    raw = "\x1b[31mFAILED\x1b[0m at 2026-11-05T22:45:01Z pid 1234 /tmp/project/app.py:9\n  elapsed 0.42 seconds"
    normalized = normalize_evidence(raw, tmp_path)
    assert "2026-11-05" not in normalized
    assert "1234" not in normalized
    assert "0.42" not in normalized
    assert str(tmp_path) not in normalized
    assert "app.py" in normalized
    assert "FAILED" in normalized


def test_timestamp_only_output_is_not_new_evidence(tmp_path):
    a = evidence_fingerprint("watching 2026-11-05 22:45:01, pid 1234", tmp_path)
    b = evidence_fingerprint("watching 2026-11-06 23:55:59, pid 9999", tmp_path)
    assert a.digest == b.digest
    assert a.timestamp_only  # waiting/watching has no observable new substance


def test_materially_different_output_is_new_evidence(tmp_path):
    a = evidence_fingerprint("3 tests failed", tmp_path)
    b = evidence_fingerprint("4 tests failed", tmp_path)
    assert a.digest != b.digest
