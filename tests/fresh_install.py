#!/usr/bin/env python3
"""A first-time user's install, from the downloaded ZIP, start to finish.

    python tests/fresh_install.py          run from the root of an unzipped copy

CI unzips `git archive` of the commit (what GitHub's "Download ZIP" serves)
into an empty folder, on a Python with nothing installed, and runs this from
inside it. It does what the README tells a new user to do, in the same order,
through the same files:

1. Install_Extras.bat (Windows) / tools/install_extras.py: libraries in;
   running it a second time with --check finds nothing missing.
2. Rules.bat (Windows) / tools/agk_serve.py up: both pages answer.
3. The gate checks a folder once (--once --recursive) and the verdict is right.
4. --bake-polypaint turns a painted sculpt into a .glb + textures that pass.
5. Stop.bat / agk_serve.py down: nothing is left running, the folder can go.

Each .bat ends with `pause`; it is fed a newline so it returns. Exit 0 = a new
user gets a working tool; anything else names the step that broke.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path.cwd()
WINDOWS = os.name == "nt"
failures: list[str] = []


def expect(label: str, condition: bool, detail: str = "") -> None:
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""),
          flush=True)
    if not condition:
        failures.append(label)


def run(command, timeout=900, **kwargs) -> subprocess.CompletedProcess:
    print("  $ " + (command if isinstance(command, str) else " ".join(map(str, command))), flush=True)
    result = subprocess.run(command, cwd=ROOT, input="\n", text=True, capture_output=True,
                            timeout=timeout, **kwargs)
    tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
    for line in tail:
        print("    | " + line)
    return result


def bat(name: str, *args: str, timeout=900) -> subprocess.CompletedProcess:
    return run(["cmd", "/c", name, *args], timeout=timeout)


def get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status
    except Exception as exc:        # noqa: BLE001 - any failure is "not up"
        print(f"    {url}: {exc}")
        return 0


def painted_sphere(rings=40, segments=80) -> str:
    """A ZBrush-style Polypaint sphere: red top, blue bottom, #MRGB lines."""
    vertices, colours, faces = [], [], []
    for i in range(rings + 1):
        theta = math.pi * i / rings
        for j in range(segments):
            phi = 2 * math.pi * j / segments
            vertices.append((math.sin(theta) * math.cos(phi), math.cos(theta),
                             math.sin(theta) * math.sin(phi)))
            colours.append((220, 40, 40) if math.cos(theta) > 0 else (40, 60, 220))
    for i in range(rings):
        for j in range(segments):
            a, b = i * segments + j, i * segments + (j + 1) % segments
            faces += [(a, a + segments, b), (b, a + segments, b + segments)]
    lines = ["# File exported by ZBrush version 2022"]
    lines += [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in vertices]
    groups = [f"ff{r:02x}{g:02x}{b:02x}" for r, g, b in colours]
    lines += ["#MRGB " + "".join(groups[k:k + 64]) for k in range(0, len(groups), 64)]
    lines += [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in faces]
    return "\n".join(lines) + "\n"


