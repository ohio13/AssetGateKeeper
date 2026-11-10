"""The terminal rules wizard (asset-gatekeeper <folder> --wizard): answers in,
one commented rules.toml out that the gate's own parser loads, for any mix of
engines, tools and platforms."""
from __future__ import annotations   # X | None annotations on Python 3.9

import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

import asset_gatekeeper as agk  # noqa: E402
import rules_wizard as wiz  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(label)


SCRATCH = Path(tempfile.mkdtemp(prefix="agk-wizard-"))
GATE = [sys.executable, str(ROOT / "asset_gatekeeper.py")]
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run(*args, stdin=subprocess.DEVNULL, timeout=60):
    return subprocess.run([*GATE, *map(str, args)], stdin=stdin, capture_output=True,
                          text=True, encoding="utf-8", timeout=timeout, env=ENV, cwd=str(SCRATCH))


def answers_file(name, answers):
    path = SCRATCH / f"{name}.json"
    path.write_text(json.dumps(answers), encoding="utf-8")
    return path


def load(path):
    document = agk.parse_rules(path)
    return document, agk.Budget.from_mapping(document)


def write_rules(name, answers):
    folder = SCRATCH / re.sub(r"[^A-Za-z0-9+=-]+", "-", name)
    folder.mkdir(parents=True, exist_ok=True)
    rules = folder / "rules.toml"
    rules.write_text(wiz.build_rules(wiz.normalise_answers(answers), str(folder)), encoding="utf-8")
    return rules


def preset_numbers(key):
    """What the test expects, read straight from the gate's tables (not the wizard's)."""
    source = {"pc": "pc", "switch": "ps3", "mobile": "mobile", "vr": "mobile", "n64": "n64"}.get(key)
    if source is None:                       # midrange: the built-in Budget defaults
        budget = agk.Budget()
        return {"character": budget.max_triangles, "prop": 8_000,
                "slots": budget.max_material_slots, "edge": budget.max_texture_edge,
                "influences": budget.max_bone_influences, "uv": budget.max_uv_channels}
    entry = agk.PLATFORM_PRESETS[source]
    return {"character": entry["profile"]["max_triangles"],
            "prop": entry["document"]["tags"]["prop"]["max_triangles"],
            "slots": entry["profile"]["max_material_slots"],
            "edge": entry["profile"]["max_texture_edge"],
            "influences": entry["profile"]["max_bone_influences"],
            "uv": entry["profile"]["max_uv_channels"]}


#: Budget fields the wizard is allowed to set; every other one must stay default.
WIZARD_FIELDS = {"max_triangles", "max_texture_edge", "max_material_slots", "max_bone_influences",
                 "max_uv_channels", "require_power_of_two", "enforce_texture_naming",
                 "naming_conventions", "degenerate_mode", "pbr_check", "check_references"}


