"""
AssetGatekeeper — Blender add-on
================================

Gives artists the verdict **inside the viewport**, before they hit export:
select objects, press "Run Gatekeeper Check", and the problems appear in red in
the 3D Viewport sidebar.

Install (legacy add-on path, works in 3.6 → 4.2+):
    Edit > Preferences > Add-ons > Install… > pick this file, tick the checkbox.

Then find the panel in the 3D Viewport sidebar (N) under the "AssetGatekeeper"
tab.

Engine requirements
-------------------
The engine (`asset_gatekeeper.py`) needs `trimesh` + `Pillow`. By default this
add-on runs it with Blender's own Python (`sys.executable`). Two options:

1. Install the deps into Blender's Python once:
       <blender>/4.2/python/bin/python.exe -m pip install trimesh pillow watchdog
2. Or point the panel's "Engine python" field at any system Python that has them.

The engine script path defaults to `$ASSETGATEKEEPER_HOME/asset_gatekeeper.py`.
"""

bl_info = {
    "name": "AssetGatekeeper",
    "author": "AssetGatekeeper contributors",
    "version": (1, 1, 1),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > AssetGatekeeper",
    "description": "Validate the active selection against the asset budget before exporting",
    "category": "Pipeline",
}

import json
import os
import shutil
import subprocess
import sys
import tempfile

import bpy

ENV_PYTHON = "ASSETGATEKEEPER_PYTHON"
ENV_HOME = "ASSETGATEKEEPER_HOME"
TEMP_EXPORT_NAME = "agk_selection.gltf"
RULES_FILENAME = "rules.toml"
# A project keeps its budget at the root; the .blend usually sits in a
# department folder two or three levels down (project/assets/props/player).
RULES_SEARCH_DEPTH = 3

# Accept both spellings so users can set either env var.
ENV_RULES = ("ASSETGATEKEEPER_RULES", "ASSETGATEKEEPER_RULES_PATH")


# --------------------------------------------------------------------------- #
# Engine bridge
# --------------------------------------------------------------------------- #

def default_engine_path() -> str:
    home = os.environ.get(ENV_HOME, "")
    return os.path.join(home, "asset_gatekeeper.py") if home else "asset_gatekeeper.py"


def default_rules_path() -> str:
    for name in ENV_RULES:
        if os.environ.get(name):
            return os.environ[name]
    return ""


def project_rules_path(depth: int = RULES_SEARCH_DEPTH) -> str:
    """The ``rules.toml`` that governs this .blend, or "" when there is none.

    The add-on used to run the engine on a *temporary* export with no rules at
    all unless someone had typed a path into the panel, so the studio budget was
    silently replaced by the shipped defaults: the viewport said PASSED and the
    export failed a minute later. The engine's own convention is
    ``<project>/rules.toml``, so look there - next to the .blend first, then up.
    """
    blend = getattr(bpy.data, "filepath", "") or ""
    if not blend:
        return ""
    folder = os.path.dirname(os.path.abspath(blend))
    for _ in range(depth + 1):
        candidate = os.path.join(folder, RULES_FILENAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(folder)
        if parent == folder:            # filesystem root: stop
            break
        folder = parent
    return ""


def effective_rules_path(settings) -> str:
    """Explicit setting, then the environment, then the project file."""
    return settings.rules_path or default_rules_path() or project_rules_path()


_INHERITED_PYTHON_VARS = ("PYTHONHOME", "PYTHONEXECUTABLE", "PYTHONSTARTUP",
                          "__PYVENV_LAUNCHER__")


def child_environment(environ=None) -> dict:
    """The environment for the engine's interpreter.

    Verified in Blender 4.0.2: with Blender started by another application that
    had set PYTHONHOME (a pipeline launcher, or another DCC - here Houdini 22),
    the engine's Python loaded that application's standard library and every
    check failed with an engine traceback. Interpreter variables, and PYTHONPATH
    entries inside Blender's own Python, are dropped; the studio's own
    PYTHONPATH entries are kept.
    """
    env = dict(os.environ if environ is None else environ)
    for key in _INHERITED_PYTHON_VARS:
        env.pop(key, None)
    roots = {os.path.normcase(os.path.abspath(root))
             for root in (sys.prefix, sys.base_prefix, sys.exec_prefix) if root}

    def inside(entry: str) -> bool:
        candidate = os.path.normcase(os.path.abspath(entry))
        return any(candidate == root or candidate.startswith(root + os.sep) for root in roots)

    kept = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep)
            if entry and not inside(entry)]
    if kept:
        env["PYTHONPATH"] = os.pathsep.join(kept)
    else:
        env.pop("PYTHONPATH", None)
    return env


