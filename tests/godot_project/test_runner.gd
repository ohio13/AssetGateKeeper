extends SceneTree
## Headless test of plugins/godot, run by tests/test_engine_plugins.py:
##
##     godot --headless --path <copy of this project> --script res://test_runner.gd
##
## It calls the add-on's own check code on the assets the Python test put in the
## project, then prints AGK_GODOT_RESULT {...} for the Python test to assert on.
## AGK_TEST_PYTHON / AGK_TEST_HOME name the gate's Python and folder;
## AGK_TEST_LOGGING_HOME the same gate behind a wrapper that appends each run's
## arguments to $AGK_HARNESS_GATE_LOG (how many runs a batch took).

const GateRunner := preload("res://addons/asset_gatekeeper/gate_runner.gd")
const CheckQueue := preload("res://addons/asset_gatekeeper/check_queue.gd")
const Dock := preload("res://addons/asset_gatekeeper/dock.gd")

var report := {}
var queue
var results := []
var batches := []
var started_msec := 0


func _runner(python: String) -> GateRunner:
	var runner := GateRunner.new()
	runner.python = python
	runner.gate_home = OS.get_environment("AGK_TEST_HOME")
	runner.project_root = ProjectSettings.globalize_path("res://")
	return runner


func _full(path: String) -> String:
	return ProjectSettings.globalize_path(path)


func _item(path: String) -> Dictionary:
	return {"path": path, "file": _full(path)}


func _take_runs() -> Array:
	var runs := []
	var log_path := OS.get_environment("AGK_HARNESS_GATE_LOG")
	if FileAccess.file_exists(log_path):
		for line in FileAccess.get_file_as_string(log_path).split("\n", false):
			runs.append(JSON.parse_string(line))
		DirAccess.remove_absolute(log_path)
	return runs


func _brief(results: Array) -> Array:
	var out := []
	for result in results:
		out.append({"path": result.path, "outcome": result.outcome, "status": result.status,
				"problems": result.problems, "reason": result.reason})
	return out