def check_combination(label, answers):
    """Write, parse and compare one answer set against the shipped numbers."""
    normal = wiz.normalise_answers(answers)
    rules = write_rules("combo-" + label, answers)
    try:
        document, budget = load(rules)
    except agk.ConfigError as exc:
        expect(f"{label}: the file parses", False, str(exc))
        return None
    platforms = normal["platforms"]
    meshes = [kind for kind in normal["assets"] if kind in ("characters", "props", "environment")]
    main = "prop" if meshes == ["props"] else "character"
    expected = {key: preset_numbers(key) for key in platforms}
    ok = (budget.max_triangles == min(n[main] for n in expected.values())
          and budget.max_texture_edge == min(n["edge"] for n in expected.values())
          and budget.max_material_slots == min(n["slots"] for n in expected.values())
          and budget.max_bone_influences == min(n["influences"] for n in expected.values())
          and budget.max_uv_channels == min(n["uv"] for n in expected.values()))
    expect(f"{label}: the base budget is the tightest of {', '.join(platforms)}", ok,
           f"{budget.max_triangles} tris / {budget.max_texture_edge}px")
    blocks_ok = set(document.get("platforms", {})) == set(platforms)
    for key in platforms:
        overlaid = agk.Budget.from_mapping(agk.apply_platform(document, key))
        blocks_ok = blocks_ok and (overlaid.max_triangles == expected[key][main]
                                   and overlaid.max_texture_edge == expected[key]["edge"]
                                   and overlaid.max_material_slots == expected[key]["slots"])
    expect(f"{label}: --platform <name> gives each platform its own shipped numbers", blocks_ok,
           str(sorted(document.get("platforms", {}))))
    wanted = [{"unreal": "unreal", "unity": "unity", "godot": "godot"}[e]
              for e in normal["engines"] if e != "other"]
    wanted += [p for t, p in (("blender", "gltf"), ("substance", "substance")) if t in normal["tools"]]
    if "other" in normal["engines"] or not wanted:
        wanted.append("default")
    labels = [c[0] for c in budget.naming_conventions if c[0] in agk.NAMING_PRESETS]
    expect(f"{label}: naming presets {wanted}", labels == wanted, str(labels))
    expect(f"{label}: broken geometry is {normal['broken_geometry'][0]}",
           budget.degenerate_mode == normal["broken_geometry"][0])
    defaults = agk.Budget()
    changed = [f.name for f in fields(agk.Budget)
               if f.name not in WIZARD_FIELDS and getattr(budget, f.name) != getattr(defaults, f.name)]
    expect(f"{label}: nothing outside the wizard's answers changes", not changed, str(changed))
    tags = agk.build_tag_budgets(document)
    if len(meshes) > 1 and len(platforms) == 1:
        tag_ok = set(tags) == set(meshes) and all(
            tags[k].max_triangles == expected[platforms[0]]["prop" if k == "props" else "character"]
            for k in meshes)
        expect(f"{label}: one folder budget per kind of mesh", tag_ok,
               str({k: b.max_triangles for k, b in tags.items()}))
    else:
        expect(f"{label}: no active folder budgets (one kind, or several platforms)", not tags,
               str(sorted(tags)))
    text = rules.read_text(encoding="utf-8")
    expect(f"{label}: the header records the answers", wiz.ANSWERS_MARKER + wiz.answers_json(normal)
           in text and "--wizard" in text)
    return document, budget, rules


print("1. Answers: names, numbers, aliases, several at once")
engines = wiz.QUESTION_BY_KEY["engines"]
expect("numbers pick choices", wiz.normalise_answer(engines, "1, 3") == ("unreal", "godot"))
expect("space-separated numbers work too", wiz.normalise_answer(engines, "1 2") == ("unreal", "unity"))
expect("names and aliases, any case, in the questions' order",
       wiz.normalise_answer(engines, "Godot, UE5") == ("unreal", "godot"))
expect("a list from JSON", wiz.normalise_answer(wiz.QUESTION_BY_KEY["tools"], ["3ds Max", "substance painter"])
       == ("max", "substance"))
expect("duplicates collapse", wiz.normalise_answer(engines, "unity,unity,2") == ("unity",))
for question, raw, needle in ((engines, "cryengine", "'cryengine' is not one of"),
                              (wiz.QUESTION_BY_KEY["style"], "realistic, stylised", "pick one"),
                              (engines, "9", "1-4"),
                              (engines, " , ", "pick at least one")):
    try:
        wiz.normalise_answer(question, raw)
        expect(f"{raw!r} is refused", False)
    except wiz.WizardError as exc:
        expect(f"{raw!r} is refused with a clear reason", needle in str(exc), str(exc))
for raw, needle in (({"engine": ["unreal"]}, "unknown answer engine"),
                    ({"replace": "yes"}, "replace: expected true or false"),
                    ({"platforms": True}, "expected a list"),
                    ([], "JSON object")):
    try:
        wiz.normalise_answers(raw)
        expect(f"answers {raw!r} are refused", False)
    except wiz.WizardError as exc:
        expect(f"answers {raw!r} are refused: {needle}", needle in str(exc), str(exc))
