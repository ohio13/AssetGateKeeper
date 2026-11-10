"""Sanity checks for asset_gatekeeper: new rules, fallbacks, edge cases."""
from __future__ import annotations   # X | None annotations on Python 3.9

import contextlib
import hashlib
import io
import importlib.util
import json
import re
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

# Allow `python tests/test_gatekeeper.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asset_gatekeeper as agk  # noqa: E402

# Group 8 prints non-ASCII asset names. A cp1252 console/pipe would raise
# UnicodeEncodeError and abort the run half-way, so force UTF-8 output here.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

DROP = Path("dropbox")
failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(label)


gate = agk.Gatekeeper(agk.Budget())
problems = lambda name: gate.check(DROP / name).problems  # noqa: E731

print("1. glTF / GLB triangle counting via the dependency-free fallback")
saved = agk.trimesh
agk.trimesh = None
try:
    expect("boss_statue.gltf = 72,500", agk.count_triangles(DROP / "boss_statue.gltf") == 72_500)
    expect("prop_crate.glb = 1,800", agk.count_triangles(DROP / "prop_crate.glb") == 1_800)
    expect("heavy_prop.glb = 80,000", agk.count_triangles(DROP / "heavy_prop.glb") == 80_000)
finally:
    agk.trimesh = saved

print("2. Material slot counting")
for name, expected in (
    ("crate_1mtl.obj", 1),
    ("crate_4mtl.obj", 4),
    ("crate_nomtl.obj", 1),   # faces with no usemtl => default material
    ("lantern.gltf", 1),
    ("boss_statue.gltf", 4),
    ("prop_crate.glb", 2),
):
    actual = agk.count_material_slots(DROP / name)
    expect(f"{name} = {expected} slot(s)", actual == expected, f"got {actual}")

print("3. Naming pattern")
pattern = agk.build_naming_pattern(agk.Budget.texture_suffixes)
for stem, should_match in (
    ("bark_Albedo", True),
    ("bark_albedo", True),      # case-insensitive
    ("bark_Normal_02", True),   # numbered variant
    ("rock_Roughness_3", True),
    ("bark_diffuse", True),           # _Diffuse joined the defaults (6 Nov real-world test)
    ("albedo", False),          # missing separator
    ("bark_Albedo_extra", False),
    ("bark_AlbedoTex", False),
):
    expect(f"'{stem}' {'matches' if should_match else 'rejected'}",
           bool(pattern.search(stem)) is should_match)

print("4. Power-of-two test")
for value, expected in ((0, False), (1, True), (256, True), (512, True), (1024, True),
                        (2048, True), (300, False), (1000, False), (4096, True)):
    expect(f"is_power_of_two({value}) is {expected}", agk.is_power_of_two(value) is expected)

print("5. Rule outcomes (one line, all violations)")
expect("bark_Albedo.png passes", not problems("bark_Albedo.png"))
expect("bark_Normal_02.png passes", not problems("bark_Normal_02.png"))
expect("atlas_max.png fails naming only", problems("atlas_max.png") == (
    f"name does not end with a standard suffix ({agk.describe_suffixes(agk.Budget().texture_suffixes)})",),
    str(problems("atlas_max.png")))
naming_pattern = agk.build_naming_pattern(agk.Budget().texture_suffixes)
expect("a _BaseColor map passes naming", bool(naming_pattern.search("bark_BaseColor")))
expect("an _ORM packed map passes naming", bool(naming_pattern.search("bark_ORM")))
expect("a numbered _BaseColor map passes naming",
       bool(naming_pattern.search("bark_BaseColor_02")))
expect("hull_Albedo.png fails resolution only", len(problems("hull_Albedo.png")) == 1,
       str(problems("hull_Albedo.png")))
expect("ui_icon.png reports 2 violations", len(problems("ui_icon.png")) == 2)
expect("boss_statue.gltf reports 2 violations (tris + slots)", len(problems("boss_statue.gltf")) == 2)
expect("crate_1mtl.obj passes", not problems("crate_1mtl.obj"))
expect("crate_nomtl.obj passes", not problems("crate_nomtl.obj"))
expect("crate_4mtl.obj fails on slots only", len(problems("crate_4mtl.obj")) == 1)
expect("prop_crate.glb passes", not problems("prop_crate.glb"))

print("6. Rule toggles")
lenient = agk.Gatekeeper(agk.Budget(require_power_of_two=False))
expect("--allow-npot silences POT failure",
       not any("power of two" in p for p in lenient.check(DROP / "ui_icon.png").problems))
unchecked = agk.Gatekeeper(agk.Budget(enforce_texture_naming=False))
expect("--no-naming silences naming failure",
       not any("suffix" in p for p in unchecked.check(DROP / "atlas_max.png").problems))
tight = agk.Gatekeeper(agk.Budget(max_material_slots=1))
expect("max_material_slots=1 rejects a 2-slot mesh",
       any("material slots" in p for p in tight.check(DROP / "prop_crate.glb").problems))

print("7. Broken exports fail fast instead of passing")
scratch = Path("scratch")
scratch.mkdir(exist_ok=True)
for name, blob in (("empty_Albedo.png", b""),
                   ("empty.obj", b""),
                   ("truncated_Albedo.png", b"\x89PNG\r\n\x1a\n")):
    (scratch / name).write_bytes(blob)

for name in ("empty_Albedo.png", "empty.obj", "truncated_Albedo.png"):
    path = scratch / name
    start = time.monotonic()
    stable = agk.wait_until_stable(path)
    elapsed = time.monotonic() - start
    verdict = gate.check(path)
    expect(f"{name} settles quickly", stable and elapsed < 3.0, f"{elapsed:.2f}s")
    expect(f"{name} fails with a reason", not verdict.passed and verdict.reason, verdict.tag)

print("8. Unicode folder + filenames")
uni = scratch / "å¯¼å‡º_assets"
uni.mkdir(exist_ok=True)
shutil.copy(DROP / "crate_4mtl.obj", uni / "å¢™ä½“.obj")
shutil.copy(DROP / "bark_Albedo.png", uni / "æ ‘çš®_Albedo.png")
for path in sorted(uni.iterdir()):
    verdict = gate.check(path)
    print(f"     {verdict.path.name}: {verdict.tag} {verdict.metric}")
    expect(f"unicode asset analysed ({path.name})", bool(verdict.metric))

print("9. rules.toml configuration")
cfg = scratch / "rules_demo"
cfg.mkdir(exist_ok=True)
(cfg / "rules.toml").write_text(
    "[mesh]\nmax_triangles = 10\nmax_material_slots = 1\n\n"
    "[texture]\nmax_edge = 512\npower_of_two = false\nnaming = false\n"
    "suffixes = [\"_Diffuse\"]\n",
    encoding="utf-8",
)

parse = lambda *extra: agk.build_parser().parse_args([str(cfg), *extra])  # noqa: E731
budget, source, overrides = agk.resolve_budget(cfg, parse())
expect("rules.toml is discovered automatically", source is not None and source.name == "rules.toml")
expect("file values are applied", (budget.max_triangles, budget.max_texture_edge,
                                  budget.max_material_slots) == (10, 512, 1), str(budget))
expect("booleans from file are applied",
       budget.require_power_of_two is False and budget.enforce_texture_naming is False)
expect("suffix list from file is applied", budget.texture_suffixes == ("_Diffuse",))
expect("no CLI overrides reported", not overrides)

strict = agk.Gatekeeper(budget)
expect("12-tri cube fails a rules.toml budget of 10",
       not strict.check(DROP / "crate_1mtl.obj").passed)
expect("NPOT texture accepted when file says power_of_two=false",
       not any("power of two" in p for p in strict.check(DROP / "ui_icon.png").problems))
expect("naming follows the file's suffix list",
       not strict.check(DROP / "bark_diffuse.png").problems)

budget2, _, overrides2 = agk.resolve_budget(cfg, parse("--triangles", "99999"))
expect("CLI overrides rules.toml", budget2.max_triangles == 99_999 and "max_triangles" in overrides2)
expect("CLI only overrides what was passed", budget2.max_texture_edge == 512)

# A negative cap is a typo, not a tighter budget: it used to be accepted and then
# failed every mesh in the repository, blaming the art.
for flag, value, label in (("--triangles", "-1", "a negative triangle cap"),
                           ("--triangles", "lots", "a non-numeric triangle cap"),
                           ("--max-texture", "0", "a zero texture edge"),
                           ("--max-materials", "-2", "a negative material count"),
                           ("--max-influences", "-1", "a negative weight count"),
                           ("--max-uvs", "0", "zero UV channels")):
    try:
        agk.build_parser().parse_args([str(cfg), flag, value])
        expect(f"{label} is refused", False, "the parser accepted it")
    except SystemExit:
        expect(f"{label} is refused", True)
try:
    agk.Budget(max_triangles=-1)
    expect("a negative budget is refused wherever it is built", False, "no error raised")
except agk.ConfigError as exc:
    expect("a negative budget is refused wherever it is built",
           "max_triangles" in str(exc), str(exc))
try:
    agk.Budget(pbr_floor=0.9, pbr_ceiling=0.1)
    expect("an inverted PBR band is refused", False, "no error raised")
except agk.ConfigError:
    expect("an inverted PBR band is refused", True)

pot_cfg = scratch / "rules_pot"
pot_cfg.mkdir(exist_ok=True)
(pot_cfg / "rules.toml").write_text("[texture]\npower_of_two = true\nnaming = true\n", encoding="utf-8")
from_file, _, _ = agk.resolve_budget(pot_cfg, agk.build_parser().parse_args([str(pot_cfg)]))
expect("file can turn power_of_two back on", from_file.require_power_of_two is True)
forced_off, _, off_overrides = agk.resolve_budget(
    pot_cfg, agk.build_parser().parse_args([str(pot_cfg), "--allow-npot"]))
expect("--allow-npot beats the file",
       forced_off.require_power_of_two is False
       and list(off_overrides) == ["require_power_of_two"],
       str(off_overrides))

missing = scratch / "no_rules_here"
missing.mkdir(exist_ok=True)
default_budget, no_source, _ = agk.resolve_budget(missing, agk.build_parser().parse_args([str(missing)]))
expect("missing rules.toml falls back to defaults",
       no_source is None and default_budget == agk.Budget())

json_rules = scratch / "rules.json"
json_rules.write_text('{"mesh": {"max_triangles": 777}}', encoding="utf-8")
json_budget, json_source, _ = agk.resolve_budget(
    missing, agk.build_parser().parse_args([str(missing), "--rules", str(json_rules)]))
expect("--rules accepts a JSON file", json_budget.max_triangles == 777, str(json_source))

bom_cfg = scratch / "rules_bom"
bom_cfg.mkdir(exist_ok=True)
(bom_cfg / "rules.toml").write_bytes(b"\xef\xbb\xbf[mesh]\nmax_triangles = 64\n")
bom_budget, _, _ = agk.resolve_budget(bom_cfg, agk.build_parser().parse_args([str(bom_cfg)]))
expect("rules.toml written with a BOM still loads (Notepad / PowerShell)",
       bom_budget.max_triangles == 64, str(bom_budget.max_triangles))
(bom_cfg / "rules.toml").write_bytes(b"# R\xe9gles\r\n[mesh]\r\nmax_triangles = 64 # caf\xe9\r\n")
try:
    agk.parse_rules(bom_cfg / "rules.toml")
    _ansi = "loaded"
except agk.ConfigError as exc:
    _ansi = str(exc)
except UnicodeDecodeError as exc:
    _ansi = f"bare {exc!r}"
expect("a rules.toml saved as Windows-1252 ('ANSI') is refused saying to save it as UTF-8, "
       "with the line", "save it as UTF-8" in _ansi and "line 1" in _ansi, _ansi)
(bom_cfg / "rules.toml").write_bytes(b"\xef\xbb\xbf[mesh]\nmax_triangles = 64\n")

for label, blob in (
    ("unknown key is rejected", "[mesh]\nmax_triange = 10\n"),
    ("wrong type is rejected", "[mesh]\nmax_triangles = \"lots\"\n"),
    ("bad section is rejected", "[meshes]\nmax_triangles = 10\n"),
    ("malformed toml is rejected", "[mesh\nmax_triangles = 10\n"),
):
    (cfg / "rules.toml").write_text(blob, encoding="utf-8")
    try:
        agk.resolve_budget(cfg, parse())
        expect(label, False, "no error raised")
    except agk.ConfigError as exc:
        expect(label, True, str(exc).split(":")[-1].strip()[:48])

# leave a valid file behind for the CLI demo
(cfg / "rules.toml").write_text("[mesh]\nmax_triangles = 10\n", encoding="utf-8")

print("10. deep data profiling")
expect("uv channels: rig_ok has 2", agk.count_uv_channels(DROP / "rig_ok.gltf") == 2)
expect("uv channels: uv_heavy has 5", agk.count_uv_channels(DROP / "uv_heavy.gltf") == 5)
expect("uv channels: obj without vt has 0", agk.count_uv_channels(DROP / "crate_1mtl.obj") == 0)
expect("influences: rig_ok has 4", agk.max_bone_influences(DROP / "rig_ok.gltf") == 4)
expect("influences: rig_heavy has 6", agk.max_bone_influences(DROP / "rig_heavy.gltf") == 6)
expect("influences: obj has none", agk.max_bone_influences(DROP / "crate_1mtl.obj") == 0)
expect("degenerate: broken_glass.obj has 1",
       agk.count_degenerate_triangles(DROP / "broken_glass.obj") == 1)
expect("degenerate: shattered.gltf has 1",
       agk.count_degenerate_triangles(DROP / "shattered.gltf") == 1)
expect("degenerate: clean mesh has 0",
       agk.count_degenerate_triangles(DROP / "crate_1mtl.obj") == 0)
expect("rig_ok.gltf passes every deep rule", not problems("rig_ok.gltf"))
expect("rig_heavy.gltf fails on influences",
       any("bone weights" in p for p in problems("rig_heavy.gltf")))
expect("uv_heavy.gltf fails on UV channels",
       any("UV channels" in p for p in problems("uv_heavy.gltf")))
expect("broken_glass.obj fails on degenerate triangles",
       any("degenerate" in p for p in problems("broken_glass.obj")))
lenient_geo = agk.Gatekeeper(agk.Budget(allow_degenerate=True))
expect("allow_degenerate=True tolerates zero-area faces",
       not any("degenerate" in p for p in lenient_geo.check(DROP / "broken_glass.obj").problems))
strict_rig = agk.Gatekeeper(agk.Budget(max_bone_influences=2))
expect("max_bone_influences=2 rejects a 4-influence mesh",
       any("bone weights" in p for p in strict_rig.check(DROP / "rig_ok.gltf").problems))

loose_rig_cfg = scratch / "rules_rig"
loose_rig_cfg.mkdir(exist_ok=True)
(loose_rig_cfg / "rules.toml").write_text(
    "[mesh]\nmax_bone_influences = 8\nmax_uv_channels = 6\nallow_degenerate = true\n", encoding="utf-8")
rig_budget, _, _ = agk.resolve_budget(loose_rig_cfg, agk.build_parser().parse_args([str(loose_rig_cfg)]))
expect("rules.toml sets the deep budgets",
       (rig_budget.max_bone_influences, rig_budget.max_uv_channels, rig_budget.allow_degenerate)
       == (8, 6, True), str(rig_budget))

print("11. telemetry (fire-and-forget, never blocks)")


_log_lines: list[str] = []


class _LogSink(logging.Handler):
    def emit(self, record):
        try:
            _log_lines.append(record.getMessage())
        except Exception:  # pragma: no cover - logging must never break a test
            _log_lines.append(str(record.msg))


_sink = _LogSink()


class _capture_logs:
    """Capture the tool's own log lines; no pytest caplog in this suite."""

    def __enter__(self):
        _log_lines.clear()
        self._level = agk.LOG.level
        agk.LOG.setLevel(logging.DEBUG)
        agk.LOG.addHandler(_sink)
        return _log_lines

    def __exit__(self, *_exc):
        agk.LOG.removeHandler(_sink)
        agk.LOG.setLevel(self._level)
        return False


def _broken_payload_swallowed(module, verdict, root):
    """A payload builder that explodes must not stop the asset line."""
    tele = module.Telemetry("https://studio.example/x", root=root)
    with patch.object(module, "build_telemetry_payload", side_effect=RuntimeError("boom")):
        try:
            tele.submit(verdict)
        except Exception:  # noqa: BLE001
            return False
    return tele.dropped == 1


def _accepts_unknown_remote(module, document, root, args_factory):
    try:
        module.resolve_budget(root, args_factory(), document)
        return True
    except module.ConfigError:
        return False


class FakeResponse:
    """Minimal stand-in for the object urllib.request.urlopen returns."""

    status = 200

    def __init__(self, body: bytes = b"{}", headers=None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


tele_cfg = scratch / "telemetry"
tele_cfg.mkdir(exist_ok=True)
subject = gate.check(DROP / "boss_statue.gltf")
payload = agk.build_telemetry_payload(subject, tele_cfg)
required = {"schema", "timestamp", "hostname", "username", "platform", "python",
            "name", "path", "size_bytes", "passed", "metric", "stats", "problems"}
expect("payload carries every required field", required <= set(payload), str(sorted(payload)))
expect("payload names the asset", payload["name"] == "boss_statue.gltf")
expect("payload carries an absolute path for the dashboard bridge",
       Path(payload["path"]).is_absolute() and Path(payload["path"]).name == "boss_statue.gltf",
       payload["path"])
expect("and keeps the relative form for display",
       payload["relative_path"] == "boss_statue.gltf", str(payload.get("relative_path")))
expect("telemetry keeps the machine name so per-host views still work",
       payload["hostname"] != "", repr(payload["hostname"]))
expect("telemetry does not volunteer the OS account by default",
       payload["username"] == "", repr(payload["username"]))
full_payload = agk.build_telemetry_payload(subject, tele_cfg, identity="full")
expect("identity=full adds the OS account for a studio that wants it",
       full_payload["username"] != "", repr(full_payload["username"]))
expect("identity=full also restores the absolute path",
       Path(full_payload["path"]).is_absolute(), full_payload["path"])
anon_payload = agk.build_telemetry_payload(subject, tele_cfg, identity="none")
expect("identity=none sends only the path inside the watch folder",
       anon_payload["path"] == "boss_statue.gltf", anon_payload["path"])
expect("identity=none sends no host and no user",
       anon_payload["hostname"] == "" and anon_payload["username"] == "",
       repr((anon_payload["hostname"], anon_payload["username"])))
expect("an unknown identity mode falls back to the default",
       agk.build_telemetry_payload(subject, tele_cfg, identity="nonsense")["username"] == "")
expect("[server] identity accepts the three documented modes",
       [agk.ServerSettings.from_mapping({"identity": mode}).identity
        for mode in agk.IDENTITY_MODES] == list(agk.IDENTITY_MODES))
expect("--telemetry-identity reaches the settings",
       agk.resolve_server(tele_cfg, agk.build_parser().parse_args(
           [str(tele_cfg), "--telemetry-identity", "none"])).identity == "none")
try:
    agk.ServerSettings.from_mapping({"identity": "anon"})
    expect("[server] identity rejects a typo", False, "no error raised")
except agk.ConfigError as exc:
    expect("[server] identity rejects a typo", "identity must be one of" in str(exc), str(exc))
expect("payload includes file size", payload["size_bytes"] > 0, str(payload["size_bytes"]))
expect("payload includes structured stats", payload["stats"].get("triangles") == 72_500,
       str(payload["stats"]))
expect("payload keeps every problem string", len(payload["problems"]) == len(subject.problems) == 2)
expect("payload survives JSON round-trip", json.loads(json.dumps(payload))["passed"] is False)

sent_requests = []


def capture(request, timeout=None):
    sent_requests.append((request, timeout))
    return FakeResponse(b'{"accepted": true}')


with patch("urllib.request.urlopen", side_effect=capture):
    tele = agk.Telemetry("https://studio.example/api/verdicts", root=tele_cfg, token="s3cret")
    tele.submit(subject)
    tele.submit(gate.check(DROP / "crate_1mtl.obj"))
    tele.close(timeout=5.0)

expect("two verdicts, two POSTs", len(sent_requests) == 2 and tele.sent == 2, str(tele.sent))
expect("request is a POST with a JSON body",
       sent_requests[0][0].method == "POST"
       and sent_requests[0][0].get_header("Content-type") == "application/json",
       str(sent_requests[0][0].method))
expect("token is sent as a bearer header",
       sent_requests[0][0].get_header("Authorization") == "Bearer s3cret")
expect("the strict 2.0s timeout is applied", all(t == 2.0 for _, t in sent_requests),
       str([t for _, t in sent_requests]))
expect("body is the payload",
       json.loads(sent_requests[1][0].data)["name"] == "crate_1mtl.obj")

# Server down / timeout: silent, counted, and still no exception.
timeout_tele = agk.Telemetry("https://studio.example/api/verdicts", root=tele_cfg)
with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
    with _capture_logs() as lines:
        try:
            timeout_tele.submit(subject)
            timeout_tele.close(timeout=5.0)
            raised = False
        except Exception:  # noqa: BLE001 - that is exactly what must not happen
            raised = True
expect("a timeout never propagates to the caller", not raised)
expect("the timeout is recorded as a failure",
       timeout_tele.failed == 1 and timeout_tele.sent == 0, str(timeout_tele.failed))
expect("the failure is only a debug line",
       any("POST failed" in line for line in lines), str(lines))

http_tele = agk.Telemetry("https://studio.example/api/verdicts", root=tele_cfg)
with patch("urllib.request.urlopen",
           side_effect=urllib.error.HTTPError("https://x", 503, "busy", None, None)):
    http_tele.submit(subject)
    http_tele.close(timeout=5.0)
expect("an HTTP error is swallowed too", http_tele.failed == 1 and http_tele.sent == 0)

with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("dns")):
    dead_tele = agk.Telemetry("https://nowhere.invalid/x", root=tele_cfg)
    dead_tele.submit(subject)
    dead_tele.close(timeout=5.0)
expect("DNS failure is silent", dead_tele.failed == 1)

# A slow endpoint must not slow submission: submit() returns while the worker is stuck.
hold = threading.Event()
slow_tele = agk.Telemetry("https://slow.example/x", root=tele_cfg, queue_max=2)
slow_tele._post = lambda _payload: hold.wait(5)   # pretend the socket hangs
started = time.monotonic()
for _ in range(6):
    slow_tele.submit(subject)
elapsed_submit = time.monotonic() - started
hold.set()
slow_tele.close(timeout=5.0)
expect("submit() does not wait for the network", elapsed_submit < 1.0, f"{elapsed_submit:.3f}s")
expect("a full queue drops instead of blocking", slow_tele.dropped >= 1, str(slow_tele.dropped))

reporter_calls = []
fake_telemetry = agk.Telemetry("https://studio.example/x", root=tele_cfg)
fake_telemetry.submit = lambda verdict, root=None: reporter_calls.append(verdict)
quiet = agk.Reporter(color=False, root=tele_cfg, telemetry=fake_telemetry)
quiet.report(subject)
quiet.close()
expect("Reporter forwards every verdict to telemetry", len(reporter_calls) == 1)
plain = agk.Reporter(color=False, root=tele_cfg)
plain.report(subject)
plain.close()
expect("Reporter without telemetry stays a no-op", plain.passed == 0 and plain.failed == 1)

expect("a broken payload cannot raise",
       _broken_payload_swallowed(agk, subject, tele_cfg))

print("12. remote rules + offline cache")

remote_cfg = scratch / "remote"
remote_cfg.mkdir(exist_ok=True)
cache_file = remote_cfg / agk.RULES_CACHE_FILENAME
if cache_file.exists():
    cache_file.unlink()

remote_doc = {"mesh": {"max_triangles": 12_345}, "texture": {"max_edge": 1_024}}
remote_body = json.dumps(remote_doc).encode("utf-8")

with patch("urllib.request.urlopen", return_value=FakeResponse(remote_body)):
    fetched = agk.fetch_remote_rules("https://studio.example/rules.json", cache_file)

expect("remote profile is fetched", fetched == remote_doc, str(fetched))
expect("remote profile is cached next to the assets", cache_file.is_file())
cached_wrapper = json.loads(cache_file.read_text(encoding="utf-8"))
expect("cache records provenance", cached_wrapper["source"] == "https://studio.example/rules.json"
       and cached_wrapper["rules"] == remote_doc)

with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
    with _capture_logs() as lines:
        fallback = agk.fetch_remote_rules("https://studio.example/rules.json", cache_file)

expect("an offline server falls back to the cached profile", fallback == remote_doc)
expect("the fallback says so, in those words",
       any("Central server offline; using cached rules profile" in line for line in lines),
       str(lines[-1] if lines else ""))

empty_cfg = scratch / "remote_cold"
empty_cfg.mkdir(exist_ok=True)
with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
    try:
        agk.fetch_remote_rules("https://studio.example/rules.json",
                               empty_cfg / agk.RULES_CACHE_FILENAME)
        expect("offline with no cache refuses instead of using looser defaults", False)
    except agk.ConfigError as exc:
        # Audited: falling back to local defaults let a studio's central budget be
        # bypassed by unplugging a cable. A gate that cannot reach its policy must
        # stop, not guess.
        expect("offline with no cache refuses instead of using looser defaults",
               "remote rules unreachable" in str(exc), str(exc)[:80])

with patch("urllib.request.urlopen", return_value=FakeResponse(b'{"mesh": {"max_trian')):
    broken = agk.fetch_remote_rules("https://studio.example/rules.json", cache_file)
expect("a malformed payload falls back to the cache", broken == remote_doc)
expect("a malformed payload cannot poison the cache",
       json.loads(cache_file.read_text(encoding="utf-8"))["rules"] == remote_doc)

with patch("urllib.request.urlopen", return_value=FakeResponse(b"not json or toml at all")):
    junk = agk.fetch_remote_rules("https://studio.example/rules.json", cache_file)
expect("unparseable payload also falls back", junk == remote_doc)

expect("JSON payload parses", agk.parse_rules_text('{"mesh": {"max_triangles": 7}}')["mesh"]["max_triangles"] == 7)
expect("TOML payload parses too (3.11+)",
       agk.parse_rules_text("[mesh]\nmax_triangles = 8\n")["mesh"]["max_triangles"] == 8)
for label, blob in (("empty payload rejected", ""), ("garbage payload rejected", "%not a rule%")):
    try:
        agk.parse_rules_text(blob)
        expect(label, False, "no error raised")
    except agk.ConfigError:
        expect(label, True)