func _initialize() -> void:
	report.godot = Engine.get_version_info().string
	report.pythonhome_before = OS.has_environment("PYTHONHOME")
	GateRunner.clean_environment()
	report.pythonhome_after = OS.has_environment("PYTHONHOME")

	var runner := _runner(OS.get_environment("AGK_TEST_PYTHON"))
	report.arguments = Array(runner.build_arguments(_full("res://models/good.obj")))
	report.rules_found = runner.resolve_rules(_full("res://models/good.obj"))
	report.is_gated = [GateRunner.is_gated("res://a/B.FBX"), GateRunner.is_gated("res://a/b.txt")]

	# The blocking check, as the worker thread calls it.
	report.good = runner.check(_full("res://models/good.obj"), "res://models/good.obj")
	report.heavy = runner.check(_full("res://models/heavy.obj"), "res://models/heavy.obj")
	report.npot = runner.check(_full("res://textures/odd_Albedo.png"), "res://textures/odd_Albedo.png")
	report.missing = runner.check(_full("res://models/missing.obj"), "res://models/missing.obj")
	report.no_python = _runner(_full("res://no/python")).check(_full("res://models/good.obj"), "res://models/unconfigured.obj")
	report.version = runner.version()
	report.parse_garbage = GateRunner.parse(GateRunner.new_result("x", "x"), "Traceback\nImportError: no trimesh\n", 2)

	# Batches: several files per gate run.
	report.batch = _brief(runner.check_batch([_item("res://models/good.obj"), _item("res://models/heavy.obj"),
			_item("res://models/missing.obj"), _item("res://textures/brick_Albedo.png"),
			_item("res://textures/odd_Albedo.png")]))
	var direct := [GateRunner.new_result("res://models/good.obj", _full("res://models/good.obj")),
			GateRunner.new_result("res://models/heavy.obj", _full("res://models/heavy.obj")),
			GateRunner.new_result("res://models/missing.obj", _full("res://models/missing.obj"))]
	runner.run_batch(direct, _full("res://rules.toml"))
	report.batch_direct = _brief(direct)
	var many := []
	for i in 120:
		many.append(_full("res://loose/SM_%d.obj" % i))
	report.plan_counts = []
	for run in runner.plan_batches(many):
		report.plan_counts.append(run.indexes.size())
	var deep := _full("res://loose/" + "d".repeat(380))
	var long_paths := []
	for i in 60:
		long_paths.append(deep.path_join("SM_%d.obj" % i))
	report.plan_long_counts = []
	report.plan_long_chars = []
	for run in runner.plan_batches(long_paths):
		var files := []
		for index in run.indexes:
			files.append(long_paths[index])
		var chars: int = runner.resolve_python().length()
		for argument in runner.build_batch_arguments(files, run.rules):
			chars += 1 + argument.length() + (2 if " " in argument else 0)
		report.plan_long_counts.append(run.indexes.size())
		report.plan_long_chars.append(chars)
	report.plan_mixed = runner.plan_batches([_full("res://models/good.obj"), _full("res://textures/brick_Albedo.png"),
			_full("res://models/heavy.obj")])
	var canned := [GateRunner.new_result("a", "C:\\Proj\\Assets\\A.fbx"), GateRunner.new_result("b", "C:/Proj/Assets/b.png"),
			GateRunner.new_result("gone", "C:/Proj/Assets/gone.fbx"), GateRunner.new_result("none", "C:/Proj/Assets/none.obj")]
	var canned_text := "\n".join(PackedStringArray([
			JSON.stringify({"file": "c:/proj/assets/B.PNG", "name": "B.PNG", "passed": false, "status": "FAILED",
				"metric": "", "problems": ["too big"], "warnings": []}),
			"12:00:00 rules   C:\\Proj\\rules.toml",
			JSON.stringify({"file": "C:\\PROJ\\Assets\\a.fbx", "name": "a.fbx", "passed": true, "status": "PASSED",
				"metric": "12 tris", "problems": [], "warnings": []}),
			JSON.stringify({"file": "C:\\Proj\\Assets\\gone.fbx", "name": "gone.fbx", "passed": false, "status": "ERROR",
				"metric": "", "problems": ["not found"], "warnings": []}),
			JSON.stringify({"summary": {"total": 3, "passed": 1, "failed": 2, "totals_failed": []}})]))
	GateRunner.parse_batch(canned, canned_text, 1, true)
	report.parse_windows = _brief(canned)
	_take_runs()

	# The add-on's other scripts load and their pure parts work.
	var plugin_script = load("res://addons/asset_gatekeeper/plugin.gd")
	report.plugin_loads = plugin_script != null and plugin_script.can_instantiate()
	report.failure_message = plugin_script.failure_message(report.heavy) if plugin_script != null else ""
	var dock = Dock.new()
	dock.show_result(report.heavy)
	dock.show_result(report.no_python)
	dock.show_result(report.good)
	report.dock_after_three = dock.listed_paths()
	var fixed: Dictionary = report.heavy.duplicate()
	fixed.outcome = "passed"
	dock.show_result(fixed)
	report.dock_after_fix = dock.listed_paths()
	dock.free()

	# The background queue, as the plug-in drives it on import.
	queue = CheckQueue.new()
	queue.result_ready.connect(func(result): results.append(result))
	queue.batch_finished.connect(func(summary): batches.append(summary))
	var imported := ["res://models/good.obj", "res://models/heavy.obj", "res://textures/brick_Albedo.png",
			"res://notes.txt", "res://addons/asset_gatekeeper/icon.png"]
	var logging_runner := _runner(OS.get_environment("AGK_TEST_PYTHON"))
	logging_runner.gate_home = OS.get_environment("AGK_TEST_LOGGING_HOME")
	report.enqueued = queue.enqueue(imported, logging_runner, false, false)
	report.enqueued_while_queued = queue.enqueue(imported, logging_runner, false, false)
	report.busy_after_enqueue = queue.is_busy()
	started_msec = Time.get_ticks_msec()


func _process(_delta: float) -> bool:
	if batches.is_empty() and Time.get_ticks_msec() - started_msec < 120000:
		return false
	if not report.has("queue_results"):
		report.queue_results = results
		report.queue_batches = batches.duplicate()
		report.queue_runs = _take_runs()
		# Unchanged files are not checked again (an import that only touched .import files).
		report.enqueued_unchanged = queue.enqueue(["res://models/good.obj", "res://models/heavy.obj"], _runner(OS.get_environment("AGK_TEST_PYTHON")), false, false)
		queue.shutdown()
		print("AGK_GODOT_RESULT " + JSON.stringify(report))
		quit(0)
	return true
