"""Run once in 3ds Max to add the "AGK" pre-flight button.

    python.ExecuteFile @"<studio>\\AssetGatekeeper\\plugins\\max\\install_max_button.py"

It defines the action "AGK Pre-flight" (category AssetGatekeeper), which Max
keeps across restarts. Then: Customize > Customize User Interface > Toolbars
(or Menus) > Category "AssetGatekeeper" > drag "AssetGatekeeper pre-flight
check" onto a toolbar or menu.
"""
import os
import sys

from pymxs import runtime as rt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import max_preflight  # noqa: E402

max_preflight.install_button()
rt.messageBox("Added the AGK button.\n\nCustomize > Customize User Interface > Toolbars, "
              "Category: AssetGatekeeper, then drag it onto a toolbar or menu.",
              title="Asset Gatekeeper")