local_cfg = scratch / "remote_precedence"
local_cfg.mkdir(exist_ok=True)
(local_cfg / "rules.toml").write_text("[mesh]\nmax_triangles = 10\n", encoding="utf-8")
cli = lambda *extra: agk.build_parser().parse_args([str(local_cfg), *extra])  # noqa: E731

from_remote, remote_source, _ = agk.resolve_budget(local_cfg, cli(), remote_doc)
expect("remote rules beat rules.toml", from_remote.max_triangles == 12_345, str(from_remote.max_triangles))
expect("remote budgets are not reported as a local file", remote_source is None)
local_only, local_source, _ = agk.resolve_budget(local_cfg, cli())
expect("without remote, rules.toml still wins",
       local_only.max_triangles == 10 and local_source is not None)
over_remote, _, over_remote_names = agk.resolve_budget(local_cfg, cli("--triangles", "99"), remote_doc)
expect("CLI flags beat remote rules",
       over_remote.max_triangles == 99 and list(over_remote_names) == ["max_triangles"],
       str(over_remote_names))
expect("remote rules are validated like any other budget",
       not _accepts_unknown_remote(agk, {"meshes": {"max_triangles": 1}}, local_cfg, cli))

# Multi-tenant: the project token must travel with the remote-rules request, or a
# multi-project server cannot tell which project is asking.
auth_seen = []


def capture_auth(request, timeout=None):
    auth_seen.append(request)
    return FakeResponse(json.dumps({"mesh": {"max_triangles": 4242}}).encode())


with patch("urllib.request.urlopen", side_effect=capture_auth):
    tenant_doc = agk.fetch_remote_rules("https://studio.example/api/rules?project_id=7",
                                        cache_file, timeout=2.0, token="project-token-xyz")
expect("the remote-rules request carries the project token",
       auth_seen and auth_seen[0].get_header("Authorization") == "Bearer project-token-xyz",
       str(auth_seen[0].get_header("Authorization") if auth_seen else None))
expect("a project-scoped rules document still builds a budget",
       agk.Budget.from_mapping(tenant_doc).max_triangles == 4242)
expect("the Accept header asks for rules, not HTML",
       "application/json" in (auth_seen[0].get_header("Accept") or ""),
       str(auth_seen[0].get_header("Accept")))

print("13. server settings resolution")
server_cfg = scratch / "server_cfg"
server_cfg.mkdir(exist_ok=True)
(server_cfg / "rules.toml").write_text(
    "[server]\nurl = \"https://file.example/telemetry\"\n"
    "remote_rules_url = \"https://file.example/rules.json\"\ntoken = \"file-token\"\n",
    encoding="utf-8")

from_files = agk.resolve_server(server_cfg, agk.build_parser().parse_args([str(server_cfg)]))
expect("[server] table is read",
       from_files.telemetry_url == "https://file.example/telemetry"
       and from_files.remote_rules_url == "https://file.example/rules.json"
       and from_files.telemetry_enabled)
with patch.dict(os.environ, {agk.SERVER_ENV_URL: "https://env.example/telemetry",
                             agk.REMOTE_RULES_ENV: "https://env.example/rules.json",
                             agk.SERVER_ENV_TOKEN: "env-token"}):
    from_env = agk.resolve_server(server_cfg, agk.build_parser().parse_args([str(server_cfg)]))
    from_cli = agk.resolve_server(server_cfg, agk.build_parser().parse_args(
        [str(server_cfg), "--telemetry-url", "https://cli.example/telemetry"]))
expect("environment beats the file", from_env.telemetry_url == "https://env.example/telemetry")
expect("environment supplies the token", from_env.token == "env-token")
expect("--telemetry-url beats the environment", from_cli.telemetry_url == "https://cli.example/telemetry")
expect("a bare folder has no server settings",
       agk.resolve_server(empty_cfg, agk.build_parser().parse_args([str(empty_cfg)]))
       == agk.ServerSettings())

# The budget planner publishes healer.enabled; that document arrives as remote
# rules, so the healer must be able to come from there too.
planner_doc = {"mesh": {"max_triangles": 1000}, "healer": {"enabled": True, "texture_mode": "resize"}}
expect("a remote rules document can switch healing on",
       agk.resolve_healer(empty_cfg, agk.build_parser().parse_args([str(empty_cfg)]),
                          planner_doc).enabled is True)
expect("remote healing settings are applied",
       agk.resolve_healer(empty_cfg, agk.build_parser().parse_args([str(empty_cfg)]),
                          planner_doc).texture_mode == "resize")
expect("--no-auto-heal still beats the remote document",
       agk.resolve_healer(empty_cfg, agk.build_parser().parse_args(
           [str(empty_cfg), "--no-auto-heal"]), planner_doc).enabled is False)
expect("without a remote document the local file is the only source",
       agk.resolve_healer(empty_cfg, agk.build_parser().parse_args([str(empty_cfg)])).enabled is False)
expect("the remote budget and healer tables coexist",
       agk.Budget.from_mapping(planner_doc).max_triangles == 1000)

print("14. opt-in auto-healing")

from PIL import Image  # noqa: E402 - only the healing group needs it

ROOT_MODULE_DIR = Path(__file__).resolve().parent.parent
STAGING = ".agk-heal-"


def force_rmtree(path):
    """rmtree that survives Windows' read-only .git objects."""
    import stat

    def on_error(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=on_error)


heal_root = scratch / "healing"
if heal_root.exists():
    force_rmtree(heal_root)
heal_root.mkdir(parents=True)
heal_budget = agk.Budget()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_png(path, size, color=(120, 90, 70)):
    Image.new("RGB", size, color).save(path)
    return Path(path)


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT_MODULE_DIR / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


agk_healer = load_module("agk_healer", "healer.py")
healer_obj = agk.load_healer(heal_budget, agk.HealSettings(enabled=True))
expect("the healer module loads on demand", healer_obj.__class__.__name__ == "Healer")

# -- default is off: the file must not be touched, ever -----------------------
off_dir = heal_root / "off"
off_dir.mkdir()
off_png = make_png(off_dir / "stone_Albedo.png", (1000, 1000))
before = digest(off_png)
off_verdict = agk.Gatekeeper(heal_budget).check(off_png)
expect("healing is off by default: the asset fails", not off_verdict.passed)
expect("healing is off by default: the bytes are identical", digest(off_png) == before)
expect("healing is off by default: no staging folder is created",
       not any(p.name.startswith(STAGING) for p in off_dir.iterdir()))
expect("an untouched verdict reports status FAILED", off_verdict.status == "FAILED")
expect("an untouched verdict has no heal notes", off_verdict.heal_notes == ())

cli_gate = agk.Gatekeeper(heal_budget)
expect("--auto-heal is tri-state (unset by default)",
       agk.build_parser().parse_args([str(off_dir)]).auto_heal is None)
expect("--no-auto-heal forces it off even over a file",
       agk.resolve_healer(off_dir, agk.build_parser().parse_args(
           [str(off_dir), "--no-auto-heal"])).enabled is False)
expect("a project file can enable healing",
       agk.resolve_healer(off_dir, agk.build_parser().parse_args(
           [str(off_dir), "--auto-heal"])).enabled is True)

# -- NPOT padding -------------------------------------------------------------
npot_dir = heal_root / "npot"
npot_dir.mkdir()
npot_png = make_png(npot_dir / "stone_Albedo.png", (1000, 1000))
npot_gate = agk.Gatekeeper(heal_budget, healer=healer_obj)
npot_verdict = npot_gate.check(npot_png)
expect("NPOT texture is healed", npot_verdict.passed and npot_verdict.healed,
       npot_verdict.tag[:60])
expect("the verdict says HEALED", npot_verdict.status == "HEALED")
expect("the tag reads PASSED WITH HEALING", npot_verdict.tag.startswith("PASSED WITH HEALING:"))
expect("the file was rewritten to 1024x1024",
       Image.open(npot_png).size == (1024, 1024), str(Image.open(npot_png).size))
expect("the note records the resize (the default mode)",
       any("resized 1000x1000 -> 1024x1024" in note for note in npot_verdict.heal_notes),
       str(npot_verdict.heal_notes))
expect("the healed file re-inspects as passing",
       agk.Gatekeeper(heal_budget).check(npot_png).passed)
expect("staging folders are cleaned up",
       not any(p.name.startswith(STAGING) for p in npot_dir.iterdir()))

pad_dir = heal_root / "pad"
pad_dir.mkdir()
pad_png = make_png(pad_dir / "stone_Albedo.png", (1000, 1000))
pad_healer = agk.load_healer(heal_budget, agk.HealSettings(enabled=True, texture_mode="pad"))
pad_verdict = agk.Gatekeeper(heal_budget, healer=pad_healer).check(pad_png)
expect("pad mode is honoured when asked for",
       pad_verdict.passed and "padded" in pad_verdict.heal_notes[0],
       str(pad_verdict.heal_notes))
expect("pad mode lands on the same size", Image.open(pad_png).size == (1024, 1024))
expect("padding replicates edge pixels instead of black",
       Image.open(pad_png).getpixel((1023, 5)) == (120, 90, 70),
       str(Image.open(pad_png).getpixel((1023, 5))))

# -- naming -------------------------------------------------------------------
name_dir = heal_root / "naming"
name_dir.mkdir()
name_png = make_png(name_dir / "stone_texture.png", (512, 512))
name_verdict = agk.Gatekeeper(heal_budget, healer=healer_obj).check(name_png)
expect("a missing suffix is healed by renaming", name_verdict.passed, name_verdict.tag[:60])
expect("the renamed file is the one reported",
       name_verdict.path.name == "stone_texture_Albedo.png", name_verdict.path.name)
expect("the old name is gone", not name_png.exists())
expect("the new name exists", (name_dir / "stone_texture_Albedo.png").is_file())

