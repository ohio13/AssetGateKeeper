@tool
extends RefCounted
## A queue of files checked on a background thread, so the editor never waits
## for the gate: up to GateRunner.MAX_BATCH_FILES files per gate run, split by
## rules.toml (gate_runner.gd check_batch). Results come back on the main thread
## through the signals. No editor API in here, so a headless test can drive it.

signal result_ready(result: Dictionary)
signal batch_finished(summary: Dictionary)

const GateRunner := preload("gate_runner.gd")

var _mutex := Mutex.new()
var _pending: Array = []        # [{path, file, runner}]
var _queued := {}               # res:// path -> true
var _stamps := {}               # res:// path -> modified time when last checked
var _thread: Thread = null
var _running := false
var _stopping := false
var _batch := {"total": 0, "done": 0, "passed": 0, "failed": 0, "not_checked": 0, "manual": false}


## Queue res:// (or absolute) paths. force: check even if unchanged since the last check.
## runner: a gate_runner.gd with the settings to use. Returns how many were added.
func enqueue(paths, runner, force := false, manual := false) -> int:
	var added := 0
	_mutex.lock()
	if _batch.done >= _batch.total:
		_batch = {"total": 0, "done": 0, "passed": 0, "failed": 0, "not_checked": 0, "manual": false}
	for raw in paths:
		var path := str(raw)
		if not GateRunner.is_gated(path) or _queued.has(path):
			continue
		if path.begins_with("res://addons/") or path.begins_with("res://.godot/"):
			continue
		var full := ProjectSettings.globalize_path(path)
		var stamp := FileAccess.get_modified_time(full) if FileAccess.file_exists(full) else 0
		if not force and stamp != 0 and _stamps.get(path, -1) == stamp:
			continue
		_stamps[path] = stamp
		_queued[path] = true
		_pending.append({"path": path, "file": full, "runner": runner})
		added += 1
	if added > 0:
		_stopping = false
		_batch.total += added
		_batch.manual = _batch.manual or manual
		if not _running:
			if _thread != null:
				_thread.wait_to_finish()      # the last worker has returned already
			_running = true
			_thread = Thread.new()
			_thread.start(_work)
	_mutex.unlock()
	return added


func is_busy() -> bool:
	_mutex.lock()
	var busy := _running or not _pending.is_empty()
	_mutex.unlock()
	return busy


func progress() -> Dictionary:
	_mutex.lock()
	var copy := _batch.duplicate()
	_mutex.unlock()
	return copy


## Drop what is queued. The batch being checked finishes (OS.execute cannot be interrupted).
func cancel() -> void:
	_mutex.lock()
	for item in _pending:
		_queued.erase(item.path)
		_stamps.erase(item.path)
	_batch.total -= _pending.size()
	_pending.clear()
	_stopping = true
	_mutex.unlock()


## Cancel and wait for the worker (the plug-in's _exit_tree).
func shutdown() -> void:
	cancel()
	if _thread != null:
		_thread.wait_to_finish()
		_thread = null
	_running = false


func _work() -> void:
	while true:
		_mutex.lock()
		if _pending.is_empty() or _stopping:
			_running = false
			_mutex.unlock()
			return
		# One batch: the next files queued with the same runner (settings), up to the cap.
		var runner = _pending[0].runner
		var items := []
		while not _pending.is_empty() and items.size() < GateRunner.MAX_BATCH_FILES \
				and _pending[0].runner == runner:
			items.append(_pending.pop_front())
		_mutex.unlock()
		for result in runner.check_batch(items):
			_deliver.call_deferred(result)


func _deliver(result: Dictionary) -> void:
	_mutex.lock()
	_queued.erase(result.path)
	_batch.done += 1
	match result.outcome:
		"passed":
			_batch.passed += 1
		"failed":
			_batch.failed += 1
		_:
			_batch.not_checked += 1
	var finished: bool = _batch.done >= _batch.total and _pending.is_empty()
	var summary: Dictionary = _batch.duplicate()
	_mutex.unlock()
	result_ready.emit(result)
	if finished:
		batch_finished.emit(summary)
