#!/usr/bin/env python3
"""One-command certification run: every test suite, one dated report.

    python tools/certify.py              everything CI runs (about 10 minutes)
    python tools/certify.py --quick      gate + workstation suites only
    python tools/certify.py --dcc        also the local DCC sweep (needs Blender/Houdini/Unreal)
    python tools/certify.py --only test_workstation --only test_analytics

Writes ``certification/<date>-<commit>.md``: each suite's pass and fail counts,
its failures, the Python and package versions, the commit and tag, and the
``--doctor`` output. Exits 0 only when every suite that ran passed - a suite
that crashes before its summary line counts as failed, never as passed.
"""

from __future__ import annotations

import argparse
import importlib
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (name, script, extra args, group). Order matters: the corpus first.
SUITES = (
    ("gen_test_assets", "tests/gen_test_assets.py", (), "setup"),
    ("test_realworld", "tests/test_realworld.py", (), "gate"),
    ("test_gatekeeper", "tests/test_gatekeeper.py", (), "gate"),
    ("test_formats", "tests/test_formats.py", (), "gate"),
    ("test_rules_wizard", "tests/test_rules_wizard.py", (), "gate"),
    ("test_lod_collision", "tests/test_lod_collision.py", (), "gate"),
    ("test_server", "tests/test_server.py", (), "gate"),
    ("test_admin", "tests/test_admin.py", (), "gate"),
    ("test_enterprise", "tests/test_enterprise.py", (), "gate"),
    ("test_analytics", "tests/test_analytics.py", (), "gate"),
    ("headless_integration", "tests/run_headless_integration.py", ("--synthetic",), "gate"),
    ("test_workstation", "tests/test_workstation.py", (), "workstation"),
    ("test_viewer_browser", "tests/test_viewer_browser.py", (), "workstation"),
    ("test_dcc_integration", "tests/test_dcc_integration.py", (), "dcc"),
)
QUICK = {"gen_test_assets", "test_gatekeeper", "test_formats", "test_workstation"}
PACKAGES = ("trimesh", "PIL", "numpy", "charset_normalizer", "watchdog", "playwright",
            "cryptography")

CHECK_LINE = re.compile(r"^\s+(ok|FAIL|skip)\s+(.*)$")
SUMMARY = re.compile(r"^FAILURES:\s*(.*)$", re.MULTILINE)
INTEGRATION = re.compile(r"(\d+) passed, (\d+) failed(?:, (\d+) skipped)?")


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def versions() -> list[tuple[str, str]]:
    found = [("python", platform.python_version()), ("platform", platform.platform())]
    for name in PACKAGES:
        try:
            module = importlib.import_module(name)
            found.append((name, str(getattr(module, "__version__", "installed"))))
        except Exception:
            found.append((name, "not installed"))
    return found


def run_suite(name: str, script: str, extra: tuple, timeout: float) -> dict:
    started = time.monotonic()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONWARNINGS="ignore")
    try:
        completed = subprocess.run([sys.executable, script, *extra], cwd=ROOT,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout, env=env)
        output, code = completed.stdout + completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        output += f"\n[certify] timed out after {timeout:.0f} s"
        code = -1
    seconds = time.monotonic() - started
    passed = failed = skipped = 0
    failures: list[str] = []
    for line in output.splitlines():
        match = CHECK_LINE.match(line)
        if not match:
            continue
        kind = match.group(1)
        if kind == "ok":
            passed += 1
        elif kind == "FAIL":
            failed += 1
            failures.append(match.group(2)[:200])
        else:
            skipped += 1
    summary = SUMMARY.search(output)
    # The LAST "N passed, M failed" line is the suite's own summary; earlier ones
    # are the gate's per-sweep logs it prints along the way.
    summaries = list(INTEGRATION.finditer(output)) if name == "headless_integration" else []
    integration = summaries[-1] if summaries else None
    if integration:
        passed, failed = int(integration.group(1)), int(integration.group(2))
        skipped = int(integration.group(3) or 0)
    ok = code == 0 and failed == 0
    if name not in ("gen_test_assets", "headless_integration") and summary is None:
        ok = False      # crashed before printing its summary: never a pass
        failures.append("no FAILURES line: the suite stopped early (see the tail below)")
    return {"name": name, "ok": ok, "code": code, "passed": passed, "failed": failed,
            "skipped": skipped, "seconds": seconds, "failures": failures,
            "tail": "\n".join(output.strip().splitlines()[-15:])}