# -- a rename must repoint the references that named the old file --------------
ref_dir = heal_root / "references"
ref_dir.mkdir()
(ref_dir / "prop.mtl").write_text("newmtl prop\nmap_Kd wood.png\n", encoding="utf-8")
(ref_dir / "prop.obj").write_text(
    "mtllib prop.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
ref_png = make_png(ref_dir / "wood.png", (512, 512))
ref_obj_before = digest(ref_dir / "prop.obj")
ref_verdict = agk.Gatekeeper(heal_budget, healer=healer_obj).check(ref_png)
expect("a renamed texture is adopted", ref_verdict.passed and ref_verdict.healed,
       ref_verdict.tag[:60])
expect("the .mtl names the healed texture",
       "map_Kd wood_Albedo.png" in (ref_dir / "prop.mtl").read_text(encoding="utf-8"),
       (ref_dir / "prop.mtl").read_text(encoding="utf-8"))
expect("the mesh's references resolve again",
       agk.missing_references(ref_dir / "prop.obj") == [],
       str(agk.missing_references(ref_dir / "prop.obj")))
expect("the verdict records the repoint",
       any("repointed 1 reference" in note for note in ref_verdict.heal_notes),
       str(ref_verdict.heal_notes))
expect("an .obj that never named the texture is byte-identical",
       digest(ref_dir / "prop.obj") == ref_obj_before)
expect("a rename matches a whole filename, not a fragment",
       agk_healer.rewrite_reference_names("map_Kd oak_wood.png\nmap_Ks wood.png",
                                          {"wood.png": "wood_Albedo.png"})
       == ("map_Kd oak_wood.png\nmap_Ks wood_Albedo.png", 1))

gltf_ref_dir = heal_root / "references_gltf"
(gltf_ref_dir / "textures").mkdir(parents=True)
(gltf_ref_dir / "scene.gltf").write_text(
    '{"asset": {"version": "2.0"}, "images": [{"uri": "textures/wood.png"}]}',
    encoding="utf-8")
gltf_ref_verdict = agk.Gatekeeper(heal_budget, healer=healer_obj).check(
    make_png(gltf_ref_dir / "textures" / "wood.png", (512, 512)))
expect("a texture in a subfolder is healed by renaming", gltf_ref_verdict.healed,
       gltf_ref_verdict.tag[:60])
expect("the glTF uri keeps its folder and takes the new name",
       "textures/wood_Albedo.png" in (gltf_ref_dir / "scene.gltf").read_text(encoding="utf-8"),
       (gltf_ref_dir / "scene.gltf").read_text(encoding="utf-8"))

discard_dir = heal_root / "references_discarded"
discard_dir.mkdir()
(discard_dir / "prop.mtl").write_text("newmtl prop\nmap_Kd wall.png\n", encoding="utf-8")
discard_mtl_before = digest(discard_dir / "prop.mtl")
discard_verdict = agk.Gatekeeper(agk.Budget(max_texture_edge=2048),
                                 healer=healer_obj).check(
    make_png(discard_dir / "wall.png", (4096, 1000), (120, 90, 70)))
expect("a refused heal leaves the references untouched",
       digest(discard_dir / "prop.mtl") == discard_mtl_before)
expect("a refused heal leaves no edit scratch file",
       not any(p.name.endswith(".agk-edit") for p in discard_dir.iterdir()),
       str([p.name for p in discard_dir.iterdir()]))

suffixes = heal_budget.texture_suffixes
expect("the suffix is inferred from 'nrm'",
       agk_healer.choose_suffix("wall_nrm", suffixes) == "_Normal")
expect("a stem that already ends with a configured suffix is left alone",
       agk_healer.choose_suffix("wall_normal", suffixes) is None,
       "_normal already IS the _Normal suffix, case-insensitively")
expect("the suffix is inferred from 'rough'",
       agk_healer.choose_suffix("metal_rough", suffixes) == "_Roughness")
expect("the suffix is inferred from 'basecolor'",
       agk_healer.choose_suffix("floor_basecolor_2k", suffixes) == "_BaseColor")
expect("the suffix is inferred from 'orm'",
       agk_healer.choose_suffix("wall_orm_4k", suffixes) == "_ORM")
expect("a name that already ends in _BaseColor is left alone",
       agk_healer.choose_suffix("floor_basecolor", suffixes) is None)
expect("a name that already ends in _ORM is left alone",
       agk_healer.choose_suffix("wall_orm", suffixes) is None)
expect("'normal' is not read as an ORM map",
       agk_healer.choose_suffix("wall_normal", suffixes) is None)
expect("'abnormal' is not read as a normal map",
       agk_healer.choose_suffix("abnormal", suffixes) == "_Albedo")
expect("a compliant name is left alone",
       agk_healer.choose_suffix("wall_Albedo", suffixes) is None)
expect("a compliant name is matched case-insensitively",
       agk_healer.choose_suffix("wall_albedo", suffixes) is None)
expect("an unknown name falls back to the first suffix",
       agk_healer.choose_suffix("mystery", suffixes) == "_Albedo")

# -- degenerate geometry -------------------------------------------------------
mesh_dir = heal_root / "mesh"
mesh_dir.mkdir()
shutil.copy(DROP / "broken_glass.obj", mesh_dir / "broken_glass.obj")
mesh_before = digest(mesh_dir / "broken_glass.obj")
mesh_verdict = agk.Gatekeeper(heal_budget, healer=healer_obj).check(mesh_dir / "broken_glass.obj")
expect("degenerate faces are stripped",
       mesh_verdict.passed and any("Stripped 1 degenerate face" in n for n in mesh_verdict.heal_notes),
       str(mesh_verdict.heal_notes))
expect("the tag matches the documented wording",
       mesh_verdict.tag == "PASSED WITH HEALING: Stripped 1 degenerate face", mesh_verdict.tag)
expect("the mesh file was actually rewritten",
       digest(mesh_dir / "broken_glass.obj") != mesh_before)
expect("the healed mesh still has geometry",
       agk.count_triangles(mesh_dir / "broken_glass.obj") >= 1)

# -- safety: refuse when the heal cannot finish the job ------------------------
safe_dir = heal_root / "safe"
safe_dir.mkdir()
safe_png = make_png(safe_dir / "huge_Albedo.png", (4096, 1000))
safe_before = digest(safe_png)
safe_gate = agk.Gatekeeper(agk.Budget(max_texture_edge=2048), healer=healer_obj)
safe_verdict = safe_gate.check(safe_png)
expect("a heal that cannot pass is not adopted", not safe_verdict.passed)
expect("the original bytes survive a rejected heal", digest(safe_png) == safe_before)
expect("the same size survives a rejected heal", Image.open(safe_png).size == (4096, 1000))
expect("a rejected heal leaves no staging folder",
       not any(p.name.startswith(STAGING) for p in safe_dir.iterdir()))

clobber_dir = heal_root / "clobber"
clobber_dir.mkdir()
clobber_src = make_png(clobber_dir / "wall.png", (512, 512))
clobber_target = make_png(clobber_dir / "wall_Albedo.png", (256, 256), (10, 200, 10))
target_before = digest(clobber_target)
clobber_verdict = agk.Gatekeeper(heal_budget, healer=healer_obj).check(clobber_src)
expect("healing never clobbers an existing file", not clobber_verdict.passed)
expect("the existing file is untouched", digest(clobber_target) == target_before)
expect("the source is left as it was", clobber_src.exists())

gltf_dir = heal_root / "gltf"
gltf_dir.mkdir()
shutil.copy(DROP / "shattered.gltf", gltf_dir / "shattered.gltf")
gltf_before = digest(gltf_dir / "shattered.gltf")
gltf_verdict = agk.Gatekeeper(heal_budget, healer=healer_obj).check(gltf_dir / "shattered.gltf")
expect("glTF geometry is not rewritten blind", not gltf_verdict.passed)
expect("the glTF is byte-identical", digest(gltf_dir / "shattered.gltf") == gltf_before)
expect("an unhealable asset still reports FAILED", gltf_verdict.status == "FAILED")

# -- telemetry + reporter contract --------------------------------------------
healed_payload = agk.build_telemetry_payload(npot_verdict, npot_dir)
expect("telemetry reports status HEALED", healed_payload["status"] == "HEALED", healed_payload["status"])
expect("telemetry carries the modifications",
       healed_payload["modifications"] and "1024x1024" in healed_payload["modifications"][0],
       str(healed_payload["modifications"]))
failed_payload = agk.build_telemetry_payload(off_verdict, off_dir)
expect("telemetry still says FAILED for untouched failures", failed_payload["status"] == "FAILED")
expect("a failed verdict reports no modifications", failed_payload["modifications"] == [])

reporter = agk.Reporter(color=False, root=npot_dir)
reporter._paint = lambda text, code: text   # keep the assertion readable
with _capture_logs() as lines:
    reporter.report(npot_verdict)
expect("the reporter prints the healing line",
       any("PASSED WITH HEALING" in line for line in lines), str(lines[-1:]))
expect("a healed asset counts as passed", reporter.passed == 1 and reporter.failed == 0)


print("14b. healing: resize by default, backups, undo, one file at a time")
import contextlib as _contextlib  # noqa: E402
import io as _io  # noqa: E402
expect("the default texture mode is resize (pad shifts UVs)",
       agk.HealSettings().texture_mode == "resize", agk.HealSettings().texture_mode)
expect("... and so is the healer's own default",
       agk_healer.heal_texture_file.__defaults__[0] == "resize")


def marker_png(path, size=(1000, 1000)):
    """Grey, with a red square at UV (0.5, 0.5)."""
    image = Image.new("RGB", size, (110, 100, 90))
    for x in range(size[0] // 2 - 5, size[0] // 2 + 6):
        for y in range(size[1] // 2 - 5, size[1] // 2 + 6):
            image.putpixel((x, y), (255, 0, 0))
    image.save(path)
    return Path(path)


def red(pixel):
    return pixel[0] > 200 and pixel[1] < 80


uv_dir = heal_root / "uv"
uv_dir.mkdir()
uv_png = marker_png(uv_dir / "marker_Albedo.png")
uv_verdict = agk.Gatekeeper(heal_budget, healer=agk.load_healer(
    heal_budget, agk.HealSettings(enabled=True), root=uv_dir)).check(uv_png)
expect("a default heal keeps UV 0.5 on the marker (pad moved it 12 px)",
       uv_verdict.healed and red(Image.open(uv_png).convert("RGB").getpixel((512, 512))),
       str(Image.open(uv_png).convert("RGB").getpixel((512, 512))))
uv_pad = marker_png(uv_dir / "padded_Albedo.png")
agk.Gatekeeper(heal_budget, healer=agk.load_healer(
    heal_budget, agk.HealSettings(enabled=True, texture_mode="pad"), root=uv_dir)).check(uv_pad)
expect("... which is the documented reason pad is opt-in",
       not red(Image.open(uv_pad).convert("RGB").getpixel((512, 512))))

# -- backups and undo ----------------------------------------------------------
undo_dir = heal_root / "undo"
(undo_dir / "tex").mkdir(parents=True)
(undo_dir / "rules.toml").write_text("[texture]\nmax_edge = 2048\n", encoding="utf-8")
(undo_dir / "prop.mtl").write_text("newmtl a\nmap_Kd tex/wood.png\nmap_Bump tex/bump.png\n",
                                   encoding="utf-8")
(undo_dir / "prop.obj").write_text("mtllib prop.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n",
                                   encoding="utf-8")
undo_png = marker_png(undo_dir / "tex" / "wood.png")
bump_png = marker_png(undo_dir / "tex" / "bump.png", (512, 512))
undo_original = digest(undo_png)
undo_mtl = digest(undo_dir / "prop.mtl")
undo_healer = agk.load_healer(heal_budget, agk.HealSettings(enabled=True), root=undo_dir)
undo_verdict = agk.Gatekeeper(heal_budget, healer=undo_healer).check(undo_png)
healed_png = undo_dir / "tex" / "wood_Albedo.png"
backups = list((undo_dir / ".agk-backups").glob("*/heal.json"))
expect("an adopted heal is backed up (was: overwritten in place, no way back)",
       undo_verdict.healed and len(backups) == 1, str(backups))
manifest = json.loads(backups[0].read_text(encoding="utf-8")) if backups else {}
expect("the backup holds the original bytes",
       backups and digest(backups[0].parent / "files" / "tex" / "wood.png") == undo_original)
expect("... and the .mtl as it was before the repoint",
       backups and digest(backups[0].parent / "files" / "prop.mtl") == undo_mtl)
expect("the manifest says what changed", manifest.get("asset") == "tex/wood.png"
       and manifest.get("healed") == "tex/wood_Albedo.png"
       and any("resized" in note for note in manifest.get("modifications", [])), manifest)
expect("the gate does not see the backups as assets",
       not agk.is_candidate(backups[0].parent / "files" / "tex" / "wood.png") if backups else False)
expect("... nor the healer's staging copies",
       not agk.is_candidate(undo_dir / ".agk-heal-x1" / "rock.png"))
expect("a folder scan skips .agk-backups",
       not any(".agk-backups" in str(path) for path in agk.walk_files(undo_dir)))
with _contextlib.redirect_stdout(_io.StringIO()) as audit_out:
    agk.run_audit(undo_dir)
expect("the audit reports no orphaned textures from the backups",
       "0 orphaned" in audit_out.getvalue(), audit_out.getvalue()[-200:])

# A second rename in the same project must not rewrite the backed-up .mtl.
(undo_dir / "prop2.mtl").write_text("newmtl b\nmap_Kd tex/wood.png\nmap_Bump tex/bump.png\n",
                                    encoding="utf-8")
backup_mtl_before = digest(backups[0].parent / "files" / "prop.mtl")
bump_verdict = agk.Gatekeeper(heal_budget, healer=undo_healer).check(bump_png)
expect("a later heal never edits a backup (undo would restore the edit)",
       bump_verdict.healed and "bump_Albedo.png" in (undo_dir / "prop2.mtl").read_text(encoding="utf-8")
       and "tex/bump.png" in (backups[0].parent / "files" / "prop.mtl").read_text(encoding="utf-8")
       and digest(backups[0].parent / "files" / "prop.mtl") == backup_mtl_before,
       str(bump_verdict.heal_notes))

edited = undo_dir / "tex" / "wood_Albedo.png"
saved = edited.read_bytes()
edited.write_bytes(saved + b"\0")
try:
    agk_healer.undo_heal(undo_dir / "tex" / "wood.png")
    expect("undo refuses when the healed file was edited since", False)
except agk_healer.HealError as exc:
    expect("undo refuses when the healed file was edited since", "changed after" in str(exc),
           str(exc))
expect("... and changes nothing", edited.read_bytes() == saved + b"\0"
       and not (undo_dir / "tex" / "wood.png").exists())
edited.write_bytes(saved)

try:
    agk_healer.undo_heal(undo_dir / "tex" / "wood.png")
    expect("an older heal waits for the later one that edited the same .mtl", False)
except agk_healer.HealError as exc:
    expect("an older heal waits for the later one that edited the same .mtl",
           "undo tex/bump_Albedo.png first" in str(exc), str(exc))
agk_healer.undo_heal(undo_dir / "tex" / "bump_Albedo.png")
expect("undoing the later heal first works", (undo_dir / "tex" / "bump.png").is_file())
record = agk_healer.undo_heal(undo_dir / "tex" / "wood.png")
expect("undo by the old name restores the original bytes",
       digest(undo_dir / "tex" / "wood.png") == undo_original)
expect("... removes the renamed copy", not healed_png.exists())
expect("... and puts the .mtl back", digest(undo_dir / "prop.mtl") == undo_mtl)
expect("the manifest records the undo", record.undone)
try:
    agk_healer.undo_heal(undo_dir / "tex" / "wood.png")
    expect("a heal is undone once", False)
except agk_healer.HealError as exc:
    expect("a heal is undone once", "no heal of" in str(exc), str(exc))

crlf_dir = heal_root / "crlf"
crlf_dir.mkdir()
(crlf_dir / "lf.mtl").write_bytes(b"newmtl a\nmap_Kd stone.png\n")
(crlf_dir / "crlf.mtl").write_bytes(b"newmtl b\r\nmap_Kd stone.png\r\n")
agk.Gatekeeper(heal_budget, healer=agk.load_healer(
    heal_budget, agk.HealSettings(enabled=True), root=crlf_dir)).check(
    marker_png(crlf_dir / "stone.png", (512, 512)))
expect("repointing keeps an LF .mtl LF (Windows wrote CRLF)",
       (crlf_dir / "lf.mtl").read_bytes() == b"newmtl a\nmap_Kd stone_Albedo.png\n",
       (crlf_dir / "lf.mtl").read_bytes())
expect("... and a CRLF one CRLF (Linux wrote LF)",
       (crlf_dir / "crlf.mtl").read_bytes() == b"newmtl b\r\nmap_Kd stone_Albedo.png\r\n",
       (crlf_dir / "crlf.mtl").read_bytes())
crlf_obj = crlf_dir / "glass.obj"
crlf_obj.write_bytes(b"# made in caf\xe9 tools\r\nusemtl verre_\xe9\r\nv 0 0 0\r\nv 1 0 0\r\n"
                     b"v 0 1 0\r\nv 2 0 0\r\nf 1 2 3\r\nf 1 2 4\r\n")
agk.Gatekeeper(agk.Budget(check_references=False), healer=agk.load_healer(
    heal_budget, agk.HealSettings(enabled=True), root=crlf_dir)).check(crlf_obj)
expect("stripping faces keeps CRLF and non-UTF-8 bytes (were rewritten)",
       crlf_obj.read_bytes() == b"# made in caf\xe9 tools\r\nusemtl verre_\xe9\r\nv 0 0 0\r\n"
                                b"v 1 0 0\r\nv 0 1 0\r\nv 2 0 0\r\nf 1 2 3\r\n",
       crlf_obj.read_bytes())
expect("without charset-normalizer, a non-UTF-8 .obj says how to fix it",
       "pip install charset-normalizer" in agk._mesh_read_error(
           ModuleNotFoundError("No module named 'charset_normalizer'", name="charset_normalizer")))
crlf_record = agk_healer.undo_heal(crlf_dir / "stone.png")
expect("... so the undo of a repoint verifies and restores",
       crlf_record.undone and (crlf_dir / "crlf.mtl").read_bytes() == b"newmtl b\r\nmap_Kd stone.png\r\n")

nobackup_dir = heal_root / "nobackup"
nobackup_dir.mkdir()
nobackup = agk.load_healer(heal_budget, agk.HealSettings(enabled=True), root=nobackup_dir,
                           backups=False)
agk.Gatekeeper(heal_budget, healer=nobackup).check(marker_png(nobackup_dir / "n_Albedo.png"))
expect("backups=False (the workstation's throwaway copies) writes none",
       not (nobackup_dir / ".agk-backups").exists())

# -- the CLI: one file, dry run, undo, list ---------------------------------------
cli_heal = heal_root / "cli"
(cli_heal / "Textures").mkdir(parents=True)
(cli_heal / "rules.toml").write_text("[texture]\nmax_edge = 2048\n", encoding="utf-8")
(cli_heal / "crate.mtl").write_text("newmtl c\nmap_Kd Textures/bark.png\n", encoding="utf-8")
bark = marker_png(cli_heal / "Textures" / "bark.png")
bark_before = digest(bark)


def gate_run(*argv):
    completed = subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"),
                                *map(str, argv), "--no-color"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
    return completed.returncode, completed.stdout + completed.stderr


code, out = gate_run(bark, "--heal", "--dry-run")
expect("--heal --dry-run says what it would do", "--heal would: resized 1000x1000" in out
       and "--heal would: repoint 1 reference" in out, out[-400:])
expect("... and writes nothing", digest(bark) == bark_before and code == 1
       and not (cli_heal / ".agk-backups").exists())
code, out = gate_run(bark, "--heal")
expect("--heal heals one file, finding the project root above it",
       code == 0 and "PASSED WITH HEALING" in out
       and "Textures/bark_Albedo.png" in (cli_heal / "crate.mtl").read_text(encoding="utf-8"),
       out[-400:])
expect("... with the backup at the project root", (cli_heal / ".agk-backups").is_dir())
code, out = gate_run(cli_heal, "--list-heals")
expect("--list-heals lists it", code == 0 and "Textures/bark.png -> Textures/bark_Albedo.png" in out,
       out[-300:])
code, out = gate_run(cli_heal / "Textures" / "bark.png", "--undo-heal")
expect("--undo-heal by the old name puts it back", code == 0 and digest(bark) == bark_before
       and "Textures/bark.png" in (cli_heal / "crate.mtl").read_text(encoding="utf-8"),
       out[-300:])
code, out = gate_run(cli_heal / "Textures" / "bark.png", "--undo-heal")
expect("... and says so when there is nothing to undo", code == 1 and "no heal of" in out, out[-200:])
code, out = gate_run(cli_heal, "--heal")
expect("--heal on a folder is refused with the alternative",
       code == 2 and "--auto-heal" in out, out[-200:])
code, out = gate_run(bark, "--dry-run")
expect("--dry-run without --heal is refused", code == 2, out[-200:])
code, out = gate_run(bark, "--heal", "--no-auto-heal")
expect("--heal with --no-auto-heal is refused", code == 2, out[-200:])
dark = cli_heal / "Textures" / "dark.png"
Image.new("RGB", (1000, 1000), (0, 0, 0)).save(dark)
code, out = gate_run(dark, "--heal")
expect("a heal that changes nothing says why (was silent at INFO)",
       code == 1 and "not healed: the healed copy would still fail" in out
       and "luminance" in out and "dark.png is unchanged" in out, out[-400:])

code, out = gate_run(cli_heal, "--once", "--recursive", "--auto-heal")
expect("--auto-heal says at startup what it may change, and who turned it on",
       "healing ON for every failing asset" in out and "turned on by --auto-heal" in out
       and "resize non-power-of-two PNG/TGA textures" in out and ".agk-backups" in out, out[:900])

print("14e. healing normal maps, TGA and JPG (G5)")
import numpy as _np  # noqa: E402
g5 = heal_root / "g5"
g5.mkdir()


def stripe_normals(path, size=(1000, 1000)):
    """Vertical stripes of two normals 90 degrees apart: resampling averages them."""
    width, height = size
    a = _np.array([0.7071, 0.0, 0.7071])
    b = _np.array([-0.7071, 0.0, 0.7071])
    columns = _np.where((_np.arange(width) // 3) % 2 == 0, 0, 1)
    rgb = _np.where(columns[None, :, None] == 0, a, b) * _np.ones((height, 1, 1))
    image = Image.fromarray(_np.clip(_np.rint((rgb + 1) * 127.5), 0, 255).astype(_np.uint8), "RGB")
    image.save(path)
    return Path(path)


def worst_length_error(path):
    data = _np.asarray(Image.open(path).convert("RGB"), dtype=_np.float32) / 127.5 - 1.0
    return float(_np.abs(_np.linalg.norm(data, axis=-1) - 1.0).max())


_normal = stripe_normals(g5 / "wall_Normal.png")
_g5_healer = agk.load_healer(heal_budget, agk.HealSettings(enabled=True), root=g5)
_nv = agk.Gatekeeper(agk.Budget(pbr_check=False), healer=_g5_healer).check(_normal)
expect("a resized normal map keeps every pixel a unit vector (+-1%)",
       _nv.healed and "renormalised" in _nv.heal_notes[0] and worst_length_error(_normal) <= 0.01,
       (_nv.heal_notes, worst_length_error(_normal)))
_plain = stripe_normals(g5 / "plain.png")
agk_healer.heal_texture_file(_plain, "resize", normal=False)
expect("... where a plain resize leaves them short (the seam this fixes)",
       worst_length_error(_plain) > 0.05, worst_length_error(_plain))
_tga = g5 / "rock_Albedo.tga"
marker_png(g5 / "tmp.png").rename(g5 / "tmp_marker.png")
Image.open(g5 / "tmp_marker.png").save(_tga)
_tga_before = digest(_tga)
_tv = agk.Gatekeeper(heal_budget, healer=_g5_healer).check(_tga)
expect("a TGA heals like a PNG", _tv.healed and Image.open(_tga).size == (1024, 1024)
       and _tga.suffix == ".tga", _tv.heal_notes)
expect("... and its undo restores the original bytes",
       agk_healer.undo_heal(_tga).undone and digest(_tga) == _tga_before)
_jpg = g5 / "sky_Albedo.jpg"
Image.open(g5 / "tmp_marker.png").convert("RGB").save(_jpg, quality=95)
_jpg_before = digest(_jpg)
_jv = agk.Gatekeeper(heal_budget, healer=_g5_healer).check(_jpg)
expect("JPEG is not healed unless jpg = true, and says why",
       not _jv.passed and digest(_jpg) == _jpg_before and "jpg = true" in _g5_healer.last_refusal,
       _g5_healer.last_refusal)
_jpg_healer = agk.load_healer(heal_budget, agk.HealSettings(enabled=True, jpg=True), root=g5)
_jv = agk.Gatekeeper(heal_budget, healer=_jpg_healer).check(_jpg)
expect("with jpg = true it heals, and the note says it was re-saved",
       _jv.healed and "re-saved as JPEG quality 95" in _jv.heal_notes[0]
       and Image.open(_jpg).size == (1024, 1024), _jv.heal_notes)
expect("[healer] jpg is read from the rules", agk.HealSettings.from_mapping({"jpg": True}).jpg is True)

print("14f. memory budgets (G2) and texel density (G8)")
mem_dir = scratch / "memory"
if mem_dir.exists():
    shutil.rmtree(mem_dir)
(mem_dir / "Props").mkdir(parents=True)
expect("a 2048 BC7 texture with mips is about 5.3 MB",
       abs(agk.texture_memory_bytes(2048, 2048, "bc7") / 2**20 - 5.333) < 0.01)
expect("... and 2.7 MB as BC1", abs(agk.texture_memory_bytes(2048, 2048, "bc1") / 2**20 - 2.667) < 0.01)
expect("a 1,000-vertex static mesh with one UV set: 24 bytes a vertex + 16-bit indices",
       agk.mesh_memory_bytes(1000, 1500, 1, False) == 1000 * 24 + 1500 * 3 * 2)
expect("... skinned, two UV sets, over 65,535 vertices: 36 bytes + 32-bit indices",
       agk.mesh_memory_bytes(70000, 100000, 2, True) == 70000 * 36 + 100000 * 3 * 4)
expect("Unreal adds a lightmap UV set and a depth-only index buffer: 28 bytes + 2 index buffers",
       agk.mesh_memory_bytes(1000, 1500, 1, False, lightmap_uv=True, depth_index_buffer=True)
       == 1000 * 28 + 1500 * 3 * 2 * 2)
_n2_budget = agk.Budget()
expect("... and those are the defaults (Unreal's own import settings)",
       _n2_budget.mesh_lightmap_uv and _n2_budget.mesh_depth_index_buffer)
# N2, 4 Nov 2026: five OBJ meshes in UE 5.3.2, Size Map in KB, vertex and
# triangle counts identical to the gate's. The old formula was 37-53% low.
_n2_unreal = {(570, 1000): 26.12, (2650, 5000): 132.776, (6260, 12000): 332.445,
              (12340, 24000): 629.982, (24470, 48000): 1.259 * 1024}
_n2_error = {key: agk.mesh_memory_bytes(key[0], key[1], 1, False, False, True, True) / 1024 / kb - 1
             for key, kb in _n2_unreal.items()}
expect("calibrated: all five N2 meshes within 10% of Unreal's Size Map (they are within 7%)",
       all(abs(error) < 0.07 for error in _n2_error.values()),
       {key: f"{error:+.1%}" for key, error in _n2_error.items()})
_n2_rules = scratch / "n2_rules.toml"
_n2_rules.write_text("[mesh]\nmax_mb = 1\nlightmap_uv = false\ndepth_index_buffer = false\n")
_n2_off = agk.Budget.from_mapping(agk.parse_rules(_n2_rules))
expect("an engine without them (Unity, Godot) switches both off in rules.toml",
       not _n2_off.mesh_lightmap_uv and not _n2_off.mesh_depth_index_buffer)
_n2_rules.write_text("[mesh]\nlightmap_uv = \"yes\"\n")
try:
    agk.Budget.from_mapping(agk.parse_rules(_n2_rules))
    _n2_refused = False
except agk.ConfigError:
    _n2_refused = True
expect("... and a value that is not true/false is refused, not guessed", _n2_refused)
Image.new("RGB", (2048, 2048), (120, 90, 70)).save(mem_dir / "Props" / "wall_Albedo.png")
Image.new("RGB", (2048, 2048), (128, 128, 255)).save(mem_dir / "Props" / "wall_Normal.png")
_mb_budget = agk.Budget(max_texture_mb=4, pbr_check=False)
_va = agk.Gatekeeper(_mb_budget).check(mem_dir / "Props" / "wall_Albedo.png")
_vn = agk.Gatekeeper(_mb_budget).check(mem_dir / "Props" / "wall_Normal.png")
expect("a 2048 albedo (BC7, 5.3 MB) fails a 4 MB texture budget, naming the format",
       not _va.passed and "~5.3 MB as BC7 with mips exceeds the 4 MB texture budget" in _va.reason, _va.reason)
expect("a 2048 normal map counts as BC5 by its name", "BC5" in _vn.metric, _vn.metric)
expect("every texture reports its memory, budgeted or not",
       dict(agk.Gatekeeper(agk.Budget(pbr_check=False)).check(mem_dir / "Props" / "wall_Albedo.png").stats)["memory_kb"] == 5461)
_vb = agk.Gatekeeper(agk.Budget(max_texture_mb=4, texture_format="bc1", pbr_check=False)).check(
    mem_dir / "Props" / "wall_Albedo.png")
expect("... and passes as BC1 (2.7 MB)", _vb.passed, _vb.reason)
_vm = agk.Gatekeeper(agk.Budget(max_mesh_mb=1, max_triangles=100000, max_material_slots=8)).check(
    DROP / "boss_statue.gltf")
expect("a mesh over its MB budget fails with its vertex count",
       not _vm.passed and "estimated GPU memory exceeds the 1 MB mesh budget" in _vm.reason
       and "217,500 vertices" in _vm.reason, _vm.reason)
_doc = {"texture": {"max_mb": 8, "format": "astc6x6", "normal_format": "bc5"}, "mesh": {"max_mb": 2},
        "platforms": {"switch": {"texture": {"max_mb": 2, "format": "astc8x8"}, "mesh": {"max_mb": 1}}}}
_b = agk.Budget.from_mapping(_doc)
expect("[texture] max_mb / format and [mesh] max_mb load",
       (_b.max_texture_mb, _b.texture_format, _b.max_mesh_mb) == (8, "astc6x6", 2))
_bs = agk.Budget.from_mapping(agk.apply_platform(_doc, "switch"))
expect("a platform profile sets its own memory budgets and format",
       (_bs.max_texture_mb, _bs.texture_format, _bs.max_mesh_mb) == (2, "astc8x8", 1))
for _bad, _why in (({"texture": {"format": "dxt99"}}, "format"), ({"mesh": {"max_mb": -1}}, "max_mb"),
                   ({"mesh": {"texel_density": [800, 400]}}, "texel_density"),
                   ({"totals": [{"path": "Props/*"}]}, "max_mb")):
    try:
        agk.Budget.from_mapping(_bad) if "totals" not in _bad else agk.parse_totals(_bad)
        expect(f"a bad memory setting is refused ({_why})", False)
    except agk.ConfigError as exc:
        expect(f"a bad memory setting is refused ({_why})", _why in str(exc), str(exc))
_ex = agk.parse_exceptions({"exceptions": [{"path": "*", "rule": "max_texture_mb", "allow": 6,
                                            "reason": "r", "owner": "o", "expires": "2099-01-01"}]})
expect("an exception can raise a memory budget",
       agk.Gatekeeper(_mb_budget, exceptions=_ex).check(mem_dir / "Props" / "wall_Albedo.png").passed)
(mem_dir / "rules.toml").write_text('[texture]\npbr = false\n[[totals]]\npath = "Props/*"\nmax_mb = 8\n',
                                    encoding="utf-8")
_tot = subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"), str(mem_dir),
                       "--once", "--recursive", "--no-color", "--report", f"junit:{mem_dir / 'r.xml'}"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
_tot_out = _tot.stdout + _tot.stderr
expect("[[totals]]: a folder over its memory budget fails the run, though each file passes",
       _tot.returncode == 1 and "2 passed, 0 failed" in _tot_out
       and "exceeds the 8 MB budget for Props/*" in _tot_out and "10.7 MB" in _tot_out, _tot_out[-600:])
expect("... and is a failing case in the JUnit report",
       "[total] Props/*" in (mem_dir / "r.xml").read_text(encoding="utf-8"))
(mem_dir / "rules.toml").write_text('[texture]\npbr = false\n[[totals]]\npath = "Props/*"\nmax_mb = 12\n',
                                    encoding="utf-8")
_tot = subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"), str(mem_dir),
                       "--once", "--recursive", "--no-color"], capture_output=True, text=True,
                      encoding="utf-8", errors="replace")
expect("... under budget it passes and says how much is used",
       _tot.returncode == 0 and "Props/*: ~10.7 MB of 12 MB (2 assets)" in _tot.stdout + _tot.stderr,
       (_tot.stdout + _tot.stderr)[-300:])

shutil.copy(Path("tests") / "fixtures" / "fbx" / "cube_subd3.fbx", mem_dir / "Props" / "hero.fbx")
_tot = subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"), str(mem_dir),
                       "--once", "--recursive", "--no-color"], capture_output=True, text=True,
                      encoding="utf-8", errors="replace")
_tot_out = _tot.stdout + _tot.stderr
# Until 10 Nov 2026 an FBX had no memory estimate and failed every total it
# was in ("1 not measured"); engine/fbx_preview.measure() now gives one.
expect("a total that holds an FBX counts its memory too (3 assets, none unmeasured)",
       _tot.returncode == 0 and "Props/*: ~10.7 MB of 12 MB (3 assets)" in _tot_out
       and "not measured" not in _tot_out, _tot_out[-500:])
with patch.object(agk, "engine_module", side_effect=RuntimeError("measuring broke")):
    _broken = agk.Gatekeeper(agk.Budget(max_mesh_mb=8)).check(mem_dir / "Props" / "hero.fbx")
expect("... and if measuring ever fails, the verdict still comes with a warning, not a crash",
       any("not measured (measuring broke)" in w for w in _broken.warnings), _broken.warnings)
_unmeasured = agk.check_totals([agk.MemoryTotal("Props/*", 12, mem_dir)], [_broken])
expect("... and a total holding an unmeasured asset fails and names it, never counts it as 0 MB",
       len(_unmeasured) == 1 and "1 asset(s) have no memory estimate (hero.fbx" in _unmeasured[0].problems[0],
       [v.problems for v in _unmeasured])
(mem_dir / "Props" / "hero.fbx").unlink()


def uv_plane(path, size_m):
    half = size_m / 2
    path.write_text(f"v {-half} 0 {-half}\nv {half} 0 {-half}\nv {half} 0 {half}\nv {-half} 0 {half}\n"
                    "vt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nf 1/1 2/2 3/3\nf 1/1 3/3 4/4\n", encoding="utf-8")
    return path


_p1 = uv_plane(mem_dir / "plane1.obj", 1.0)
_p2 = uv_plane(mem_dir / "plane2.obj", 2.0)
_facts = agk.mesh_facts(_p1, 1024)
expect("texel density: a 1 m plane with 0-1 UVs and a 1024 texture reads 1024 px/m",
       abs(_facts["texel_density"] - 1024) < 1 and _facts["assumed_texture"], _facts)
expect("... and the same plane at 2 m reads 512",
       abs(agk.mesh_facts(_p2, 1024)["texel_density"] - 512) < 1)
_tb = agk.Budget(texel_density_min=800, texel_density_max=2000, enforce_texture_naming=False)
_v1, _v2 = agk.Gatekeeper(_tb).check(_p1), agk.Gatekeeper(_tb).check(_p2)
expect("[mesh] texel_density = [800, 2000]: the 1 m plane passes, the 2 m one fails, saying why",
       _v1.passed and not _v2.passed and "texel density 512 px/m is below the 800 px/m minimum "
       "(assuming a 1024px texture)" in _v2.reason, (_v1.reason, _v2.reason))
expect("a mesh with no UVs is warned about, not failed",
       agk.Gatekeeper(_tb).check(DROP / "crate_1mtl.obj").passed
       and any("no UVs" in w for w in agk.Gatekeeper(_tb).check(DROP / "crate_1mtl.obj").warnings))
expect("[mesh] texel_density and texel_texture load",
       (agk.Budget.from_mapping({"mesh": {"texel_density": [400, 2048], "texel_texture": 2048}}).texel_texture)
       == 2048)
expect("the budget line shows memory and texel budgets when set",
       "texture<=4MB(bc7)" in agk.describe_budget(_mb_budget)
       and "texel=800-2000px/m" in agk.describe_budget(_tb))

print("14c. expiring exceptions (G1)")
from datetime import date as _date, timedelta as _timedelta  # noqa: E402
ex_root = scratch / "exceptions"
if ex_root.exists():
    shutil.rmtree(ex_root)
(ex_root / "Characters" / "Boss").mkdir(parents=True)
(ex_root / "Props").mkdir()
shutil.copy(DROP / "boss_statue.gltf", ex_root / "Characters" / "Boss" / "boss_statue.gltf")
shutil.copy(DROP / "boss_statue.gltf", ex_root / "Props" / "statue_copy.gltf")
_today = _date.today()
_soon, _later, _past = _today + _timedelta(days=5), _today + _timedelta(days=60), _today - _timedelta(days=1)


def ex_rules(expires, path="Characters/Boss/*", extra=""):
    (ex_root / "rules.toml").write_text(
        "[mesh]\nmax_triangles = 50000\nmax_material_slots = 8\n\n[[exceptions]]\n"
        f'path = "{path}"\nrule = "max_triangles"\nallow = 80000\n'
        'reason = "hero boss, art director sign-off"\nowner = "lena"\n'
        f"expires = {expires.isoformat()}\n{extra}", encoding="utf-8")


ex_rules(_later)
_exs = agk.resolve_exceptions(ex_root, agk.build_parser().parse_args([str(ex_root)]))
expect("[[exceptions]] load from rules.toml", len(_exs) == 1 and _exs[0].allow == 80000
       and _exs[0].expires == _later, str(_exs))
_ex_budget = agk.Budget(max_triangles=50000, max_material_slots=8)
_boss = ex_root / "Characters" / "Boss" / "boss_statue.gltf"
_copy = ex_root / "Props" / "statue_copy.gltf"
_v = agk.Gatekeeper(_ex_budget, exceptions=_exs).check(_boss)
expect("inside its dates, the exception lets the boss through, with a note",
       _v.passed and any("exception: max_triangles = 80,000" in w and "lena" in w for w in _v.warnings),
       (_v.reason, _v.warnings))
_v = agk.Gatekeeper(_ex_budget, exceptions=_exs).check(_copy)
expect("... and only the paths it names (the same mesh in Props still fails)", not _v.passed, _v.reason)
_v = agk.Gatekeeper(_ex_budget, exceptions=_exs, today=_later + _timedelta(days=1)).check(_boss)
expect("the day after it ends, the boss fails and says the exception expired",
       not _v.passed and "expired on" in _v.reason and "80,000" in _v.reason, _v.reason)
_v = agk.Gatekeeper(_ex_budget, exceptions=_exs, today=_later).check(_boss)
expect("on its last day it still applies, and warns that it ends soon",
       _v.passed and any("ends in 0 days" in w for w in _v.warnings), _v.warnings)
_small = ex_root / "Characters" / "Boss" / "small.obj"
shutil.copy(DROP / "crate_1mtl.obj", _small)
shutil.copy(DROP / "crate_1mtl.mtl", ex_root / "Characters" / "Boss" / "crate_1mtl.mtl")
_v = agk.Gatekeeper(_ex_budget, exceptions=_exs, today=_later + _timedelta(days=9)).check(_small)
expect("an asset that fits the normal budget never mentions an expired exception",
       _v.passed and "expired" not in _v.reason, _v.reason)
_name_only = agk.parse_exceptions({"exceptions": [{
    "path": "BOSS_statue.gltf", "rule": "max_triangles", "allow": 80000, "reason": "r",
    "owner": "o", "expires": _later.isoformat()}]}, base=ex_root)
expect("a bare file name matches that file in any folder, ignoring case",
       agk.Gatekeeper(_ex_budget, exceptions=_name_only).check(_boss).passed
       and not agk.Gatekeeper(_ex_budget, exceptions=_name_only).check(_copy).passed)
for _bad, _why in (({"rule": "max_triangles", "allow": 1, "owner": "o", "expires": "2030-01-01",
                     "path": "x"}, "reason"),
                   ({"rule": "max_triangle", "allow": 1, "reason": "r", "owner": "o",
                     "expires": "2030-01-01", "path": "x"}, "max_triangles"),
                   ({"rule": "max_triangles", "allow": "lots", "reason": "r", "owner": "o",
                     "expires": "2030-01-01", "path": "x"}, "whole number"),
                   ({"rule": "require_power_of_two", "allow": 0, "reason": "r", "owner": "o",
                     "expires": "2030-01-01", "path": "x"}, "true or false"),
                   ({"rule": "max_triangles", "allow": 1, "reason": "r", "owner": "o",
                     "expires": "next week", "path": "x"}, "date"),
                   ({"rule": "max_triangles", "allow": 1, "reason": "r", "owner": "o",
                     "expires": "2030-01-01", "path": "x", "approved": True}, "approved"),
                   # a value no budget can hold: refused here, not a traceback mid-run
                   ({"rule": "max_texture_edge", "allow": 0, "reason": "r", "owner": "o",
                     "expires": "2030-01-01", "path": "x"}, "max_texture_edge must be at least 1"),
                   ({"rule": "max_uv_channels", "allow": 0, "reason": "r", "owner": "o",
                     "expires": "2030-01-01", "path": "x"}, "max_uv_channels must be at least 1")):
    try:
        agk.parse_exceptions({"exceptions": [_bad]})
        expect(f"a bad exception is refused ({_why})", False)
    except agk.ConfigError as exc:
        expect(f"a bad exception is refused ({_why})", _why in str(exc), str(exc))
_remote = {"mesh": {"max_triangles": 50000, "max_material_slots": 8}, "exceptions": [{
    "path": "Characters/Boss/*", "rule": "max_triangles", "allow": 80000, "reason": "r",
    "owner": "o", "expires": _later.isoformat()}]}
_rex = agk.resolve_exceptions(ex_root, agk.build_parser().parse_args([str(ex_root)]), _remote)
expect("exceptions come through the remote rules too (dates as text)",
       len(_rex) == 1 and _rex[0].expires == _later
       and agk.Gatekeeper(_ex_budget, exceptions=_rex).check(_boss).passed)
expect("the budget parser accepts a document with exceptions", agk.Budget.from_mapping(_remote).max_triangles == 50000)
ex_rules(_soon)
_cli = subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"), str(ex_root),
                       "--once", "--recursive", "--no-color"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
expect("a real run says how many exceptions apply and which end soon",
       "except  1 active, 0 expired, 1 ending within 14 days" in _cli.stdout + _cli.stderr,
       (_cli.stdout + _cli.stderr)[-500:])
expect("... passes the boss with the note, and fails the copy",
       "boss_statue.gltf" in _cli.stdout and "exception: max_triangles" in _cli.stdout
       and "FAILED" in _cli.stdout and "statue_copy.gltf" in _cli.stdout, _cli.stdout[-600:])
ex_rules(_past)
_cli = subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"), str(_boss),
                       "--rules", str(ex_root / "rules.toml"), "--no-color"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
expect("one file with --rules (how the DCC plugins run it): paths are relative to the rules "
       "file's folder, and an expired exception fails it",
       _cli.returncode == 1 and "expired on" in _cli.stdout, _cli.stdout[-400:])
(ex_root / "rules.toml").write_text('[[exceptions]]\npath = "x"\nrule = "max_triangles"\n',
                                    encoding="utf-8")
_cli = subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"), str(ex_root),
                       "--once", "--no-color"], capture_output=True, text=True, encoding="utf-8",
                      errors="replace")
