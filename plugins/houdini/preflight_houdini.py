"""Headless pre-flight probe for Houdini, driving tools/houdini_export_hook.py.

    hython plugins/houdini/preflight_houdini.py --out result.json --preset retro boss.obj cube.obj

This exists to prove the thing a studio actually relies on: Houdini's bundled
interpreter has neither trimesh nor Pillow, so the hook shells out to the
interpreter named by ``AGK_PYTHON``. If that environment variable is not honoured
the probe says so instead of quietly passing.

Exit codes: 0 all gated files passed, 1 at least one failed its budget, 2 the
harness failed.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

HOOK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools", "houdini_export_hook.py")


def parse_args(argv):
    options = {"out": "", "preset": "", "rules": "", "files": []}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--") and index + 1 < len(argv):
            key = token[2:].replace("-", "_")
            if key == "files":
                options["files"] = argv[index + 1:]
                break
            options[key] = argv[index + 1]
            index += 2
            continue
        if not token.startswith("-"):
            options["files"].append(token)
        index += 1
    return options


def load_hook():
    import importlib.util

    spec = importlib.util.spec_from_file_location("agk_houdini_hook", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agk_houdini_hook"] = module
    spec.loader.exec_module(module)
    return module


def main(argv) -> int:
    options = parse_args(argv)
    started = time.time()
    result = {"dcc": "houdini", "version": "", "interpreter": sys.executable,
              "agk_python": os.environ.get("AGK_PYTHON", ""), "results": [], "exit": 2}
    try:
        try:
            import hou                                   # noqa: F401
            result["version"] = hou.applicationVersionString()
            result["has_hou"] = True
        except Exception:                                # pragma: no cover
            result["has_hou"] = False
        hook = load_hook()
        extra = []
        if options["preset"]:
            extra += ["--preset", options["preset"]]
        if options["rules"]:
            extra += ["--rules", options["rules"]]
        files = [os.path.abspath(path) for path in options["files"]]
        verdicts = hook.verify_export(files, extra_args=extra) if files else []
        result["results"] = [{
            "path": os.path.basename(item["path"]),
            "returncode": item["returncode"],
            "status": (item.get("verdict") or {}).get("status"),
            "problems": (item.get("verdict") or {}).get("problems") or [],
        } for item in verdicts]
        failed = [item for item in result["results"] if item["returncode"] == 1]
        errors = [item for item in result["results"] if item["returncode"] not in (0, 1)]
        result["exit"] = 2 if errors else (1 if failed else 0)
    except Exception as exc:                             # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["exit"] = 2
    finally:
        result["duration_ms"] = int((time.time() - started) * 1000)
        if options["out"]:
            with open(options["out"], "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2)
        print("AGK_PROBE_RESULT " + json.dumps(result.get("exit")))
    return int(result["exit"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
