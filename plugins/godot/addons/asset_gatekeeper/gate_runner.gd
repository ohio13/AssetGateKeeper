@tool
extends RefCounted
## Runs the AssetGatekeeper gate on files and reads their verdicts.
##
## No editor API in here, so a headless test can call it (tests/godot_project).
## The gate is the same command line the CLI, CI and the other plug-ins use:
##
##     <python> <gate folder>/asset_gatekeeper.py <file> [<file> ...] --once --json [--rules <rules.toml>]
##
## or `<python> -m asset_gatekeeper ...` when no gate folder is set (the pip
## install). It prints one JSON object per file on stdout, each with its "file",
## in the order given (several files: then a summary line, which is skipped),
## logs on stderr, and exits 0 (passed), 1 (failed) or 2 (bad setup).
##
## check_batch() checks up to MAX_BATCH_FILES files that share a rules.toml in
## one run: one Python start-up (~0.7 s) for all of them instead of one per file.

const ENV_PYTHON := "ASSETGATEKEEPER_PYTHON"
const ENV_HOME := "ASSETGATEKEEPER_HOME"
const ENV_RULES := "ASSETGATEKEEPER_RULES"
const RULES_FILE_NAME := "rules.toml"

## At most this many files per gate run: a run reports back only when it ends, and
## OS.execute cannot be interrupted, so this bounds the wait and what a crash takes down.
const MAX_BATCH_FILES := 50
## At most this many characters of command line. Windows' CreateProcess allows
## 32,767; 8,000 also stays below cmd.exe's 8,191, which applies when the Python
## setting is a .bat/.cmd shim (pyenv-win's python.bat).
const MAX_COMMAND_CHARS := 8000

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


## The arguments for several files that share one rules.toml ("" = none).
func build_batch_arguments(full_paths: Array, rules: String) -> PackedStringArray:
	var arguments := PackedStringArray()
	var script := resolve_gate_script()
	if script != "":
		arguments.append(script)
	else:
		arguments.append_array(["-m", "asset_gatekeeper"])
	for full_path in full_paths:
		arguments.append(str(full_path))
	arguments.append_array(["--once", "--json"])
	if rules != "":
		arguments.append_array(["--rules", rules])
	return arguments


## Split files into gate runs: grouped by the rules.toml each one uses (order kept
## within a group), then cut at max_files files or max_chars characters of command
## line. A file too long for the limit alone still gets a run of its own.
## Returns [{"rules": String, "indexes": [int, ...]}, ...].
func plan_batches(full_paths: Array, max_files := MAX_BATCH_FILES, max_chars := MAX_COMMAND_CHARS) -> Array:
	var groups := []
	var by_rules := {}
	for i in full_paths.size():
		var rules := resolve_rules(str(full_paths[i]))
		if not by_rules.has(rules):
			by_rules[rules] = []
			groups.append(rules)
		by_rules[rules].append(i)
	var fixed := _quoted_length(resolve_python()) + 1
	for argument in build_batch_arguments([], ""):
		fixed += _quoted_length(argument) + 1
	var runs := []
	for rules in groups:
		var base: int = fixed + ((" --rules ".length() + _quoted_length(rules)) if rules != "" else 0)
		var run: Dictionary = {}
		var length := 0
		for index in by_rules[rules]:
			var extra := 1 + _quoted_length(str(full_paths[index]))
			if run.is_empty() or run.indexes.size() >= maxi(1, max_files) or length + extra > max_chars:
				run = {"rules": rules, "indexes": []}
				runs.append(run)
				length = base
			run.indexes.append(index)
			length += extra
	return runs


## Length of an argument as Windows quotes it (a safe upper bound elsewhere).
static func _quoted_length(argument: String) -> int:
	if argument != "" and not (" " in argument or "\t" in argument or "\"" in argument):
		return argument.length()
	return argument.length() + 2 + argument.count("\"") + argument.count("\\")


## A path as a key for matching the gate's "file" back to the file sent:
## forward slashes, simplified, no trailing slash; on Windows also case.
static func path_key(path: String, windows: bool) -> String:
	var key := path.replace("\\", "/") if windows else path
	key = key.simplify_path().trim_suffix("/")
	return key.to_lower() if windows else key


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