expect("an incomplete exception stops the run with exit 2 and names what is missing",
       _cli.returncode == 2 and "needs allow, reason, owner, expires" in _cli.stdout + _cli.stderr,
       (_cli.stdout + _cli.stderr)[-300:])
_kept = agk.Gatekeeper(_ex_budget, exceptions=_name_only)
_kept._inspect = lambda path: agk.Verdict(path, ("cannot be read",), unreadable=True)
expect("check() keeps an ERROR an ERROR when it adds a note (was turned into FAILED)",
       _kept.check(_boss).status == "ERROR", _kept.check(_boss).status)
expect("an ERROR keeps being an ERROR when a note is added (was reported as FAILED)",
       agk.Verdict(Path("x.obj"), ("e",), "", (), (), ("note",), True).status == "ERROR")

print("14d. JUnit and SARIF reports for CI (G3)")
import xml.etree.ElementTree as _ET  # noqa: E402
rep_dir = scratch / "reports"
if rep_dir.exists():
    shutil.rmtree(rep_dir)
(rep_dir / "Props").mkdir(parents=True)
shutil.copy(DROP / "crate_1mtl.obj", rep_dir / "Props" / "crate_1mtl.obj")
shutil.copy(DROP / "crate_1mtl.mtl", rep_dir / "Props" / "crate_1mtl.mtl")
shutil.copy(DROP / "boss_statue.gltf", rep_dir / "Props" / "boss & statue's.gltf")
_run = subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"), str(rep_dir),
                       "--once", "--recursive", "--no-color",
                       "--report", f"junit:{rep_dir / 'out' / 'gate.xml'}",
                       "--report", f"sarif:{rep_dir / 'out' / 'gate.sarif'}"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
expect("--report writes both files on a --once run", _run.returncode == 1
       and (rep_dir / "out" / "gate.xml").is_file() and (rep_dir / "out" / "gate.sarif").is_file(),
       (_run.stdout + _run.stderr)[-300:])
_junit = _ET.parse(rep_dir / "out" / "gate.xml").getroot()
_cases = _junit.findall(".//testcase")
expect("JUnit: well-formed XML, one test case per asset, even with & and ' in a name",
       len(_cases) == 2 and _junit.get("tests") == "2" and _junit.get("failures") == "1",
       [c.get("name") for c in _cases])
_failed = [c for c in _cases if c.find("failure") is not None]
expect("... the failing asset is named by its path and carries the reason",
       len(_failed) == 1 and _failed[0].get("name") == "Props/boss & statue's.gltf"
       and "triangles exceeds" in _failed[0].find("failure").get("message")
       and _failed[0].get("classname") == "Props", _failed[0].attrib if _failed else None)
_sarif = json.loads((rep_dir / "out" / "gate.sarif").read_text(encoding="utf-8"))
_results = _sarif["runs"][0]["results"]
expect("SARIF 2.1.0: one result per problem of each failing asset, with its path",
       _sarif["version"] == "2.1.0" and _results
       and all(r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "Props/boss%20%26%20statue%27s.gltf"
               for r in _results) and all(r["ruleId"] == "agk/budget" for r in _results), _results[:1])
expect("... and passing assets add nothing to it", len(_results) == len(
    [c for c in _failed[0].find("failure").text.splitlines() if c]))
_err = agk.Verdict(rep_dir / "locked.obj", ("cannot be read",), unreadable=True)
_junit_err = _ET.fromstring(agk.junit_report([_err], rep_dir))
_angle = agk.Verdict(rep_dir / "a<b>.gltf", ("x < y & z",))
_angle_xml = _ET.fromstring(agk.junit_report([_angle], rep_dir))
expect("names and reasons with < > & stay well-formed XML (no such file on Windows, so built here)",
       _angle_xml.find(".//testcase").get("name") == "a<b>.gltf"
       and _angle_xml.find(".//failure").get("message") == "x < y & z")
_control = agk.Verdict(rep_dir / "SM_\x01Crate.gltf", ("collision mesh 'UCX_\x01Bad\x1b[31m' matches no render mesh",),
                       warnings=("note \x07",))
try:
    _control_xml = _ET.fromstring(agk.junit_report([_control], rep_dir))
    _control_ok = "UCX_" in _control_xml.find(".//failure").get("message")
except _ET.ParseError as exc:
    _control_ok = exc
expect("a control character in a node or file name keeps the JUnit report readable XML",
       _control_ok is True, _control_ok)
_undecodable = agk.Verdict(rep_dir / "SM_\udcff\udcfe.obj", ("1 degenerate triangles",))
try:
    agk.write_reports([("github", str(rep_dir / "odd.txt")), ("junit", str(rep_dir / "odd.xml"))],
                      [_undecodable], rep_dir)
    _odd = (rep_dir / "odd.txt").read_text(encoding="utf-8")
except Exception as exc:          # UnicodeEncodeError: the run stopped with a traceback
    _odd = repr(exc)
expect("a file name that is not UTF-8 (bytes Linux allows) is written to every report, no traceback",
       "degenerate" in _odd and (rep_dir / "odd.xml").is_file(), _odd)
_total_sarif = json.loads(agk.sarif_report([agk.Verdict(Path("[total] Props/*"), ("over",))], rep_dir))
expect("SARIF: a folder total points at rules.toml, not at '[total] Props/*' (not a URI)",
       _total_sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
       == "rules.toml")
expect("an unreadable asset is a JUnit error, not a failure",
       _junit_err.get("errors") == "1" and _junit_err.find(".//error") is not None)
_single = subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"),
                          str(rep_dir / "Props" / "crate_1mtl.obj"), "--no-color",
                          "--report", f"junit:{rep_dir / 'one.xml'}"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
expect("a single-file run writes a one-case report",
       _single.returncode == 0 and len(_ET.parse(rep_dir / "one.xml").getroot().findall(".//testcase")) == 1)
for _badarg in ("junit", "xml:out.xml", "junit:"):
    try:
        with _contextlib.redirect_stderr(_io.StringIO()):
            agk.build_parser().parse_args([str(rep_dir), "--report", _badarg])
        expect(f"--report {_badarg} is refused", False)
    except SystemExit:
        expect(f"--report {_badarg} is refused", True)
expect("a plain run keeps no verdicts in memory (a watch runs for weeks)",
       agk.Reporter(color=False).keep_verdicts is False)

print("14g. incremental cache and --jobs (G4)")
cache_dir = scratch / "cache_run"
if cache_dir.exists():
    shutil.rmtree(cache_dir)
(cache_dir / "Props").mkdir(parents=True)
for _name in ("crate_1mtl.obj", "crate_1mtl.mtl", "crate_4mtl.obj", "crate_4mtl.mtl",
              "boss_statue.gltf", "prop_crate.glb", "bark_Albedo.png", "ui_icon.png"):
    shutil.copy(DROP / _name, cache_dir / "Props" / _name)
(cache_dir / "Props" / "wood.obj").write_text(
    "mtllib wood.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 0 1\nusemtl wood\nf 1/1 2/2 3/3\n",
    encoding="utf-8")
(cache_dir / "Props" / "wood.mtl").write_text("newmtl wood\nmap_Kd wood_Albedo.png\n", encoding="utf-8")
make_png(cache_dir / "Props" / "wood_Albedo.png", (64, 64))
_cache_file = scratch / "cache_run.json"   # outside the checked folder, as the CLI insists
if _cache_file.exists():
    _cache_file.unlink()


def _cached_run(budget=None, healer=None, gatekeeper_args=None):
    """One --once run in-process; returns (verdicts by name, files inspected, cache)."""
    keeper = agk.Gatekeeper(budget or agk.Budget(), healer=healer, tag_root=cache_dir,
                            **(gatekeeper_args or {}))
    cache = agk.VerdictCache(_cache_file, cache_dir, agk.rules_fingerprint(keeper),
                             reuse_failures=healer is None)
    reporter = agk.Reporter(color=False, root=cache_dir)
    reporter.keep_verdicts = True
    inspected = []
    real = agk.Gatekeeper._inspect

    def counting(self, path):
        inspected.append(path.name)
        return real(self, path)
    with patch.object(agk.Gatekeeper, "_inspect", counting), \
            _contextlib.redirect_stderr(_io.StringIO()):
        agk.run_once(cache_dir, keeper, reporter, True, cache=cache)
    return {v.path.name: v for v in reporter.verdicts}, sorted(inspected), cache


_first, _seen1, _ = _cached_run()
expect("first run checks every asset and writes the cache",
       len(_seen1) == 8 and _cache_file.is_file(), _seen1)
_second, _seen2, _c2 = _cached_run()
expect("second run reads no unchanged file", _seen2 == [] and _c2.hits == 8, _seen2)
expect("... and gives the very same verdicts (status, problems, numbers, notes)",
       {n: (v.status, v.problems, v.metric, v.stats, v.warnings) for n, v in _first.items()}
       == {n: (v.status, v.problems, v.metric, v.stats, v.warnings) for n, v in _second.items()})
expect("... failures included (a cached failure still fails the run)",
       any(not v.passed for v in _second.values()))
_t = time.time() + 30
os.utime(cache_dir / "Props" / "boss_statue.gltf", (_t, _t))
_, _seen3, _ = _cached_run()
expect("a file only touched (a fresh checkout) is hashed, found identical, not re-checked",
       _seen3 == [], _seen3)
(cache_dir / "Props" / "crate_nomtl_copy.obj").write_bytes((DROP / "crate_nomtl.obj").read_bytes())
_target = cache_dir / "Props" / "crate_1mtl.obj"
_target.write_bytes(_target.read_bytes() + b"\n# edited\n")
_, _seen4, _ = _cached_run()
expect("editing one file re-checks exactly that file (and the new one)",
       _seen4 == ["crate_1mtl.obj", "crate_nomtl_copy.obj"], _seen4)
time.sleep(0.01)
make_png(cache_dir / "Props" / "wood_Albedo.png", (128, 128))
_, _seen5, _ = _cached_run()
expect("changing a texture an .obj's .mtl names re-checks that .obj (and the texture)",
       _seen5 == ["wood.obj", "wood_Albedo.png"], _seen5)
(cache_dir / "Props" / "wood_Albedo.png").unlink()
_v6, _seen6, _ = _cached_run()
expect("deleting that texture re-checks the .obj, which now fails its reference",
       _seen6 == ["wood.obj"] and not _v6["wood.obj"].passed
       and any("wood_Albedo.png" in reason for reason in _v6["wood.obj"].problems),
       (_seen6, _v6["wood.obj"].problems))
_v7, _seen7, _ = _cached_run(budget=agk.Budget(max_triangles=10))
expect("changing a rule re-checks everything, with the new rule applied",
       len(_seen7) == 8 and not _v7["crate_nomtl_copy.obj"].passed, _seen7)
_, _seen8, _ = _cached_run(budget=agk.Budget(max_triangles=10))
expect("... and the run after that reuses again", _seen8 == [], _seen8)
with patch.object(agk, "__version__", "9.9.9"):
    _, _seen9, _c9 = _cached_run(budget=agk.Budget(max_triangles=10))
expect("a new gate version re-checks everything", len(_seen9) == 8 and _c9.reset_reason, _seen9)
_cache_file.write_text("{ not json", encoding="utf-8")
_v10, _seen10, _c10 = _cached_run()
expect("a corrupt cache file is set aside with a note, never a crash",
       len(_seen10) == 8 and "unreadable" in _c10.reset_reason, _c10.reset_reason)


class _NoHealer:
    last_refusal = ""

    def heal(self, path, inspect):
        return None


_, _seen11, _ = _cached_run(healer=_NoHealer())
expect("with healing on, failures are re-checked so the healer gets its chance; passes are reused",
       _seen11 and set(_seen11) == {n for n, v in _v10.items() if not v.passed}, _seen11)
_doc = json.loads(_cache_file.read_text(encoding="utf-8"))
(cache_dir / "Props" / "crate_nomtl_copy.obj").unlink()
_cached_run()
_doc2 = json.loads(_cache_file.read_text(encoding="utf-8"))
expect("entries for files that are gone are dropped",
       "Props/crate_nomtl_copy.obj" in _doc["entries"]
       and "Props/crate_nomtl_copy.obj" not in _doc2["entries"])
_exc_soon = agk.BudgetException(path="Props/*", rule="max_triangles", allow=99999,
                                 reason="hero", owner="alex", expires=agk.date(2026, 12, 10))
_fp = [agk.rules_fingerprint(agk.Gatekeeper(agk.Budget(), exceptions=[_exc_soon], today=_day))
       for _day in (agk.date(2026, 11, 1), agk.date(2026, 11, 2), agk.date(2026, 12, 1),
                    agk.date(2026, 12, 2))]
expect("an exception far from its end date keeps the cache; inside the 14-day window it turns over daily",
       _fp[0] == _fp[1] and _fp[2] != _fp[3], _fp)

_gate_cli = [sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"), str(cache_dir),
             "--once", "--recursive", "--no-color"]


def _lines(run):
    return [line.split("  ", 1)[-1] for line in (run.stdout + run.stderr).splitlines()
            if "PASSED" in line or "FAILED" in line or "ERROR" in line]


_cache_home = scratch / "agk-cache-home"
if _cache_home.exists():
    shutil.rmtree(_cache_home)
_cli_env = dict(os.environ, AGK_CACHE_DIR=str(_cache_home))
_default_cache = agk.default_cache_path(cache_dir)
with patch.dict(os.environ, {"AGK_CACHE_DIR": str(_cache_home)}):
    _default_cache = agk.default_cache_path(cache_dir)
expect("the default cache lives outside the checked folder, one file per folder",
       not agk.cache_inside(cache_dir, _default_cache) and _default_cache.parent == _cache_home
       and agk.default_cache_path(cache_dir) != agk.default_cache_path(scratch), _default_cache)


def _gate(*extra):
    return subprocess.run(_gate_cli + list(extra), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=_cli_env)


_one = _gate("--jobs", "1", "--no-cache")
expect("--no-cache writes no cache file", not _cache_home.exists())
_four = _gate("--jobs", "4", "--no-cache")
expect("--jobs 4 gives the same verdicts, in the same order, and exit code as --jobs 1",
       _lines(_one) == _lines(_four) and len(_lines(_one)) == 7 and _one.returncode == _four.returncode == 1,
       (_lines(_one), _lines(_four), _four.stderr[-300:]))
_cold = _gate("--jobs", "2")
_warm = _gate()
expect("CLI: the second run says how much it reused, and prints the same verdicts",
       "7 reused, 0 checked" in _warm.stdout + _warm.stderr and _lines(_warm) == _lines(_one)
       and _warm.returncode == 1 and _default_cache.is_file(), (_warm.stdout + _warm.stderr)[-400:])
# A cache committed with the assets must count for nothing (audit 2026-11-02:
# an entry with the over-budget file's hash and no problems passed it in CI).
_forged = json.loads(_default_cache.read_text(encoding="utf-8"))
for _entry in _forged["entries"].values():
    _entry["verdict"]["problems"] = []
(cache_dir / agk.CACHE_FILENAME).write_text(json.dumps(_forged), encoding="utf-8")
_planted = _gate("--cache", str(cache_dir / agk.CACHE_FILENAME))
expect("--cache inside the checked folder is refused (exit 2), never read",
       _planted.returncode == 2 and "inside the folder being checked" in _planted.stdout + _planted.stderr,
       (_planted.stdout + _planted.stderr)[-300:])
_ignored = _gate()
expect("... and a cache file planted in the folder is simply not the cache: the failures stay",
       _ignored.returncode == 1 and _lines(_ignored) == _lines(_one))
(cache_dir / agk.CACHE_FILENAME).unlink()
_elsewhere = scratch / "ci-cache-dir" / "verdicts.json"
if _elsewhere.parent.exists():
    shutil.rmtree(_elsewhere.parent)
_gate("--cache", str(_elsewhere))
expect("--cache FILE puts it where CI keeps its caches, creating the folder", _elsewhere.is_file())

_json_run = subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"),
                            str(DROP / "bark_Albedo.png"), "--json"], capture_output=True, text=True)
_json_line = json.loads(_json_run.stdout.splitlines()[0])
expect("--json carries the numbers behind the verdict (memory_kb, as docs/05 says)",
       _json_line.get("stats", {}).get("memory_kb", 0) > 0, _json_line)
_cli_doc = (Path(ROOT_MODULE_DIR) / "docs" / "06-CLI-REFERENCE.md").read_text(encoding="utf-8")
_shape = _cli_doc.split("### The JSON shape", 1)[1].split("```json", 1)[1].split("```", 1)[0]
try:
    _documented = json.loads(_shape)
except ValueError:
    _documented = json.loads(_shape.strip().splitlines()[0])
_documented = _documented[0] if isinstance(_documented, list) else _documented
expect("docs/06's JSON example has the keys a verdict line really carries (file, not path)",
       set(_documented) == set(_json_line), (sorted(_documented), sorted(_json_line)))

_proj = scratch / "single_file_project"
if _proj.exists():
    shutil.rmtree(_proj)
(_proj / "Props").mkdir(parents=True)
(_proj / "Materials").mkdir()
(_proj / "rules.toml").write_text("[tags.props]\nmax_triangles = 1\n", encoding="utf-8")
(_proj / "Materials" / "a.mtl").write_text("newmtl m\nKd 1 1 1\n", encoding="utf-8")
(_proj / "Props" / "SM_two.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 1 1 0\nf 1 2 3\nf 2 4 3\n",
                                             encoding="utf-8")
(_proj / "Props" / "SM_shared.obj").write_text("mtllib ../Materials/a.mtl\nusemtl m\nv 0 0 0\nv 1 0 0\n"
                                                "v 0 1 0\nf 1 2 3\n", encoding="utf-8")
_one = {name: subprocess.run([sys.executable, str(ROOT_MODULE_DIR / "asset_gatekeeper.py"),
                              str(_proj / "Props" / name), "--rules", str(_proj / "rules.toml"),
                              "--no-color"], capture_output=True, text=True).returncode
        for name in ("SM_two.obj", "SM_shared.obj")}
expect("one file with --rules from the project above it gets the project's folder tags "
       "([tags.props] for Props/), as a --once run there does (the Perforce trigger's case)",
       _one["SM_two.obj"] == 1, _one)
expect("... and a ../Materials reference inside that project is not 'outside the project'",
       _one["SM_shared.obj"] == 0, _one)

print("15. VCS hook integrations")

p4_hook = load_module("agk_p4_hook", Path("vcs_hooks") / "p4_pre_submit.py")
git_hook = load_module("agk_git_hook", Path("vcs_hooks") / "git_pre_commit.py")

# -- p4: changelist parsing and the reject path --------------------------------
DESCRIBE = """Change 4711 by jdoe@ws-art-014 on 2026/10/31 12:00:00

\tSculpt pass on the boss

Affected files ...

... //depot/art/creatures/boss_statue.gltf#7 edit
... //depot/art/ui/ui_icon.png#2 add
... //depot/art/props/old.obj#3 delete
... //depot/art/readme.txt#1 edit
"""
p4_hook.p4 = lambda *a, **k: DESCRIBE
parsed = p4_hook.changelist_files("4711")
expect("p4 describe is parsed",
       ("//depot/art/ui/ui_icon.png", "add") in parsed, str(parsed))
expect("delete revisions are parsed too",
       ("//depot/art/props/old.obj", "delete") in parsed)
candidates = [(p, a) for p, a in parsed if a in p4_hook.CONTENT_ACTIONS
              and Path(p).suffix.lower() in p4_hook.ASSET_SUFFIXES]
expect("p4 candidate filter drops deletes and non-assets",
       [p for p, _ in candidates] == ["//depot/art/creatures/boss_statue.gltf",
                                     "//depot/art/ui/ui_icon.png"], str(candidates))
# (the no-heal guarantee is asserted below by capturing the argv the hook builds)

p4_temp = heal_root / "p4"
p4_temp.mkdir()
p4_hook.p4 = lambda *a, **k: DESCRIBE
p4_hook.fetch_revision = lambda cl, depot, dest: dest.write_bytes(b"")
seen_extra = []


def fake_gate(path, extra):
    seen_extra.append(list(extra))
    return subprocess.CompletedProcess(
        args=[], returncode=1,
        stdout="12:00:00 FAILED: 4,000 triangles exceeds the 3,000 budget (+1,000)  boss_statue.gltf",
        stderr="")


p4_hook.gate = fake_gate
p4_exit = p4_hook.main(["4711", "jdoe"])
expect("a failing changelist is rejected", p4_exit == 1, str(p4_exit))
expect("the p4 hook forces healing off on every file",
       bool(seen_extra) and all("--no-auto-heal" in item for item in seen_extra),
       str(seen_extra[:1]))

p4_hook.gate = lambda path, extra: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
expect("a clean changelist passes", p4_hook.main(["4711", "jdoe"]) == 0)

p4_hook.p4 = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("p4: server unreachable"))
expect("an unreadable changelist fails closed", p4_hook.main(["4711", "jdoe"]) == 1)

p4_hook.p4 = lambda *a, **k: DESCRIBE
p4_hook.gate = lambda path, extra: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
p4_hook.fetch_revision = lambda cl, depot, dest: (_ for _ in ()).throw(RuntimeError("no such rev"))
expect("a fetch failure fails closed", p4_hook.main(["4711", "jdoe"]) == 1)

p4_hook.p4 = lambda *a, **k: "Change 9 by x@y\n\nAffected files ...\n\n... //depot/x/readme.txt#1 edit\n"
expect("a changelist with no assets passes", p4_hook.main(["9", "x"]) == 0)

# -- p4: sign-off in the Art Workstation (W2) and the review queue (W8) --------
_ws = load_module("agk_ws_for_p4", Path("server") / "art_workstation_server.py")
_ws_dir = heal_root / "p4_workstation"
_ws_dir.mkdir()
_ws.init_db(_ws_dir / "ws.db")
_ws_server = _ws.build_server("127.0.0.1", 0, _ws_dir / "ws.db", _ws_dir / "data",
                              token="p4tok", quiet=True)
_ws_url = f"http://127.0.0.1:{_ws_server.server_address[1]}"
threading.Thread(target=_ws_server.serve_forever, daemon=True).start()
_P4_BYTES = {"//depot/art/creatures/boss_statue.gltf": b'{"asset":{"version":"2.0"}}',
             "//depot/art/ui/ui_icon.png": b"\x89PNG-not-really"}
p4_hook.p4 = lambda *a, **k: DESCRIBE
p4_hook.fetch_revision = lambda cl, depot, dest: dest.write_bytes(_P4_BYTES[depot])
p4_hook.gate = lambda path, extra: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def p4_run(*extra):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(out):
        code = p4_hook.main(["4711", "jdoe", *extra])
    return code, out.getvalue()


code, out = p4_run("--require-approval")
expect("--require-approval without a workstation refuses (fails closed)", code == 1
       and "needs --workstation" in out, out[-200:])
code, out = p4_run("--require-approval", "--workstation", _ws_url, "--workstation-token", "p4tok")
expect("an asset nobody approved is refused, with the reason", code == 1
       and "may never have been sent" in out, out[-300:])
_ids = []
for _depot, _bytes in _P4_BYTES.items():
    _asset = _ws.create_asset(_ws_dir / "ws.db", _ws_dir / "data", Path(_depot).name, _bytes)
    _ids.append(_asset["id"])
_ws.set_review(_ws_dir / "ws.db", _ids[0], "approved", reviewer="Lead")
_ws.set_review(_ws_dir / "ws.db", _ids[1], "changes_requested", reviewer="Lead")
code, out = p4_run("--require-approval", "--workstation", _ws_url, "--workstation-token", "p4tok")
expect("one approved and one with changes requested: refused, naming the second", code == 1
       and "ui_icon.png" in out and "changes requested" in out
       and "boss_statue" not in out.split("Submit rejected")[1].split("ui_icon")[0], out[-400:])
_ws.set_review(_ws_dir / "ws.db", _ids[1], "approved", reviewer="Lead")
code, out = p4_run("--require-approval", "--workstation", _ws_url, "--workstation-token", "p4tok")
expect("both approved: the submit goes through", code == 0, out[-300:])
code, out = p4_run("--require-approval", "--workstation", _ws_url, "--workstation-token", "wrong")
expect("a wrong workstation token fails closed", code == 1 and "could not ask" in out, out[-200:])
code, out = p4_run("--require-approval", "--workstation", "http://127.0.0.1:9", "--workstation-token", "x")
expect("an unreachable workstation fails closed", code == 1 and "could not ask" in out, out[-200:])
_P4_BYTES["//depot/art/creatures/boss_statue.gltf"] = b'{"asset":{"version":"2.0"},"v":2}'
code, out = p4_run("--workstation", _ws_url, "--workstation-token", "p4tok", "--verbose")
_sent = [a for a in _ws.list_assets(_ws_dir / "ws.db") if a["source_path"] == "//depot/art/creatures/boss_statue.gltf"]
expect("--workstation sends a passing submit for review, with depot path and changelist",
       code == 0 and _sent and _sent[0]["changelist"] == "4711" and "sent to the Art Workstation" in out,
       (out[-300:], _sent))
p4_hook.gate = fake_gate
_before = len(_ws.list_assets(_ws_dir / "ws.db", all_versions=True))
_P4_BYTES["//depot/art/creatures/boss_statue.gltf"] = b'{"asset":{"version":"2.0"},"v":3}'
code, out = p4_run("--workstation", _ws_url, "--workstation-token", "p4tok")
expect("a rejected submit sends nothing",
       code == 1 and len(_ws.list_assets(_ws_dir / "ws.db", all_versions=True)) == _before)