expect("a missing question takes its default",
       wiz.normalise_answers({"style": "stylized"})["platforms"] == ("pc",)
       and wiz.normalise_answers({"style": "stylized"})["style"] == ("stylised",))

print("2. Every number comes from the gate's own tables")
for platform in wiz.PLATFORMS:
    expect(f"{platform.key} numbers = the shipped table",
           wiz.platform_numbers(platform.key) == preset_numbers(platform.key))
template = (ROOT / "templates" / "rules.toml").read_text(encoding="utf-8")
expect("the midrange prop number is templates/rules.toml's [tags.prop] example",
       f"[tags.prop]\n# max_triangles = {wiz.MIDRANGE_PROP_TRIANGLES}" in template)
expect("every naming preset the wizard writes exists",
       set(wiz.ENGINE_NAMING.values()) - {""} | set(wiz.TOOL_NAMING.values()) <= set(agk.NAMING_PRESETS))

print("3. All-single answers, one question at a time, each file parses and fits")
base = {"engines": "unreal", "tools": "other", "platforms": "pc", "assets": "characters",
        "style": "realistic", "broken_geometry": "block"}
singles = [("defaults", {})]
for question in wiz.QUESTIONS:
    for choice in question.choices:
        singles.append((f"{question.key}={choice.key}", {question.key: choice.key}))
for label, change in singles:
    check_combination(label, {**base, **change})

print("4. Several at once: engines, tools, platforms, kinds of asset")
combos = {
    "unreal+unity / pc+switch / chars+props": {
        "engines": ["unreal", "unity"], "tools": ["maya", "zbrush", "substance"],
        "platforms": ["pc", "switch"], "assets": ["characters", "props"]},
    "all engines / all platforms": {
        "engines": [c.key for c in engines.choices],
        "tools": [c.key for c in wiz.QUESTION_BY_KEY["tools"].choices],
        "platforms": [p.key for p in wiz.PLATFORMS],
        "assets": ["characters", "props", "environment", "ui"], "style": "stylised"},
    "godot+other / mobile+vr / props only": {
        "engines": ["godot", "other"], "tools": ["blender"], "platforms": ["mobile", "vr"],
        "assets": ["props"], "style": "lowpoly", "broken_geometry": "warn"},
    "unity / midrange+n64 / env+ui": {
        "engines": "unity", "platforms": "midrange, n64", "assets": "environment, ui"},
    "unreal / n64 / chars+props+env": {
        "engines": "unreal", "tools": "zbrush", "platforms": "n64",
        "assets": "characters, props, environment", "style": "lowpoly"},
}
results = {label: check_combination(label, answers) for label, answers in combos.items()}
# Pairs of platforms, every one: the base is always the tighter of the two.
for pair in itertools.combinations([p.key for p in wiz.PLATFORMS], 2):
    check_combination("+".join(pair), {**base, "platforms": list(pair),
                                       "assets": ["characters", "props"]})

print("5. What the comments promise is what the gate does")
document, budget, path = results["unreal+unity / pc+switch / chars+props"]
text = path.read_text(encoding="utf-8")
expect("several platforms: folder budgets are written as comments, with each platform's numbers",
       "# [tags.props]\n# max_triangles = 8000" in text and "120,000" in text and "30,000" in text)
expect("... and the base mesh budget is the Switch-class one", budget.max_triangles == 20_000)
expect("the file says how to pick a platform", "--platform pc" in text)
expect("ZBrush points at the Polypaint bake", "--bake-polypaint" in text)
expect("Maya points at the DCC plug-ins doc", "docs/09-DCC-PLUGINS.md" in text)
expect("Unity's bone-weight default is noted, not written", "Skin Weights" in text)
# The reason the tags are comments: a tag's budget beats --platform. If the gate
# ever changes that, the comment (and this choice) must change with it.
tagged = dict(document, tags={"props": {"max_triangles": 8000}})
expect("a [tags.*] budget wins over --platform (why multi-platform tags are comments)",
       agk.build_tag_budgets(agk.apply_platform(tagged, "pc"))["props"].max_triangles == 8000)
