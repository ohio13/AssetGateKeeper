@tool
extends EditorPlugin
## AssetGatekeeper for Godot 4.2+.
##
## * When Godot imports or reimports a model or texture (EditorFileSystem's
##   resources_reimported signal), the gate checks the source file on a background
##   thread. Failures go to the Output panel as errors (push_error) naming the file
##   and the gate's own words, and are listed in the AssetGatekeeper dock.
## * The dock's "Check project" button checks every model and texture in res://.
## * Settings: Editor > Editor Settings > Asset Gatekeeper (per user, because the
##   paths differ from machine to machine).

const GateRunner := preload("gate_runner.gd")
const CheckQueue := preload("check_queue.gd")
const Dock := preload("dock.gd")
const TAG := "AssetGatekeeper: "

const SETTINGS := {
	"asset_gatekeeper/check_on_import": [TYPE_BOOL, true, PROPERTY_HINT_NONE, ""],
	"asset_gatekeeper/gate_folder": [TYPE_STRING, "", PROPERTY_HINT_GLOBAL_DIR, ""],
	"asset_gatekeeper/python": [TYPE_STRING, "", PROPERTY_HINT_GLOBAL_FILE, ""],
	"asset_gatekeeper/rules_file": [TYPE_STRING, "", PROPERTY_HINT_GLOBAL_FILE, "*.toml"],
}

var _queue: RefCounted
var _dock: Control
var _reasons_shown := {}


func _enter_tree() -> void:
	_register_settings()
	GateRunner.clean_environment()
	_queue = CheckQueue.new()
	_queue.result_ready.connect(_on_result)
	_queue.batch_finished.connect(_on_batch_finished)
	_dock = Dock.new()
	_dock.check_project_pressed.connect(check_project)
	_dock.asset_activated.connect(_on_asset_activated)
	add_control_to_dock(DOCK_SLOT_RIGHT_UL, _dock)
	var filesystem := EditorInterface.get_resource_filesystem()
	filesystem.resources_reimported.connect(_on_resources_reimported)
	var runner := make_runner()
	var script := runner.resolve_gate_script()
	print("[AssetGatekeeper] import check %s - %s %s" % [
		"active" if _setting("check_on_import") else "off (Editor Settings > Asset Gatekeeper)",
		runner.resolve_python(), script if script != "" else "-m asset_gatekeeper"])


func _exit_tree() -> void:
	var filesystem := EditorInterface.get_resource_filesystem()
	if filesystem.resources_reimported.is_connected(_on_resources_reimported):
		filesystem.resources_reimported.disconnect(_on_resources_reimported)
	if _queue != null:
		_queue.shutdown()
		_queue = null
	if _dock != null:
		remove_control_from_docks(_dock)
		_dock.queue_free()
		_dock = null


func _register_settings() -> void:
	var editor_settings := EditorInterface.get_editor_settings()
	for key in SETTINGS:
		var spec: Array = SETTINGS[key]
		if not editor_settings.has_setting(key):
			editor_settings.set_setting(key, spec[1])
		editor_settings.set_initial_value(key, spec[1], false)
		editor_settings.add_property_info({"name": key, "type": spec[0], "hint": spec[2], "hint_string": spec[3]})


func _setting(setting: String):
	var key := "asset_gatekeeper/" + setting
	var editor_settings := EditorInterface.get_editor_settings()
	return editor_settings.get_setting(key) if editor_settings.has_setting(key) else SETTINGS[key][1]


## A gate_runner.gd with the current settings (made on the main thread, used by the worker).
func make_runner() -> GateRunner:
	var runner := GateRunner.new()
	runner.python = str(_setting("python"))
	runner.gate_home = str(_setting("gate_folder"))
	runner.rules_file = str(_setting("rules_file"))
	runner.project_root = ProjectSettings.globalize_path("res://")
	return runner


func _on_resources_reimported(resources: PackedStringArray) -> void:
	if not _setting("check_on_import"):
		return
	_start(resources, false, false)


## Check every model and texture under res:// (the dock's button).
func check_project() -> void:
	var paths: Array = []
	_collect("res://", paths)
	_dock.clear_results()
	if _start(paths, true, true) == 0:
		_dock.set_status("No models or textures to check in this project.")


func _start(paths, force: bool, manual: bool) -> int:
	if not _queue.is_busy():
		_reasons_shown.clear()
	var added: int = _queue.enqueue(paths, make_runner(), force, manual)
	if added > 0:
		var progress: Dictionary = _queue.progress()
		_dock.set_status("Checking %d of %d..." % [progress.done + 1, progress.total])
	return added


func _collect(folder: String, into: Array) -> void:
	var directory := DirAccess.open(folder)
	if directory == null:
		return
	directory.list_dir_begin()
	var entry := directory.get_next()
	while entry != "":
		if not entry.begins_with("."):
			var path := folder.path_join(entry)
			if directory.current_is_dir():
				if path != "res://addons":
					_collect(path, into)
			elif GateRunner.is_gated(path):
				into.append(path)
		entry = directory.get_next()
	directory.list_dir_end()


func _on_result(result: Dictionary) -> void:
	if _dock == null:
		return
	_dock.show_result(result)
	match result.outcome:
		"failed":
			push_error(failure_message(result))
		"passed":
			for warning in result.warnings:
				push_warning("%s%s note: %s" % [TAG, result.path, warning])
		_:
			# One line per distinct reason and batch: a missing Python would
			# otherwise print the same warning for every file in the project.
			if not _reasons_shown.has(result.reason):
				_reasons_shown[result.reason] = true
				push_warning("%s%s NOT checked: %s  (Editor Settings > Asset Gatekeeper)" % [TAG, result.path, result.reason])
	var progress: Dictionary = _queue.progress() if _queue != null else {}
	if not progress.is_empty() and progress.done < progress.total:
		_dock.set_status("Checking %d of %d..." % [progress.done + 1, progress.total])


static func failure_message(result: Dictionary) -> String:
	return "%s%s %s: %s" % [TAG, result.path, result.status, "; ".join(PackedStringArray(result.problems))]


func _on_batch_finished(summary: Dictionary) -> void:
	if _dock == null:
		return
	var text := "%d checked: %d passed, %d failed" % [summary.done, summary.passed, summary.failed]
	if summary.not_checked > 0:
		text += ", %d NOT checked" % summary.not_checked
	_dock.set_status(text)
	if summary.manual or summary.failed > 0 or summary.not_checked > 0:
		print("[AssetGatekeeper] " + text)


func _on_asset_activated(path: String) -> void:
	if path.begins_with("res://"):
		EditorInterface.select_file(path)
