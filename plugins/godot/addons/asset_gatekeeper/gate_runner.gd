@tool
extends RefCounted
## Runs the AssetGatekeeper gate on one file and reads its verdict.
##
## No editor API in here, so a headless test can call it (tests/godot_project).
## The gate is the same command line the CLI, CI and the other plug-ins use:
##
##     <python> <gate folder>/asset_gatekeeper.py <file> --once --json [--rules <rules.toml>]
##
## or `<python> -m asset_gatekeeper ...` when no gate folder is set (the pip
## install). It prints one JSON object for the file on stdout, logs on stderr,
## and exits 0 (passed), 1 (failed) or 2 (bad setup).

const ENV_PYTHON := "ASSETGATEKEEPER_PYTHON"
const ENV_HOME := "ASSETGATEKEEPER_HOME"
const ENV_RULES := "ASSETGATEKEEPER_RULES"
const RULES_FILE_NAME := "rules.toml"

## Source formats the gate measures (its TRACKED_SUFFIXES). Anything else is left alone.
const GATED_EXTENSIONS := ["fbx", "obj", "gltf", "glb",
		"png", "tga", "jpg", "jpeg", "tif", "tiff", "webp", "dds", "exr", "ktx2"]

## Variables that point a child Python at some other install. Inherited by the
## gate's interpreter, they make it load the wrong standard library and die
## before measuring anything (seen in Unreal when the editor was started from Houdini).
const INHERITED_PYTHON_VARIABLES := ["PYTHONHOME", "PYTHONEXECUTABLE", "PYTHONSTARTUP",
		"__PYVENV_LAUNCHER__"]

## Python with trimesh + Pillow. Empty: ASSETGATEKEEPER_PYTHON, else python / python3.
var python := ""
## Folder holding asset_gatekeeper.py. Empty: ASSETGATEKEEPER_HOME, else `python -m asset_gatekeeper`.
var gate_home := ""
## One rules.toml for every file. Empty: ASSETGATEKEEPER_RULES, else the nearest rules.toml.
var rules_file := ""
## Highest folder the rules.toml search climbs to (the project folder, absolute).
var project_root := ""


static func is_gated(path: String) -> bool:
	return path.get_extension().to_lower() in GATED_EXTENSIONS


## Drop the variables above from this process, so every child inherits a clean
## environment (Godot itself has no Python, so nothing here needs them).
static func clean_environment() -> void:
	for name in INHERITED_PYTHON_VARIABLES:
		if OS.has_environment(name):
			OS.unset_environment(name)
	OS.set_environment("PYTHONIOENCODING", "utf-8")


func resolve_python() -> String:
	if python.strip_edges() != "":
		return python.strip_edges()
	var from_env := OS.get_environment(ENV_PYTHON).strip_edges()
	if from_env != "":
		return from_env
	return "python" if OS.get_name() == "Windows" else "python3"


## The gate script, or "" for `python -m asset_gatekeeper`.
func resolve_gate_script() -> String:
	var home := gate_home.strip_edges()
	if home == "":
		home = OS.get_environment(ENV_HOME).strip_edges()
	if home == "":
		return ""
	if home.to_lower().ends_with(".py"):
		return home
	return home.path_join("asset_gatekeeper.py")


## The rules.toml for this file: the setting, ASSETGATEKEEPER_RULES, or the nearest
## rules.toml in the file's folder or a parent, up to the project folder.
## "" = the gate's built-in defaults.
func resolve_rules(full_path: String) -> String:
	var explicit := rules_file.strip_edges()
	if explicit == "":
		explicit = OS.get_environment(ENV_RULES).strip_edges()
	if explicit != "":
		return explicit
	var stop := project_root.simplify_path().trim_suffix("/")
	var folder := full_path.simplify_path().get_base_dir()
	while folder != "":
		var candidate := folder.path_join(RULES_FILE_NAME)
		if FileAccess.file_exists(candidate):
			return candidate
		if stop == "" or _same_path(folder, stop):
			break
		var parent := folder.get_base_dir()
		if parent == folder:
			break
		folder = parent
	return ""