n64_doc, n64_budget, n64_path = results["unreal / n64 / chars+props+env"]
expect("retro: power of two on, albedo check off, 64 px textures",
       n64_budget.require_power_of_two and not n64_budget.pbr_check and n64_budget.max_texture_edge == 64)
expect("... and the bake hint says --bake-preset n64",
       "--bake-preset n64" in n64_path.read_text(encoding="utf-8"))
_doc, ui_budget, _path = results["unity / midrange+n64 / env+ui"]
expect("UI: names starting UI_ pass naming without a map suffix, others still need one",
       not agk.texture_name_problem("UI_Icon_Sword", ui_budget)
       and agk.texture_name_problem("Icon_Sword", ui_budget)
       and not agk.texture_name_problem("Rock_Albedo", ui_budget))
_doc, mixed, _path = results["all engines / all platforms"]
expect("several engines: a name in any engine's convention passes",
       all(not agk.texture_name_problem(name, mixed)
           for name in ("T_Rock_D", "Rock_MaskMap", "rock_orm", "Rock_BaseColor", "rock_baseColor")))
expect("stylised art: the albedo check is off", not mixed.pbr_check)
_doc, pc_default, _path = check_combination("pc default again", base)
expect("high-end PC only: power of two is not required", not pc_default.require_power_of_two)

print("6. Folder budgets work in a real run")
project = SCRATCH / "n64-project"
for sub in ("Characters", "Props"):
    (project / sub).mkdir(parents=True, exist_ok=True)