def doctor() -> str:
    try:
        completed = subprocess.run([sys.executable, "asset_gatekeeper.py", "--doctor",
                                    "--no-color"], cwd=ROOT, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
        return (completed.stdout + completed.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"doctor did not run: {exc}"


def report(results: list[dict], out_dir: Path, args: argparse.Namespace) -> Path:
    commit = git("rev-parse", "--short", "HEAD") or "unknown"
    tag = git("describe", "--tags", "--always") or "unknown"
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    total_ok = all(result["ok"] for result in results)
    lines = [f"# AssetGatekeeper certification - {stamp}", "",
             f"**Result: {'PASSED' if total_ok else 'FAILED'}** - "
             f"{sum(r['passed'] for r in results)} checks passed, "
             f"{sum(r['failed'] for r in results)} failed, "
             f"{sum(r['skipped'] for r in results)} skipped.", "",
             f"* commit `{commit}`{' (with uncommitted changes)' if dirty else ''}, "
             f"`git describe`: `{tag}`",
             f"* run: `{' '.join(sys.argv)}`", "",
             "| Suite | Result | Passed | Failed | Skipped | Seconds |",
             "| --- | --- | --- | --- | --- | --- |"]
    for result in results:
        lines.append(f"| {result['name']} | {'pass' if result['ok'] else '**FAIL**'} | "
                     f"{result['passed']} | {result['failed']} | {result['skipped']} | "
                     f"{result['seconds']:.0f} |")
    lines += ["", "## Environment", ""]
    lines += [f"* {name}: {value}" for name, value in versions()]
    for result in results:
        if result["ok"]:
            continue
        lines += ["", f"## {result['name']} - failures", ""]
        lines += [f"* {failure}" for failure in result["failures"]] or ["* (exit code "
                                                                       f"{result['code']})"]
        lines += ["", "```", result["tail"], "```"]
    lines += ["", "## Doctor", "", "```", doctor(), "```", ""]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stamp}-{commit}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true",
                        help="only the gate and workstation unit suites (about 3 minutes)")
    parser.add_argument("--dcc", action="store_true",
                        help="also run the DCC sweep (needs Blender, Houdini or Unreal)")
    parser.add_argument("--no-browser", action="store_true",
                        help="skip the Chromium suite (no playwright on this machine)")
    parser.add_argument("--only", action="append", metavar="SUITE", default=[],
                        help="run just this suite (repeatable): " +
                             ", ".join(name for name, *_ in SUITES))
    parser.add_argument("--out", default=str(ROOT / "certification"),
                        help="folder for the report (default: certification/)")
    parser.add_argument("--timeout", type=float, default=1200.0,
                        help="seconds before one suite is stopped (default 1200)")
    parser.add_argument("--list", action="store_true", help="print the suites and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, script, extra, group in SUITES:
            print(f"{group:<12}{name:<24}{script} {' '.join(extra)}")
        return 0
    unknown = [name for name in args.only if name not in {suite[0] for suite in SUITES}]
    if unknown:
        parser.error(f"unknown suite(s): {', '.join(unknown)}")

    chosen = []
    for name, script, extra, group in SUITES:
        if args.only:
            if name in args.only or (name == "gen_test_assets" and args.only):
                chosen.append((name, script, extra))
            continue
        if group == "dcc" and not args.dcc:
            continue
        if args.quick and name not in QUICK:
            continue
        if args.no_browser and name == "test_viewer_browser":
            continue
        chosen.append((name, script, extra))

    results = []
    for name, script, extra in chosen:
        print(f"running {name} ...", flush=True)
        result = run_suite(name, script, extra, args.timeout)
        results.append(result)
        print(f"  {'pass' if result['ok'] else 'FAIL'}  {result['passed']} passed, "
              f"{result['failed']} failed, {result['skipped']} skipped "
              f"({result['seconds']:.0f} s)", flush=True)
        for failure in result["failures"][:5]:
            print(f"        {failure}")
    path = report(results, Path(args.out), args)
    ok = all(result["ok"] for result in results)
    print(f"\n{'CERTIFIED' if ok else 'NOT CERTIFIED'} - report: {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
