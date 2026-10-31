"""Sanity checks for asset_gatekeeper: new rules, fallbacks, edge cases."""
import json
import logging
import os
import shutil
import socket
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
    ("bark_diffuse", False),
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
    "name does not end with a standard suffix (_Albedo, _Normal, _Roughness)",))
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
uni = scratch / "导出_assets"
uni.mkdir(exist_ok=True)
shutil.copy(DROP / "crate_4mtl.obj", uni / "墙体.obj")
shutil.copy(DROP / "bark_Albedo.png", uni / "树皮_Albedo.png")
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
expect("no CLI overrides reported", overrides == [])

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

pot_cfg = scratch / "rules_pot"
pot_cfg.mkdir(exist_ok=True)
(pot_cfg / "rules.toml").write_text("[texture]\npower_of_two = true\nnaming = true\n", encoding="utf-8")
from_file, _, _ = agk.resolve_budget(pot_cfg, agk.build_parser().parse_args([str(pot_cfg)]))
expect("file can turn power_of_two back on", from_file.require_power_of_two is True)
forced_off, _, off_overrides = agk.resolve_budget(
    pot_cfg, agk.build_parser().parse_args([str(pot_cfg), "--allow-npot"]))
expect("--allow-npot beats the file",
       forced_off.require_power_of_two is False and off_overrides == ["require_power_of_two"],
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
expect("payload path is relative to the watch folder",
       payload["path"] == "boss_statue.gltf" and ":" not in payload["path"])
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
    cold = agk.fetch_remote_rules("https://studio.example/rules.json",
                                  empty_cfg / agk.RULES_CACHE_FILENAME)
expect("offline with no cache degrades to local settings", cold is None)

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
       over_remote.max_triangles == 99 and over_remote_names == ["max_triangles"],
       str(over_remote_names))
expect("remote rules are validated like any other budget",
       not _accepts_unknown_remote(agk, {"meshes": {"max_triangles": 1}}, local_cfg, cli))

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

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
