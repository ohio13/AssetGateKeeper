@tool
extends VBoxContainer
## The AssetGatekeeper dock: a "Check project" button, a status line, and the
## assets that failed (or could not be checked) with the gate's reasons.
## Double-click an entry to show the file in the FileSystem dock.

signal check_project_pressed
signal asset_activated(path: String)

var _status: Label
var _list: ItemList
var _rows := {}          # res:// path -> item index


func _init() -> void:
	name = "AssetGatekeeper"
	custom_minimum_size = Vector2(160, 120)
	var bar := HBoxContainer.new()
	add_child(bar)
	var button := Button.new()
	button.text = "Check project"
	button.tooltip_text = "Run the gate on every model and texture in the project"
	button.pressed.connect(func(): check_project_pressed.emit())
	bar.add_child(button)
	var clear := Button.new()
	clear.text = "Clear"
	clear.pressed.connect(clear_results)
	bar.add_child(clear)
	_status = Label.new()
	_status.text = "Checks models and textures when they are imported."
	_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	add_child(_status)
	_list = ItemList.new()
	_list.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_list.auto_height = false
	_list.item_activated.connect(func(index: int): asset_activated.emit(str(_list.get_item_metadata(index))))
	add_child(_list)


func set_status(text: String) -> void:
	_status.text = text


func clear_results() -> void:
	_list.clear()
	_rows.clear()


## Show or remove one asset's row: failures and "not checked" are listed, passes are removed.
func show_result(result: Dictionary) -> void:
	var path: String = result.path
	if result.outcome == "passed":
		if _rows.has(path):
			_list.remove_item(_rows[path])
			_reindex()
		return
	var text: String
	var tooltip: String
	if result.outcome == "failed":
		text = "%s  %s: %s" % [path.get_file(), result.status, "; ".join(PackedStringArray(result.problems))]
		tooltip = path + "\n" + "\n".join(PackedStringArray(result.problems))
	else:
		text = "%s  NOT checked: %s" % [path.get_file(), result.reason]
		tooltip = path + "\n" + str(result.reason)
	var index: int
	if _rows.has(path):
		index = _rows[path]
		_list.set_item_text(index, text)
	else:
		index = _list.add_item(text)
		_rows[path] = index
	_list.set_item_metadata(index, path)
	_list.set_item_tooltip(index, tooltip)
	_list.set_item_custom_fg_color(index, Color(1.0, 0.45, 0.4) if result.outcome == "failed" else Color(1.0, 0.8, 0.4))


## The paths currently listed (for tests).
func listed_paths() -> Array:
	return _rows.keys()


func _reindex() -> void:
	_rows.clear()
	for index in _list.item_count:
		_rows[str(_list.get_item_metadata(index))] = index
