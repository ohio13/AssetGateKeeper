"""
Maya startup hook for AssetGatekeeper.

Maya runs the ``userSetup.py`` it finds in its ``scripts`` folders or on
``PYTHONPATH`` at launch. This one adds:

  * a "Run Gatekeeper Check" shelf button on the current shelf, and
  * a ``Gatekeeper`` menu item under the main menu bar's ``AssetGatekeeper`` menu.

Copy this file and ``gatekeeper_maya.py`` into ``Documents/maya/scripts`` (or
put this folder on ``PYTHONPATH``), and set ``ASSETGATEKEEPER_HOME`` (env var or
in ``Maya.env``) to the folder that contains ``asset_gatekeeper.py``.
``MAYA_SCRIPT_PATH`` is not enough: verified in Maya 2027, a ``userSetup.py``
found only there is never run.

If you already have a ``userSetup.py``, paste the two blocks marked below instead
of overwriting yours.
"""

import os
import sys

try:
    from maya import cmds, mel
except ImportError:  # imported outside Maya by tooling/tests
    cmds = mel = None

BUTTON_LABEL = "AGK"
MENU_NAME = "assetGatekeeperMenu"


def _scripts_dir() -> str:
    """The folder holding ``gatekeeper_maya.py``.

    Maya exec()s ``userSetup.py`` without defining ``__file__`` (verified in
    Maya 2027), so reading it raised NameError, the setup below was skipped,
    and no menu or shelf button ever appeared. Use it when it exists, and
    otherwise find the folder on ``sys.path`` (Maya puts its scripts folders
    there).
    """
    here = globals().get("__file__")
    if here:
        return os.path.dirname(os.path.abspath(here))
    for entry in sys.path:
        if entry and os.path.isfile(os.path.join(entry, "gatekeeper_maya.py")):
            return entry
    return ""


def _ensure_path() -> None:
    directory = _scripts_dir()
    if directory and directory not in sys.path:
        sys.path.insert(0, directory)


def run_check(*_args) -> None:
    _ensure_path()
    import gatekeeper_maya

    gatekeeper_maya.run()


def build_shelf_button() -> None:
    """Add a shelf button to the currently active shelf."""
    if cmds is None:
        return
    shelf = mel.eval("$tmp = $gShelfTopLevel;")
    current = cmds.tabLayout(shelf, query=True, selectTab=True)
    existing = cmds.shelfLayout(current, query=True, childArray=True) or []

    for child in existing:
        try:
            if cmds.shelfButton(child, query=True, label=True) == BUTTON_LABEL:
                return  # already installed
        except RuntimeError:
            continue

    cmds.shelfButton(
        parent=current,
        label=BUTTON_LABEL,
        annotation="Run the AssetGatekeeper budget check on the selection",
        image="commandButton.png",
        command="import userSetup; userSetup.run_check()",
    )


def build_menu() -> None:
    if cmds is None:
        return
    main_window = mel.eval("$tmp = $gMainWindow;")
    if cmds.menu(MENU_NAME, exists=True):
        cmds.deleteUI(MENU_NAME)
    cmds.menu(MENU_NAME, parent=main_window, label="AssetGatekeeper", tearOff=True)
    cmds.menuItem(parent=MENU_NAME, label="Run Gatekeeper Check",
                  command="import userSetup; userSetup.run_check()")


# --- block to paste into an existing userSetup.py -------------------------- #
def _build_ui() -> None:
    try:
        build_shelf_button()
        build_menu()
    except Exception as error:  # never break Maya startup over a pipeline tool
        cmds.warning(f"[AssetGatekeeper] shelf/menu setup skipped: {error}")


try:
    _ensure_path()
    # userSetup.py runs before Maya's main window and shelves exist. Building
    # them here failed every time (verified in Maya 2027: no menu, no button),
    # so the UI is built once Maya is idle. No UI at all in batch / mayapy.
    if cmds is not None and not cmds.about(batch=True):
        cmds.evalDeferred(_build_ui, lowestPriority=True)
except Exception as error:  # never break Maya startup over a pipeline tool
    if cmds is not None:
        cmds.warning(f"[AssetGatekeeper] shelf/menu setup skipped: {error}")
    else:
        print(f"[AssetGatekeeper] shelf/menu setup skipped: {error}")
# --------------------------------------------------------------------------- #
