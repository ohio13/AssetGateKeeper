"""Unity and Godot editor plug-ins (plugins/unity, plugins/godot).

    python tests/test_engine_plugins.py              # what this machine can run
    python tests/test_engine_plugins.py --require    # a missing .NET SDK or Godot is a failure
    python tests/test_engine_plugins.py --only unity # or: contract, godot

1. The gate's --json contract, run exactly as both plug-ins run it: the fields
   they read exist and have the types they expect.
2. Static checks: the Unity package files and .meta GUIDs, and the extension and
   environment-variable lists in the C# and GDScript match the gate's own.
3. Unity: compiles the plug-in with stand-ins for UnityEngine/UnityEditor
   (tests/unity_stubs: netstandard2.1, C# 9, warnings are errors) and runs it end
   to end against the real gate. Needs the .NET SDK 8 (`dotnet`, or $DOTNET).
   Unity itself is not involved: this proves the C# compiles and behaves, not
   that Unity's import pipeline calls it the way its documentation says.
4. Godot: runs tests/godot_project headless with the add-on in it. Needs a
   Godot 4.2+ editor binary ($GODOT, or `godot` on PATH).

A tool that is not installed is a SKIP, unless --require is given.
"""
import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asset_gatekeeper as agk  # noqa: E402

UNITY = ROOT / "plugins" / "unity"
GODOT_ADDON = ROOT / "plugins" / "godot" / "addons" / "asset_gatekeeper"
FIXTURES = ROOT / "tests" / "fixtures" / "fbx"
WORK = ROOT / "scratch" / "engine_plugin_tests"

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--require", action="store_true", help="fail instead of skipping a missing tool")
parser.add_argument("--only", choices=("contract", "unity", "godot"), help="run one part")
options = parser.parse_args()

failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label
          + (f"  [{str(detail)[:600]}]" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def skip(label):
    if options.require:
        expect(label + " (required)", False, "not installed")
    else:
        print("  skip " + label)


def wanted(part):
    return options.only in (None, part)


def write_png(path, width, height, rgb=(128, 96, 64)):
    """A plain RGB PNG without Pillow (the gate has Pillow; this test need not)."""
    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height)

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def write_grid_obj(path, cells):
    """A flat grid of cells x cells quads: 2 * cells^2 triangles, one material, UVs."""
    lines = []
    for y in range(cells + 1):
        for x in range(cells + 1):
            lines.append(f"v {x} 0 {y}")
            lines.append(f"vt {x / cells:.4f} {y / cells:.4f}")
    for y in range(cells):
        for x in range(cells):
            a = y * (cells + 1) + x + 1
            b, c, d = a + 1, a + cells + 2, a + cells + 1
            lines.append(f"f {a}/{a} {b}/{b} {c}/{c}")
            lines.append(f"f {a}/{a} {c}/{c} {d}/{d}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def gate_like_plugins(path, rules=None):
    """The command line plugins/unity and plugins/godot build (GateRunner.BuildArguments)."""
    command = [sys.executable, str(ROOT / "asset_gatekeeper.py"), str(path), "--once", "--json"]
    if rules:
        command += ["--rules", str(rules)]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180)
    verdict = None
    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "problems" in payload:
                verdict = payload
                break
    return completed, verdict


shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)

