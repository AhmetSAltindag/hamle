#!/usr/bin/env python3
"""Run only synthetic/unit test entry points. Never run live/deploy scripts."""
from pathlib import Path
import os, subprocess, sys
root = Path(__file__).resolve().parent / "hamle"
suites = ["hamle-gmail-connector", "hamle-workflow", "hamle-runtime", "hamle-cloud-probe", "hamle-internal-intake", "hamle-review-control", "hamle-account-manager"]
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
for name in suites:
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"], cwd=root / "work" / name, env=env)
    if result.returncode: raise SystemExit(result.returncode)
raise SystemExit(subprocess.run([sys.executable, "-B", str(root / "outputs/gamechangers-pilot/offline/run_tests.py")], cwd=root, env=env).returncode)