def grid_obj(path, size):
    lines = [f"v {x} {y} 0" for y in range(size + 1) for x in range(size + 1)]
    for y in range(size):
        for x in range(size):
            a = y * (size + 1) + x + 1
            lines += [f"f {a} {a + 1} {a + size + 2}", f"f {a} {a + size + 2} {a + size + 1}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


grid_obj(project / "Characters" / "hero.obj", 20)     # 800 triangles
grid_obj(project / "Props" / "crate.obj", 20)         # 800 triangles
n64_answers = answers_file("n64", {"engines": ["other"], "platforms": ["n64"],
                                   "assets": ["characters", "props"], "style": "lowpoly"})
made = run(project, "--wizard-answers", n64_answers)
expect("the answers file writes the rules (exit 0)", made.returncode == 0, made.stderr[-300:])
gate = run(project, "--once", "--recursive", "--json")
verdicts = {}
for line in gate.stdout.splitlines():
    try:
        item = json.loads(line)
    except ValueError:
        continue
    for entry in item if isinstance(item, list) else [item]:
        if isinstance(entry, dict) and "name" in entry:
            verdicts[entry["name"]] = entry["status"]
expect("an 800-triangle character fits the N64 character budget (1,200)",
       verdicts.get("hero.obj") == "PASSED", str(verdicts) or gate.stderr[-300:])
expect("the same mesh under Props/ fails the N64 prop budget (400)",
       verdicts.get("crate.obj") == "FAILED", str(verdicts))

print("7. The command line: answers file, refusals, replacing")
cli = SCRATCH / "cli"
multi = answers_file("multi", {"engines": ["unreal", "godot"], "tools": ["houdini", "substance"],
                               "platforms": ["pc", "mobile", "switch"],
                               "assets": ["characters", "environment"], "style": "realistic"})
done = run(cli, "--wizard-answers", multi)
expect("--wizard-answers creates the folder and the file", done.returncode == 0
       and (cli / "rules.toml").is_file(), done.stderr[-300:])
_doc, cli_budget = load(cli / "rules.toml")
expect("... with the tightest of pc, mobile and switch", cli_budget.max_triangles == 15_000
       and cli_budget.max_material_slots == 2 and cli_budget.require_power_of_two)
expect("... and it tells the user what to run next", "--once --recursive" in done.stdout
       and "--platform" in done.stdout)
original = (cli / "rules.toml").read_text(encoding="utf-8")
again = run(cli, "--wizard-answers", answers_file("pc", {"platforms": "pc"}))
expect("an existing rules.toml is kept unless the answers say replace (exit 1)",
       again.returncode == 1 and (cli / "rules.toml").read_text(encoding="utf-8") == original
       and "replace" in again.stdout, again.stdout[-300:])
expect("... and no backup is made when nothing is written", not list(cli.glob("rules.backup-*")))
replaced = run(cli, "--wizard-answers", answers_file("pc-replace", {"platforms": "pc", "replace": True}))
backups = list(cli.glob("rules.backup-*.toml"))
expect('"replace": true replaces it and keeps a copy', replaced.returncode == 0 and len(backups) == 1
       and backups[0].read_text(encoding="utf-8") == original
       and load(cli / "rules.toml")[1].max_triangles == 120_000, replaced.stdout[-300:])
forced = run(cli, "--wizard-answers", answers_file("pc", {"platforms": "pc"}), "--force")
expect("--force replaces too, with a second copy", forced.returncode == 0
       and len(list(cli.glob("rules.backup-*.toml"))) == 2)
expect("a backup is not a rules file the gate would load", not any(
    path.name == agk.RULES_FILENAME for path in cli.glob("rules.backup-*")))

refused = run(SCRATCH / "no-tty", "--wizard", timeout=30)
expect("no terminal and no answers file: refuses at once (exit 2), never hangs",
       refused.returncode == 2 and "--wizard-answers" in refused.stderr
       and not (SCRATCH / "no-tty").exists(), refused.stderr[-300:])
bad = run(SCRATCH / "bad", "--wizard-answers", answers_file("bad", {"platforms": ["ps9"]}))
expect("an invalid answer: exit 2 naming the key and the choices",
       bad.returncode == 2 and "platforms: 'ps9' is not one of: pc, midrange" in bad.stderr
       and not (SCRATCH / "bad" / "rules.toml").exists(), bad.stderr[-300:])
broken = SCRATCH / "broken.json"
broken.write_text("{engines: unreal}", encoding="utf-8")
notjson = run(SCRATCH / "bad", "--wizard-answers", broken)
expect("an answers file that is not JSON: exit 2 saying so",
       notjson.returncode == 2 and "not valid JSON" in notjson.stderr, notjson.stderr[-300:])
missing = run(SCRATCH / "bad", "--wizard-answers", SCRATCH / "nope.json")
expect("a missing answers file: exit 2", missing.returncode == 2 and "cannot read" in missing.stderr)
nofolder = run("--wizard-answers", multi)
expect("no folder with an answers file: exit 2 asking for one",
       nofolder.returncode == 2 and "name the folder" in nofolder.stderr, nofolder.stderr[-300:])
blocker = SCRATCH / "a-file"
blocker.write_text("x", encoding="utf-8")
onfile = run(blocker, "--wizard-answers", multi)
expect("a file instead of a folder: exit 2", onfile.returncode == 2 and "not a folder" in onfile.stderr)
yes = run(SCRATCH / "yes", "--wizard", "--yes")
expect("--wizard --yes takes every default without a terminal", yes.returncode == 0
       and load(SCRATCH / "yes" / "rules.toml")[1].max_triangles == 120_000, yes.stderr[-300:])
plain = run(SCRATCH / "plain-init", "--init", "--yes")
expect("plain --init is unchanged: no wizard header, the five-question template",
       plain.returncode == 0 and "created by `asset-gatekeeper --init`"
       in (SCRATCH / "plain-init" / "rules.toml").read_text(encoding="utf-8"))
expect("--init --wizard is the wizard", run(SCRATCH / "init-wizard", "--init", "--wizard", "--yes")
       .returncode == 0 and wiz.ANSWERS_MARKER in (SCRATCH / "init-wizard" / "rules.toml")
       .read_text(encoding="utf-8"))

print("8. Interactive: questions, re-asking, the summary, the replace prompt")


def scripted(replies):
    asked, said = [], []

    def ask(prompt):
        asked.append(prompt)
        if not replies:
            raise EOFError
        return replies.pop(0)
    return ask, asked, said


talk = SCRATCH / "talk"
ask, asked, said = scripted(["1,2", "", "pc, ps9", "pc switch", "", "3", "", ""])
code = wiz.run_wizard(talk, ask=ask, out=said.append, err=said.append, interactive=True)
expect("six questions, a re-ask after a bad answer, then confirm", code == 0 and len(asked) == 8,
       f"exit {code}, {len(asked)} prompts")
expect("the bad answer is explained and asked again", any("'ps9' is not one of" in line for line in said))
expect("Enter takes the default shown in brackets", "[unreal]" in asked[0] and "[other]" in asked[1])
expect("a summary comes before writing", any(line.startswith("Summary") for line in said)
       and any("--platform" in line and "pc, switch" in line for line in said))
_doc, talk_budget = load(talk / "rules.toml")
expect("the typed answers are what was written",
       [c[0] for c in talk_budget.naming_conventions][:2] == ["unreal", "unity"]
       and talk_budget.max_triangles == 20_000 and talk_budget.degenerate_mode == "block"
       and not talk_budget.pbr_check)
written = (talk / "rules.toml").read_text(encoding="utf-8")

ask, asked, said = scripted(["", "", "", "", "", "", "", ""])
code = wiz.run_wizard(talk, ask=ask, out=said.append, err=said.append, interactive=True)
expect("re-run: the old answers are the defaults", "[unreal, unity]" in asked[0]
       and "[pc, switch]" in asked[2], asked[0])
expect("replacing asks, and Enter (No) keeps the file (exit 1)",
       code == 1 and "Replace it" in asked[-1] and "[y/N]" in asked[-1]
       and (talk / "rules.toml").read_text(encoding="utf-8") == written, asked[-1])
ask, asked, said = scripted(["", "", "n64", "", "", "", "", "y"])
code = wiz.run_wizard(talk, ask=ask, out=said.append, err=said.append, interactive=True)
expect("'y' replaces it and keeps a copy of the old one",
       code == 0 and load(talk / "rules.toml")[1].max_triangles == 1_200
       and [p.read_text(encoding="utf-8") for p in talk.glob("rules.backup-*.toml")] == [written])
ask, asked, said = scripted(["", "", "", "", "", "", "n"])
expect("'n' at the summary writes nothing (exit 1)",
       wiz.run_wizard(SCRATCH / "talk-no", ask=ask, out=said.append, err=said.append,
                      interactive=True) == 1 and not (SCRATCH / "talk-no").exists())
ask, asked, said = scripted(["2"])
expect("Ctrl+D / Ctrl+Z part-way: cancelled, nothing written (exit 1)",
       wiz.run_wizard(SCRATCH / "talk-eof", ask=ask, out=said.append, err=said.append,
                      interactive=True) == 1 and not (SCRATCH / "talk-eof").exists()
       and any("Cancelled" in line for line in said))
ask, asked, said = scripted(["", '"' + str(SCRATCH / "asked-folder") + '"'] + [""] * 7)
expect("no folder given: it is asked for, quotes from drag-and-drop removed",
       wiz.run_wizard(None, ask=ask, out=said.append, err=said.append, interactive=True) == 0
       and (SCRATCH / "asked-folder" / "rules.toml").is_file())

print("9. Windows: the double-click file")
bat = (ROOT / "tools" / "rules_wizard.bat").read_bytes()
expect("tools\\rules_wizard.bat has Windows line endings",
       b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b""))
expect("... finds Python the way the other tools\\ files do",
       b'"%~dp0..\\.venv\\Scripts\\python.exe"' in bat and b"py -3 -c" in bat)
expect("... runs the wizard in its window and waits",
       b'"%~dp0..\\asset_gatekeeper.py"' in bat and b"--wizard" in bat and b"pause" in bat)
expect("... and leaves its folder before pausing", b'cd /d "%USERPROFILE%"' in bat)
expect("the start file mentions it",
       b"tools\\rules_wizard.bat" in (ROOT / "Start AssetGatekeeper.bat").read_bytes())

shutil.rmtree(SCRATCH, ignore_errors=True)
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