# --------------------------------------------------------------------------- #
if wanted("contract"):
    print("1. The --json contract both plug-ins read")
    contract = WORK / "contract"
    contract.mkdir()
    shutil.copy(FIXTURES / "cube_subd3.fbx", contract / "good.fbx")
    shutil.copy(FIXTURES / "mats5.fbx", contract / "heavy.fbx")
    write_png(contract / "brick_Albedo.png", 64, 64)
    write_png(contract / "odd.png", 100, 60)
    fields = {"file": str, "name": str, "passed": bool, "status": str, "metric": str,
              "problems": list, "warnings": list, "stats": dict}
    for name, should_pass in (("good.fbx", True), ("heavy.fbx", False),
                              ("brick_Albedo.png", True), ("odd.png", False)):
        completed, verdict = gate_like_plugins(contract / name)
        expect(f"{name}: one verdict line on stdout", verdict is not None, completed.stdout[-400:])
        if verdict is None:
            continue
        expect(f"{name}: every field the plug-ins read, with the type they expect",
               all(isinstance(verdict.get(key), kind) for key, kind in fields.items()), verdict)
        expect(f"{name}: passed={should_pass}, exit {0 if should_pass else 1}",
               verdict["passed"] is should_pass and completed.returncode == (0 if should_pass else 1),
               (verdict, completed.returncode))
        expect(f"{name}: problems are strings, and only a failure has them",
               all(isinstance(item, str) for item in verdict["problems"])
               and bool(verdict["problems"]) is (not should_pass), verdict["problems"])
        expect(f"{name}: stdout holds nothing but JSON (logs go to stderr)",
               all(line.startswith("{") for line in completed.stdout.splitlines() if line.strip()),
               completed.stdout[-300:])
    rules = contract / "rules" / "rules.toml"
    rules.parent.mkdir()
    rules.write_text("[mesh]\nmax_material_slots = 8\n", encoding="utf-8")
    completed, verdict = gate_like_plugins(contract / "heavy.fbx", rules)
    expect("--rules (the nearest rules.toml the plug-ins find) is applied",
           verdict is not None and verdict["passed"] is True, (completed.returncode, verdict))
    completed, verdict = gate_like_plugins(contract / "gone.fbx")
    expect("a file that is not there: exit 2 and no verdict line (the plug-ins report NOT checked)",
           completed.returncode == 2 and verdict is None, (completed.returncode, completed.stdout[-200:]))