# A changelist that only edits a .bin changes an approved .gltf: with
# --require-approval it needs a reviewed version holding those bytes too.
_saved_p4, _saved_fetch = p4_hook.p4, p4_hook.fetch_revision
_bin_v1 = b"\x00" * 48
p4_hook.p4 = lambda *a, **k: ("Change 4712 by jdoe@ws\n\n\tbin\n\nAffected files ...\n\n"
                               "... //depot/art/creatures/statue.bin#2 edit\n")
_bin_bytes = {"now": b"\x01" * 48}
p4_hook.fetch_revision = lambda cl, depot, dest: dest.write_bytes(_bin_bytes["now"])
with contextlib.redirect_stdout(io.StringIO()) as _bin_out, contextlib.redirect_stderr(_bin_out):
    _bin_code = p4_hook.main(["4712", "jdoe", "--require-approval", "--workstation", _ws_url,
                              "--workstation-token", "p4tok"])
expect("--require-approval: a changelist that only edits a .bin is checked, and refused unreviewed",
       _bin_code == 1 and "statue.bin" in _bin_out.getvalue(), _bin_out.getvalue()[-300:])
_statue = _ws.create_asset(_ws_dir / "ws.db", _ws_dir / "data", "statue.gltf", b'{"asset":{"version":"2.0"},"s":1}')
_ws.add_sidecar(_ws_dir / "ws.db", _ws_dir / "data", _statue["id"], "statue.bin", _bin_bytes["now"])
_ws.set_review(_ws_dir / "ws.db", _statue["id"], "approved", reviewer="Lead")
with contextlib.redirect_stdout(io.StringIO()) as _bin_out, contextlib.redirect_stderr(_bin_out):
    _bin_code = p4_hook.main(["4712", "jdoe", "--require-approval", "--workstation", _ws_url,
                              "--workstation-token", "p4tok"])
expect("... and goes through once a reviewed, approved version holds those exact bytes",
       _bin_code == 0, _bin_out.getvalue()[-300:])
p4_hook.p4, p4_hook.fetch_revision = _saved_p4, _saved_fetch
_ws_server.shutdown()
_ws_server.server_close()

# -- p4: [[exceptions]] at submit time (audit 2026-11-02: never matched) -------
p4_exc = load_module("agk_p4_exc", Path("vcs_hooks") / "p4_pre_submit.py")
_EXC_RULES = (b'[mesh]\nmax_triangles = 50000\nmax_material_slots = 4\n\n[[exceptions]]\n'
              b'path = "creatures/*"\nrule = "max_triangles"\nallow = 80000\n'
              b'reason = "hero boss"\nowner = "lena"\nexpires = 2099-01-01\n')


def _exc_p4(*args, **kwargs):
    if "describe" in args:
        return ("Change 77 by lena@ws\n\n\tboss\n\nAffected files ...\n\n"
                "... //depot/art/creatures/boss_statue.gltf#3 edit\n"
                "... //depot/art/props/statue_copy.gltf#1 add\n")
    if "print" in args and args[-1] == "//depot/art/rules.toml#head":
        Path(args[args.index("-o") + 1]).write_bytes(_EXC_RULES)
        return ""
    raise RuntimeError("no such file")


p4_exc.p4 = _exc_p4
p4_exc.fetch_revision = lambda cl, depot, dest: dest.write_bytes((DROP / "boss_statue.gltf").read_bytes())
with contextlib.redirect_stdout(io.StringIO()) as _exc_out, contextlib.redirect_stderr(_exc_out):
    _exc_code = p4_exc.main(["77", "lena"])
_exc_text = _exc_out.getvalue()
expect("P4: an [[exceptions]] path in the depot's rules.toml applies at submit (creatures/* passes)",
       _exc_code == 1 and "statue_copy.gltf" in _exc_text
       and "creatures/boss_statue.gltf" not in _exc_text.split("Submit rejected")[1], _exc_text[-500:])
with patch.dict(os.environ, {"AGK_REQUIRE_APPROVAL": "True"}):
    with contextlib.redirect_stdout(io.StringIO()) as _ra_out, contextlib.redirect_stderr(_ra_out):
        _ra_code = p4_exc.main(["77", "lena"])
expect("AGK_REQUIRE_APPROVAL=True (any case) turns approval on, never silently off",
       _ra_code == 1 and "needs --workstation" in _ra_out.getvalue(), _ra_out.getvalue()[-200:])

# -- p4: the project's rules.toml comes from the depot -------------------------
# The gate reads rules.toml next to the file it is given; the trigger gates a
# temp copy, so without this the studio budget was replaced by the defaults.
DEPOT_RULES = {"//depot/art/rules.toml": "[mesh]\nmax_triangles = 120000\n"}
p4_calls = []


def fake_p4(*args, **kwargs):
    p4_calls.append(args)
    if args[1] == "describe":
        return DESCRIBE
    if args[1] == "print":
        target, spec = args[4], args[5]
        depot_file = spec.split("#")[0]
        if depot_file not in DEPOT_RULES:
            raise RuntimeError(f"{spec} - no such file(s).")
        Path(target).write_text(DEPOT_RULES[depot_file], encoding="utf-8")
        return ""
    raise RuntimeError(f"unexpected p4 call {args}")


seen_rules = []


def rules_gate(path, extra):
    if "--rules" in extra:
        rules_file = Path(extra[extra.index("--rules") + 1])
        seen_rules.append(rules_file.read_text(encoding="utf-8"))
    else:
        seen_rules.append(None)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


p4_hook.p4 = fake_p4
p4_hook.fetch_revision = lambda cl, depot, dest: dest.write_bytes(b"")
p4_hook.gate = rules_gate
expect("a clean changelist with depot rules passes", p4_hook.main(["4711", "jdoe"]) == 0)
expect("every asset is gated with the depot's rules.toml, not the defaults",
       len(seen_rules) == 2 and all(item and "120000" in item for item in seen_rules), str(seen_rules))
rules_specs = [call[5] for call in p4_calls if call[1] == "print"]
expect("the rules are read at the submitted head, not from this changelist",
       rules_specs and all(spec.endswith("#head") for spec in rules_specs), str(rules_specs))
expect("each depot folder is looked up once per changelist",
       len(rules_specs) == len(set(rules_specs)), str(rules_specs))
seen_rules.clear()
DEPOT_RULES.clear()
p4_hook.main(["4711", "jdoe"])
expect("no rules.toml in the depot: the defaults, and the gate still runs",
       seen_rules == [None, None], str(seen_rules))
seen_rules.clear()
DEPOT_RULES["//depot/art/rules.toml"] = "[mesh]\nmax_triangles = 120000\n"
(heal_root / "explicit_rules.toml").write_text("[mesh]\nmax_triangles = 777\n", encoding="utf-8")
p4_calls.clear()
p4_hook.main(["4711", "jdoe", "--rules", str(heal_root / "explicit_rules.toml")])
expect("--rules on the trigger line is what the gate gets",
       seen_rules and all(item and "777" in item for item in seen_rules), str(seen_rules))
expect("--rules on the trigger line skips the depot lookup",
       not any(call[1] == "print" and "rules.toml" in call[5] for call in p4_calls))

# -- p4: the trigger is told where the server is -------------------------------
# A trigger runs inside the Perforce service: no P4PORT, no P4USER, no login.
# Found on a real P4 Server: without these every submit failed with
# "TCP connect to perforce:1666 failed".
p4_conn = load_module("agk_p4_conn", Path("vcs_hooks") / "p4_pre_submit.py")
conn_calls = []


def fake_run(args, **kwargs):
    conn_calls.append((list(args), kwargs.get("env")))
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=DESCRIBE, stderr="")


p4_conn.subprocess = type("S", (), {"run": staticmethod(fake_run),
                                    "CompletedProcess": subprocess.CompletedProcess})
p4_conn.gate = lambda path, extra: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
p4_conn.fetch_revision = lambda cl, depot, dest: dest.write_bytes(b"")
tickets_file = str(heal_root / "p4tickets.txt")
p4_conn.main(["4711", "jdoe", "--port", "ssl:p4:1666", "--p4user", "agk-trigger",
              "--tickets", tickets_file, "--p4", "C:/Perforce/p4.exe"])
expect("every trigger p4 call carries the server address and user",
       conn_calls and all(call[0][:5] == ["C:/Perforce/p4.exe", "-p", "ssl:p4:1666", "-u", "agk-trigger"]
                          for call in conn_calls), str([call[0] for call in conn_calls[:1]]))
expect("the trigger's p4 calls read the given tickets file",
       all(call[1] and call[1].get("P4TICKETS") == tickets_file for call in conn_calls))
conn_calls.clear()
os.environ["AGK_P4PORT"] = "p4box:1666"
p4_conn.main(["4711", "jdoe"])
del os.environ["AGK_P4PORT"]
expect("AGK_P4PORT works when the trigger line has no --port",
       conn_calls and conn_calls[0][0][:3] == ["p4", "-p", "p4box:1666"], str([call[0] for call in conn_calls[:1]]))
conn_calls.clear()
p4_conn.main(["4711", "jdoe"])
expect("with nothing configured the p4 call is unchanged",
       conn_calls and conn_calls[0][0][:2] == ["p4", "describe"] and conn_calls[0][1] is None,
       str([call[0] for call in conn_calls[:1]]))

# -- p4: gatekeeper resolution -----------------------------------------------
os.environ["AGK_GATEKEEPER"] = str(ROOT_MODULE_DIR / "asset_gatekeeper.py")
expect("AGK_GATEKEEPER overrides the script path",
       p4_hook.gatekeeper_command()[-1].endswith("asset_gatekeeper.py"))
del os.environ["AGK_GATEKEEPER"]

# -- p4: a mesh and its texture in one changelist (the developer, 4 Nov 2026) ----------
# P4V submitted crate.obj + crate.mtl + T_Crate_Albedo.png together and the
# trigger refused the .obj: "missing reference 'T_Crate_Albedo.png'". Each file
# was gated the moment it was fetched, so the .obj went before its texture.
p4_ref = load_module("agk_p4_ref", Path("vcs_hooks") / "p4_pre_submit.py")
_ref_dir = heal_root / "p4_refs"
_ref_dir.mkdir()
_CUBE_V = "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nv 0 0 1\nv 1 0 1\nv 1 1 1\nv 0 1 1\n"
_CUBE_T = "vt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\n"
_CUBE_F = ("f 1/1 2/2 3/3\nf 1/1 3/3 4/4\nf 5/1 7/3 6/2\nf 5/1 8/4 7/3\nf 1/1 5/2 6/3\nf 1/1 6/3 2/4\n"
           "f 2/1 6/2 7/3\nf 2/1 7/3 3/4\nf 3/1 7/2 8/3\nf 3/1 8/3 4/4\nf 4/1 8/2 5/3\nf 4/1 5/3 1/4\n")
_REF_BYTES = {
    "//depot/art/props/crate.obj":
        ("mtllib crate.mtl\nusemtl wood\n" + _CUBE_V + _CUBE_T + _CUBE_F).encode(),
    "//depot/art/props/crate.mtl": b"newmtl wood\nKd 1 1 1\nmap_Kd T_Crate_Albedo.png\n",
}
_png_buffer = io.BytesIO()
Image.new("RGB", (64, 64), (128, 100, 80)).save(_png_buffer, "PNG")
_REF_BYTES["//depot/art/props/T_Crate_Albedo.png"] = _png_buffer.getvalue()
_head = {}          # what is already in the depot: path -> bytes
_head_asked = []


def _describe(paths):
    lines = "".join(f"... {path}#1 add\n" for path in paths)
    return f"Change 20 by artist@artist_ws\n\n\tcrate\n\nAffected files ...\n\n{lines}"


def _fake_p4_for(paths):
    def fake(*args, **kwargs):
        if "describe" in args:
            return _describe(paths)
        if "print" in args:
            spec = args[-1]
            depot = spec.rsplit("#", 1)[0] if spec.endswith("#head") else spec
            _head_asked.append(spec)
            if spec.endswith("#head") and depot in _head:
                Path(args[args.index("-o") + 1]).write_bytes(_head[depot])
                return ""
            raise RuntimeError(f"{spec} - no such file(s).")
        return ""
    return fake


p4_ref.fetch_revision = lambda cl, depot, dest: dest.write_bytes(_REF_BYTES[depot])
# Alphabetical, as P4V lists them: .mtl, .obj, then the texture.
_all_three = ["//depot/art/props/crate.mtl", "//depot/art/props/crate.obj",
              "//depot/art/props/T_Crate_Albedo.png"]
p4_ref.p4 = _fake_p4_for(_all_three)
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _exit = p4_ref.main(["20", "artist"])
expect("an .obj submitted with its .mtl and texture passes (was refused: 'missing reference')",
       _exit == 0, _out.getvalue()[-600:])

# The texture was submitted last week; this change only edits the .obj and .mtl.
_head["//depot/art/props/T_Crate_Albedo.png"] = _REF_BYTES["//depot/art/props/T_Crate_Albedo.png"]
_head_asked.clear()
p4_ref.p4 = _fake_p4_for(_all_three[:2])
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _exit = p4_ref.main(["21", "artist"])
expect("... and so does one whose texture is already in the depot (fetched at #head)",
       _exit == 0 and "//depot/art/props/T_Crate_Albedo.png#head" in _head_asked,
       (_out.getvalue()[-400:], _head_asked))

# A texture that is in neither the change nor the depot is a real broken link.
_head.clear()
p4_ref.p4 = _fake_p4_for(_all_three[:2])
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _exit = p4_ref.main(["22", "artist"])
expect("... but a texture that exists nowhere is still refused as a missing reference",
       _exit == 1 and "missing reference" in _out.getvalue(), _out.getvalue()[-400:])

# A reference that climbs out of the depot is never fetched from anywhere.
expect("a reference outside the depot layout maps to no depot path",
       p4_ref.depot_path_of(_ref_dir / "x.png", _ref_dir / "agk-p4-root") is None
       and p4_ref.depot_path_of(_ref_dir / "root" / "_rules" / "1" / "rules.toml", _ref_dir / "root") is None
       and p4_ref.depot_path_of(_ref_dir / "root" / "depot" / "a.png", _ref_dir / "root") == "//depot/a.png")

# Sent for review (N16): the revision is shown, and the texture travels with
# its mesh instead of becoming an asset of its own.
_ws_ref = load_module("agk_ws_for_p4_ref", Path("server") / "art_workstation_server.py")
_ws_ref_dir = heal_root / "p4_ref_workstation"
_ws_ref_dir.mkdir()
_ws_ref.init_db(_ws_ref_dir / "ws.db")
_ws_ref_server = _ws_ref.build_server("127.0.0.1", 0, _ws_ref_dir / "ws.db", _ws_ref_dir / "data",
                                      token="reftok", quiet=True)
threading.Thread(target=_ws_ref_server.serve_forever, daemon=True).start()
_ws_ref_url = f"http://127.0.0.1:{_ws_ref_server.server_address[1]}"
p4_ref.p4 = _fake_p4_for(_all_three)
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _exit = p4_ref.main(["23", "artist", "--workstation", _ws_ref_url,
                         "--workstation-token", "reftok", "--verbose"])
_ref_assets = _ws_ref.list_assets(_ws_ref_dir / "ws.db")
_ref_names = sorted(row["filename"] for row in _ref_assets)
expect("sent for review, the .obj arrives with its .mtl and texture, not three assets",
       _exit == 0 and _ref_names == ["crate.obj"], (_ref_names, _out.getvalue()[-500:]))
_ref_crate = next((row for row in _ref_assets if row["filename"] == "crate.obj"), {})
expect("... showing its revision as well as the changelist (was '//depot/...  @18')",
       _ref_crate.get("source_rev") == "#1" and str(_ref_crate.get("changelist")) == "23"
       and _ref_crate.get("source_path") == "//depot/art/props/crate.obj", _ref_crate)
_ref_detail = _ws_ref.asset_detail(_ws_ref_dir / "ws.db", _ws_ref_dir / "data",
                                   int(_ref_crate["id"])) if _ref_crate else {}
expect("... and nothing it references is missing",
       _ref_detail.get("missing") == [], _ref_detail.get("missing"))

# 5 Nov: the edit of a #1 file was shown as "#1" - a pending change lists the
# revision it replaces. The trigger now asks `p4 files <path>@=<change>`.
_edit_describe = ("Change 24 by artist@artist_ws\n\n\tedit\n\nAffected files ...\n\n"
                  "... //depot/art/props/T_Crate_Albedo.png#1 edit\n")


def _fake_p4_edit(files_answer):
    def fake(*args, **kwargs):
        if "describe" in args:
            return _edit_describe
        if "files" in args:
            if files_answer is None:
                raise RuntimeError("files: not supported here")
            return files_answer
        if "print" in args:
            raise RuntimeError(f"{args[-1]} - no such file(s).")
        return ""
    return fake


p4_ref.p4 = _fake_p4_edit("//depot/art/props/T_Crate_Albedo.png#2 - edit change 24 (binary)\n")
p4_ref.fetch_revision = lambda cl, depot, dest: dest.write_bytes(_REF_BYTES[depot])
with contextlib.redirect_stdout(io.StringIO()):
    _exit = p4_ref.main(["24", "artist", "--workstation", _ws_ref_url, "--workstation-token", "reftok"])
_tex = next((row for row in _ws_ref.list_assets(_ws_ref_dir / "ws.db")
             if row["filename"] == "T_Crate_Albedo.png"), {})
expect("an edit shows the revision it creates (#2), not the one it replaces (was #1)",
       _exit == 0 and _tex.get("source_rev") == "#2" and str(_tex.get("changelist")) == "24", _tex)
_saved_ref_p4 = p4_ref.p4
p4_ref.p4 = _fake_p4_edit(None)
expect("... and when `p4 files` cannot say, an edit shows no number rather than a wrong one",
       p4_ref.submitted_revision("24", "//depot/x.png", "1", "edit") == ""
       and p4_ref.submitted_revision("24", "//depot/x.png", "1", "add") == "1")
p4_ref.p4 = _saved_ref_p4

# 5 Nov: an approved .obj and .mtl were refused at submit. The Workstation had
# them with LF line endings; Perforce on Windows printed them with CRLF.
_crlf_db = _ws_ref_dir / "ws.db"
_crlf_obj = _ws_ref.create_asset(_crlf_db, _ws_ref_dir / "data", "lf_crate.obj",
                                 _REF_BYTES["//depot/art/props/crate.obj"].replace(b"crate.mtl", b"lf_crate.mtl"),
                                 new_asset=True)
_ws_ref.add_sidecar(_crlf_db, _ws_ref_dir / "data", int(_crlf_obj["id"]), "lf_crate.mtl",
                    _REF_BYTES["//depot/art/props/crate.mtl"])
_ws_ref.add_sidecar(_crlf_db, _ws_ref_dir / "data", int(_crlf_obj["id"]), "T_Crate_Albedo.png",
                    _REF_BYTES["//depot/art/props/T_Crate_Albedo.png"])
_ws_ref.set_review(_crlf_db, int(_crlf_obj["id"]), "approved", reviewer="Lead")
_CRLF_BYTES = {
    "//depot/art/lf/lf_crate.obj": _REF_BYTES["//depot/art/props/crate.obj"]
        .replace(b"crate.mtl", b"lf_crate.mtl").replace(b"\n", b"\r\n"),
    "//depot/art/lf/lf_crate.mtl": _REF_BYTES["//depot/art/props/crate.mtl"].replace(b"\n", b"\r\n"),
    "//depot/art/lf/T_Crate_Albedo.png": _REF_BYTES["//depot/art/props/T_Crate_Albedo.png"],
}
p4_ref.p4 = _fake_p4_for(list(_CRLF_BYTES))
p4_ref.fetch_revision = lambda cl, depot, dest: dest.write_bytes(_CRLF_BYTES[depot])
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _exit = p4_ref.main(["25", "artist", "--workstation", _ws_ref_url, "--workstation-token",
                         "reftok", "--require-approval"])
expect("an approved .obj + .mtl + texture submits although Perforce gave the trigger CRLF copies",
       _exit == 0, _out.getvalue()[-600:])
_CRLF_BYTES["//depot/art/lf/lf_crate.mtl"] = b"newmtl wood\r\nKd 0.9 0.9 0.9\r\nmap_Kd T_Crate_Albedo.png\r\n"
_out = io.StringIO()
with contextlib.redirect_stdout(_out):
    _exit = p4_ref.main(["26", "artist", "--workstation", _ws_ref_url, "--workstation-token",
                         "reftok", "--require-approval"])
expect("... but a .mtl whose content changed is still refused (line endings are all that is forgiven)",
       _exit == 1 and "lf_crate.mtl" in _out.getvalue() and "not approved" in _out.getvalue(),
       _out.getvalue()[-600:])
expect("... and a binary file is never 'normalised': one exact form only",
       len(p4_ref.content_variants(Path(_ws_ref_dir / "data" / "assets" / str(_crlf_obj["id"]) / "T_Crate_Albedo.png"))) == 1)
_ws_ref_server.shutdown()

# -- git: pre-commit against a real repository --------------------------------
git_dir = heal_root / "gitrepo"
git_dir.mkdir()


def run(cmd, cwd, env_extra=None):
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                "AGK_GATEKEEPER": str(ROOT_MODULE_DIR / "asset_gatekeeper.py"),
                "AGK_PYTHON": sys.executable})
    env.update(env_extra or {})
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)

if shutil.which("git") is None:
    expect("git is available for the hook tests", False, "git not found")
else:
    run(["git", "init", "-q", "-b", "main"], git_dir)
    run(["git", "config", "user.email", "t@t"], git_dir)
    run(["git", "config", "user.name", "t"], git_dir)
    (git_dir / "README.txt").write_text("x", encoding="utf-8")
    run(["git", "add", "README.txt"], git_dir)
    run(["git", "commit", "-q", "-m", "init"], git_dir)

    shutil.copy(DROP / "crate_4mtl.obj", git_dir / "crate_4mtl.obj")
    run(["git", "add", "crate_4mtl.obj"], git_dir)
    blocked = run([sys.executable, str(ROOT_MODULE_DIR / "vcs_hooks" / "git_pre_commit.py")],
                  git_dir, {"AGK_GATEKEEPER": str(ROOT_MODULE_DIR / "asset_gatekeeper.py")})
    expect("a failing staged asset blocks the commit", blocked.returncode == 1,
           f"exit {blocked.returncode}")
    expect("the rejection names the file", "crate_4mtl.obj" in blocked.stderr, blocked.stderr[:80])
    expect("the rejection explains nothing was healed",
           "Nothing was healed" in blocked.stderr)
    expect("the asset is byte-identical after a blocked commit",
           digest(git_dir / "crate_4mtl.obj") == digest(DROP / "crate_4mtl.obj"))

    run(["git", "reset", "-q", "crate_4mtl.obj"], git_dir)
    shutil.copy(DROP / "crate_1mtl.obj", git_dir / "crate_ok.obj")
    shutil.copy(DROP / "crate_1mtl.mtl", git_dir / "crate_1mtl.mtl")   # the OBJ references it
    run(["git", "add", "crate_ok.obj"], git_dir)
    allowed = run([sys.executable, str(ROOT_MODULE_DIR / "vcs_hooks" / "git_pre_commit.py")],
                  git_dir, {"AGK_GATEKEEPER": str(ROOT_MODULE_DIR / "asset_gatekeeper.py")})
    expect("a passing asset is allowed through", allowed.returncode == 0,
           f"exit {allowed.returncode} {allowed.stderr[:120]}")
    # Staged: the 4-material crate. On disk: the 1-material one, not staged. The
    # commit would hold the failing version; the hook read the passing one.
    shutil.copy(DROP / "crate_4mtl.obj", git_dir / "crate_swap.obj")
    run(["git", "add", "crate_swap.obj"], git_dir)
    shutil.copy(DROP / "crate_1mtl.obj", git_dir / "crate_swap.obj")
    swapped = run([sys.executable, str(ROOT_MODULE_DIR / "vcs_hooks" / "git_pre_commit.py")],
                  git_dir, {"AGK_GATEKEEPER": str(ROOT_MODULE_DIR / "asset_gatekeeper.py")})
    expect("a staged asset whose file on disk has unstaged changes is not passed on the disk copy",
           swapped.returncode == 1 and "not staged" in swapped.stderr,
           (swapped.returncode, swapped.stderr[-300:]))
    run(["git", "reset", "-q", "crate_swap.obj"], git_dir)
    (git_dir / "crate_swap.obj").unlink()
    # One rules.toml at the top of the repository, the asset in a subfolder.
    (git_dir / "rules.toml").write_text("[mesh]\nmax_triangles = 1\n", encoding="utf-8")
    (git_dir / "Props").mkdir(exist_ok=True)
    (git_dir / "Props" / "SM_two.obj").write_text(
        "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 1 1 0\nf 1 2 3\nf 2 4 3\n", encoding="utf-8")
    run(["git", "add", "Props/SM_two.obj"], git_dir)
    nested = run([sys.executable, str(ROOT_MODULE_DIR / "vcs_hooks" / "git_pre_commit.py")],
                 git_dir, {"AGK_GATEKEEPER": str(ROOT_MODULE_DIR / "asset_gatekeeper.py")})
    expect("an asset in a subfolder is judged by the repository's rules.toml above it, as CI's "
           "--once run there judges it", nested.returncode == 1
           and "exceeds the 1 budget (+1)  SM_two.obj" in nested.stderr,
           (nested.returncode, nested.stderr[-300:]))
    run(["git", "reset", "-q", "Props/SM_two.obj"], git_dir)
    shutil.rmtree(git_dir / "Props")
    (git_dir / "rules.toml").unlink()

    (git_dir / "notes.txt").write_text("not an asset", encoding="utf-8")
    run(["git", "add", "notes.txt"], git_dir)
    only_text = run([sys.executable, str(ROOT_MODULE_DIR / "vcs_hooks" / "git_pre_commit.py")],
                    git_dir, {"AGK_GATEKEEPER": str(ROOT_MODULE_DIR / "asset_gatekeeper.py")})
    expect("a commit with no assets is not gated", only_text.returncode == 0)
    _copied = (git_dir / ".git" / "copied_pre_commit.py").resolve()     # copied, as people do
    shutil.copy(ROOT_MODULE_DIR / "vcs_hooks" / "git_pre_commit.py", _copied)
    shutil.copy(DROP / "crate_1mtl.obj", git_dir / "crate_copied.obj")
    run(["git", "add", "crate_copied.obj"], git_dir)
    _lost = run([sys.executable, str(_copied)], git_dir, {"AGK_GATEKEEPER": ""})
    expect("a copied hook that cannot find the gate exits 2 saying how to install it, "
           "not a traceback", _lost.returncode == 2 and "Traceback" not in _lost.stderr
           and "--install" in _lost.stderr, (_lost.returncode, _lost.stderr[-300:]))
    run(["git", "reset", "-q", "crate_copied.obj"], git_dir)
    _copied.unlink()

    install = run([sys.executable, str(ROOT_MODULE_DIR / "vcs_hooks" / "git_pre_commit.py"),
                   "--install"], git_dir)
    hook_path = git_dir / ".git" / "hooks" / "pre-commit"
    expect("--install writes the hook shim", install.returncode == 0 and hook_path.is_file(),
           install.stderr[:120])
    expect("the shim calls this script",
           "git_pre_commit.py" in hook_path.read_text(encoding="utf-8"))

    run(["git", "reset", "-q"], git_dir)   # start from a clean index
    shutil.copy(DROP / "crate_1mtl.obj", git_dir / "crate_new.obj")
    (git_dir / "more_notes.txt").write_text("still not an asset", encoding="utf-8")
    run(["git", "add", "crate_new.obj", "more_notes.txt"], git_dir)
    staged_names = sorted(p.name for p in git_hook.staged_assets(cwd=str(git_dir)))
    expect("staged_assets keeps assets and drops text",
           staged_names == ["crate_new.obj"], str(staged_names))
    run(["git", "reset", "-q", "crate_new.obj", "more_notes.txt"], git_dir)

    # pre-push runs over the whole branch, not just the index. Prefer Git's own
    # bash: on Windows `which bash` can be WSL's, which cannot see C:\ paths.
    bash = None
    for candidate in (r"C:\Program Files\Git\bin\bash.exe",
                      r"C:\Program Files\Git\usr\bin\bash.exe",
                      shutil.which("bash")):
        if candidate and Path(candidate).is_file():
            bash = candidate
            break
    if bash is None:
        print("  skip no bash available: cannot exercise git_pre_push.sh")
    else:
        # The hook is installed now, so a real `git commit` must be blocked by it.
        run(["git", "add", "crate_4mtl.obj"], git_dir)
        blocked_commit = run(["git", "commit", "-q", "-m", "add a fat crate"], git_dir)
        expect("the installed hook blocks a real commit", blocked_commit.returncode != 0,
               f"exit {blocked_commit.returncode}")
        expect("the block is reported by the gate",
               "AssetGatekeeper" in (blocked_commit.stderr + blocked_commit.stdout))
        # --no-verify is the documented escape hatch; use it to build the history
        # the push test needs instead of fighting the commit hook.
        forced = run(["git", "commit", "-q", "--no-verify", "-m", "add a fat crate"], git_dir)
        expect("--no-verify bypasses the hook", forced.returncode == 0, forced.stderr[:120])
        push_script = (ROOT_MODULE_DIR / "vcs_hooks" / "git_pre_push.sh").as_posix()
        push_blocked = run([bash, push_script],
                           git_dir, {"AGK_GATEKEEPER": str(ROOT_MODULE_DIR / "asset_gatekeeper.py")})
        expect("pre-push blocks a branch that contains a failing asset",
               push_blocked.returncode == 1,
               f"exit {push_blocked.returncode} out={push_blocked.stdout[-200:]} "
               f"err={push_blocked.stderr[-200:]}")
        expect("pre-push reports the offending asset",
               "crate_4mtl.obj" in push_blocked.stderr, push_blocked.stderr[-160:])
        _push_dir = (scratch / "push_nested_rules").resolve()
        if _push_dir.exists():
            force_rmtree(_push_dir)
        (_push_dir / "Props").mkdir(parents=True)
        run(["git", "init", "-q", "-b", "main"], _push_dir)
        (_push_dir / "rules.toml").write_text("[mesh]\nmax_triangles = 1\n", encoding="utf-8")
        (_push_dir / "Props" / "SM_two.obj").write_text(
            "v 0 0 0\nv 1 0 0\nv 0 1 0\nv 1 1 0\nf 1 2 3\nf 2 4 3\n", encoding="utf-8")
        run(["git", "add", "."], _push_dir)
        run(["git", "commit", "-q", "--no-verify", "-m", "nested"], _push_dir)
        _nested_push = run([bash, push_script], _push_dir,
                           {"AGK_GATEKEEPER": str(ROOT_MODULE_DIR / "asset_gatekeeper.py")})
        expect("pre-push judges Props/SM_two.obj by the rules.toml at the repository's top",
               _nested_push.returncode == 1 and "SM_two.obj" in _nested_push.stderr,
               (_nested_push.returncode, _nested_push.stderr[-200:]))

