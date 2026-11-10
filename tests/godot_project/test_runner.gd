extends SceneTree
## Headless test of plugins/godot, run by tests/test_engine_plugins.py:
##
##     godot --headless --path <copy of this project> --script res://test_runner.gd
##
## It calls the add-on's own check code on the assets the Python test put in the
## project, then prints AGK_GODOT_RESULT {...} for the Python test to assert on.
## AGK_TEST_PYTHON / AGK_TEST_HOME name the gate's Python and folder.

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
	report.enqueued = queue.enqueue(imported, runner, false, false)
	report.enqueued_while_queued = queue.enqueue(imported, runner, false, false)
	report.busy_after_enqueue = queue.is_busy()
	started_msec = Time.get_ticks_msec()


func _process(_delta: float) -> bool:
	if batches.is_empty() and Time.get_ticks_msec() - started_msec < 120000:
		return false
	if not report.has("queue_results"):
		report.queue_results = results
		report.queue_batches = batches.duplicate()
		# Unchanged files are not checked again (an import that only touched .import files).
		report.enqueued_unchanged = queue.enqueue(["res://models/good.obj", "res://models/heavy.obj"], _runner(OS.get_environment("AGK_TEST_PYTHON")), false, false)
		queue.shutdown()
		print("AGK_GODOT_RESULT " + JSON.stringify(report))
		quit(0)
	return true