# --------------------------------------------------------------------------- #
if wanted("contract") or wanted("unity") or wanted("godot"):
    print("2. Package files, and the lists the plug-ins copy from the gate")
    runner_cs = (UNITY / "Editor" / "GateRunner.cs").read_text(encoding="utf-8")
    runner_gd = (GODOT_ADDON / "gate_runner.gd").read_text(encoding="utf-8")
    tracked = sorted(agk.TRACKED_SUFFIXES)
    cs_list = re.search(r"GatedExtensions\s*=\s*\{(.*?)\};", runner_cs, re.S)
    cs_extensions = sorted(re.findall(r'"(\.[a-z0-9]+)"', cs_list.group(1))) if cs_list else []
    expect("Unity gates exactly the formats the gate measures (TRACKED_SUFFIXES)",
           cs_extensions == tracked, (cs_extensions, tracked))
    gd_list = re.search(r"GATED_EXTENSIONS := \[(.*?)\]", runner_gd, re.S)
    gd_extensions = sorted("." + item for item in re.findall(r'"([a-z0-9]+)"', gd_list.group(1))) if gd_list else []
    expect("Godot gates exactly the formats the gate measures", gd_extensions == tracked,
           (gd_extensions, tracked))
    unreal_source = (ROOT / "plugins" / "unreal" / "import_gatekeeper.py").read_text(encoding="utf-8")
    unreal_vars = sorted(re.findall(r'"([A-Z_]+)"', re.search(
        r"_INHERITED_PYTHON_VARS = \((.*?)\)", unreal_source, re.S).group(1)))
    cs_vars = sorted(re.findall(r'"([A-Z_]+)"', re.search(
        r"InheritedPythonVariables\s*=\s*\{(.*?)\};", runner_cs, re.S).group(1)))
    gd_vars = sorted(re.findall(r'"([A-Z_]+)"', re.search(
        r"INHERITED_PYTHON_VARIABLES := \[(.*?)\]", runner_gd, re.S).group(1)))
    expect("Unity and Godot drop the same inherited Python variables as the Unreal plug-in",
           cs_vars == unreal_vars == gd_vars, (cs_vars, gd_vars, unreal_vars))
    for source, label in ((runner_cs, "Unity"), (runner_gd, "Godot")):
        expect(f"{label} runs the gate with --once --json",
               '"--once"' in source and '"--json"' in source)

    package = json.loads((UNITY / "package.json").read_text(encoding="utf-8"))
    expect("package.json: a valid Unity package name, a semver version, Unity 2021.3",
           re.fullmatch(r"com\.[a-z0-9-]+\.[a-z0-9.-]+", package.get("name", "")) is not None
           and re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", package.get("version", "")) is not None
           and package.get("unity") == "2021.3", package)
    asmdef = json.loads((UNITY / "Editor" / "AssetGatekeeper.Editor.asmdef").read_text(encoding="utf-8"))
    expect("the assembly is Editor-only (never in a player build)", asmdef.get("includePlatforms") == ["Editor"],
           asmdef)
    guids = {}
    for path in sorted(UNITY.rglob("*")):
        if path.suffix == ".meta":
            found = re.search(r"^guid: ([0-9a-f]{32})$", path.read_text(encoding="utf-8"), re.M)
            expect(f"{path.relative_to(UNITY)} has a GUID", found is not None)
            if found:
                guids.setdefault(found.group(1), []).append(path.name)
            expect(f"{path.relative_to(UNITY)} belongs to a file that exists",
                   path.with_suffix("").exists())
        else:
            expect(f"{path.relative_to(UNITY)} has a .meta (packages from disk need them)",
                   Path(str(path) + ".meta").is_file())
    expect("every .meta GUID is unique", all(len(names) == 1 for names in guids.values()), guids)
    # Every file of both plug-ins ships: in the wheel (package-data), the sdist
    # (MANIFEST.in) and the drop-in ZIP (tools/build_release.py).
    project_toml = agk.tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tool = project_toml["tool"]["setuptools"]
    data_globs = tool["package-data"]["agk_suite.plugins"]
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    manifest_line = next((line for line in manifest.splitlines()
                          if line.startswith("recursive-include plugins ")), "")
    sys.path.insert(0, str(ROOT / "tools"))
    import build_release  # noqa: E402
    shipped = [path for base in (UNITY, GODOT_ADDON.parent.parent) for path in base.rglob("*") if path.is_file()]
    for suffix in sorted({path.suffix for path in shipped}):
        expect(f"{suffix} files ship in the wheel, the sdist and the drop-in ZIP",
               f"**/*{suffix}" in data_globs and f"*{suffix}" in manifest_line.split()
               and suffix not in build_release.SKIP_SUFFIXES, (data_globs, manifest_line))
    folders = {"agk_suite.plugins." + ".".join(path.relative_to(ROOT / "plugins").parts)
               for path in shipped for path in [path.parent]}
    expect("every plug-in folder is a listed package (setuptools only ships listed ones)",
           folders <= set(tool["packages"]), sorted(folders - set(tool["packages"])))
    expect("the drop-in ZIP copies plugins/", "plugins" in build_release.DROP_IN_DIRS)

    cfg = (GODOT_ADDON / "plugin.cfg").read_text(encoding="utf-8")
    expect("plugin.cfg names plugin.gd, and it exists",
           'script="plugin.gd"' in cfg and (GODOT_ADDON / "plugin.gd").is_file())
    expect("the Godot test project enables the add-on",
           "res://addons/asset_gatekeeper/plugin.cfg" in
           (ROOT / "tests" / "godot_project" / "project.godot").read_text(encoding="utf-8"))