print("16. references, platforms, PBR and transforms")

import base64 as _b64  # noqa: E402 - only this group needs it
import struct as _struct  # noqa: E402

audit_root = scratch / "audit"
if audit_root.exists():
    force_rmtree(audit_root)
audit_root.mkdir(parents=True)


def write_probe_gltf(path, images=(), translation=None, indexed=True,
                     bmin=(-0.5, -0.5, 0.0), bmax=(0.5, 0.5, 0.0), extra_buffer=None):
    """A minimal but valid glTF: one *centred* triangle, optional extras.

    Centred matters: an off-origin probe would fail the pivot audit for a reason
    the test did not intend, and hiding that behind a flag would make the audit
    look like it works when it is only being skipped.
    """
    positions = [(-0.5, -0.5, 0.0), (0.5, -0.5, 0.0), (-0.5, 0.5, 0.0)]
    position_bytes = _struct.pack("<9f", *[c for p in positions for c in p])
    index_bytes = _struct.pack("<3I", 0, 1, 2)
    blob = position_bytes + index_bytes
    document = {
        "asset": {"version": "2.0", "generator": "test"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "probe"}],
        "meshes": [{"name": "probe", "primitives": [{"attributes": {"POSITION": 0}}]}],
        "buffers": [{"byteLength": len(blob),
                     "uri": "data:application/octet-stream;base64,"
                            + _b64.b64encode(blob).decode()}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(position_bytes)},
            {"buffer": 0, "byteOffset": len(position_bytes), "byteLength": len(index_bytes)},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
             "min": list(bmin), "max": list(bmax)},
            {"bufferView": 1, "componentType": 5125, "count": 3, "type": "SCALAR"},
        ],
    }
    if indexed:
        document["meshes"][0]["primitives"][0]["indices"] = 1
    if images:
        document["images"] = [{"uri": uri} for uri in images]
    if translation is not None:
        document["nodes"][0]["translation"] = list(translation)
    if extra_buffer:
        document["buffers"].append({"byteLength": 12, "uri": extra_buffer})
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def check(path, budget=None, **kwargs):
    return agk.Gatekeeper(budget or agk.Budget()).check(Path(path))


# -- STEP 1: references -------------------------------------------------------
ref_dir = audit_root / "references"
ref_dir.mkdir()
broken = write_probe_gltf(ref_dir / "broken.gltf", images=["missing_Albedo.png"])
ok = write_probe_gltf(ref_dir / "ok.gltf", images=["present_Albedo.png"])
make_png(ref_dir / "present_Albedo.png", (64, 64), (120, 120, 120))

expect("a missing image reference fails the asset",
       not check(broken).passed and "missing reference" in check(broken).reason,
       check(broken).reason[:80])
expect("the broken filename is named in the verdict",
       "missing_Albedo.png" in check(broken).reason)
expect("a satisfied reference passes", check(ok).passed, check(ok).reason[:80])
expect("broken references are counted in stats",
       dict(check(broken).stats).get("broken_references") == 1, str(check(broken).stats))
expect("--no-reference-check suppresses the failure",
       check(broken, agk.Budget(check_references=False)).passed)
expect("data URIs are never treated as references",
       agk.collect_references(ok) == ["present_Albedo.png"], str(agk.collect_references(ok)))

