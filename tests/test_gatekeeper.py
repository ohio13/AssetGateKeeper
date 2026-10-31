"""Sanity checks for asset_gatekeeper: new rules, fallbacks, edge cases."""
import hashlib
import importlib.util
import json
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
expect("the note records the padding",
       any("1000x1000 -> 1024x1024" in note for note in npot_verdict.heal_notes),
       str(npot_verdict.heal_notes))
expect("padding replicates edge pixels instead of black",
       Image.open(npot_png).getpixel((1023, 5)) == (120, 90, 70),
       str(Image.open(npot_png).getpixel((1023, 5))))
expect("the healed file re-inspects as passing",
       agk.Gatekeeper(heal_budget).check(npot_png).passed)
expect("staging folders are cleaned up",
       not any(p.name.startswith(STAGING) for p in npot_dir.iterdir()))

resize_dir = heal_root / "resize"
resize_dir.mkdir()
resize_png = make_png(resize_dir / "stone_Albedo.png", (1000, 1000))
resize_healer = agk.load_healer(heal_budget, agk.HealSettings(enabled=True, texture_mode="resize"))
resize_verdict = agk.Gatekeeper(heal_budget, healer=resize_healer).check(resize_png)
expect("resize mode is honoured",
       resize_verdict.passed and "resized" in resize_verdict.heal_notes[0],
       str(resize_verdict.heal_notes))
expect("resize mode lands on the same size", Image.open(resize_png).size == (1024, 1024))

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

suffixes = heal_budget.texture_suffixes
expect("the suffix is inferred from 'nrm'",
       agk_healer.choose_suffix("wall_nrm", suffixes) == "_Normal")
expect("a stem that already ends with a configured suffix is left alone",
       agk_healer.choose_suffix("wall_normal", suffixes) is None,
       "_normal already IS the _Normal suffix, case-insensitively")
expect("the suffix is inferred from 'rough'",
       agk_healer.choose_suffix("metal_rough", suffixes) == "_Roughness")
expect("the suffix is inferred from 'basecolor'",
       agk_healer.choose_suffix("floor_basecolor", suffixes) == "_Albedo")
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

# -- p4: gatekeeper resolution -----------------------------------------------
os.environ["AGK_GATEKEEPER"] = str(ROOT_MODULE_DIR / "asset_gatekeeper.py")
expect("AGK_GATEKEEPER overrides the script path",
       p4_hook.gatekeeper_command()[-1].endswith("asset_gatekeeper.py"))
del os.environ["AGK_GATEKEEPER"]

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
    run(["git", "add", "crate_ok.obj"], git_dir)
    allowed = run([sys.executable, str(ROOT_MODULE_DIR / "vcs_hooks" / "git_pre_commit.py")],
                  git_dir, {"AGK_GATEKEEPER": str(ROOT_MODULE_DIR / "asset_gatekeeper.py")})
    expect("a passing asset is allowed through", allowed.returncode == 0,
           f"exit {allowed.returncode} {allowed.stderr[:120]}")

    (git_dir / "notes.txt").write_text("not an asset", encoding="utf-8")
    run(["git", "add", "notes.txt"], git_dir)
    only_text = run([sys.executable, str(ROOT_MODULE_DIR / "vcs_hooks" / "git_pre_commit.py")],
                    git_dir, {"AGK_GATEKEEPER": str(ROOT_MODULE_DIR / "asset_gatekeeper.py")})
    expect("a commit with no assets is not gated", only_text.returncode == 0)

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

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