## Check several files: items are [{"path": res:// path, "file": absolute path}].
## Blocking: call it from a thread (check_queue.gd does). Returns one result per
## item, in the same order, as check() would give them one at a time.
func check_batch(items: Array) -> Array:
	var results := []
	var present := []
	for item in items:
		var result := new_result(str(item.get("path", item.file)), str(item.file))
		results.append(result)
		if not FileAccess.file_exists(result.file):
			result.reason = "source file not found"
		else:
			present.append(result)
	if present.is_empty():
		return results
	var script := resolve_gate_script()
	if script != "" and not FileAccess.file_exists(script):
		for result in present:
			result.reason = "the gate is not at %s (check the gate folder setting or %s)" % [script, ENV_HOME]
		return results
	var full_paths := []
	for result in present:
		full_paths.append(result.file)
	for run in plan_batches(full_paths):
		var batch := []
		for index in run.indexes:
			batch.append(present[index])
		run_batch(batch, run.rules)
	return results


## One gate run for results whose files share the rules file ("" = none).
func run_batch(batch: Array, rules: String) -> void:
	if batch.is_empty():
		return
	var full_paths := []
	for result in batch:
		full_paths.append(result.file)
	var output := []
	var executable := resolve_python()
	var code := OS.execute(executable, build_batch_arguments(full_paths, rules), output, true, false)
	var text := "" if output.is_empty() else str(output[0])
	if code == -1 and text.strip_edges() == "":
		for result in batch:
			result.reason = "could not start %s; set the Python executable in Editor Settings > Asset Gatekeeper" % executable
		return
	if batch.size() == 1:
		parse(batch[0], text, code)
	else:
		parse_batch(batch, text, code, OS.get_name() == "Windows")


## Read a run over several files: each JSON line with "problems" and "file" is the
## verdict for the result whose path_key matches. A file the gate resolved to another
## spelling (a symlink, an 8.3 name) is matched by position when there is exactly one
## verdict line per file and the names agree. A file with no line is "not_checked",
## with the gate's last line as the reason.
static func parse_batch(batch: Array, text: String, code: int, windows: bool) -> void:
	var verdicts := []
	var lines := text.split("\n")
	for raw in lines:
		var line := raw.strip_edges()
		if not line.begins_with("{"):
			continue
		var verdict = JSON.parse_string(line)
		if typeof(verdict) == TYPE_DICTIONARY and verdict.has("problems") and verdict.has("file"):
			verdicts.append(verdict)
	var by_key := {}
	for i in verdicts.size():
		var key := path_key(str(verdicts[i].file), windows)
		if not by_key.has(key):
			by_key[key] = i
	var used := []
	used.resize(verdicts.size())
	used.fill(false)
	var detail := ""       # the gate's last log line (its stderr comes in the same text)
	for i in range(lines.size() - 1, -1, -1):
		var line := lines[i].strip_edges()
		if line != "" and not line.begins_with("{"):
			detail = line.left(300)
			break
	for i in batch.size():
		var result: Dictionary = batch[i]
		var found: int = by_key.get(path_key(result.file, windows), -1)
		if found < 0 or used[found]:
			found = -1
			if verdicts.size() == batch.size() and not used[i]:
				var name := str(verdicts[i].get("name", ""))
				var own: String = result.file.get_file()
				if name == own or (windows and name.to_lower() == own.to_lower()):
					found = i
		result.exit = code
		if found < 0:
			result.outcome = "not_checked"
			result.reason = "the gate gave no verdict (exit %d)%s" % [code, (": " + detail) if detail != "" else ""]
			continue
		used[found] = true
		_read_verdict(result, verdicts[found], code)


static func _read_verdict(result: Dictionary, verdict: Dictionary, code: int) -> void:
	var passed: bool = verdict.get("passed", false) == true
	result.outcome = "passed" if passed else "failed"
	result.status = str(verdict.get("status", "PASSED" if passed else "FAILED"))
	result.metric = str(verdict.get("metric", ""))
	result.problems = _strings(verdict.get("problems", []))
	result.warnings = _strings(verdict.get("warnings", []))
	if not passed and result.problems.is_empty():
		result.problems = ["failed (exit %d) without a reason" % code]


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
		_read_verdict(result, verdict, code)
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