# OBJ: mtllib, then the maps inside the .mtl.
obj_dir = audit_root / "obj_refs"
obj_dir.mkdir()
(obj_dir / "prop.obj").write_text(
    "mtllib prop.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
expect("a missing .mtl is a broken reference",
       "prop.mtl" in check(obj_dir / "prop.obj").reason,
       check(obj_dir / "prop.obj").reason[:80])
(obj_dir / "prop.mtl").write_text("newmtl wood\nmap_Kd wood_Albedo.png\n", encoding="utf-8")
obj_verdict = check(obj_dir / "prop.obj")
expect("a map inside the .mtl is followed", "wood_Albedo.png" in obj_verdict.reason,
       obj_verdict.reason[:100])
make_png(obj_dir / "wood_Albedo.png", (64, 64), (100, 90, 80))
expect("with both files present the OBJ passes", check(obj_dir / "prop.obj").passed,
       check(obj_dir / "prop.obj").reason[:80])

# Orphans: a texture nobody points at.
make_png(obj_dir / "unused_Albedo.png", (64, 64), (90, 90, 90))
orphans = [p.name for p in agk.orphaned_files(obj_dir)]
expect("an unreferenced texture is reported as an orphan", orphans == ["unused_Albedo.png"],
       str(orphans))
make_png(ref_dir / "lonely_Albedo.png", (64, 64), (90, 90, 90))
expect("orphans are found across the repository",
       "lonely_Albedo.png" in [p.name for p in agk.orphaned_files(audit_root)])

expect("the audit report exits 1 on a dirty repository",
       agk.run_audit(audit_root) == 1)
clean = audit_root / "clean"
clean.mkdir()
write_probe_gltf(clean / "solo.gltf")
expect("a clean repository audits to 0", agk.run_audit(clean) == 0)

# -- STEP 2: platform profiles ------------------------------------------------
plat_dir = audit_root / "platforms"
plat_dir.mkdir()
(plat_dir / "rules.toml").write_text(
    "[mesh]\nmax_triangles = 60000\nmax_material_slots = 4\n"
    "[texture]\nmax_edge = 4096\npower_of_two = true\n"
    "[platforms.mobile]\nmax_triangles = 15000\nmax_texture_edge = 1024\nmax_material_slots = 2\n"
    "[platforms.console]\nmax_triangles = 120000\nmax_texture_edge = 4096\n",
    encoding="utf-8")


def platform_args(*extra):
    return agk.build_parser().parse_args([str(plat_dir), *extra])


base_budget, _src, _ = agk.resolve_budget(plat_dir, platform_args())
expect("without a profile the base budget applies", base_budget.max_triangles == 60_000)
mobile, _src, _ = agk.resolve_budget(plat_dir, platform_args("--platform", "mobile"))
expect("--platform applies the profile", mobile.max_triangles == 15_000, str(mobile.max_triangles))
expect("the profile can lower texture resolution", mobile.max_texture_edge == 1024)
expect("the profile can lower material slots", mobile.max_material_slots == 2)
expect("the selected platform is recorded", mobile.platform == "mobile")
expect("untouched keys fall back to the base budget", mobile.require_power_of_two is True)
console, _src, _ = agk.resolve_budget(plat_dir, platform_args("--platform", "console"))
expect("a second profile is independent", console.max_triangles == 120_000)
over, _src, names = agk.resolve_budget(plat_dir, platform_args("--platform", "mobile",
                                                             "--triangles", "9000"))
expect("an explicit CLI flag still beats the profile",
       over.max_triangles == 9000 and "max_triangles" in names, str(names))

(plat_dir / "default.toml").write_text(
    'platform = "mobile"\n[mesh]\nmax_triangles = 60000\n'
    "[platforms.mobile]\nmax_triangles = 7000\n", encoding="utf-8")
defaulted, _src, _ = agk.resolve_budget(
    plat_dir, agk.build_parser().parse_args([str(plat_dir), "--rules",
                                             str(plat_dir / "default.toml")]))
expect("rules.toml can pick a default platform", defaulted.max_triangles == 7000,
       str(defaulted.max_triangles))
expect("the platform is reported for the log line", defaulted.platform == "mobile")

try:
    agk.resolve_budget(plat_dir, platform_args("--platform", "switch"))
    expect("an unknown platform is a config error", False, "no error raised")
except agk.ConfigError as exc:
    expect("an unknown platform is a config error", "unknown platform" in str(exc), str(exc)[:60])
(plat_dir / "bad.toml").write_text("[platforms.mobile]\nmax_pixels = 10\n", encoding="utf-8")
try:
    agk.resolve_budget(plat_dir, agk.build_parser().parse_args(
        [str(plat_dir), "--rules", str(plat_dir / "bad.toml"), "--platform", "mobile"]))
    expect("an unknown profile key is rejected", False, "no error raised")
except agk.ConfigError as exc:
    expect("an unknown profile key is rejected", "max_pixels" in str(exc), str(exc)[:60])

# -- STEP 3: PBR sanitisation -------------------------------------------------
pbr_dir = audit_root / "pbr"
pbr_dir.mkdir()
make_png(pbr_dir / "black_Albedo.png", (64, 64), (0, 0, 0))
make_png(pbr_dir / "white_Albedo.png", (64, 64), (255, 255, 255))
make_png(pbr_dir / "grey_Albedo.png", (64, 64), (128, 128, 128))
make_png(pbr_dir / "black_Normal.png", (64, 64), (0, 0, 0))
make_png(pbr_dir / "black_Diffuse.png", (64, 64), (0, 0, 0))

expect("a pure-black albedo is a PBR violation",
       "below the PBR floor" in check(pbr_dir / "black_Albedo.png").reason,
       check(pbr_dir / "black_Albedo.png").reason[:80])
expect("a pure-white albedo is a PBR violation",
       "above the PBR ceiling" in check(pbr_dir / "white_Albedo.png").reason,
       check(pbr_dir / "white_Albedo.png").reason[:80])
expect("a mid-grey albedo passes", check(pbr_dir / "grey_Albedo.png").passed,
       check(pbr_dir / "grey_Albedo.png").reason[:80])
expect("_Diffuse is checked the same way",
       "below the PBR floor" in check(pbr_dir / "black_Diffuse.png").reason)
expect("a normal map is not an albedo", check(pbr_dir / "black_Normal.png").passed,
       check(pbr_dir / "black_Normal.png").reason[:80])
expect("the violation names the measured luminance",
       "0.000" in check(pbr_dir / "black_Albedo.png").reason)
expect("pbr can be switched off",
       check(pbr_dir / "black_Albedo.png", agk.Budget(pbr_check=False)).passed)

# Configurable thresholds, validated like everything else.
(pbr_dir / "floor.toml").write_text("[texture]\npbr_floor = 0.6\n", encoding="utf-8")
strict = agk.Budget.from_mapping(agk.parse_rules(pbr_dir / "floor.toml"))
expect("a stricter floor catches mid-grey too",
       "below the PBR floor" in check(pbr_dir / "grey_Albedo.png", strict).reason)
(pbr_dir / "bad.toml").write_text("[texture]\npbr_floor = \"dark\"\n", encoding="utf-8")
try:
    agk.Budget.from_mapping(agk.parse_rules(pbr_dir / "bad.toml"))
    expect("a non-numeric threshold is rejected", False, "no error raised")
except agk.ConfigError:
    expect("a non-numeric threshold is rejected", True)

# -- STEP 4: geometric boundary profiling -------------------------------------
geo_dir = audit_root / "geometry"
geo_dir.mkdir()
floating = write_probe_gltf(geo_dir / "floating.gltf", translation=(3.0, 0.0, 0.0))
centred = write_probe_gltf(geo_dir / "centred.gltf")
unindexed = write_probe_gltf(geo_dir / "unindexed.gltf", indexed=False)
offset_geo = write_probe_gltf(geo_dir / "offset.gltf", bmin=(10.0, 10.0, 10.0),
                              bmax=(11.0, 11.0, 11.0))

audit_budget = agk.Budget(audit_transforms=True)
expect("an unbaked node translation is flagged",
       "unbaked translation" in check(floating, audit_budget).reason,
       check(floating, audit_budget).reason[:90])
expect("the flagged offset is printed in metres",
       "3.000" in check(floating, audit_budget).reason)
expect("a baked node passes the audit", check(centred, audit_budget).passed,
       check(centred, audit_budget).reason[:90])
expect("the audit is off by default", check(floating).passed,
       check(floating).reason[:90])
expect("off-centre geometry is an uncentred pivot",
       "uncentred" in check(offset_geo, audit_budget).reason,
       check(offset_geo, audit_budget).reason[:90])
expect("an unindexed primitive is only flagged when asked",
       check(unindexed, audit_budget).passed)
indexed_budget = agk.Budget(audit_transforms=True, require_indexed=True)
expect("require_indexed flags the missing index buffer",
       "unindexed geometry" in check(unindexed, indexed_budget).reason,
       check(unindexed, indexed_budget).reason[:90])
expect("--require-indexed implies the transform audit",
       agk.resolve_budget(geo_dir, agk.build_parser().parse_args(
           [str(geo_dir), "--require-indexed"]))[0].audit_transforms is True)

soup = geo_dir / "soup.obj"
soup.write_text("".join(
    f"v {i} 0 0\nv {i} 1 0\nv {i + 1} 1 0\nf {i * 3 + 1} {i * 3 + 2} {i * 3 + 3}\n"
    for i in range(20)), encoding="utf-8")
expect("triangle soup is flagged as unindexed",
       "unindexed geometry" in check(soup, indexed_budget).reason,
       check(soup, indexed_budget).reason[:90])

print("17. packaging, the setup wizard, and profile whitelists")

pkg_root = Path(__file__).resolve().parent.parent
pyproject_path = pkg_root / "pyproject.toml"
expect("pyproject.toml exists", pyproject_path.is_file())
pyproject_text = pyproject_path.read_text(encoding="utf-8")
expect("the console script is declared",
       'asset-gatekeeper = "asset_gatekeeper:main"' in pyproject_text)
expect("the version is dynamic so it cannot drift",
       'dynamic = ["version"]' in pyproject_text
       and 'version = { attr = "asset_gatekeeper.__version__" }' in pyproject_text)
# The gate refuses to run without trimesh+Pillow, so they are honest hard
# requirements; watchdog is a speed-up and must stay optional.
expect("trimesh and pillow are declared as required",
       '"trimesh>=4.0,<6"' in pyproject_text and '"pillow>=9.0,<13"' in pyproject_text)
# Audited: without tomli a plain pip install on 3.9/3.10 cannot read rules.toml
# at all, so it is a hard requirement below 3.11 - and must not be installed on
# 3.11+, where tomllib is in the standard library.
expect("tomli is declared for interpreters older than 3.11",
       '"tomli>=2.0; python_version < \'3.11\'"' in pyproject_text,
       pyproject_text[:160])
expect("watchdog is only an extra",
       'full = ["watchdog>=3.0"' in pyproject_text
       and "watchdog" not in pyproject_text.split("[project.optional-dependencies]")[0])
expect("the version attribute exists", isinstance(agk.__version__, str) and agk.__version__)
expect("setup.py is a shim, not a second configuration",
       (pkg_root / "setup.py").is_file()
       and 'setup()' in (pkg_root / "setup.py").read_text(encoding="utf-8"))

wiz_root = audit_root / "wizard"
if wiz_root.exists():
    force_rmtree(wiz_root)

# The wizard, driven by an injected `ask` so it needs no stdin.
# With the folder supplied, the prompts are: triangles, materials, texture edge,
# platform, auto-heal â€” in that order.
answers = ["15000", "2", "1024", "", "y"]
asked = []


def fake_ask(question):
    asked.append(question)
    return answers[len(asked) - 1] if len(asked) <= len(answers) else ""


wiz_target = wiz_root / "game_assets"
wiz_out = []
expect("--init succeeds", agk.run_init(wiz_target, ask=fake_ask, out=wiz_out.append) == 0)
wiz_rules = wiz_target / agk.RULES_FILENAME
expect("it creates the folder and the rules file", wiz_rules.is_file())
expect("the first question is the triangle budget", "Triangle budget" in asked[0], asked[0])
expect("it asks all five questions", len(asked) == 5, str(asked))
wiz_doc = agk.parse_rules(wiz_rules)
wiz_budget = agk.Budget.from_mapping(wiz_doc)
expect("the answers become the budget", wiz_budget.max_triangles == 15_000,
       str(wiz_budget.max_triangles))
expect("the texture edge answer sticks", wiz_budget.max_texture_edge == 1024)
expect("the material answer sticks", wiz_budget.max_material_slots == 2)
expect("yes enables healing in the generated file",
       wiz_doc.get("healer", {}).get("enabled") is True, str(wiz_doc.get("healer")))
expect("the generated file ships platform profiles",
       set(wiz_doc.get("platforms", {})) >= {"mobile", "console", "pc"},
       str(sorted(wiz_doc.get("platforms", {}))))
expect("the mobile profile is clamped to the smaller budget",
       wiz_doc["platforms"]["mobile"]["max_triangles"] == 15_000)
expect("the wizard prints next steps", any("--once" in line for line in wiz_out))

# Re-running must not silently destroy a studio's rules.
expect("an existing rules.toml is protected",
       agk.run_init(wiz_target, ask=lambda _q: "", out=wiz_out.append) == 2)
expect("--force overwrites it",
       agk.run_init(wiz_target, ask=lambda _q: "", out=wiz_out.append, force=True) == 0)

# Piped input / CI: every prompt hitting EOF must fall back, never hang.
eof_target = wiz_root / "ci_assets"


def eof_ask(_question):
    raise EOFError


expect("EOF falls back to defaults", agk.run_init(eof_target, ask=eof_ask,
                                                 out=wiz_out.append) == 0)
expect("the CI default budget is 50k",
       agk.Budget.from_mapping(agk.parse_rules(eof_target / agk.RULES_FILENAME))
       .max_triangles == 50_000)

# The CLI entry point itself.
cli_target = wiz_root / "cli_assets"
expect("--init --yes works through main()",
       agk.main(["--init", str(cli_target), "--yes"]) == 0)
expect("and it wrote a usable rules file", (cli_target / agk.RULES_FILENAME).is_file())
# Platform-neutral "impossible" target: a file cannot contain a folder.
blocker = wiz_root / "i_am_a_file"
blocker.write_text("x", encoding="utf-8")
expect("--init on an impossible path exits 2",
       agk.main(["--init", str(blocker / "assets"), "--yes"]) == 2,
       "mkdir under a regular file must fail")

# Secondary whitelist: platform profiles may tighten thresholds, never disable checks.
prof_dir = audit_root / "profile_sections"
prof_dir.mkdir()
(prof_dir / "rules.toml").write_text(
    "[mesh]\nmax_triangles = 50000\n"
    "[platforms.mobile]\nmax_triangles = 15000\n"
    "[platforms.mobile.texture]\npbr_floor = 0.08\n"
    "[platforms.mobile.mesh]\npivot_tolerance = 0.01\n",
    encoding="utf-8")
sec_budget, _src, _ = agk.resolve_budget(prof_dir, agk.build_parser().parse_args(
    [str(prof_dir), "--platform", "mobile"]))
expect("a nested table tightens a threshold", sec_budget.pbr_floor == 0.08,
       str(sec_budget.pbr_floor))
expect("the other section works too", sec_budget.pivot_tolerance == 0.01)
expect("budget keys still apply alongside", sec_budget.max_triangles == 15_000)

for label, body, expected in (
    ("an unknown section is rejected",
     "[platforms.mobile.nonsense]\nx = 1\n", "section"),
    ("an unknown threshold is rejected",
     "[platforms.mobile.texture]\npbr_brightness = 0.5\n", "pbr_brightness"),
    ("a profile cannot switch a check off",
     "check_references = false\n", "check_references"),
):
    (prof_dir / "bad.toml").write_text("[platforms.mobile]\n" + body, encoding="utf-8")
    try:
        agk.resolve_budget(prof_dir, agk.build_parser().parse_args(
            [str(prof_dir), "--rules", str(prof_dir / "bad.toml"), "--platform", "mobile"]))
        expect(label, False, "no error raised")
    except agk.ConfigError as exc:
        expect(label, expected in str(exc), str(exc)[:70])

print("18. tags, presets and native notifications")

import subprocess as _subprocess  # noqa: E402

# ---- presets ---------------------------------------------------------------
retro = agk.Budget.from_mapping(agk.apply_preset({}, "retro"))
expect("a shipped preset applies without a rules file",
       retro.max_triangles == 1500 and retro.max_texture_edge == 256,
       f"{retro.max_triangles}/{retro.max_texture_edge}")
expect("a preset does not require textures to be named",
       agk.Budget.from_mapping(agk.apply_preset({}, "pc")).max_triangles == 120000)
try:
    agk.apply_preset({}, "nintendo-64")
    expect("an unknown preset is rejected", False)
except agk.ConfigError as exc:
    expect("an unknown preset is rejected", "nintendo-64" in str(exc), str(exc))
expect("no preset name changes nothing", agk.apply_preset({"mesh": {}}, "") == {"mesh": {}})

preset_cfg = scratch / "tags"
preset_cfg.mkdir(parents=True, exist_ok=True)
(preset_cfg / "rules.toml").write_text(
    "[mesh]\nmax_triangles = 90000\n[texture]\nmax_edge = 4096\n", encoding="utf-8")
def preset_args(*extra):
    return agk.build_parser().parse_args([str(preset_cfg), *extra])

preset_budget = agk.resolve_budget(preset_cfg, preset_args("--preset", "mobile"))[0]
expect("--preset beats the project file", preset_budget.max_triangles == 15000,
       str(preset_budget.max_triangles))
preset_budget = agk.resolve_budget(preset_cfg, preset_args("--preset", "mobile",
                                                         "--triangles", "4200"))[0]
expect("an explicit flag still beats the preset", preset_budget.max_triangles == 4200)

# The CLI path used to drop a preset whenever the folder had no rules.toml, so
# `asset-gatekeeper <folder> --preset retro` - the first command in the manual -
# silently kept the 50k default. The pure function was tested; the resolver was
# not, and that is exactly where the bug lived.
bare_dir = scratch / "preset_bare"
bare_dir.mkdir(parents=True, exist_ok=True)
bare = agk.build_parser().parse_args([str(bare_dir), "--preset", "retro"])
bare_budget, bare_source, _ = agk.resolve_budget(bare_dir, bare)
expect("--preset works with no rules file at all",
       bare_budget.max_triangles == 1500 and bare_budget.max_texture_edge == 256,
       f"{bare_budget.max_triangles}/{bare_budget.max_texture_edge}")
expect("a preset applied without a rules file reports no rules source", bare_source is None)
expect("no rules file and no preset is still the shipped default",
       agk.resolve_budget(bare_dir, agk.build_parser().parse_args([str(bare_dir)]))[0]
       .max_triangles == 50_000)
try:
    agk.resolve_budget(bare_dir, agk.build_parser().parse_args(
        [str(bare_dir), "--platform", "mobile"]))
    expect("a platform with no [platforms.*] table is refused, not ignored", False)
except agk.ConfigError as exc:
    expect("a platform with no [platforms.*] table is refused, not ignored",
           "unknown platform profile" in str(exc) and "--preset" in str(exc), str(exc)[:80])

# ---- hierarchical tags -----------------------------------------------------
tag_doc = {
    "mesh": {"max_triangles": 50000},
    "tags": {"Monsters_Boss": {"max_triangles": 1000},
             "Props": {"max_triangles": 5000, "texture": {"pbr_floor": 0.05}}},
}
tag_budgets = agk.build_tag_budgets(tag_doc)
expect("each tag compiles to its own budget",
       sorted(tag_budgets) == ["monsters boss", "props"] and
       tag_budgets["monsters boss"].max_triangles == 1000)
expect("a tag inherits the base budget it does not override",
       tag_budgets["monsters boss"].max_texture_edge == 2048)
expect("a tag can tighten a strictness threshold",
       tag_budgets["props"].pbr_floor == 0.05)

exact_path = Path("Exports/Monsters_Boss/boss.gltf")
expect("an exact folder match resolves",
       agk.resolve_tag(exact_path, tag_budgets) == ("monsters boss", None))
expect("case and separators do not matter",
       agk.resolve_tag(Path("Exports/monsters-boss/boss.gltf"), tag_budgets)[0] == "monsters boss")
typo_tag, typo_warning = agk.resolve_tag(Path("Exports/Monsters_Bos/boss.gltf"), tag_budgets)
expect("a near-miss folder is recovered, not ignored", typo_tag == "monsters boss", str(typo_tag))
expect("the recovery is announced", typo_warning is not None and "closest match" in typo_warning,
       str(typo_warning))
expect("an unrelated folder matches nothing",
       agk.resolve_tag(Path("Exports/Vehicles/truck.gltf"), tag_budgets) == (None, None))
expect("a filename prefix is a valid tag source",
       agk.resolve_tag(Path("Exports/Props_01.obj"), tag_budgets)[0] == "props")

heal_cfg = tag_budgets
boss = scratch / "tags" / "Monsters_Boss"
boss.mkdir(parents=True, exist_ok=True)
shutil.copy(DROP / "boss_statue.gltf", boss / "boss.gltf")
typo_dir = scratch / "tags" / "Monsters_Bos"
typo_dir.mkdir(parents=True, exist_ok=True)
shutil.copy(DROP / "boss_statue.gltf", typo_dir / "typo.gltf")

tag_gate = agk.Gatekeeper(agk.Budget(), tag_budgets=tag_budgets, tag_mode="suggest")
tag_verdict = tag_gate.check(boss / "boss.gltf")
expect("the tag budget is what fails the asset",
       any("1,000 budget" in problem for problem in tag_verdict.problems),
       str(tag_verdict.problems))
expect("an exact match produces no advisory", tag_verdict.warnings == ())
typo_verdict = agk.Gatekeeper(agk.Budget(), tag_budgets=tag_budgets).check(typo_dir / "typo.gltf")
expect("a fuzzy match applies the tag budget too",
       any("1,000 budget" in problem for problem in typo_verdict.problems))
expect("and says so as a warning", len(typo_verdict.warnings) == 1, str(typo_verdict.warnings))
expect("a warning never flips the verdict",
       typo_verdict.passed == (not typo_verdict.problems))
strict_verdict = agk.Gatekeeper(agk.Budget(), tag_budgets=tag_budgets,
                               tag_mode="strict").check(typo_dir / "typo.gltf")
expect("strict mode fails a near-miss with the fix",
       any("rename the folder" in problem for problem in strict_verdict.problems),
       str(strict_verdict.problems))
expect("strict mode does not silently apply the guess",
       any("50,000 budget" in problem for problem in strict_verdict.problems))

tagging_cfg = scratch / "tagging"
tagging_cfg.mkdir(parents=True, exist_ok=True)
(tagging_cfg / "rules.toml").write_text(
    "[tagging]\nmode = \"strict\"\nmin_similarity = 0.9\n", encoding="utf-8")
budgets, mode, similarity = agk.resolve_tagging(
    tagging_cfg, agk.build_parser().parse_args([str(tagging_cfg)]))
expect("the tagging policy loads from the file", mode == "strict" and similarity == 0.9,
       f"{mode} {similarity}")
budgets, mode, _ = agk.resolve_tagging(
    tagging_cfg, agk.build_parser().parse_args([str(tagging_cfg), "--tag-mode", "auto"]))
expect("the CLI beats the tagging policy", mode == "auto")
(tagging_cfg / "rules.toml").write_text("[tagging]\nmode = \"lenient\"\n", encoding="utf-8")
try:
    agk.resolve_tagging(tagging_cfg, agk.build_parser().parse_args([str(tagging_cfg)]))
    expect("an unknown tagging mode is rejected", False)
except agk.ConfigError as exc:
    expect("an unknown tagging mode is rejected", "lenient" in str(exc), str(exc))

# ---- native notifications --------------------------------------------------
import notifier as _notifier  # noqa: E402

argv, env = _notifier._command("notify-send", "Title", "Body")
expect("linux pushes text through argv", argv[-2:] == ["Title", "Body"], str(argv))
argv, env = _notifier._command("windows-toast", "Title; rm -rf /", "Body $(evil)")
expect("windows never interpolates text into the script",
       not any("rm -rf" in part for part in argv), str(argv))
expect("windows passes text through the environment",
       env["AGK_TOAST_TITLE"] == "Title; rm -rf /" and env["AGK_TOAST_BODY"] == "Body $(evil)")

sent_calls = []
def fake_run(argv, **kwargs):
    sent_calls.append(argv)
    return _subprocess.CompletedProcess(argv, 0, "", "")

with patch("notifier.subprocess.run", side_effect=fake_run):
    toast = _notifier.Notifier(max_per_minute=2, backend="notify-send")
    for index in range(3):
        toast.notify("T", f"body {index}")
    toast.close(timeout=5.0)
expect("the rate limit holds", toast.sent == 2 and toast.suppressed == 1,
       f"sent={toast.sent} suppressed={toast.suppressed}")

sent_calls.clear()          # the rate-limit block above already sent two
with patch("notifier.subprocess.run", side_effect=fake_run):
    toast = _notifier.Notifier(backend="notify-send")
    failed_verdict = agk.Verdict(Path("heavy.glb"), ("too many triangles", "too many slots"),
                                 "1 tris")
    healed_verdict = agk.Verdict(Path("fixed.png"), (), "8x8", (), ("padded",))
    passed_verdict = agk.Verdict(Path("fine.obj"), (), "1 tris")
    expect("a failure raises a toast", toast.notify_verdict(failed_verdict) is True)
    expect("a heal raises a toast", toast.notify_verdict(healed_verdict) is True)
    expect("a pass is silent", toast.notify_verdict(passed_verdict) is False)
    toast.close(timeout=5.0)
expect("toasts carry the asset name", any("heavy.glb" in str(call) for call in sent_calls))
expect("only failing/ healed assets were announced", len(sent_calls) == 2, str(len(sent_calls)))
expect("a disabled notifier never spawns anything",
       _notifier.Notifier(enabled=False).notify("T", "B") is False)

print("19. exclude filters and folder tags")

# Real asset trees keep review renders and paint sources next to shipped art.
expect("a plain file is a candidate", agk.is_candidate(Path("crate.obj")))
expect("an excluded glob is skipped",
       agk.is_candidate(Path("preview_crate.png"), ["preview_*"]) is False)
expect("other files still pass the filter",
       agk.is_candidate(Path("crate_Albedo.png"), ["preview_*"]) is True)
expect("filters are matched case-sensitively like a shell",
       agk.is_candidate(Path("Preview_crate.png"), ["preview_*"]) is True)

filter_cfg = scratch / "filters"
filter_cfg.mkdir(parents=True, exist_ok=True)
(filter_cfg / "rules.toml").write_text(
    '[texture]\nmax_edge = 64\n[filters]\nexclude = ["preview_*", "*_src4x.png"]\n',
    encoding="utf-8")
filter_budget = agk.resolve_budget(filter_cfg, agk.build_parser().parse_args([str(filter_cfg)]))[0]
expect("filters load from the rules file",
       filter_budget.exclude == ("preview_*", "*_src4x.png"), str(filter_budget.exclude))
cli_budget = agk.resolve_budget(
    filter_cfg, agk.build_parser().parse_args([str(filter_cfg), "--exclude", "*.tmp.png"]))[0]
expect("the CLI replaces the file's filters", cli_budget.exclude == ("*.tmp.png",))
(filter_cfg / "rules.toml").write_text('[filters]\nignore = ["x"]\n', encoding="utf-8")
try:
    agk.resolve_budget(filter_cfg, agk.build_parser().parse_args([str(filter_cfg)]))
    expect("an unknown filters key is rejected", False)
except agk.ConfigError as exc:
    expect("an unknown filters key is rejected", "ignore" in str(exc), str(exc))

# A tag must resolve from the folder an asset was dropped into.
folder_root = scratch / "folder_tags"
(folder_root / "rooms").mkdir(parents=True, exist_ok=True)
(folder_root / "props").mkdir(parents=True, exist_ok=True)
(folder_root / "rules.toml").write_text(
    "[mesh]\nmax_triangles = 1000\n[tags.rooms]\nmax_triangles = 8000\n", encoding="utf-8")
folder_args = agk.build_parser().parse_args([str(folder_root)])
folder_budget, _, _ = agk.resolve_budget(folder_root, folder_args)
folder_tags, folder_mode, _ = agk.resolve_tagging(folder_root, folder_args)
shutil.copy(DROP / "boss_statue.gltf", folder_root / "rooms" / "huge.gltf")
folder_gate = agk.Gatekeeper(folder_budget, tag_budgets=folder_tags, tag_mode=folder_mode,
                             tag_root=folder_root)
room_verdict = folder_gate.check(folder_root / "rooms" / "huge.gltf")
# The fixture is deliberately bigger than even the room budget, so the proof that
# the tag applied is the limit quoted in the message (8,000, not the 1,000 base).
expect("the room tag raises the budget for a folder",
       any("8,000 budget" in problem for problem in room_verdict.problems),
       str(room_verdict.problems))
expect("a room tag does not leak to a sibling folder",
       agk.resolve_tag(folder_root / "props" / "huge.obj", folder_tags, folder_root)[0] is None)

print("20. doctor, port hints and a friendly CLI")
doc_root = DROP.parent / "scratch" / "doctor"
doc_root.mkdir(parents=True, exist_ok=True)
(doc_root / "rules.toml").write_text(
    'platform = "n64"\n\n[mesh]\nmax_triangles = 1200\n\n[platforms.n64]\nmax_texture_edge = 64\n\n[tags.ruins]\nmax_triangles = 4000\n',
    encoding="utf-8")
doc_args = agk.build_parser().parse_args([str(doc_root)])
# Doctor output is many lines; a detail must stay on one line in the log.
flat = lambda s: " | ".join(str(s).splitlines())[:160]  # noqa: E731
lines = []
code = agk.run_doctor(doc_root, doc_args, out=lines.append)
text = "\n".join(lines)
expect("doctor passes on a healthy setup", code == 0, flat(text))
expect("it names the interpreter", "python" in text)
expect("it checks the two hard dependencies", "trimesh" in text and "pillow" in text)
expect("it finds the rules file", str(doc_root / "rules.toml") in text, flat(text)[:200])
expect("it reports the effective budget", "1,200 tris" in text, flat(text))
expect("it lists the tag budgets", "ruins" in text)
expect("it suggests the next command", "--recursive --once" in text)

# A broken rules file must fail the doctor, with the parser's own words.
(doc_root / "rules.toml").write_text('[mesh]\nmax_triangles = "lots"\n', encoding="utf-8")
lines = []
code = agk.run_doctor(doc_root, doc_args, out=lines.append)
text = "\n".join(lines)
expect("a broken rules file fails the doctor", code == 1, flat(text))
expect("the doctor quotes the parser error", "must be an integer" in text, flat(text))
expect("and tells you how to fix it", "example.toml" in text, flat(text))
(doc_root / "rules.toml").unlink()

# A missing folder is a failure, not a traceback.
lines = []
code = agk.run_doctor(DROP.parent / "scratch" / "no_such_folder", doc_args, out=lines.append)
expect("a missing folder fails the doctor", code == 1, flat("\n".join(lines)))
expect("the doctor still finishes and summarises", "problem(s) found" in "\n".join(lines))

# Port advice must be copy-pasteable on the platform it runs on.
hint = agk.port_in_use_hint(8080)
expect("the port hint says what to run next",
       ("netstat -ano" if __import__("os").name == "nt" else "lsof") in hint, hint.splitlines()[0])
expect("the port hint does not suggest another server's port", "--port 8100" not in hint, hint.splitlines()[0])
expect("the port hint suggests a nearby spare", "--port 8081" in hint, hint.splitlines()[0])

# --help must show a worked example, not just a flag wall.
help_text = agk.build_parser().format_help()
expect("--help lists the common first runs", "--recursive --once" in help_text)
expect("--help documents the exit codes", "0 all passed" in help_text)
expect("--version is in the parser",
       agk.build_parser().parse_args(["--doctor"]).doctor is True)

print()
print("21. port fallback and the service launcher")
import socket  # noqa: E402 - local to this section on purpose

# A port that is genuinely held, for the length of this section only.
held = socket.socket()
held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
held.bind(("127.0.0.1", 0))
held.listen(5)
busy_port = held.getsockname()[1]


def _bind_and_listen(host, port):
    """Stand-in for build_server on a *free* port."""
    sock = socket.socket()
    sock.bind((host, port))
    sock.listen(5)
    return sock


def _refuse_busy(host, port):
    """Stand-in for a server that sees EADDRINUSE (Linux, or exclusive Windows)."""
    if port == busy_port:
        raise OSError(10048, "only one usage of each socket address")
    return _bind_and_listen(host, port)


expect("a real EADDRINUSE is recognised as a busy port",
       agk.is_addr_in_use(OSError(98, "Address already in use")))
expect("Windows reports this two different ways",
       agk.is_addr_in_use(OSError(10048, "only one usage")) and
       agk.is_addr_in_use(OSError(10013, "an attempt was made to access a socket")))
expect("a missing interface is not a busy port",
       not agk.is_addr_in_use(OSError(99, "Cannot assign requested address")))
expect("a port with a listener counts as busy", agk.port_is_busy("127.0.0.1", busy_port))
expect("next_free_port walks past the busy port",
       agk.next_free_port("127.0.0.1", busy_port) > busy_port)
expect("next_free_port never lands on a reserved port",
       agk.next_free_port("127.0.0.1", busy_port + 1, reserved=(busy_port + 1,)) >= busy_port + 2)

# Default behaviour must stay a hard failure: silently moving the dashboard is
# how a studio ends up posting verdicts to the wrong port.
try:
    agk.bind_with_fallback(_refuse_busy, "127.0.0.1", busy_port, auto=False)
    raised_without_auto = False
except OSError:
    raised_without_auto = True
expect("without --port-auto a busy port still fails honestly", raised_without_auto)

hops = []
moved_server, moved_port = agk.bind_with_fallback(
    _refuse_busy, "127.0.0.1", busy_port, auto=True,
    report=lambda old, new, exc: hops.append((old, new)))
expect("--port-auto moves to a free port", moved_port > busy_port, str(moved_port))
expect("it reports one hop, from the requested port",
       len(hops) == 1 and hops[0][0] == busy_port, str(hops))
moved_server.close()

# The important case on Windows: the OS *would* let this bind succeed (the port
# is listening, but SO_REUSEADDR permits a second bind). It must move anyway, or
# two servers end up sharing a port. The pre-check is what guarantees that.
windows_hops = []
precheck_server, precheck_port = agk.bind_with_fallback(
    _bind_and_listen, "127.0.0.1", busy_port, auto=True,
    report=lambda old, new, exc: windows_hops.append((old, new)))
expect("a busy port is detected before binding, not after",
       precheck_port > busy_port, str(precheck_port))
expect("the pre-check reports the same single hop", len(windows_hops) == 1, str(windows_hops))
precheck_server.close()
held.close()

# The launcher's probe must not be fooled by a listening socket (Windows
# SO_REUSEADDR would happily "bind" a port someone is already serving).
import tools.agk_serve as serve  # noqa: E402
squatter = socket.socket()
squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
squatter.bind(("127.0.0.1", 0))
squatter.listen(5)
taken = squatter.getsockname()[1]
expect("the launcher sees a listening port as busy",
       serve.port_is_free("127.0.0.1", taken) is False)
expect("the launcher sees a closed port as free",
       serve.port_is_free("127.0.0.1", taken + 1) is True)
squatter.close()

state_dir = DROP.parent / "scratch" / "serve_state"
state_dir.mkdir(parents=True, exist_ok=True)
serve.STATE_DIR = state_dir
serve.PORTS_FILE = state_dir / "ports.json"
serve.write_ports({"dashboard": {"url": "http://127.0.0.1:8123", "port": 8123, "pid": 1},
                   "workstation": {"url": "http://127.0.0.1:8124", "port": 8124, "pid": 2}})
records = serve.read_ports()
expect("the launcher records the ports it chose", records["dashboard"]["port"] == 8123)
expect("the gate command follows the recorded port, not the default",
       "8123/api/telemetry" in serve.gate_command(records), serve.gate_command(records))
expect("the workstation port is not in the telemetry URL",
       "8124" not in serve.gate_command(records))
expect("a missing record reads as empty", serve.read_ports().get("nope") is None)

# 9 Nov 2026: double-clicking agk-serve.bat stopped nothing, the folder stayed
# locked, and the only way to allow a folder was an environment variable.
serve.ALLOWED_FILE = state_dir / "allowed_folders.txt"
extra = DROP.parent / "scratch" / "My Game Project"
extra.mkdir(parents=True, exist_ok=True)
expect("allow adds a folder to this copy's list",
       serve.main(["allow", str(extra)]) == 0 and serve.read_allowed() == [str(extra.resolve())],
       str(serve.read_allowed()))
expect("adding it twice keeps one line",
       serve.main(["allow", str(extra)]) == 0 and len(serve.read_allowed()) == 1)
expect("a folder that does not exist is refused",
       serve.main(["allow", str(extra / "nope")]) == 1 and len(serve.read_allowed()) == 1)
_saved_env = os.environ.pop("AGK_PROJECT_ROOTS", None)
roots = [str(root) for root in serve.project_roots(serve.build_parser().parse_args(["up"]))]
expect("the picker gets the added folder and still this copy",
       str(extra.resolve()) in roots and str(serve.REPO_ROOT) in roots, str(roots))
if _saved_env is not None:
    os.environ["AGK_PROJECT_ROOTS"] = _saved_env
expect("allow --remove takes it off again",
       serve.main(["allow", "--remove", str(extra)]) == 0 and serve.read_allowed() == [])
_real_processes = serve._python_processes
serve._python_processes = lambda: [
    (11, str(serve.WORKSTATION_SCRIPT)), (12, "/elsewhere/server/art_workstation_server.py"),
    (13, ""), (os.getpid(), str(serve.HEALTH_SCRIPT))]
strays = [pid for pid, _ in serve.stray_servers()] if os.name != "nt" else [11]
expect("only servers started from this copy count as its strays", strays == [11], str(strays))
serve._python_processes = _real_processes
for name in ("Stop.bat", "Allow_Folder.bat", "Start AssetGatekeeper.bat", "tools/agk-serve.bat",
             "Install_Extras.bat", "tools/start_workstation.bat", "tools/start_asset_health.bat",
             "tools/rules_wizard.bat"):
    text = (ROOT_MODULE_DIR / name).read_bytes()
    expect(f"{name} has Windows line endings", b"\r\n" in text and b"\n" not in text.replace(b"\r\n", b""))
    if not name.startswith("tools/start_"):    # those run the server in their window
        expect(f"{name} leaves its folder before pausing, so it never locks it",
               b'cd /d "%USERPROFILE%"' in text)
expect("Stop.bat stops everything", b"agk_serve.py\" down" in (ROOT_MODULE_DIR / "Stop.bat").read_bytes())
expect("agk-serve.bat with no command waits instead of flashing its help",
       b'if "%~1"=="" goto :menu' in (ROOT_MODULE_DIR / "tools" / "agk-serve.bat").read_bytes())
# 10 Nov 2026, one front door: six .bat files sat at the top level. Now one
# start file, with Stop, Allow_Folder and Install_Extras beside it; the rest
# live in tools\ and find the install from there. Rules.bat stays as a shim.
_top_bats = sorted(path.name for path in ROOT_MODULE_DIR.glob("*.bat"))
expect("the top level holds the start file, its three helpers and the Rules.bat shim",
       _top_bats == ["Allow_Folder.bat", "Install_Extras.bat", "Rules.bat",
                     "Start AssetGatekeeper.bat", "Stop.bat"], str(_top_bats))
_shim = (ROOT_MODULE_DIR / "Rules.bat").read_bytes()
expect("Rules.bat only runs Start AssetGatekeeper.bat, passing its arguments and exit code",
       b'call "%~dp0Start AssetGatekeeper.bat" %*' in _shim and b"exit /b %ERRORLEVEL%" in _shim
       and b"\r\n" in _shim and len(_shim.splitlines()) <= 6, _shim[:200])
for _moved in ("agk-serve.bat", "start_workstation.bat", "start_asset_health.bat",
               "rules_wizard.bat"):
    _text = (ROOT_MODULE_DIR / "tools" / _moved).read_bytes()
    expect(f"tools\\{_moved} finds the install folder from tools\\",
           b'"%~dp0..\\.venv\\Scripts\\python.exe"' in _text
           and b'"%~dp0tools\\' not in _text and b'"%~dp0server\\' not in _text, _moved)
for _moved in ("start_workstation.bat", "start_asset_health.bat"):
    _text = (ROOT_MODULE_DIR / "tools" / _moved).read_bytes()
    expect(f"... and {_moved} runs the server from the install folder",
           b'cd /d "%~dp0.."' in _text and b'"%~dp0..\\server\\' in _text)
expect("a copy with only the new start file is still a checkout",
       serve.is_checkout.__doc__ and "Start AssetGatekeeper.bat" in serve.is_checkout.__doc__)
# 9 Nov 2026 audit: B2 (pip installs kept their databases in site-packages) and
# B6 (pip users were told to run a tools/ script they do not have).
expect("this repository is a downloaded copy", serve.is_checkout() is True)
expect("a pip install is not", serve.is_checkout(DROP.parent / "scratch") is False)
_saved_home = os.environ.get("AGK_HOME")
os.environ["AGK_HOME"] = str(DROP.parent / "scratch" / "agk-home")
expect("$AGK_HOME decides where the servers keep their data",
       serve.data_root() == DROP.parent / "scratch" / "agk-home")
if _saved_home is None:
    os.environ.pop("AGK_HOME")
else:
    os.environ["AGK_HOME"] = _saved_home
expect("a downloaded copy keeps its data beside itself", serve.data_root() == serve.REPO_ROOT
       or os.environ.get("AGK_HOME"))
_real_checkout = serve.is_checkout
serve.is_checkout = lambda root=serve.REPO_ROOT: False
_pip_home = serve.data_root()
expect("a pip install keeps its data in the user's own folder, not site-packages",
       serve.REPO_ROOT not in _pip_home.parents and _pip_home != serve.REPO_ROOT
       and _pip_home.name in ("AssetGatekeeper", "asset-gatekeeper"), str(_pip_home))
expect("pip users are told the command they have",
       serve.command_hint("down") == "agk-serve down", serve.command_hint("down"))
serve.is_checkout = _real_checkout
expect("a downloaded copy names its own launcher",
       serve.command_hint("down") in ("double-click Stop.bat", "python tools/agk_serve.py down"))

# B8: --jobs 0 on a many-core PC started a worker per core whatever the files.
_big = DROP.parent / "scratch" / "big.obj"
_big.write_bytes(b"#" * (8 * 1024 * 1024))
expect("big files lower the number of parallel checks to fit in memory",
       agk.memory_safe_jobs(16, [_big] * 16, free=1024 ** 3) == 2,
       str(agk.memory_safe_jobs(16, [_big] * 16, free=1024 ** 3)))
expect("small files keep every worker",
       agk.memory_safe_jobs(16, [ROOT_MODULE_DIR / "healer.py"] * 16, free=16 * 1024 ** 3) == 16)
expect("unknown free memory changes nothing",
       agk.memory_safe_jobs(4, [_big], free=0) == 4)
expect("this system reports its free memory", (agk.available_memory() or 0) > 0)
_big.unlink()
# B7: the scene loader trimesh no longer calls a compatibility wrapper.
expect("meshes load through trimesh.load_scene when trimesh has it",
       agk.load_trimesh_scene.__doc__ and hasattr(agk.trimesh, "load_scene"))

leftovers = [str(path.relative_to(ROOT_MODULE_DIR)) for path in ROOT_MODULE_DIR.rglob("*.bat")
             if b"chatbox-sandbox" in path.read_bytes()]
expect("no batch file points at a developer's own sandbox", not leftovers, str(leftovers))

print("22. platform presets and the TOML writer")
HARDWARE = {"n64", "ps1", "nds", "gamecube", "xbox", "ps2", "n3ds", "wii", "ps3", "pc"}
summary = {row["name"]: row for row in agk.preset_summary()}
expect("ten hardware targets ship", HARDWARE <= set(summary), str(sorted(summary)))
expect("legacy aliases are kept for old scripts",
       {"retro", "mobile"} <= set(summary))
expect("legacy targets are labelled as such",
       summary["retro"]["legacy"] is True and summary["n64"]["legacy"] is False)
expect("every target separates character and prop budgets",
       all(row["character_triangles"] > row["prop_triangles"] > 0
           for row in summary.values()),
       str({n: (r["character_triangles"], r["prop_triangles"]) for n, r in summary.items()}))
expect("every target states texture policy",
       all(isinstance(row["power_of_two"], bool) and isinstance(row["pbr"], bool)
           for row in summary.values()))
expect("handhelds want power-of-two textures, PC does not",
       summary["n64"]["power_of_two"] and not summary["pc"]["power_of_two"])
expect("only the modern target requires PBR",
       summary["pc"]["pbr"] and not summary["ps3"]["pbr"] and not summary["n64"]["pbr"])
expect("a character budget is not smaller than a prop budget on the same target",
       summary["n64"]["character_triangles"] >= summary["n64"]["prop_triangles"])

n64 = agk.preset_document("n64")
expect("a preset document carries mesh, texture and categories",
       {"mesh", "texture", "tags"} <= set(n64))
expect("its categories are character and prop",
       set(n64["tags"]) == {"character", "prop"}, str(sorted(n64["tags"])))
expect("a preset document is valid input to the gate",
       agk.Budget.from_mapping(n64).max_triangles == 1500 or
       agk.Budget.from_mapping(n64).max_triangles == n64["mesh"]["max_triangles"])
expect("preset_document returns a copy, not the catalogue itself",
       agk.preset_document("n64") is not agk.PLATFORM_PRESETS["n64"]["document"])
try:
    agk.preset_document("dreamcast")
    raised_missing = False
except agk.ConfigError as exc:
    raised_missing = "dreamcast" in str(exc)
expect("an unknown target is a clear error", raised_missing)
expect("--preset stays budget-only so it cannot switch a check off",
       "tags" not in agk.apply_preset({}, "n64"))

# The writer has to round-trip the annotated example, or the admin page would
# silently reformat somebody's file into something else.
example = Path(__file__).resolve().parent.parent / "asset_gatekeeper.example.toml"
try:                                     # 3.11+: the standard library
    import tomllib  # noqa: E402, PLC0415 - local to this section
except ModuleNotFoundError:              # 3.9/3.10: the same parser, vendored
    from agk_vendor import tomli as tomllib  # noqa: E402, PLC0415
original = tomllib.loads(example.read_text(encoding="utf-8"))
written = agk.to_toml(original)
expect("the example round-trips through to_toml", tomllib.loads(written) == original)
expect("the written file explains itself", written.startswith("# rules.toml"))
expect("tables are written in TOML syntax", "[mesh]" in written and "[texture]" in written)
expect("arrays stay inline", 'exclude = ["' in written or "exclude" not in written)
expect("booleans are lowercase TOML", " = true" in written or " = false" in written)
expect("parse_rules_text sees what to_toml wrote",
       agk.parse_rules_text(written)["mesh"] == original["mesh"])

preset_cli = agk.build_parser().parse_args(["--list-presets"])
expect("the CLI accepts --list-presets", preset_cli.list_presets is True)

expect("the example config ships with the checkout", example.is_file())

print("23. walking a folder that contains something unreadable")
import shutil as _shutil  # noqa: PLC0415 - local to this section

walk_root = DROP.parent / "scratch" / "walk"
_shutil.rmtree(walk_root, ignore_errors=True)
(walk_root / "sub").mkdir(parents=True)
(walk_root / "a.obj").write_text("v 0 0 0\n", encoding="utf-8")
(walk_root / "sub" / "b.obj").write_text("v 0 0 0\n", encoding="utf-8")
(walk_root / "notes.txt").write_text("x", encoding="utf-8")
expect("the walker finds files at every depth",
       len(agk.walk_files(walk_root)) == 3,
       str(sorted(p.name for p in agk.walk_files(walk_root))))
expect("a non-recursive walk stops at the top level",
       len(agk.walk_files(walk_root, recursive=False)) == 2)

# The failure from the field: one entry the OS refuses to traverse took the
# whole scan down. Simulate a scandir failure (WinError 1920) at the root.
skipped: list[str] = []
original_walk = agk.os.walk


def _broken_walk(top, **_kwargs):
    raise OSError(1920, "The file cannot be accessed by the system",
                  str(Path(top) / "MyGame-Final"))


agk.os.walk = _broken_walk          # same module object os.walk lives on
try:
    survived = agk.walk_files(walk_root, on_skip=skipped.append)
finally:
    agk.os.walk = original_walk
expect("an unreadable entry does not abort the walk", survived == [], str(survived))
expect("the skipped path is reported rather than swallowed",
       bool(skipped) and "MyGame-Final" in skipped[0], str(skipped))
expect("the walker works again once the entry is gone",
       len(agk.walk_files(walk_root)) == 3)

# A dangling symlink is not an asset and must not be handed to the analyzer.
try:
    dangling = walk_root / "gone"
    dangling.symlink_to(walk_root / "does-not-exist")
    try:
        names = {path.name for path in agk.walk_files(walk_root)}
        expect("a dangling symlink is not reported as a file", "gone" not in names,
               str(sorted(names)))
    finally:
        dangling.unlink()
except (OSError, NotImplementedError):
    pass                            # Windows without symlink privilege: skip

symlink_flag = agk.build_parser().parse_args(["--follow-symlinks"])
expect("symlinked folders are opt-in", symlink_flag.follow_symlinks is True)
expect("and off by default", agk.build_parser().parse_args([]).follow_symlinks is False)

print("an unreadable file is an ERROR, not an empty repository")
import contextlib  # noqa: E402 - only this group needs it
import io  # noqa: E402

locked = scratch / "locked"
locked.mkdir(exist_ok=True)
empty = scratch / "empty_folder"
empty.mkdir(exist_ok=True)

original_walk_files = agk.walk_files


def walk_with_a_skip(root, recursive=True, follow_symlinks=False, on_skip=None):
    """Pretend the OS refused one entry, which is what a locked file looks like."""
    if on_skip is not None:
        on_skip(str(locked / "big_mesh.obj"))
    return []


def run_json(target, allow_empty=False):
    buffer = io.StringIO()
    reporter = agk.Reporter(color=False, root=target, json_mode=True)
    with contextlib.redirect_stdout(buffer):
        code = agk.run_once(target, agk.Gatekeeper(agk.Budget()), reporter, False,
                            allow_empty=allow_empty)
    payloads = [json.loads(line) for line in buffer.getvalue().splitlines()
                if line.startswith("{") and '"file"' in line]
    return code, payloads


agk.walk_files = walk_with_a_skip
try:
    code, payloads = run_json(locked)
finally:
    agk.walk_files = original_walk_files
expect("an unreadable entry fails the run", code == 1, str(code))
expect("it is reported as ERROR, not FAILED",
       [item["status"] for item in payloads] == ["ERROR"], str(payloads))
expect("an ERROR is not a pass", payloads and payloads[0]["passed"] is False)
expect("the payload says the file could not be read",
       payloads and "cannot be read" in payloads[0]["problems"][0], str(payloads))
expect("the status word survives on the verdict itself",
       agk.Verdict(Path("x.obj"), ("cannot be read",), unreadable=True).status == "ERROR")
code, payloads = run_json(empty)
expect("an empty folder fails the run", code == 1 and payloads == [], str(code))
code, payloads = run_json(empty, allow_empty=True)
expect("--allow-empty lets an empty folder pass", code == 0, str(code))

print("every CLI flag survives a real run through main()")
# argparse accepting a flag proves nothing: --triangles 1000 used to reach
# Gatekeeper.__init__ as a list of names and die with ValueError, after argparse
# had already printed a budget banner. Each flag here is run the way a pipeline
# runs it, over a real folder.
cli_dir = scratch / "cli_e2e"
cli_dir.mkdir(exist_ok=True)
shutil.copy(DROP / "crate_1mtl.obj", cli_dir / "crate_1mtl.obj")
shutil.copy(DROP / "crate_1mtl.mtl", cli_dir / "crate_1mtl.mtl")
shutil.copy(DROP / "bark_Albedo.png", cli_dir / "bark_Albedo.png")
shutil.copy(DROP / "crate_4mtl.obj", cli_dir / "crate_4mtl.obj")
shutil.copy(DROP / "crate_4mtl.mtl", cli_dir / "crate_4mtl.mtl")
shutil.copy(DROP / "boss_statue.gltf", cli_dir / "boss_statue.gltf")

CLI_CASES = (
    ("--triangles", "1000"),
    ("--max-texture", "512"),
    ("--max-materials", "5"),
    ("--max-influences", "2"),
    ("--max-uvs", "8"),
    ("--allow-degenerate",),
    ("--allow-npot",),
    ("--no-naming",),
    ("--no-reference-check",),
    ("--no-pbr-check",),
    ("--audit-transforms",),
    ("--require-indexed",),
    ("--exclude", "*nomatch*"),
    ("--allow-empty",),
    ("--recursive",),
    ("--follow-symlinks",),
    ("--no-notify",),
    ("--preset", "retro"),
    ("--tag-mode", "strict"),
    ("--json",),
)

cli_failures = []
for case in CLI_CASES:
    try:
        # No output capture: the gate binds its log handler at import, so a
        # redirect hides it. What matters is that main() returns a verdict code
        # instead of raising, which the except below proves.
        with contextlib.redirect_stdout(io.StringIO()):
            code = agk.main([str(cli_dir), "--once", "--no-color", *case])
        if code not in (0, 1):
            cli_failures.append(f"{' '.join(case)} -> exit {code}")
    except BaseException as exc:              # noqa: BLE001 - that is the point
        cli_failures.append(f"{' '.join(case)} raised {type(exc).__name__}: {exc}")
expect("every flag runs to a verdict, not a traceback", not cli_failures,
       "; ".join(cli_failures[:3]))


# The override must actually bite, not merely parse: a 4-slot mesh passes a
# 5-slot budget and fails a 1-slot one. Run it as a subprocess, which is how a
# pipeline invokes the gate.
def gate_output(*extra):
    completed = subprocess.run([sys.executable, "asset_gatekeeper.py", str(cli_dir),
                                "--once", "--no-color", *extra],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
    return completed.returncode, completed.stdout + completed.stderr


_, relaxed = gate_output("--max-materials", "5")
_, strict = gate_output("--max-materials", "1")
expect("--max-materials 5 lets the 4-slot mesh through",
       any("PASSED" in line and "crate_4mtl.obj" in line
           for line in relaxed.splitlines()), relaxed[-300:])
expect("--max-materials 1 fails it",
       any("FAILED" in line and "crate_4mtl.obj" in line
           for line in strict.splitlines()), strict[-300:])

print("the certification runner")
_cert_spec = importlib.util.spec_from_file_location("agk_certify", ROOT_MODULE_DIR / "tools" / "certify.py")
certify = importlib.util.module_from_spec(_cert_spec)
_cert_spec.loader.exec_module(certify)
_cert_dir = scratch / "certify"
_cert_dir.mkdir(exist_ok=True)
(_cert_dir / "good.py").write_text("print('  ok   one')\nprint('FAILURES: none')\n", encoding="utf-8")
(_cert_dir / "crash.py").write_text("print('  ok   one')\nraise SystemExit(0)\n", encoding="utf-8")
(_cert_dir / "bad.py").write_text("print('  FAIL two')\nprint(\"FAILURES: ['two']\")\nraise SystemExit(1)\n",
                                  encoding="utf-8")
(_cert_dir / "integration.py").write_text(
    "print('12:00 2 asset(s): 0 passed, 1 failed')\nprint('3 passed, 0 failed, 1 skipped')\n",
    encoding="utf-8")
_integration = certify.run_suite("headless_integration", str(_cert_dir / "integration.py"), (), 60)
expect("certify reads the integration suite's own summary, not a sweep log before it",
       _integration["ok"] and _integration["passed"] == 3 and _integration["skipped"] == 1,
       str(_integration))
_good = certify.run_suite("good", str(_cert_dir / "good.py"), (), 60)
_crash = certify.run_suite("crash", str(_cert_dir / "crash.py"), (), 60)
_bad = certify.run_suite("bad", str(_cert_dir / "bad.py"), (), 60)
expect("certify counts a clean suite as a pass", _good["ok"] and _good["passed"] == 1, str(_good))
expect("... a suite that stops before its summary as a failure, even with exit 0",
       not _crash["ok"], str(_crash))
expect("... and a failing check as a failure, with its name", not _bad["ok"]
       and _bad["failures"] == ["two"], str(_bad["failures"]))
_report = certify.report([_good, _bad], _cert_dir, None)
expect("the report says FAILED and lists the failure",
       "**Result: FAILED**" in _report.read_text(encoding="utf-8")
       and "* two" in _report.read_text(encoding="utf-8"))
expect("no control characters in the README (a stray \\a once hid in it)",
       not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]",
                     (ROOT_MODULE_DIR / "README.md").read_text(encoding="utf-8")
                     + (ROOT_MODULE_DIR / "docs" / "REFERENCE.md").read_text(encoding="utf-8")))