def run_engine(asset_path: str, python_exe: str, engine_path: str,
               rules_path: str = "", timeout: float = 180.0) -> tuple[dict | None, str]:
    """Run the engine over one file and return (verdict dict, error text)."""
    command = [python_exe, engine_path, asset_path, "--json"]
    if rules_path:
        command += ["--rules", rules_path]

    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout,
                                   env=child_environment())
    except FileNotFoundError:
        return None, f"interpreter not found: {python_exe}"
    except subprocess.TimeoutExpired:
        return None, f"engine timed out after {timeout:.0f}s"

    # The engine writes one JSON object per file; logs go to stderr.
    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "problems" in payload:
                return payload, ""
            break
    return None, (completed.stderr.strip() or completed.stdout.strip() or "no output")


def export_selection(directory: str) -> str:
    """Write the selected objects to a temporary .gltf and return its path."""
    path = os.path.join(directory, TEMP_EXPORT_NAME)
    try:
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLTF_SEPARATE",
                                  use_selection=True, export_apply=True)
    except AttributeError:
        raise RuntimeError(
            "glTF exporter unavailable — enable the 'Import-Export: glTF 2.0 format' "
            "add-on in Preferences > Add-ons"
        )
    if not os.path.isfile(path):
        raise RuntimeError("export produced no file (is anything selected?)")
    return path


# --------------------------------------------------------------------------- #
# Settings + operator
# --------------------------------------------------------------------------- #

class AGK_Settings(bpy.types.PropertyGroup):
    python_exe: bpy.props.StringProperty(
        name="Engine python", description="Interpreter that has trimesh + Pillow",
        default="", subtype="FILE_PATH")
    engine_path: bpy.props.StringProperty(
        name="Engine script", description="Path to asset_gatekeeper.py",
        default="", subtype="FILE_PATH")
    rules_path: bpy.props.StringProperty(
        name="rules.toml", description="Optional budget file", default="",
        subtype="FILE_PATH")


class AGK_OT_check_selection(bpy.types.Operator):
    """Check the selected objects against the studio asset budget"""

    bl_idname = "agk.check_selection"
    bl_label = "Run Gatekeeper Check"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.agk_settings
        python_exe = settings.python_exe or os.environ.get(ENV_PYTHON) or sys.executable
        engine_path = settings.engine_path or default_engine_path()
        # The panel shows this too, so "why is it passing?" has a visible answer.
        rules_path = effective_rules_path(settings)

        if not os.path.isfile(engine_path):
            message = f"engine not found: {engine_path}"
            context.scene["agk_report"] = json.dumps(
                {"passed": False, "problems": [message], "metric": ""})
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        if not context.selected_objects:
            self.report({"WARNING"}, "Select at least one object first")
            return {"CANCELLED"}

        directory = tempfile.mkdtemp(prefix="agk_")
        try:
            asset = export_selection(directory)
            verdict, error = run_engine(asset, python_exe, engine_path, rules_path)
        except RuntimeError as exc:
            verdict, error = None, str(exc)
        finally:
            # A temp export per click adds up fast on a shared drive; the verdict
            # already carries everything the panel needs.
            shutil.rmtree(directory, ignore_errors=True)

        if verdict is None:
            context.scene["agk_report"] = json.dumps(
                {"passed": False, "problems": [f"engine error: {error}"], "metric": ""})
            self.report({"ERROR"}, "AssetGatekeeper could not run — see the panel")
            return {"CANCELLED"}

        context.scene["agk_report"] = json.dumps(verdict)
        if verdict["passed"]:
            self.report({"INFO"}, f"PASSED  {verdict.get('metric', '')}")
            return {"FINISHED"}

        self.report({"ERROR"}, f"{len(verdict['problems'])} problem(s) — see the panel")
        return {"FINISHED"}


