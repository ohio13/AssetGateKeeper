#!/usr/bin/env python3
"""The terminal rules wizard: ``asset-gatekeeper <folder> --wizard``.

Six plain-English questions (engines, art tools, platforms, kinds of asset,
art style, broken geometry), each with a default on Enter, then a summary to
confirm. The answers become one commented ``rules.toml`` that works for every
engine, tool and platform chosen at once:

* several engines or tools -> one ``[naming] preset = [...]`` list (a name
  passes when it follows any of them);
* several platforms -> the base budget is the tightest limit of all of them,
  and each platform gets a ``[platforms.<name>]`` block, picked at run time
  with ``--platform <name>``;
* several kinds of asset -> ``[tags.<folder>]`` budgets, the tool's per-folder
  mechanism (a tag is a folder name).

Every number comes from the tool's own tables (``PLATFORM_PRESETS``, the
``Budget`` defaults, the shipped ``templates/rules.toml``); nothing is made up
here. The text is parsed with the gate's own parser before it is written, so a
file this writes always loads.

For scripts and tests the same answers come from a JSON file
(``--wizard-answers answers.json``); without one, and without a terminal to
ask in, the wizard refuses instead of waiting for input that never comes.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import asset_gatekeeper as agk  # noqa: E402 - the gate's own tables and parser


class WizardError(Exception):
    """An answer the wizard cannot use; the message says which and why."""


class Cancelled(Exception):
    """The user ended input (Ctrl+D / Ctrl+Z) or pressed Ctrl+C."""


# --- #
# What each answer means
# --- #

@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Question:
    key: str
    prompt: str
    choices: tuple[Choice, ...]
    default: tuple[str, ...]
    multi: bool


#: Engine -> the [naming] preset with its map names ("" = no preset of its own).
ENGINE_NAMING = {"unreal": "unreal", "unity": "unity", "godot": "godot", "other": ""}
#: Art tool -> the [naming] preset its exports follow, where there is one.
TOOL_NAMING = {"substance": "substance", "blender": "gltf"}
#: The tools with a checking panel in plugins/ (docs/09-DCC-PLUGINS.md).
TOOL_PANELS = ("maya", "max", "blender", "houdini")


@dataclass(frozen=True)
class Platform:
    """A wizard platform: which shipped table its numbers come from."""

    key: str
    label: str
    source: str            # a PLATFORM_PRESETS key, or "" for the Budget defaults
    why: str               # one line for the file: where the numbers come from
    power_of_two: bool     # does this hardware want power-of-two textures
    pbr: bool              # does it render physically based materials


#: The prop budget for "midrange": the [tags.prop] example in templates/rules.toml
#: (the Budget defaults have one mesh budget and no prop number of their own).
MIDRANGE_PROP_TRIANGLES = 8_000

PLATFORMS: tuple[Platform, ...] = (
    Platform("pc", "High-end PC / current console", "pc",
             'the shipped "pc" target (8 GB+ VRAM, PBR with mip chains)', False, True),
    Platform("midrange", "Mid-range PC / last-gen console", "",
             "the gate's built-in default budget (50k suits indie characters); "
             "the prop number is the [tags.prop] example in templates/rules.toml",
             True, True),
    Platform("switch", "Nintendo Switch-class handheld", "ps3",
             'the shipped "ps3" target: there is no Switch table, and a Switch-class '
             "handheld has about a PS3-generation budget", False, True),
    Platform("mobile", "Mobile (phones, tablets)", "mobile",
             'the shipped "mobile" target', True, True),
    Platform("vr", "VR headset", "mobile",
             'the shipped "mobile" target: a standalone headset is a phone chip that '
             "draws every frame twice (for PC-only VR, choose high-end PC instead)",
             True, True),
    Platform("n64", "Retro / N64 style", "n64",
             'the shipped "n64" target (4 MB RDRAM, 4 KB texture memory)', True, False),
)
PLATFORM_BY_KEY = {platform.key: platform for platform in PLATFORMS}

#: The kinds of asset that are meshes, and the [tags.*] name (= folder name) for each.
MESH_KINDS = {"characters": "characters", "props": "props", "environment": "environment"}

QUESTIONS: tuple[Question, ...] = (
    Question("engines", "Which engines do the assets go into?", (
        Choice("unreal", "Unreal Engine", ("ue", "ue4", "ue5", "unrealengine")),
        Choice("unity", "Unity"),
        Choice("godot", "Godot"),
        Choice("other", "Other / custom engine", ("custom", "none")),
    ), ("unreal",), True),
    Question("tools", "Which programs does the art come from?", (
        Choice("maya", "Maya"),
        Choice("max", "3ds Max", ("3dsmax", "3ds", "max")),
        Choice("blender", "Blender"),
        Choice("houdini", "Houdini"),
        Choice("zbrush", "ZBrush"),
        Choice("substance", "Substance 3D Painter / Designer",
               ("substancepainter", "substancedesigner", "painter", "designer")),
        Choice("other", "Other", ("none",)),
    ), ("other",), True),
    Question("platforms", "Which platforms must the assets run on?", tuple(
        Choice(platform.key, platform.label, aliases) for platform, aliases in zip(PLATFORMS, (
            ("highend", "high", "currentgen", "console", "ps5", "xboxseries"),
            ("mid", "lastgen", "ps4", "xboxone"),
            ("nintendoswitch",),
            ("phone", "tablet", "android", "ios"),
            ("quest", "headset"),
            ("retro", "nintendo64"),
        ))), ("pc",), True),
    Question("assets", "What kinds of asset are there?", (
        Choice("characters", "Characters", ("character", "creatures")),
        Choice("props", "Props", ("prop",)),
        Choice("environment", "Environment / architecture", ("environments", "architecture", "env")),
        Choice("ui", "UI / 2D", ("2d", "sprites", "icons")),
    ), ("characters", "props"), True),
    Question("style", "Which art style?", (
        Choice("realistic", "Realistic", ("pbr", "photoreal")),
        Choice("stylised", "Stylised", ("stylized", "handpainted")),
        Choice("lowpoly", "Low-poly / retro", ("retro", "low")),
    ), ("realistic",), False),
    Question("broken_geometry", "Broken or degenerate geometry (zero-area triangles):", (
        Choice("block", "Block: the asset fails", ("fail", "strict")),
        Choice("warn", "Warn: the asset passes with a note", ("note", "lenient")),
    ), ("block",), False),
)
QUESTION_BY_KEY = {question.key: question for question in QUESTIONS}
#: Keys an answers file may hold besides the questions.
EXTRA_KEYS = ("replace",)
ANSWERS_MARKER = "# wizard-answers: "


def _squash(text: str) -> str:
    return re.sub(r"[\s_\-/.]+", "", str(text).strip().lower())


def _resolve(question: Question, token: str) -> str:
    """One typed token (a number, a key, a label or an alias) to a choice key."""
    text = str(token).strip()
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(question.choices):
            return question.choices[index - 1].key
    wanted = _squash(text)
    for choice in question.choices:
        if wanted in {_squash(choice.key), _squash(choice.label), *map(_squash, choice.aliases)}:
            return choice.key
    raise WizardError(f"{question.key}: {text!r} is not one of: "
                      + ", ".join(choice.key for choice in question.choices)
                      + f" (or 1-{len(question.choices)})")


def _tokens(text: str, question: Question) -> list[str]:
    """Split typed input on commas; "1 3" and "unreal unity" split on spaces too."""
    pieces = []
    for piece in str(text).split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            _resolve(question, piece)
            pieces.append(piece)
        except WizardError:
            words = piece.split()
            if len(words) > 1:
                pieces.extend(words)       # resolved (or refused) one by one below
            else:
                pieces.append(piece)
    return pieces


def normalise_answer(question: Question, value: object) -> tuple[str, ...]:
    """A raw answer (list, string, number) to choice keys in the questions' order."""
    if isinstance(value, bool) or value is None:
        raise WizardError(f"{question.key}: expected "
                          + ("a list of choices" if question.multi else "one choice")
                          + f", got {value!r}")
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = _tokens(str(value), question)
    keys = []
    for token in raw:
        key = _resolve(question, token)
        if key not in keys:
            keys.append(key)
    if not keys:
        raise WizardError(f"{question.key}: pick at least one of: "
                          + ", ".join(choice.key for choice in question.choices))
    if not question.multi and len(keys) > 1:
        raise WizardError(f"{question.key}: pick one, not {len(keys)} ({', '.join(keys)})")
    order = [choice.key for choice in question.choices]
    return tuple(sorted(keys, key=order.index))