print("Install_Extras.bat: one double-click for the libraries (10 Nov 2026)")
extras = load_module("agk_install_extras", "tools/install_extras.py")
_pyproject = (ROOT_MODULE_DIR / "pyproject.toml").read_text(encoding="utf-8")
_toml = agk.tomllib.loads(_pyproject) if getattr(agk, "tomllib", None) else None
if _toml is None:                      # pragma: no cover - every supported Python has one
    import tomllib as _tl
    _toml = _tl.loads(_pyproject)


def _bare(requirement):
    for mark in "<>=!~;[ ":
        requirement = requirement.split(mark, 1)[0]
    return requirement.lower()


_declared = {_bare(r) for r in _toml["project"]["dependencies"]}
_declared |= {_bare(r) for r in _toml["project"]["optional-dependencies"]["full"]}
_listed = {p.name.lower() for p in extras.PACKAGES}
expect("the installer lists exactly the packages pyproject.toml declares (gate + [full])",
       _declared == _listed, f"pyproject only: {sorted(_declared - _listed)}, "
       f"installer only: {sorted(_listed - _declared)}")
_needed = {_bare(r) for r in _toml["project"]["dependencies"]}
expect("what the gate needs is marked needed, extras are not",
       {p.name.lower() for p in extras.PACKAGES if p.needed} == _needed)
_ranges = {_bare(r): r.split(";")[0].strip() for r in
           _toml["project"]["dependencies"] + _toml["project"]["optional-dependencies"]["full"]}
expect("... with the same version ranges", all(_ranges[p.name.lower()].replace(" ", "")
       == p.pip_name.replace(" ", "") for p in extras.PACKAGES),
       str([(p.pip_name, _ranges.get(p.name.lower())) for p in extras.PACKAGES]))
_bake = {p.name for p in extras.PACKAGES if p.min_python == (3, 10)}
expect("the bake libraries are skipped on Python 3.9 and installed on 3.10+",
       _bake == {"fast-simplification", "xatlas", "scipy"}
       and not any(p.applies((3, 9)) for p in extras.PACKAGES if p.name in _bake)
       and all(p.applies((3, 12)) for p in extras.PACKAGES if p.name in _bake))
_tomli = next(p for p in extras.PACKAGES if p.name == "tomli")
expect("tomli is only for Pythons without tomllib", _tomli.applies((3, 10))
       and not _tomli.applies((3, 11)))
_cmd = extras.pip_command([p for p in extras.PACKAGES if p.name in _bake], "py.exe")
expect("the pip command uses the chosen Python and pip's own syntax",
       _cmd[:4] == ["py.exe", "-m", "pip", "install"] and "scipy>=1.7" in _cmd, str(_cmd))
expect("ranges are quoted so cmd does not treat < and > as redirection",
       '"trimesh>=4.0,<6"' in extras.quoted(["x", "-m", "pip", "install", "trimesh>=4.0,<6"]))
expect("a module that imports counts as working", extras.loads("json")[0] is True)
_ok, _why = extras.loads("agk_no_such_module_here")
expect("a missing one does not, with Python's own reason", not _ok and "agk_no_such" in _why, _why)
_real_loads = extras.loads
extras.loads = lambda module, python=None: (module not in ("xatlas", "trimesh"), "")
_w, _m, _s = extras.survey((3, 12))
expect("survey sorts working from missing", {p.name for p in _m} == {"xatlas", "trimesh"}
       and "tomli" not in {p.name for p in _w + _m + _s}, str([p.name for p in _m]))
_w, _m, _s = extras.survey((3, 9))
expect("on 3.9 the bake is reported as skipped, not missing",
       {p.name for p in _s} == _bake and "xatlas" not in {p.name for p in _m})
import contextlib as _ctx
import io as _io
_out = _io.StringIO()
with _ctx.redirect_stdout(_out):
    _code = extras.main(["--check"])
expect("--check with a needed package missing exits 1 and prints the command",
       _code == 1 and "pip install" in _out.getvalue() and "MISSING" in _out.getvalue(),
       _out.getvalue()[-300:])
extras.loads = lambda module, python=None: (module != "watchdog.observers", "")
_out = _io.StringIO()
with _ctx.redirect_stdout(_out):
    _code = extras.main(["--check"])
expect("--check with only an extra missing exits 0 (the gate still runs)", _code == 0,
       _out.getvalue()[-300:])
_calls = []
_real_call = extras.subprocess.call
extras.subprocess.call = lambda command: _calls.append(command) or 0
_out = _io.StringIO()
with _ctx.redirect_stdout(_out):
    _code = extras.main(["--dry-run"])
expect("--dry-run runs nothing", _code == 0 and not _calls and "watchdog" in _out.getvalue())
extras.loads = lambda module, python=None: (True, "")
with _ctx.redirect_stdout(_io.StringIO()):
    _code = extras.main([])
expect("with everything installed it installs nothing", _code == 0 and not _calls)
_state = {"installed": False}
extras.loads = lambda module, python=None: (module != "watchdog.observers" or _state["installed"], "")
extras.pip_available = lambda python=None: True
extras.subprocess.call = lambda command: (_calls.append(command), _state.update(installed=True))[0] and 0
with _ctx.redirect_stdout(_io.StringIO()):
    _code = extras.main([])
expect("it installs only what is missing, then checks it loads",
       _code == 0 and len(_calls) == 1 and _calls[0][-1] == "watchdog>=3.0", str(_calls))
_state["installed"] = False
_calls.clear()
extras.subprocess.call = lambda command: _calls.append(command) or 1
_out = _io.StringIO()
with _ctx.redirect_stdout(_out):
    _code = extras.main([])
expect("a failed install exits 1 and says what still does not load",
       _code == 1 and "still do not load" in _out.getvalue() and "watchdog" in _out.getvalue())
extras.subprocess.call = _real_call
extras.loads = _real_loads
_bat = (ROOT_MODULE_DIR / "Install_Extras.bat").read_bytes()
expect("Install_Extras.bat finds Python the way the start file does",
       all(line in (ROOT_MODULE_DIR / "Start AssetGatekeeper.bat").read_bytes()
           for line in _bat.split(b"\r\n")
           if b"PYEXE=" in line and b"%%" in line))
expect("Install_Extras.bat runs the installer with its arguments",
       b'tools\\install_extras.py" %*' in _bat)
expect("the start file points a user with missing libraries at it",
       b"Install_Extras.bat" in (ROOT_MODULE_DIR / "Start AssetGatekeeper.bat").read_bytes())
_build = load_module("agk_build_release", "tools/build_release.py")
expect("the release ZIP ships it", "Install_Extras.bat" in _build.DROP_IN_FILES)
expect("the release is named by the version string alone, not the comment after it",
       _build.read_version() == agk.__version__, _build.read_version())

print("Release workflow: the tag, the source and the notes agree (10 Nov 2026)")
# .github/workflows/release.yml runs build_release.py --check-tag on the pushed
# tag before it builds or publishes anything.
for _tag, _norm in (("v0.1.0-alpha.1", "0.1.0a1"), ("0.1.0a1", "0.1.0a1"), ("v1.2.0-beta.3", "1.2.0b3"),
                    ("v1.0.0rc1", "1.0.0rc1"), ("v1.0.0", "1.0.0"), ("v2.0.post1", "2.0.post1")):
    expect(f"tag {_tag} reads as version {_norm}", _build.normalise_version(_tag) == _norm,
           _build.normalise_version(_tag))
_notes_text = ("# v0.2.0 (unreleased)\n\n* new\n\n# v0.1.0-alpha.1 (12 Nov 2026)\n\n* first\n\n"
               "# v1.1.1 (internal)\n\n* old\n")
_heading, _body = _build.release_notes("0.1.0a1", _notes_text)
expect("the notes for a version are its own section only",
       _heading == "v0.1.0-alpha.1 (12 Nov 2026)" and _body == "* first\n", repr((_heading, _body)))
expect("a tag that is not the source's version is refused",
       any("is not the version" in p for p in _build.check_tag("v9.9.9", "0.1.0a1")))
expect("a release whose notes still say 'unreleased' is refused",
       any("unreleased" in p for p in _build.check_tag("v0.2.0", "0.2.0", _notes_text)))
expect("... and a dated one with a matching tag goes ahead",
       _build.check_tag("v0.1.0-alpha.1", "0.1.0a1", _notes_text) == [])
_wf = (ROOT_MODULE_DIR / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
expect("the release workflow checks the tag before it builds",
       _wf.index("--check-tag") < _wf.index("python tools/build_release.py\n"))
expect("the release runner installs what build_release.py builds with (setuptools 77+)",
       'pip install --upgrade pip build "setuptools>=77"' in _wf)
expect("a tag pushed again refreshes its release instead of failing",
       "gh release view" in _wf and "--clobber" in _wf)
expect("... and writes nothing beside the code before the build (it refuses a dirty tree)",
       "--notes notes.md" not in _wf and "--notes dist/notes.md" in _wf
       and _wf.index("python tools/build_release.py\n") < _wf.index("--notes dist/notes.md"))
expect("PyPI publishing is opt-in and uses trusted publishing (no stored token)",
       "vars.PUBLISH_TO_PYPI == 'true'" in _wf and "id-token: write" in _wf
       and "password" not in _wf.replace("no password", ""))
_pp = load_module("agk_polypaint_hint", "engine/polypaint.py")
expect("the bake's install hint names the exact pip command",
       "-m pip install fast-simplification xatlas scipy" in _pp.install_hint())
if os.name == "nt":
    expect("... and on Windows, the double-click file", "Install_Extras.bat" in _pp.install_hint())


print("Say what was skipped (10 Nov 2026)")
# The developer's test folder held six .jfif reference images; the gate ignored them
# without a word, so a folder looked checked when half of it was not.
import tempfile as _tempfile
_skip_dir = Path(_tempfile.mkdtemp(prefix="agk-skip-"))
make_png(_skip_dir / "T_Crate_BaseColor.png", (64, 64))
for _n in range(3):
    (_skip_dir / f"ref{_n}.jfif").write_bytes(b"\xff\xd8\xff\xe0")
(_skip_dir / "scene.blend").write_bytes(b"BLENDER")
(_skip_dir / "rules.toml").write_text("", encoding="utf-8")
(_skip_dir / ".hidden.png").write_bytes(b"")
(_skip_dir / "T_Draft_BaseColor.png.tmp").write_bytes(b"")
make_png(_skip_dir / "T_Wip_BaseColor.png", (64, 64))
_info = agk.skipped_files(sorted(_skip_dir.iterdir()), ("*Wip*",))
expect("unsupported types are counted by extension, most first",
       _info["unsupported"] == {".jfif": 3, ".blend": 1}, str(_info))
expect("rules.toml, hidden and scratch files are not mentioned; --exclude is counted",
       _info["excluded"] == 1 and _info["total"] == 5, str(_info))
_text = agk.describe_skipped(_info)
expect("the line says what to do with the common ones",
       _text.startswith("5 file(s) skipped: unsupported type .jfif x3 (a JPEG: rename it to .jpg")
       and "a Blender scene: export FBX" in _text and "1 by [filters] exclude" in _text, _text)
_code, _out = gate_run(str(_skip_dir), "--once", "--exclude", "*Wip*")
expect("each run ends with the skipped line", "file(s) skipped: unsupported type .jfif x3"
       in _out, _out[-400:])
_code, _out = gate_run(str(_skip_dir), "--once", "--json", "--exclude", "*Wip*")
_summary = json.loads([line for line in _out.splitlines() if line.startswith('{"summary"')][-1])
expect("--json puts it in the summary", _summary["summary"]["skipped"]["unsupported"]
       == {".jfif": 3, ".blend": 1}, _out[-300:])
_refs = _skip_dir / "refs"
_refs.mkdir()
for _n in range(2):
    (_refs / f"ref{_n}.jfif").write_bytes(b"\xff\xd8\xff\xe0")
_code, _out = gate_run(str(_refs), "--once")
expect("'no tracked assets' says what was there instead",
       _code == 1 and "no tracked assets found" in _out and ".jfif x2" in _out, _out[-300:])
_many = agk.describe_skipped({"total": 7, "unsupported": {f".x{i}": 1 for i in range(7)},
                              "excluded": 0}, limit=5)
expect("a long list is cut short", "2 more type(s)" in _many, _many)
shutil.rmtree(_skip_dir, ignore_errors=True)

print("Ready for PyPI (10 Nov 2026)")
# PyPI shows README.md as the project page, where a relative link like
# docs/README.md leads nowhere; and the licence as a TOML table stops building
# with setuptools after Feb 2027.
_front = (ROOT_MODULE_DIR / "README.md").read_text(encoding="utf-8")
import re as _re
_relative = [link for link in _re.findall(r"\]\(([^)\s]+)\)", _front)
             if not link.startswith(("http://", "https://", "#", "mailto:"))]
expect("every README link is absolute, so it works on PyPI too", not _relative, str(_relative))
_project = agk.tomllib.loads((ROOT_MODULE_DIR / "pyproject.toml").read_text(encoding="utf-8"))
expect("the licence is an SPDX string, with no licence classifier",
       _project["project"].get("license") == "MIT"
       and not any(c.startswith("License ::") for c in _project["project"]["classifiers"]))
expect("... and the build needs a setuptools that understands it",
       any(r.replace(" ", "").startswith("setuptools>=77") for r in _project["build-system"]["requires"]))

print("The reference and the CLI agree")
# The long manual moved from README.md to docs/REFERENCE.md for the alpha (the
# front page is now short); the flag table lives there.
readme = Path(__file__).resolve().parent.parent / "docs" / "REFERENCE.md"
if readme.is_file():
    documentation = readme.read_text(encoding="utf-8")
    # The flag table drifted from the parser once already: --preset, --tag-mode,
    # --exclude and the whole --init/--doctor group were documented nowhere.
    undocumented = sorted(option for action in agk.build_parser()._actions
                          for option in action.option_strings
                          if option.startswith("--") and option != "--help"
                          and f"`{option}" not in documentation)
    expect("every CLI flag appears in docs/REFERENCE.md", not undocumented, str(undocumented))
else:                                    # pragma: no cover - shipped without docs
    print("  skip  README.md is not next to the tests")

print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