# --------------------------------------------------------------------------- #
# Panel
# --------------------------------------------------------------------------- #

def wrap_text(text: str, width_px: float, ui_scale: float = 1.0) -> list:
    """Split a label into lines that fit the sidebar.

    Blender labels never wrap; they are cut with "..." in the middle. In a
    default-width sidebar (Blender 4.0.2) "159,200 triangles exceeds the 50,000
    budget (+109,200)" showed as "159,200 tri... (+109,200)" - the reason was
    the part that disappeared.
    """
    import textwrap

    usable = max(60.0, width_px - 60.0 * ui_scale)   # icon, margins, panel border
    per_line = max(14, int(usable / (7.0 * ui_scale)))
    return textwrap.wrap(text, per_line) or [""]


def _wrapped_label(layout, text: str, context, icon: str = "NONE", alert: bool = False) -> None:
    region = getattr(context, "region", None)
    width = getattr(region, "width", 300) or 300
    scale = getattr(getattr(context.preferences, "system", None), "ui_scale", 1.0) or 1.0
    for index, line in enumerate(wrap_text(text, width, scale)):
        row = layout.row()
        row.alert = alert
        row.label(text=line, icon=icon if index == 0 else "BLANK1")


class AGK_PT_panel(bpy.types.Panel):
    bl_label = "AssetGatekeeper"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AssetGatekeeper"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.agk_settings

        layout.operator(AGK_OT_check_selection.bl_idname, icon="CHECKMARK")

        report_raw = context.scene.get("agk_report")
        if report_raw:
            try:
                report = json.loads(report_raw)
            except json.JSONDecodeError:
                report = None

            if report:
                box = layout.box()
                if report.get("passed"):
                    row = box.row()
                    row.label(text="PASSED", icon="CHECKMARK")
                    if report.get("metric"):
                        _wrapped_label(box, report["metric"], context)
                else:
                    # `alert` is what tints a sub-layout red in Blender's theme.
                    column = box.column(align=True)
                    column.alert = True
                    column.label(text="FAILED", icon="CANCEL")
                    for problem in report.get("problems", []):
                        _wrapped_label(column, problem, context, icon="ERROR", alert=True)
                    if report.get("metric"):
                        _wrapped_label(box, f"measured: {report['metric']}", context)

        column = layout.column(align=True)
        column.label(text="Engine")
        # Labels above the fields: beside them, the sidebar cut every one of
        # them to "Engin..." (Blender 4.0.2, default width).
        for prop, label in (("python_exe", "Engine python"), ("engine_path", "Engine script"),
                            ("rules_path", "rules.toml")):
            column.label(text=label)
            column.prop(settings, prop, text="")
        active_rules = effective_rules_path(settings)
        if active_rules:
            _wrapped_label(column, "budget: " + os.path.basename(os.path.dirname(active_rules))
                           + "/" + os.path.basename(active_rules), context, icon="FILE")
        else:
            _wrapped_label(column, "budget: shipped defaults (no rules.toml found)",
                           context, icon="INFO")


CLASSES = (AGK_Settings, AGK_OT_check_selection, AGK_PT_panel)


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.agk_settings = bpy.props.PointerProperty(type=AGK_Settings)
    bpy.types.Scene.agk_report = bpy.props.StringProperty(default="")


def unregister() -> None:
    del bpy.types.Scene.agk_report
    del bpy.types.Scene.agk_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