def main() -> int:
    print(f"Fresh install in {ROOT} (Python {sys.version.split()[0]}, "
          f"{'Windows' if WINDOWS else sys.platform})")
    expect("this is a downloaded copy, not a git checkout", not (ROOT / ".git").exists())
    probe = subprocess.run([sys.executable, "-c", "import trimesh"], capture_output=True)
    expect("this Python starts with nothing installed", probe.returncode != 0)
    python = sys.executable

    print("1. install the libraries")
    started = time.time()
    done = bat("Install_Extras.bat") if WINDOWS else run([sys.executable, "tools/install_extras.py"])
    expect("the installer succeeds", done.returncode == 0, f"exit {done.returncode}")
    expect("... and says so", "everything is installed and loads" in done.stdout.lower())
    print(f"    ({time.time() - started:.0f} s)")
    # The .bat files pick their own Python (.venv, then py -3, then PATH), which
    # need not be the one running this; every later step uses the one they chose,
    # as a user's Rules.bat would.
    chosen = re.search(r"^Python \d+\.\d+ at (.+)$", done.stdout, re.M)
    python = chosen.group(1).strip() if chosen else sys.executable
    expect("the installer says which Python it used", bool(chosen))
    print(f"    using {python}")
    again = (bat("Install_Extras.bat", "--check") if WINDOWS
             else run([sys.executable, "tools/install_extras.py", "--check"]))
    expect("a second run finds nothing missing", again.returncode == 0
           and "MISSING" not in again.stdout and "not yet" not in again.stdout)

    print("2. start the pages")
    up = bat("Rules.bat") if WINDOWS else run([sys.executable, "tools/agk_serve.py", "up"])
    expect("Rules.bat starts both pages", up.returncode == 0, f"exit {up.returncode}")
    try:
        ports = json.loads((ROOT / ".agk" / "ports.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        ports = {}
        expect("the chosen ports are written down", False, str(exc))
    dashboard = (ports.get("dashboard") or {}).get("url", "http://127.0.0.1:8080")
    workstation = (ports.get("workstation") or {}).get("url", "http://127.0.0.1:8100")
    expect("the admin page answers", get(f"{dashboard}/admin") == 200, dashboard)
    expect("the review page answers", get(f"{workstation}/viewer") == 200, workstation)
    expect("the 3D viewer's library is served locally",
           get(f"{workstation}/static/vendor/three.module.js") == 200)

    with tempfile.TemporaryDirectory(prefix="agk-fresh-") as scratch:
        project = Path(scratch) / "MyGame" / "Assets"
        project.mkdir(parents=True)
        from_pil = subprocess.run([python, "-c", (
            "from PIL import Image;import sys;p=sys.argv[1];"
            "Image.new('RGB',(512,512),(120,90,60)).save(p+'/T_Crate_BaseColor.png');"
            "Image.new('RGB',(1000,1000),(120,90,60)).save(p+'/T_Bad_BaseColor.png')"),
            str(project)], capture_output=True, text=True)
        expect("test textures written", from_pil.returncode == 0, from_pil.stderr[-200:])

        print("3. check a folder once")
        gate = run([python, "asset_gatekeeper.py", str(project), "--once", "--recursive"])
        verdicts = gate.stdout + gate.stderr
        expect("the gate runs and reports a failure (exit 1)", gate.returncode == 1,
               f"exit {gate.returncode}")
        expect("the good texture passes", "T_Crate_BaseColor.png" in verdicts
               and "PASSED" in verdicts)
        expect("the 1000x1000 texture fails for the right reason",
               "1000x1000 is not a power of two" in verdicts)

        print("4. bake a Polypaint sculpt")
        sculpt = project / "SM_Ball_HP.obj"
        sculpt.write_text(painted_sphere(), encoding="utf-8")
        version = subprocess.run([python, "-c", "import sys;print(sys.version_info[:2] >= (3, 10))"],
                                 capture_output=True, text=True).stdout.strip()
        if version != "True":
            print("  skip  the bake needs Python 3.10+")
        else:
            bake = run([python, "asset_gatekeeper.py", str(sculpt), "--bake-polypaint",
                        "--bake-triangles", "2000", "--bake-texture", "256"])
            expect("the bake succeeds", bake.returncode == 0, f"exit {bake.returncode}")
            for name in ("SM_Ball_LP.glb", "T_Ball_LP_BaseColor.png", "T_Ball_LP_Normal.png"):
                expect(f"... and writes {name}", (project / name).is_file(),
                       str(sorted(p.name for p in project.iterdir())))
            n64 = run([python, "asset_gatekeeper.py", str(sculpt), "--bake-polypaint",
                       "--bake-preset", "n64"])
            expect("the N64 preset bakes too", n64.returncode == 0, f"exit {n64.returncode}")

    print("5. stop everything")
    down = bat("Stop.bat") if WINDOWS else run([sys.executable, "tools/agk_serve.py", "down"])
    expect("Stop.bat stops everything", down.returncode == 0, f"exit {down.returncode}")
    time.sleep(1)
    expect("the admin page is gone", get(f"{dashboard}/admin") == 0)
    expect("the review page is gone", get(f"{workstation}/viewer") == 0)

    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
