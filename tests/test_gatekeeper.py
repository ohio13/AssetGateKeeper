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
    shutil.copy(DROP / "crate_1mtl.mtl", git_dir / "crate_1mtl.mtl")   # the OBJ references it
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
       'dependencies = ["trimesh>=4.0", "pillow>=9.0"]' in pyproject_text)
expect("watchdog is only an extra",
       'full = ["watchdog>=3.0"]' in pyproject_text
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
# platform, auto-heal — in that order.
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

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
