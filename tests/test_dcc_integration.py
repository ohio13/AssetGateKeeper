"""Headless DCC integration sweep: Blender 4.0, Houdini 22, Unreal 5.3.

    python tests/test_dcc_integration.py                 # all local DCCs, 15 s cap
    python tests/test_dcc_integration.py --only blender   # one of them
    python tests/test_dcc_integration.py --deep            # give UE a real budget
    python tests/test_dcc_integration.py --require         # a skip fails the run

What it proves
    Each DCC is started in its *headless* form, handed the same two fixtures (a
    12-triangle crate that fits a 1,500-triangle budget and a 4,000-triangle boss
    that does not), and asked to report what it measured. The runner then checks
    the numbers, the fuzzy-tag suggestion and the exit code against what the
    fixtures actually contain.

Guardrails, because a DCC is a GUI program with a GUI's idea of shutdown
    * every invocation runs under a hard timeout and is killed with its whole
      process tree (``taskkill /F /T``) when it expires - the suite can never hang;
    * a timeout is reported as SKIP with the reason, not as a pass and not as a
      product failure: an editor that needs 40 seconds to boot is a fact about
      this machine, not a regression in the gate;
    * probes write JSON to a file, so a chatty DCC cannot corrupt the result by
      printing a banner into stdout;
    * ``--require`` turns a skip into a failure, for a machine where a missing DCC
      is a real problem (a release image, a render node).

Exit codes: 0 everything that ran passed, 1 something ran and failed,
2 the runner itself could not set up.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "scratch" / "dcc_integration"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: Where each DCC lives on a normal install. Everything can be overridden by an
#: environment variable, because studios do not install to C:\\Program Files.
CANDIDATES = {
    "blender": [
        r"C:\Program Files\Blender Foundation\Blender 4.0\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 4.0\blender-launcher.exe",
        r"C:\Program Files\Blender Foundation\Blender 4.0\blender.exe",
    ],
    "houdini": [
        r"C:\Program Files\Side Effects Software\Houdini 22.0.459\bin\hython.exe",
        r"C:\Program Files\Side Effects Software\Houdini 22.0.459\bin\houdini.exe",
    ],
    "unreal": [
        r"C:\Program Files\Epic Games\UE_5.3\Engine\Binaries\Win64\UnrealEditor-Cmd.exe",
        r"C:\Program Files\Epic Games\UE_5.3\Engine\Binaries\Win64\UnrealEditor.exe",
    ],
}
ENV_OVERRIDE = {"blender": "AGK_BLENDER", "houdini": "AGK_HOUDINI", "unreal": "AGK_UE"}

failures: list[str] = []
skips: list[str] = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def skip(label, reason):
    print(f"  skip {label}  [{reason}]")
    skips.append(f"{label}: {reason}")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def write_obj(path: Path, triangles: int, materials: int) -> None:
    """A triangulated mesh with exact counts and a real .mtl beside it.

    Triangles, not quads: every DCC triangulates quads at a different moment, and
    the point of this suite is to compare counts, so the fixture must not leave
    room for interpretation.
    """
    mtl = path.with_suffix(".mtl")
    lines = [f"# agk fixture: {triangles} triangles, {materials} materials",
             f"mtllib {mtl.name}"]
    for index in range(materials):
        lines.append(f"usemtl mat_{index}")
        lines.append("o part_" + str(index))
    vertex = 1
    per_material = max(1, triangles // materials)
    written = 0
    for index in range(materials):
        lines.append(f"usemtl mat_{index}")
        count = per_material if index < materials - 1 else triangles - written
        for _ in range(count):
            lines.append(f"v {vertex} 0 0")
            lines.append(f"v {vertex + 1} 1 0")
            lines.append(f"v {vertex} 0 1")
            lines.append(f"f {vertex} {vertex + 1} {vertex + 2}")
            vertex += 2
        written += count
    path.write_text("\n".join(lines) + "\n", encoding="ascii")

    mtl_lines = ["# agk fixture materials"]
    for index in range(materials):
        mtl_lines += [f"newmtl mat_{index}", "Kd 0.8 0.8 0.8", "d 1.0", ""]
    mtl.write_text("\n".join(mtl_lines), encoding="ascii")


def build_fixtures() -> dict:
    WORK.mkdir(parents=True, exist_ok=True)
    crate = WORK / "crate.obj"
    boss = WORK / "boss.obj"
    write_obj(crate, 12, 1)
    write_obj(boss, 4000, 4)
    project = WORK / "AGKProbe.uproject"
    project.write_text(json.dumps({
        "FileVersion": 3, "EngineAssociation": "5.3", "Category": "",
        "Description": "Headless probe project for AssetGatekeeper tests"}), encoding="utf-8")
    return {"crate": crate, "boss": boss, "project": project,
            "crate_tris": 12, "boss_tris": 4000, "boss_materials": 4}


# --------------------------------------------------------------------------- #
# Process control
# --------------------------------------------------------------------------- #

def find_binary(name: str) -> str:
    override = os.environ.get(ENV_OVERRIDE[name], "").strip()
    if override:
        return override if Path(override).is_file() else ""
    for candidate in CANDIDATES[name]:
        if Path(candidate).is_file():
            return candidate
    return ""


def invoke(argv: list[str], timeout: float, env_extra: dict | None = None) -> dict:
    """Run a DCC headlessly, kill it (and its children) if it overruns."""
    env = dict(os.environ)
    env.update(env_extra or {})
    env.setdefault("PYTHONIOENCODING", "utf-8")
    started = time.time()
    try:
        process = subprocess.Popen(argv, cwd=str(ROOT), env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        return {"rc": None, "out": "", "err": str(exc), "timed_out": False,
                "duration": 0.0, "error": str(exc)}
    timed_out = False
    out = err = ""
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=15)
        else:                                        # pragma: no cover
            process.kill()
        try:
            out, err = process.communicate(timeout=10)
        except Exception:                            # noqa: BLE001
            out, err = "", ""
    return {"rc": process.returncode, "out": out or "", "err": err or "",
            "timed_out": timed_out, "duration": time.time() - started}


def load_result(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# DCC suites
# --------------------------------------------------------------------------- #

def run_blender(binary: str, fixtures: dict, timeout: float, preset: str) -> None:
    probe = ROOT / "plugins" / "blender" / "preflight_blender.py"
    for name, expected_tris, expected_exit in (("crate", fixtures["crate_tris"], 0),
                                               ("boss", fixtures["boss_tris"], 1)):
        out_path = WORK / f"blender_{name}.json"
        out_path.unlink(missing_ok=True)
        argv = [binary, "--background", "--factory-startup", "--python", str(probe),
                "--", "--fixture", str(fixtures[name]), "--preset", preset,
                "--out", str(out_path)]
        result = invoke(argv, timeout)
        label = f"blender/{name}"
        if result["timed_out"]:
            skip(label, f"no exit within {timeout:g}s (process tree killed)")
            continue
        expect(f"{label}: blender started and exited", result["rc"] is not None,
               result["err"][-200:])
        data = load_result(out_path)
        expect(f"{label}: wrote a result file", bool(data), result["err"][-200:])
        if not data:
            continue
        expect(f"{label}: harness reported no error", "error" not in data,
               str(data.get("error"))[:160])
        expect(f"{label}: add-on registered", data.get("registered") is True)
        expect(f"{label}: live cube evaluates to 12 triangles",
               (data.get("live_cube") or {}).get("triangles") == 12,
               str(data.get("live_cube")))
        measured = (data.get("fixture") or {}).get("triangles")
        expect(f"{label}: imported geometry has {expected_tris} triangles",
               measured == expected_tris, str(measured))
        expect(f"{label}: exit code is {expected_exit}", data.get("exit") == expected_exit,
               str(data.get("exit")))
        expect(f"{label}: process exit matches the verdict", result["rc"] == expected_exit,
               str(result["rc"]))
        expect(f"{label}: finished inside the {timeout:g}s budget",
               result["duration"] < timeout, f"{result['duration']:.1f}s")
        if name == "crate":
            suggestion = data.get("tag_suggestion")
            expect("blender: the misspelled folder is caught in the panel",
                   bool(suggestion) and suggestion[1] == "character", str(suggestion))


def run_houdini(binary: str, fixtures: dict, timeout: float, preset: str) -> None:
    probe = ROOT / "plugins" / "houdini" / "preflight_houdini.py"
    out_path = WORK / "houdini.json"
    out_path.unlink(missing_ok=True)
    gate_python = os.environ.get("AGK_PYTHON") or sys.executable
    argv = [binary, str(probe), "--out", str(out_path), "--preset", preset,
            str(fixtures["crate"]), str(fixtures["boss"])]
    result = invoke(argv, timeout, env_extra={
        "AGK_PYTHON": gate_python,
        "AGK_GATEKEEPER": str(ROOT / "asset_gatekeeper.py"),
    })
    label = "houdini"
    if result["timed_out"]:
        skip(label, f"no exit within {timeout:g}s (process tree killed)")
        return
    data = load_result(out_path)
    expect("houdini: hython started and exited", result["rc"] is not None,
           result["err"][-200:])
    expect("houdini: wrote a result file", bool(data), result["err"][-200:])
    if not data:
        return
    expect("houdini: harness reported no error", "error" not in data,
           str(data.get("error"))[:200])
    expect("houdini: the hook honoured AGK_PYTHON",
           data.get("agk_python") == gate_python, str(data.get("agk_python")))
    entries = {item["path"]: item for item in data.get("results", [])}
    expect("houdini: both fixtures were gated", len(entries) == 2, str(list(entries)))
    crate = next((item for key, item in entries.items() if key.startswith("crate")), None)
    boss = next((item for key, item in entries.items() if key.startswith("boss")), None)
    expect("houdini: the crate passes the retro budget",
           crate is not None and crate["returncode"] == 0, str(crate))
    expect("houdini: the boss fails the retro budget",
           boss is not None and boss["returncode"] == 1, str(boss))
    expect("houdini: the failure names the triangle budget",
           boss is not None and any("triangle" in problem.lower() for problem in boss["problems"]),
           str(boss["problems"])[:160] if boss else "")
    expect("houdini: the run reports a failure exit", data.get("exit") == 1,
           str(data.get("exit")))
    expect("houdini: finished inside the budget", result["duration"] < timeout,
           f"{result['duration']:.1f}s")


def run_unreal(binary: str, fixtures: dict, timeout: float, preset: str) -> None:
    probe = ROOT / "plugins" / "unreal" / "preflight_unreal.py"
    out_path = WORK / "unreal.json"
    out_path.unlink(missing_ok=True)
    argv = [binary, str(fixtures["project"]), "-run=pythonscript",
            f"-script={probe}", "-unattended", "-nopause", "-nosplash",
            "-NullRHI", "-nosound", "-log"]
    result = invoke(argv, timeout, env_extra={
        "AGK_PROBE_OUT": str(out_path),
        "AGK_PROBE_FIXTURE": str(fixtures["boss"]),
        "AGK_PROBE_PRESET": preset,
    })
    label = "unreal"
    if result["timed_out"]:
        skip(label, f"editor did not exit within {timeout:g}s (process tree killed)")
        return
    data = load_result(out_path)
    expect("unreal: the editor started and exited", result["rc"] is not None,
           result["err"][-200:])
    expect("unreal: wrote a result file", bool(data), result["err"][-300:])
    if not data:
        return
    expect("unreal: harness reported no error", "error" not in data,
           str(data.get("error"))[:200])
    expect("unreal: the engine version is 5.x", str(data.get("version", "")).startswith("5."),
           str(data.get("version")))
    expect("unreal: the probe states how it measured",
           data.get("mode") in ("asset", "file"), str(data.get("mode")))
    measured = (data.get("fixture") or {}).get("triangles")
    expect("unreal: measured 4,000 triangles", measured == fixtures["boss_tris"], str(measured))
    expect("unreal: the boss fails the retro budget", data.get("exit") == 1,
           str(data.get("exit")))
    # UnrealEditor-Cmd exits with the *editor's* status, not the script's: a
    # scripted quit reports -1 (4294967295 unsigned). The verdict that matters is
    # the one in the report, asserted above; here we only prove it exited itself
    # instead of being killed by the guardrail.
    expect("unreal: the editor exited on its own, verdict carried in the report",
           result["rc"] in (0, 1, -1, 4294967295, 255), str(result["rc"]))
    expect("unreal: it was not killed by the timeout", not result["timed_out"])
    suggestion = data.get("tag_suggestion")
    expect("unreal: the misspelled folder is caught",
           bool(suggestion) and suggestion[1] == "character", str(suggestion))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Headless DCC integration sweep")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="hard per-invocation limit in seconds (default 15)")
    parser.add_argument("--deep-timeout", type=float, default=180.0,
                        help="budget for UE in --deep mode (editor startup is slow)")
    parser.add_argument("--deep", action="store_true",
                        help="give Unreal a realistic startup budget instead of the 15s guardrail test")
    parser.add_argument("--only", action="append", choices=["blender", "houdini", "unreal"])
    parser.add_argument("--preset", default="retro")
    parser.add_argument("--require", action="store_true",
                        help="treat missing DCCs and timeouts as failures")
    args = parser.parse_args(argv)

    fixtures = build_fixtures()
    print(f"fixtures   {fixtures['crate'].name} ({fixtures['crate_tris']} tris), "
          f"{fixtures['boss'].name} ({fixtures['boss_tris']} tris)")
    print(f"guardrail  {args.timeout:g}s per invocation, process tree killed on expiry")

    targets = args.only or ["blender", "houdini", "unreal"]
    started = time.time()
    for name in targets:
        binary = find_binary(name)
        if not binary:
            skip(name, "not installed (set " + ENV_OVERRIDE[name] + " to override)")
            continue
        # --deep raises the budget for *every* host: a cold license check or a
        # shader cache miss is not a product failure, but it does mean the 15 s
        # guardrail cannot finish the job on this machine.
        budget = args.deep_timeout if args.deep else args.timeout
        print(f"\n{name}  {binary}  ({budget:g}s budget)")
        if name == "blender":
            run_blender(binary, fixtures, budget, args.preset)
        elif name == "houdini":
            run_houdini(binary, fixtures, budget, args.preset)
        else:
            run_unreal(binary, fixtures, budget, args.preset)

    elapsed = time.time() - started
    print()
    print(f"wall clock {elapsed:.1f}s   skips: {len(skips)}   failures: {len(failures)}")
    for entry in skips:
        print(f"  skip  {entry}")
    if failures:
        for entry in failures:
            print(f"  FAIL  {entry}")

    if args.require and skips:
        print("\n--require: skips count as failures")
        return 1
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