def default_answers() -> dict[str, tuple[str, ...]]:
    return {question.key: question.default for question in QUESTIONS}


def normalise_answers(raw: dict, defaults: Optional[dict] = None) -> dict:
    """Validate a whole answers mapping; a missing question takes its default."""
    if not isinstance(raw, dict):
        raise WizardError("the answers must be a JSON object, e.g. "
                          '{"engines": ["unreal", "unity"], "platforms": ["pc", "switch"]}')
    unknown = sorted(set(raw) - set(QUESTION_BY_KEY) - set(EXTRA_KEYS))
    if unknown:
        raise WizardError(f"unknown answer {', '.join(unknown)} (known: "
                          + ", ".join([*QUESTION_BY_KEY, *EXTRA_KEYS]) + ")")
    answers = dict(defaults or default_answers())
    for key, value in raw.items():
        if key in QUESTION_BY_KEY:
            answers[key] = normalise_answer(QUESTION_BY_KEY[key], value)
    if "replace" in raw and not isinstance(raw["replace"], bool):
        raise WizardError(f"replace: expected true or false, got {raw['replace']!r}")
    answers["replace"] = bool(raw.get("replace", False))
    return answers


def load_answers(path: Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise WizardError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise WizardError(f"{Path(path).name} is not valid JSON: {exc}") from exc
    try:
        return normalise_answers(raw)
    except WizardError as exc:
        raise WizardError(f"{Path(path).name}: {exc}") from exc


def previous_answers(rules_path: Path) -> Optional[dict]:
    """The answers recorded in a rules.toml this wizard wrote, if any."""
    try:
        with open(rules_path, encoding="utf-8-sig") as handle:
            for _number, line in zip(range(40), handle):
                if line.startswith(ANSWERS_MARKER):
                    return normalise_answers(json.loads(line[len(ANSWERS_MARKER):]))
    except (OSError, ValueError, WizardError):
        return None
    return None


# --- #
# The numbers
# --- #

def platform_numbers(key: str) -> dict:
    """{character, prop, slots, edge, influences, uv} for one wizard platform,
    straight from the shipped tables."""
    platform = PLATFORM_BY_KEY[key]
    if platform.source:
        entry = agk.PLATFORM_PRESETS[platform.source]
        profile = entry["profile"]
        tags = entry["document"]["tags"]
        return {"character": profile["max_triangles"], "prop": tags["prop"]["max_triangles"],
                "slots": profile["max_material_slots"], "edge": profile["max_texture_edge"],
                "influences": profile["max_bone_influences"], "uv": profile["max_uv_channels"]}
    budget = agk.Budget()
    return {"character": budget.max_triangles, "prop": MIDRANGE_PROP_TRIANGLES,
            "slots": budget.max_material_slots, "edge": budget.max_texture_edge,
            "influences": budget.max_bone_influences, "uv": budget.max_uv_channels}


def _main_kind(assets: Sequence[str]) -> str:
    """Which number is the [mesh] budget: the prop one only for a props-only project."""
    meshes = [kind for kind in assets if kind in MESH_KINDS]
    return "prop" if meshes == ["props"] else "character"


def _kind_number(kind: str, numbers: dict) -> int:
    return numbers["prop"] if kind == "props" else numbers["character"]


def plan(answers: dict) -> dict:
    """Everything the file will say, worked out once (also used for the summary)."""
    platforms = list(answers["platforms"])
    numbers = {key: platform_numbers(key) for key in platforms}
    main = _main_kind(answers["assets"])

    def triangles(key: str) -> int:
        return numbers[key][main]

    fields = ("slots", "edge", "influences", "uv")
    base = {"triangles": min(triangles(key) for key in platforms)}
    owner = {"triangles": min(platforms, key=lambda key: (triangles(key), platforms.index(key)))}
    for field in fields:
        base[field] = min(numbers[key][field] for key in platforms)
        owner[field] = min(platforms, key=lambda key: (numbers[key][field], platforms.index(key)))
    style = answers["style"][0]
    naming: list[str] = []
    for engine in answers["engines"]:
        if ENGINE_NAMING[engine] and ENGINE_NAMING[engine] not in naming:
            naming.append(ENGINE_NAMING[engine])
    for tool in answers["tools"]:
        if tool in TOOL_NAMING and TOOL_NAMING[tool] not in naming:
            naming.append(TOOL_NAMING[tool])
    # "Other" engine, or nothing engine-specific: the common map names pass too.
    if "other" in answers["engines"] or not naming:
        naming.append("default")
    mesh_kinds = [kind for kind in answers["assets"] if kind in MESH_KINDS]
    return {
        "platforms": platforms,
        "numbers": numbers,
        "main": main,
        "base": base,
        "owner": owner,
        "power_of_two": any(PLATFORM_BY_KEY[key].power_of_two for key in platforms),
        "pbr": style == "realistic" and any(PLATFORM_BY_KEY[key].pbr for key in platforms),
        "style": style,
        "naming": naming,
        "ui_names": "ui" in answers["assets"],
        "tags": mesh_kinds if len(mesh_kinds) > 1 else [],
        "tags_active": len(platforms) == 1,
        "degenerate": answers["broken_geometry"][0],
    }


# --- #
# The file
# --- #

def _label(question_key: str, key: str) -> str:
    for choice in QUESTION_BY_KEY[question_key].choices:
        if choice.key == key:
            return choice.label
    return key


def _n(value: int) -> str:
    return f"{value:,}"


def _wrap(text: str, prefix: str = "# ", width: int = 78) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(prefix) + len(line) + 1 + len(word) > width:
            lines.append(prefix + line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(prefix + line)
    return lines or [prefix.rstrip()]


def answers_json(answers: dict) -> str:
    return json.dumps({question.key: (list(answers[question.key]) if question.multi
                                      else answers[question.key][0])
                       for question in QUESTIONS}, separators=(", ", ": "))


def build_rules(answers: dict, folder: str = "<folder>", today: Optional[str] = None) -> str:
    """The commented rules.toml for these answers."""
    p = plan(answers)
    base, owner, numbers, platforms = p["base"], p["owner"], p["numbers"], p["platforms"]
    multi = len(platforms) > 1
    kind_word = "prop" if p["main"] == "prop" else "character"
    today = today or datetime.now().strftime("%Y-%m-%d")
    plabel = {key: PLATFORM_BY_KEY[key].label for key in platforms}
    out: list[str] = []
    add = out.append

    add(f"# rules.toml - written by the AssetGatekeeper rules wizard on {today}")
    add("#")
    add("# Your answers:")
    for question in QUESTIONS:
        shown = ", ".join(_label(question.key, key) for key in answers[question.key])
        add(f"#   {question.key.replace('_', ' '):<16} {shown}")
    add(ANSWERS_MARKER + answers_json(answers))
    add("#")
    add("# Start again (it asks before replacing this file, and keeps a copy):")
    add(f'#   asset-gatekeeper "{folder}" --wizard')
    add("#   or on Windows double-click tools\\rules_wizard.bat")
    add("# Same answers without questions: save the wizard-answers line above as")
    add(f'# answers.json and run  asset-gatekeeper "{folder}" --wizard-answers answers.json')
    add("# Or just edit this file: docs/05-RULES-REFERENCE.md explains every key.")
    add("#")
    add("# Precedence: CLI flag > remote rules > [platforms.<name>] > this file > defaults.")
    add("")

    # - budget - #
    add("# " + "-" * 75)
    add("# The budget every asset must fit")
    add("#")
    if multi:
        out.extend(_wrap("A plain run checks the tightest limit of every platform you chose, so an "
                         "asset that passes it ships on all of them. To check against one "
                         "platform's own budget instead, add --platform with its name "
                         "(the [platforms.*] blocks further down), for example:"))
        example = next(k for k in reversed(platforms) if k != owner["triangles"])
        add(f'#   asset-gatekeeper "{folder}" --once --recursive --platform {example}')
    else:
        out.extend(_wrap(f"The numbers of your platform, {plabel[platforms[0]]}: "
                         f"{PLATFORM_BY_KEY[platforms[0]].why}."))
    add("# " + "-" * 75)
    add("")

    def source(field: str, word: str) -> str:
        if not multi:
            return word
        column = p["main"] if field == "triangles" else field
        values = {k: numbers[k][column] for k in platforms}
        if len(set(values.values())) == 1:
            return word + " - the same on all your platforms"
        key = owner[field]
        others = [f"{k} {_n(values[k])}" for k in platforms if k != key]
        return word + f" - from {plabel[key]} (others: {', '.join(others)})"

    add("[mesh]")
    tri_note = (f"Triangles per mesh: the {kind_word} budget"
                + ("" if kind_word == "prop" else
                   (", and the budget for any folder without a tag below" if p["tags"] else "")))
    out.extend(_wrap(source("triangles", tri_note)))
    add(f"max_triangles = {base['triangles']}")
    out.extend(_wrap(source("slots", "Materials per mesh (each one is a draw call)")))
    add(f"max_material_slots = {base['slots']}")
    out.extend(_wrap(source("influences", "Bone weights per skinned vertex")))
    add(f"max_bone_influences = {base['influences']}")
    out.extend(_wrap(source("uv", "UV sets per mesh")))
    add(f"max_uv_channels = {base['uv']}")
    add('# Zero-area or collapsed triangles: "block" fails the asset, "warn" passes it')
    add('# with a note (they break Nanite, ray tracing and lightmap baking).')
    add(f'degenerate = "{p["degenerate"]}"')
    add("# A .gltf/.obj that points at a missing texture is broken in every engine.")
    add("check_references = true")
    add("")

    add("[texture]")
    out.extend(_wrap(source("edge", "Longest texture edge in pixels")))
    add(f"max_edge = {base['edge']}")
    if p["power_of_two"]:
        needs = [plabel[k] for k in platforms if PLATFORM_BY_KEY[k].power_of_two]
        out.extend(_wrap("Power-of-two sizes (256, 512, 1024...): wanted by " + ", ".join(needs) + "."
                         + (" A platform block cannot switch this off, so it holds for all."
                            if multi else "")
                         + (" Pack UI sprites into power-of-two atlases." if p["ui_names"] else "")))
    else:
        out.extend(_wrap("Power-of-two sizes are not required: none of your platforms needs them."))
    add(f"power_of_two = {'true' if p['power_of_two'] else 'false'}")
    add("# Texture names must follow the [naming] conventions below.")
    add("naming = true")
    if p["pbr"]:
        add("# Realistic art: flag albedo (base colour) maps darker or brighter than any")
        add("# real material, the usual sign of lighting painted into the colour.")
    elif p["style"] == "realistic":
        add("# The albedo brightness check is off: none of your platforms renders")
        add("# physically based materials.")
    else:
        style = _label("style", p["style"]).lower()
        out.extend(_wrap(f"The albedo brightness check is off for {style} art, whose colours are "
                         "often brighter or darker than real materials on purpose. Set true for "
                         "physically based (PBR) art."))
    add(f"pbr = {'true' if p['pbr'] else 'false'}")
    add("")

    # - naming - #
    add("[naming]")
    names = {"unreal": "Unreal (T_ textures with a map suffix, SM_/SK_ meshes)",
             "unity": "Unity (_Albedo, _MaskMap, _Normal...)",
             "godot": "Godot (_albedo, _orm, _normal...)",
             "substance": "Substance export presets (_BaseColor, _OcclusionRoughnessMetallic...)",
             "gltf": "glTF slot names, as Blender's glTF exporter writes them (_baseColor...)",
             "default": "the common map names (_BaseColor, _Albedo, _Normal, _D, _N...)"}
    add("# A texture name passes when it follows ANY of these conventions:")
    for name in p["naming"]:
        add(f"#   {name:<10} {names[name]}")
    if "unreal" in p["naming"] and len(p["naming"]) > 1:
        add("# Only the Unreal convention names meshes, so with it in the list every mesh")
        add("# still needs SM_ or SK_ in front. Remove \"unreal\" if your other engines' meshes")
        add("# are named differently.")
    add("preset = [" + ", ".join(f'"{name}"' for name in p["naming"]) + "]")
    if p["ui_names"]:
        add("# UI / 2D: icons and sprites rarely end with a map name, so a texture whose")
        add("# name starts with UI_ or T_UI_ passes without one. Delete this line if your")
        add("# UI textures do carry map suffixes.")
        add('texture_prefixes = ["UI_", "T_UI_"]')
    add("")

    # - platforms - #
    add("# " + "-" * 75)
    add("# Platforms: check against one with --platform <name>")
    if multi:
        add("# (a plain run uses the tightest limits, above)")
    add("# " + "-" * 75)
    for key in platforms:
        numbers_here = numbers[key]
        add("")
        out.extend(_wrap(f"{plabel[key]}: {PLATFORM_BY_KEY[key].why}."))
        if multi and all(numbers_here[f] == base[f] for f in ("slots", "edge", "influences", "uv")) \
                and numbers_here[p["main"]] == base["triangles"]:
            add("# The same as the budget above; listed so --platform works for every platform.")
        add(f"[platforms.{key}]")
        add(f"max_triangles = {numbers_here[p['main']]}")
        add(f"max_material_slots = {numbers_here['slots']}")
        add(f"max_texture_edge = {numbers_here['edge']}")
        add(f"max_bone_influences = {numbers_here['influences']}")
        add(f"max_uv_channels = {numbers_here['uv']}")
    add("")

    # - kinds of asset - #
    if p["tags"]:
        add("# " + "-" * 75)
        add("# Budgets per folder")
        add("#")
        folders = [f'"{MESH_KINDS[k].capitalize()}"' for k in p["tags"]]
        out.extend(_wrap("A tag is a folder name: a mesh anywhere under a folder called "
                         + ", ".join(folders[:-1]) + " or " + folders[-1]
                         + " (any case, up to four levels up) gets that folder's budget. A file name "
                         "that starts with the tag (props_crate.fbx) counts too. A near miss "
                         "(Prop, Character) still matches, with a note."))
        if "environment" in p["tags"]:
            out.extend(_wrap("There is no shipped number for environment pieces: they start at "
                             "the character budget. Lower it for modular kit pieces."))
        add("# " + "-" * 75)
        if p["tags_active"]:
            for kind in p["tags"]:
                add("")
                add(f"[tags.{MESH_KINDS[kind]}]")
                add(f"max_triangles = {_kind_number(kind, numbers[platforms[0]])}")
        else:
            add("#")
            out.extend(_wrap("Written as comments, because you chose more than one platform: a "
                             "tag's budget wins over --platform, so one number per folder would "
                             "be wrong on every platform but one. Without tags every mesh uses "
                             "the [mesh] budget above (or its --platform block). Uncomment them "
                             "with the numbers of the platform you check most often:"))
            add("#")
            header = "#   " + f"{'platform':<12}" + "".join(f"{kind:>13}" for kind in p["tags"])
            add(header)
            for key in platforms:
                add("#   " + f"{key:<12}" + "".join(
                    f"{_n(_kind_number(kind, numbers[key])):>13}" for kind in p["tags"]))
            strictest = owner["triangles"]
            for kind in p["tags"]:
                add("#")
                add(f"# [tags.{MESH_KINDS[kind]}]")
                add(f"# max_triangles = {_kind_number(kind, numbers[strictest])}")
        add("")
    elif len([k for k in answers["assets"] if k in MESH_KINDS]) == 1 and len(answers["assets"]) > 0:
        kind = [k for k in answers["assets"] if k in MESH_KINDS][0]
        add(f"# Every mesh here is judged as {_label('assets', kind).lower()}. For a second kind")
        add("# with its own budget, add a [tags.<folder name>] table with max_triangles")
        add("# (docs/05-RULES-REFERENCE.md, [tags.<name>]) or run the wizard again.")
        add("")

    # - engines and tools - #
    notes: list[str] = []
    engines, tools = answers["engines"], answers["tools"]
    if "unreal" in engines:
        notes.append("Unreal: memory estimates ([mesh] max_mb, off unless you set it) are "
                     "calibrated on Unreal 5.3, which adds a lightmap UV set and a depth-only "
                     "index buffer to every static mesh on import. The editor plug-in checks "
                     "imports against this file: docs/09-DCC-PLUGINS.md.")
    if "unity" in engines:
        notes.append("Unity: skins with 4 bones per vertex unless Quality > Skin Weights allows "
                     "more; lower max_bone_influences to 4 if you keep that default. For "
                     "memory budgets set [mesh] lightmap_uv = false and depth_index_buffer = "
                     "false (those are Unreal's additions).")
    if "godot" in engines:
        notes.append("Godot: uses 4 bone weights per vertex unless 8 are enabled, and its "
                     "materials read two UV sets (UV, UV2). For memory budgets set [mesh] "
                     "lightmap_uv = false and depth_index_buffer = false.")
    panels = [_label("tools", tool) for tool in tools if tool in TOOL_PANELS]
    if panels:
        notes.append(f"{', '.join(panels)}: a panel inside the program checks an export "
                     "against this same file before it leaves the program - "
                     "docs/09-DCC-PLUGINS.md.")
    if "zbrush" in tools:
        bake = "--bake-polypaint" + (" --bake-preset n64" if "n64" in platforms else "")
        notes.append("ZBrush: a sculpt is not a game asset yet. Export it as .obj with "
                     f'Polypaint and run  asset-gatekeeper "Hero_HP.obj" {bake}  for a '
                     "reduced .glb with baked textures, sized to this file's budget.")
    if "substance" in tools:
        notes.append('Substance: its export names (_BaseColor, _Normal...) pass through the '
                     '"substance" naming preset above.')
    if "blender" in tools:
        notes.append("Blender's glTF exports name their maps by slot (_baseColor, _normal...), "
                     'covered by the "gltf" naming preset above.')
    if notes:
        add("# " + "-" * 75)
        add("# Your engines and tools")
        add("# " + "-" * 75)
        for note in notes:
            out.extend(_wrap(note))
            add("#")
        out.pop()
        add("")

    add("# Healing (automatic repairs) is off; see [healer] in docs/05-RULES-REFERENCE.md.")
    return "\n".join(out).rstrip("\n") + "\n"


def check_text(text: str, answers: dict) -> "agk.Budget":
    """Parse the text the way the gate will; raise WizardError if it would not load."""
    try:
        document = agk.parse_rules_text(text, "rules.toml")
        budget = agk.Budget.from_mapping(document)
        agk.build_tag_budgets(document)
        for key in answers["platforms"]:
            agk.Budget.from_mapping(agk.apply_platform(document, key))
    except Exception as exc:          # a bug here must never write a broken file
        raise WizardError(f"internal error: the generated rules do not load ({exc})") from exc
    return budget


# --- #
# Asking
# --- #

def _ask(ask: Callable[[str], str], prompt: str) -> str:
    try:
        return ask(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Cancelled() from None


def ask_question(question: Question, number: int, default: tuple[str, ...],
                 ask: Callable[[str], str], say: Callable[[str], None]) -> tuple[str, ...]:
    say("")
    say(f"{number}/{len(QUESTIONS)}  {question.prompt}"
        + ("  (several: separate with commas)" if question.multi else ""))
    for index, choice in enumerate(question.choices, 1):
        say(f"   {index}  {choice.label}")
    while True:
        raw = _ask(ask, f"  Your answer [{', '.join(default)}]: ")
        if not (raw or "").strip():
            return default
        try:
            return normalise_answer(question, raw)
        except WizardError as exc:
            say(f"  {str(exc).split(': ', 1)[-1]}")


def ask_yes_no(question: str, default: bool, ask: Callable[[str], str],
               say: Callable[[str], None]) -> bool:
    while True:
        raw = (_ask(ask, f"{question} [{'Y/n' if default else 'y/N'}]: ") or "").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        say("  type y or n")


def summary(answers: dict) -> list[str]:
    p = plan(answers)
    base = p["base"]
    lines = [f"  {question.key.replace('_', ' '):<16} "
             + ", ".join(_label(question.key, key) for key in answers[question.key])
             for question in QUESTIONS]
    lines.append("")
    lines.append(f"  budget           {_n(base['triangles'])} triangles, {base['edge']} px textures, "
                 f"{base['slots']} material slot(s)"
                 + (" - the tightest of your platforms" if len(p["platforms"]) > 1 else ""))
    if len(p["platforms"]) > 1:
        lines.append("  --platform       " + ", ".join(p["platforms"]))
    lines.append("  naming           " + ", ".join(p["naming"]))
    if p["tags"]:
        lines.append("  per folder       " + ", ".join(MESH_KINDS[k] for k in p["tags"])
                     + ("" if p["tags_active"] else " (as comments: several platforms)"))
    return lines


def _backup_path(rules_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = rules_path.with_name(f"rules.backup-{stamp}.toml")
    counter = 2
    while candidate.exists():
        candidate = rules_path.with_name(f"rules.backup-{stamp}-{counter}.toml")
        counter += 1
    return candidate


def _write(rules_path: Path, text: str) -> Optional[Path]:
    """Write atomically; an existing file is copied aside first. Returns the copy."""
    backup = None
    if rules_path.exists():
        backup = _backup_path(rules_path)
        shutil.copy2(rules_path, backup)
    temporary = rules_path.with_name(f".{rules_path.name}.wizard-tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(rules_path)
    return backup


def run_wizard(target: Optional[Path], *, answers_file: Optional[Path] = None,
               ask: Callable[[str], str] = input, out: Optional[Callable[[str], None]] = None,
               err: Optional[Callable[[str], None]] = None, interactive: Optional[bool] = None,
               force: bool = False, assume_yes: bool = False) -> int:
    """The wizard. 0 = written, 1 = nothing written (the user said no), 2 = bad input."""
    say = out or print
    complain = err or (lambda text: print(text, file=sys.stderr))
    scripted = answers_file is not None or assume_yes
    if interactive is None:
        interactive = not scripted and bool(getattr(sys.stdin, "isatty", lambda: False)())
    if not scripted and not interactive:
        complain("error: the rules wizard asks its questions in a terminal, and this input is "
                 "not one.\n"
                 "  Run it in a terminal:   asset-gatekeeper <folder> --wizard\n"
                 "  On Windows:             double-click tools\\rules_wizard.bat\n"
                 "  From a script:          asset-gatekeeper <folder> --wizard-answers answers.json\n"
                 '  answers.json, e.g.:     {"engines": ["unreal", "unity"], "platforms": '
                 '["pc", "switch"]}\n'
                 "  Every default, no questions: asset-gatekeeper <folder> --wizard --yes")
        return 2

    try:
        file_answers = load_answers(answers_file) if answers_file is not None else None
    except WizardError as exc:
        complain(f"error: {exc}")
        return 2

    try:
        if target is None:
            if not interactive:
                complain("error: name the folder: asset-gatekeeper <folder> --wizard-answers "
                         "answers.json")
                return 2
            while target is None:
                text = (_ask(ask, "Folder your exports go into (type or drag it here): ")
                        or "").strip().strip('"').strip("'")
                if text:
                    target = Path(text)
        target = Path(target).expanduser()
        if target.exists() and not target.is_dir():
            complain(f"error: not a folder: {target}")
            return 2
        rules_path = target / agk.RULES_FILENAME
        earlier = previous_answers(rules_path) if rules_path.is_file() else None

        if file_answers is not None:
            answers = file_answers
        elif assume_yes or not interactive:
            answers = dict(earlier or default_answers())
            answers["replace"] = False
        else:
            say(f"AssetGatekeeper rules wizard - {len(QUESTIONS)} questions for {target}")
            say("Press Enter to take the answer in [brackets]. Type numbers or names.")
            if earlier:
                say("The defaults are the answers that wrote the rules.toml already there.")
            defaults = earlier or default_answers()
            answers = {}
            for number, question in enumerate(QUESTIONS, 1):
                answers[question.key] = ask_question(question, number, defaults[question.key],
                                                     ask, say)
            answers["replace"] = False

        text = build_rules(answers, folder=str(target))
        budget = check_text(text, answers)

        if interactive and file_answers is None and not assume_yes:
            say("")
            say("Summary")
            for line in summary(answers):
                say(line)
            say("")
            if not ask_yes_no(f"Write {rules_path}?", True, ask, say):
                say("Nothing written.")
                return 1

        if rules_path.exists():
            replace = force or answers.get("replace", False)
            if not replace and interactive and file_answers is None and not assume_yes:
                replace = ask_yes_no(f"{rules_path} already exists. Replace it (a copy is kept)?",
                                     False, ask, say)
            if not replace:
                say(f"Kept the existing {rules_path}; nothing written.")
                if not interactive or assume_yes or file_answers is not None:
                    say('  To replace it: add --force, or "replace": true in the answers file.')
                return 1
    except Cancelled:
        say("")
        say("Cancelled; nothing written.")
        return 1
    except WizardError as exc:
        complain(f"error: {exc}")
        return 2

    try:
        target.mkdir(parents=True, exist_ok=True)
        backup = _write(rules_path, text)
    except OSError as exc:
        complain(f"error: cannot write {rules_path}: {exc.strerror or exc}")
        return 2

    p = plan(answers)
    say("")
    say(f"  rules    {rules_path}")
    if backup is not None:
        say(f"  backup   {backup}  (the file it replaced)")
    say(f"  budget   tris<={_n(budget.max_triangles)}  texture<={budget.max_texture_edge}px  "
        f"materials<={budget.max_material_slots}")
    if len(p["platforms"]) > 1:
        say(f"  --platform {' | '.join(p['platforms'])}  for one platform's own budget")
    say("")
    say("Next:")
    say(f'  asset-gatekeeper "{target}" --once --recursive     # check everything once')
    say(f'  asset-gatekeeper "{target}" --recursive            # keep watching')
    return 0


if __name__ == "__main__":       # python engine/rules_wizard.py <folder> [answers.json]
    _args = sys.argv[1:]
    raise SystemExit(run_wizard(Path(_args[0]) if _args else None,
                                answers_file=Path(_args[1]) if len(_args) > 1 else None))
