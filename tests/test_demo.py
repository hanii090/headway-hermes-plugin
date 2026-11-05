import subprocess
import sys
from pathlib import Path


def test_demo_proves_varied_thrash_intervention_and_real_write_reset():
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, str(root / "scripts" / "demo.py")],
                          cwd=root, text=True, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PROOF 1 PASS" in proc.stdout
    assert "PROOF 2 PASS" in proc.stdout
    assert "different commands" in proc.stdout
    assert "real write" in proc.stdout
    assert proc.stderr == ""