static func _same_path(a: String, b: String) -> bool:
	a = a.trim_suffix("/")
	b = b.trim_suffix("/")
	return a.to_lower() == b.to_lower() if OS.get_name() == "Windows" else a == b


## The arguments after the interpreter (Godot quotes them itself).
func build_arguments(full_path: String) -> PackedStringArray:
	var arguments := PackedStringArray()
	var script := resolve_gate_script()
	if script != "":
		arguments.append(script)
	else:
		arguments.append_array(["-m", "asset_gatekeeper"])
	arguments.append_array([full_path, "--once", "--json"])
	var rules := resolve_rules(full_path)
	if rules != "":
		arguments.append_array(["--rules", rules])
	return arguments


static func new_result(path: String, full_path: String) -> Dictionary:
	return {
		"path": path, "file": full_path,
		# "passed", "failed" (FAILED or ERROR), or "not_checked" (no verdict)
		"outcome": "not_checked",
		"status": "", "metric": "", "problems": [], "warnings": [],
		"reason": "", "exit": -1,
	}


## Check one file. Blocking: call it from a thread (check_queue.gd does).
## Never fails: a gate that cannot say is "not_checked" with the reason.
func check(full_path: String, path := "") -> Dictionary:
	var result := new_result(path if path != "" else full_path, full_path)
	if not FileAccess.file_exists(full_path):
		result.reason = "source file not found"
		return result
	var script := resolve_gate_script()
	if script != "" and not FileAccess.file_exists(script):
		result.reason = "the gate is not at %s (check the gate folder setting or %s)" % [script, ENV_HOME]
		return result
	var output := []
	var executable := resolve_python()
	var code := OS.execute(executable, build_arguments(full_path), output, true, false)
	var text := "" if output.is_empty() else str(output[0])
	if code == -1 and text.strip_edges() == "":
		result.reason = "could not start %s; set the Python executable in Editor Settings > Asset Gatekeeper" % executable
		return result
	return parse(result, text, code)


## Read the gate's output: the last JSON object that has "problems" is the verdict
## (the same rule as the Unreal and Unity plug-ins). None is "not_checked", with
## the last line the gate wrote as the reason (e.g. "missing dependencies: trimesh").
static func parse(result: Dictionary, text: String, code: int) -> Dictionary:
	result.exit = code
	var lines := text.split("\n")
	for i in range(lines.size() - 1, -1, -1):
		var line := lines[i].strip_edges()
		if not line.begins_with("{"):
			continue
		var verdict = JSON.parse_string(line)
		if typeof(verdict) != TYPE_DICTIONARY or not verdict.has("problems"):
			continue
		var passed: bool = verdict.get("passed", false) == true
		result.outcome = "passed" if passed else "failed"
		result.status = str(verdict.get("status", "PASSED" if passed else "FAILED"))
		result.metric = str(verdict.get("metric", ""))
		result.problems = _strings(verdict.get("problems", []))
		result.warnings = _strings(verdict.get("warnings", []))
		if not passed and result.problems.is_empty():
			result.problems = ["failed (exit %d) without a reason" % code]
		return result
	var detail := ""
	for i in range(lines.size() - 1, -1, -1):
		if lines[i].strip_edges() != "":
			detail = lines[i].strip_edges().left(300)
			break
	result.outcome = "not_checked"
	result.reason = "the gate gave no verdict (exit %d)%s" % [code, (": " + detail) if detail != "" else ""]
	return result


static func _strings(value) -> Array:
	var out := []
	if typeof(value) == TYPE_ARRAY:
		for item in value:
			out.append(str(item))
	return out


## `--version`, for a quick setup check. Returns the reply or why it failed.
func version() -> String:
	var arguments := PackedStringArray()
	var script := resolve_gate_script()
	if script != "":
		if not FileAccess.file_exists(script):
			return "the gate is not at " + script
		arguments.append(script)
	else:
		arguments.append_array(["-m", "asset_gatekeeper"])
	arguments.append("--version")
	var output := []
	var code := OS.execute(resolve_python(), arguments, output, true, false)
	var text := "" if output.is_empty() else str(output[0]).strip_edges()
	return text if code == 0 else "exit %d: %s" % [code, text.get_slice("\n", text.count("\n"))]
