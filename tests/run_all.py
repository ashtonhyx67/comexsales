"""Run every suite. From the project root:  python tests/run_all.py"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["test_bot", "test_bundles", "test_live_shape", "test_layout",
          "test_flow", "test_multiple", "test_delete", "test_days", "test_stress"]
env = dict(os.environ, PYTHONIOENCODING="utf-8",
           BOT_TOKEN=os.environ.get("BOT_TOKEN", "x"),
           GOOGLE_CREDENTIALS=os.environ.get("GOOGLE_CREDENTIALS", "{}"))

total = failed = 0
for name in SUITES:
    r = subprocess.run([sys.executable, os.path.join(HERE, name + ".py")],
                       capture_output=True, text=True, cwd=HERE, env=env,
                       encoding="utf-8", errors="replace")
    out = r.stdout or ""
    n = out.count("\nPASS") + out.startswith("PASS")
    bad = [l for l in out.splitlines() if l.startswith("FAIL ")]
    total += n
    failed += len(bad)
    print(f"  {name:<14} {n:>3} passed" + (f"  {len(bad)} FAILED" if bad else ""))
    for l in bad:
        print("     ", l)
    if r.returncode and not bad:
        print("      crashed:", (r.stderr or "").strip().splitlines()[-1:])
        failed += 1
print(f"\n{total} checks, {failed} failures")
sys.exit(1 if failed else 0)