# --------------------------------------------------------------------------- #
if wanted("unity"):
    print("3. Unity: compile with stand-ins and run against the real gate")
    dotnet = os.environ.get("DOTNET") or shutil.which("dotnet")
    if not dotnet:
        skip("Unity plug-in compile + harness: the .NET SDK (dotnet) is not installed")
    else:
        stubs = ROOT / "tests" / "unity_stubs"
        env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1",
                   DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1")
        build = subprocess.run([dotnet, "build", str(stubs / "AgkUnityPlugin.csproj"), "-nologo", "-v", "q"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=600, env=env)
        expect("the plug-in compiles as netstandard2.1 / C# 9 with no warnings",
               build.returncode == 0 and " 0 Warning(s)" in build.stdout, build.stdout[-1500:])

        project = WORK / "unity_project"
        (project / "Assets" / "Models").mkdir(parents=True)
        shutil.copy(FIXTURES / "cube_subd3.fbx", project / "Assets" / "Models" / "good.fbx")
        shutil.copy(FIXTURES / "mats5.fbx", project / "Assets" / "Models" / "heavy.fbx")
        write_png(project / "Assets" / "My Textures" / "brick_Albedo.png", 64, 64)
        write_png(project / "Assets" / "My Textures" / "héros_odd.png", 100, 60)
        (project / "Assets" / "readme.txt").write_text("not an asset\n", encoding="utf-8")
        # The project's rules.toml sits beside Assets/, where Unity does not import it.
        (project / "rules.toml").write_text("[mesh]\nmax_material_slots = 4\n", encoding="utf-8")
        slow = WORK / "slow_gate"
        slow.mkdir()
        (slow / "asset_gatekeeper.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

        run = subprocess.run([dotnet, "run", "--project", str(stubs / "harness" / "AgkUnityHarness.csproj"),
                              "--", str(project), sys.executable, str(ROOT), str(slow)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=900, env=env)
        result = None
        for line in run.stdout.splitlines():
            if line.startswith("AGK_HARNESS_RESULT "):
                result = json.loads(line[len("AGK_HARNESS_RESULT "):])
        expect("the harness ran to the end", run.returncode == 0 and result is not None,
               (run.returncode, run.stdout[-1500:], run.stderr[-800:]))
        if result:
            lines = result["import_lines"]
            errors = [line for line in lines if line.startswith("error: ")]
            labels = result["import_labels"]
            expect("an import checks the four art files and fails two, as Console errors",
                   len(errors) == 2, lines)
            expect("... the heavy model's error names the asset and the gate's line, from the project rules.toml",
                   any("Assets/Models/heavy.fbx FAILED: 5 material slots exceeds the 4 slot budget" in line
                       for line in errors), errors)
            expect("... the odd texture (a space and an accent in its path) fails for its size and its name",
                   any("Assets/My Textures/héros_odd.png FAILED" in line and "power of two" in line
                       and "suffix" in line.lower() for line in errors), errors)
            expect("... and lists each problem on its own line",
                   any("\n  - " in line for line in errors if "héros_odd" in line), errors)
            expect("the failures are labelled AGK_Failed, the passes are not",
                   labels.get("Assets/Models/heavy.fbx") == ["AGK_Failed"]
                   and labels.get("Assets/My Textures/héros_odd.png") == ["AGK_Failed"]
                   and not labels.get("Assets/Models/good.fbx")
                   and not labels.get("Assets/My Textures/brick_Albedo.png"), labels)
            expect("an inherited PYTHONHOME (editor started from another DCC) did not break the good model",
                   not any("good.fbx" in line for line in lines if not line.startswith("log: ")), lines)
            expect("files outside Assets/ and non-art files are left alone",
                   not any("readme.txt" in line or "Packages/" in line for line in lines), lines)
            expect("a summary line follows a batch with failures",
                   any(line.startswith("log: AssetGatekeeper: 4 asset(s) checked: 2 passed, 2 failed")
                       for line in lines), lines)
            expect("the background-task progress ends as Failed", result["finish_status"] == "Failed",
                   result["finish_status"])
            expect("re-importing unchanged files checks nothing (no label-change loop)",
                   result["reimport_busy"] is False and result["reimport_lines"] == []
                   and result["reimport_set_labels"] == 0, result)
            expect("Check Selected on a folder re-checks it; a fixed model loses its label",
                   "AGK_Failed" not in result["fixed_labels"].get("Assets/Models/heavy.fbx", [])
                   and result["fixed_labels"].get("Assets/My Textures/héros_odd.png") == ["AGK_Failed"]
                   and any("2 asset(s) checked: 2 passed, 0 failed" in line for line in result["fixed_lines"]),
                   result["fixed_lines"])
            warnings = [line for line in result["missing_python_lines"] if line.startswith("warning: ")]
            expect("a Python that does not exist: NOT checked, said once for the whole batch",
                   len(warnings) == 1 and "NOT checked: could not start" in warnings[0],
                   result["missing_python_lines"])
            expect("... labels are left as they were (no verdict is not a pass)",
                   result["missing_python_labels"] == result["fixed_labels"], result["missing_python_labels"])
            expect("... and the summary counts them", any("4 NOT checked" in line
                                                          for line in result["missing_python_lines"]),
                   result["missing_python_lines"])
            expect("a gate that hangs is stopped at the timeout (5 s) and reported",
                   result["timeout_seconds"] < 30
                   and any("took longer than 5 s" in line for line in result["timeout_lines"]),
                   (result["timeout_seconds"], result["timeout_lines"]))
            expect("with the import check off, an import queues nothing", result["disabled_busy"] is False)
            received = json.loads(result["quoted_received"]) if result["quoted_received"].startswith("[") else None
            expect("arguments with spaces, quotes, trailing backslashes, empty and non-ASCII arrive intact",
                   received == result["quoted_sent"], (result["quoted_sent"], result["quoted_received"]))
            expect("the child environment drops PYTHONHOME/PYTHONSTARTUP and Unity's own PYTHONPATH entries",
                   result["clean_environment"] == {"KEEP": "1", "PYTHONPATH": "/studio/site"},
                   result["clean_environment"])
            expect("the command line is <gate> <file> --once --json --rules <nearest rules.toml>",
                   result["arguments"][1:] == [str(project / "Assets" / "Models" / "good.fbx"), "--once",
                                               "--json", "--rules", str(project / "rules.toml")]
                   and result["arguments"][0].endswith("asset_gatekeeper.py"), result["arguments"])
            expect("Test the gate prints the version", result["version"].startswith("asset-gatekeeper")
                   or agk.__version__ in result["version"], result["version"])

# --------------------------------------------------------------------------- #
if wanted("godot"):
    print("4. Godot: the add-on headless in tests/godot_project")
    godot = os.environ.get("GODOT") or shutil.which("godot") or shutil.which("godot4")
    if not godot:
        skip("Godot add-on headless run: no Godot 4.2+ editor ($GODOT or godot on PATH)")
    else:
        project = WORK / "godot_project"
        shutil.copytree(ROOT / "tests" / "godot_project", project)
        shutil.copytree(GODOT_ADDON, project / "addons" / "asset_gatekeeper")
        write_grid_obj(project / "models" / "good.obj", 4)        # 32 triangles
        write_grid_obj(project / "models" / "heavy.obj", 10)      # 200 triangles
        write_png(project / "textures" / "brick_Albedo.png", 64, 64)
        write_png(project / "textures" / "odd_Albedo.png", 100, 60)
        write_png(project / "addons" / "asset_gatekeeper" / "icon.png", 100, 60)
        (project / "notes.txt").write_text("not an asset\n", encoding="utf-8")
        (project / "rules.toml").write_text("[mesh]\nmax_triangles = 100\n", encoding="utf-8")
        bogus = WORK / "someone_elses_python"
        bogus.mkdir()
        env = dict(os.environ, AGK_TEST_PYTHON=sys.executable, AGK_TEST_HOME=str(ROOT),
                   PYTHONHOME=str(bogus))
        run = subprocess.run([godot, "--headless", "--path", str(project), "--script", "res://test_runner.gd"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=600, env=env)
        result = None
        for line in run.stdout.splitlines():
            if line.startswith("AGK_GODOT_RESULT "):
                result = json.loads(line[len("AGK_GODOT_RESULT "):])
        script_errors = [line for line in (run.stdout + run.stderr).splitlines()
                         if "SCRIPT ERROR" in line or "Parse Error" in line or "Compile Error" in line]
        expect("Godot ran the test script to the end without script errors",
               run.returncode == 0 and result is not None and not script_errors,
               (run.returncode, script_errors, run.stdout[-1500:], run.stderr[-1500:]))
        if result:
            print("       " + result.get("godot", "?"))
            expect("PYTHONHOME is dropped before the gate runs",
                   result["pythonhome_before"] is True and result["pythonhome_after"] is False, result)
            expect("the nearest rules.toml is the project's", result["rules_found"] ==
                   str(project / "rules.toml").replace("\\", "/"), result["rules_found"])
            expect("arguments: <gate> <file> --once --json --rules <rules.toml>",
                   result["arguments"][2:4] == ["--once", "--json"] and result["arguments"][4] == "--rules"
                   and result["arguments"][0].endswith("asset_gatekeeper.py"), result["arguments"])
            expect("is_gated: by extension, any case", result["is_gated"] == [True, False], result["is_gated"])
            expect("a good model passes", result["good"]["outcome"] == "passed"
                   and result["good"]["status"] == "PASSED", result["good"])
            expect("an over-budget model fails with the gate's line, under the project rules",
                   result["heavy"]["outcome"] == "failed"
                   and any("200 triangles exceeds the 100 budget" in p for p in result["heavy"]["problems"]),
                   result["heavy"])
            expect("a non-power-of-two texture fails", result["npot"]["outcome"] == "failed"
                   and any("power of two" in p for p in result["npot"]["problems"]), result["npot"])
            expect("a missing file is NOT checked with the reason",
                   result["missing"]["outcome"] == "not_checked"
                   and result["missing"]["reason"] == "source file not found", result["missing"])
            expect("a Python that does not exist is NOT checked, not passed",
                   result["no_python"]["outcome"] == "not_checked" and result["no_python"]["reason"],
                   result["no_python"])
            expect("output without a verdict is NOT checked, quoting the gate's last line",
                   result["parse_garbage"]["outcome"] == "not_checked"
                   and "ImportError: no trimesh" in result["parse_garbage"]["reason"], result["parse_garbage"])
            expect("the version check answers", agk.__version__ in result["version"]
                   or "asset-gatekeeper" in result["version"], result["version"])
            expect("plugin.gd loads (an EditorPlugin, in the editor binary)", result["plugin_loads"] is True)
            expect("the error line names the file and the gate's line",
                   result["failure_message"].startswith("AssetGatekeeper: res://models/heavy.obj FAILED: ")
                   and "200 triangles" in result["failure_message"], result["failure_message"])
            expect("the dock lists failures and NOT checked, not passes",
                   sorted(result["dock_after_three"]) == ["res://models/heavy.obj", "res://models/unconfigured.obj"],
                   result["dock_after_three"])
            expect("... and an asset that passes again leaves the list",
                   result["dock_after_fix"] == ["res://models/unconfigured.obj"], result["dock_after_fix"])
            expect("the queue takes the three art files, skips text and the add-on's own folder",
                   result["enqueued"] == 3 and result["enqueued_while_queued"] == 0, result)
            outcomes = {item["path"]: item["outcome"] for item in result["queue_results"]}
            expect("the background queue checked them: 1 failed, 2 passed",
                   outcomes == {"res://models/good.obj": "passed", "res://models/heavy.obj": "failed",
                                "res://textures/brick_Albedo.png": "passed"}, outcomes)
            expect("... and finished one batch with the counts",
                   len(result["queue_batches"]) == 1
                   and result["queue_batches"][0]["done"] == 3 and result["queue_batches"][0]["failed"] == 1,
                   result["queue_batches"])
            expect("unchanged files are not queued again", result["enqueued_unchanged"] == 0,
                   result["enqueued_unchanged"])

        # The editor itself: the add-on enables, the first import of the project
        # reaches it (resources_reimported), and it reports through push_error. The
        # gate is found through the environment variables, as a user without
        # Editor Settings entries would have it.
        editor_env = dict(env, ASSETGATEKEEPER_PYTHON=sys.executable, ASSETGATEKEEPER_HOME=str(ROOT))
        editor = subprocess.run([godot, "--headless", "--editor", "--path", str(project), "--quit-after", "600"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=600, env=editor_env)
        output = editor.stdout + editor.stderr
        addon_errors = [line for line in output.splitlines()
                        if ("SCRIPT ERROR" in line or "Parse Error" in line)
                        or ("asset_gatekeeper" in line and "ERROR" in line and "AssetGatekeeper:" not in line)]
        expect("the editor enables the add-on without script errors",
               "[AssetGatekeeper] import check active" in output and not addon_errors,
               (addon_errors, output[-2000:]))
        expect("the first import of the project reaches the add-on: the heavy model's error is printed",
               "AssetGatekeeper: res://models/heavy.obj FAILED: 200 triangles exceeds the 100 budget" in output,
               output[-2000:])
        expect("... the odd texture's too, and the batch summary counts both",
               "AssetGatekeeper: res://textures/odd_Albedo.png FAILED" in output
               and "[AssetGatekeeper] 4 checked: 2 passed, 2 failed" in output, output[-2000:])

print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
