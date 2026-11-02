"""Sanity checks for the Art Workstation: storage, LODs, collision, notes, auth.

Everything runs in-process: the real ThreadingTCPServer is started on an ephemeral
port inside this script and shut down before it exits, so the suite can never
leave a process behind or hang a terminal.
"""
import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(ROOT / "server"), str(ROOT / "engine")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import art_workstation_server as aws  # noqa: E402
import lod_manager  # noqa: E402

WORK = Path("scratch") / "workstation_tests"
if WORK.exists():
    shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)

failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(label)


def fresh(name):
    directory = WORK / name
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    db = directory / "ws.db"
    aws.init_db(db)
    return db, directory / "data"


OBJ = """# a unit cube, 12 triangles
v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
v 0 0 1
v 1 0 1
v 1 1 1
v 0 1 1
f 1 2 3
f 1 3 4
f 5 8 7
f 5 7 6
f 1 5 6
f 1 6 2
f 2 6 7
f 2 7 3
f 3 7 8
f 3 8 4
f 4 8 5
f 4 5 1
"""


class Client:
    """Tiny urllib wrapper so the assertions read like the API."""

    def __init__(self, port, token=""):
        self.port = port
        self.token = token

    def call(self, path, method="GET", body=None, headers=None):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                         data=data, method=method)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def json(self, path, method="GET", body=None):
        status, raw, _ = self.call(path, method=method, body=body)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {"raw": raw[:200].decode("utf-8", "replace")}


print("1. schema and storage")
db, data_dir = fresh("storage")
with aws.open_db(db) as connection:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
expect("assets table", "assets" in tables, str(sorted(tables)))
expect("lod_versions table", "lod_versions" in tables)
expect("reviews_and_notes table", "reviews_and_notes" in tables)
expect("collisions table", "collisions" in tables)

asset = aws.create_asset(db, data_dir, "cube.obj", OBJ.encode(), uploader="alice")
expect("upload stores a row", asset["id"] == 1 and asset["filename"] == "cube.obj")
expect("triangle count is measured", asset["triangle_count"] == 12, str(asset["triangle_count"]))
expect("vertex count is measured", asset["vertex_count"] == 8, str(asset["vertex_count"]))
expect("material slots default to 1", asset["material_slots"] == 1)
expect("the file lands in its own folder", (data_dir / "assets" / "1" / "cube.obj").is_file())
expect("an AABB collision is derived on upload",
       aws.get_collision(db, 1)["kind"] == "box", str(aws.get_collision(db, 1)))
with aws.open_db(db) as connection:
    box = connection.execute("SELECT data_json FROM collisions WHERE asset_id = 1").fetchone()
payload = json.loads(box["data_json"])
expect("the derived box matches the mesh",
       payload["min"] == [0.0, 0.0, 0.0] and payload["max"] == [1.0, 1.0, 1.0], str(payload))

# A .txt is not a game export: the suffix is unknown, so it is stored as
# "unverified" rather than refused - reviewers sometimes attach sidecar notes.
# Executables are the only thing the policy refuses outright.
try:
    aws.create_asset(db, data_dir, "payload.exe", b"MZ\x90\x00")
    expect("executables are refused with 415", False, "no error raised")
except aws.WorkstationError as exc:
    expect("executables are refused with 415", exc.status == 415, str(exc))

fbx = aws.create_asset(db, data_dir, "character.fbx", b"Kaydara FBX Binary  \x00")
# FBX is measured now, so a truncated one is *broken* (a problem to fix), not
# "unverified" (a format nobody here can open). It is still stored, not a 422.
expect("an unreadable game export is accepted, not 422", fbx["kind"] == "broken", str(fbx)[:120])
expect("and says why", "cannot be read" in (fbx["analyse_note"] or ""), fbx["analyse_note"][:90])

from PIL import Image as _PILImage  # noqa: PLC0415 - local to this section
import io as _io
buffer = _io.BytesIO()
_PILImage.new("RGBA", (32, 16), (255, 0, 0, 255)).save(buffer, format="PNG")
skin = aws.create_asset(db, data_dir, "hero_skin.png", buffer.getvalue())
expect("a texture is accepted as an asset", skin["kind"] == "texture", str(skin)[:120])
expect("its size is measured", "32x16" in (skin["analyse_note"] or ""), skin["analyse_note"])
try:
    aws.create_asset(db, data_dir, "../../evil.obj", OBJ.encode())
    stored = [p.name for p in (data_dir / "assets").iterdir()]
    expect("path traversal is neutralised",
           all(".." not in name for name in stored), str(stored))
except aws.WorkstationError:
    expect("path traversal is neutralised", True)

# The traversal probe may legitimately have created an asset; drop it so the rest
# of the suite starts from a known state (one asset).
for extra_asset in [row for row in aws.list_assets(db) if row["id"] != 1]:
    aws.delete_asset(db, data_dir, extra_asset["id"])

print("2. LOD generation")
lods = aws.generate_lods(db, data_dir, 1, ratios=[0.5, 0.25])
expect("two LOD levels are produced", [lod["level"] for lod in lods] == ["LOD1", "LOD2"],
       str([lod["level"] for lod in lods]))
expect("LODs never exceed the source",
       all(lod["triangle_count"] <= 12 for lod in lods), str([lod["triangle_count"] for lod in lods]))
expect("LOD files exist",
       all((data_dir / lod["file_path"]).is_file() for lod in lods),
       str([lod["file_path"] for lod in lods]))
expect("regenerating replaces rather than duplicates",
       len(aws.generate_lods(db, data_dir, 1, ratios=[0.5])) == 1)

plan = lod_manager.plan_targets(100_000, [0.5, 0.25, 0.125], [20_000, 10_000])
expect("ratios set the targets", [p[1] for p in plan] == [20_000, 10_000, 12_500],
       str([p[1] for p in plan]))
expect("a hard cap beats a looser ratio",
       lod_manager.plan_targets(100_000, [0.9], [5_000])[0][1] == 5_000)
try:
    lod_manager.plan_targets(1000, [1.5])
    expect("invalid ratios are rejected", False, "no error")
except lod_manager.LodError:
    expect("invalid ratios are rejected", True)

print("3. collision proxies")
collision = aws.set_collision(db, 1, "sphere", {"center": [0.5, 0.5, 0.5], "radius": 0.9})
expect("a sphere proxy is stored", collision["kind"] == "sphere")
expect("it round-trips", aws.get_collision(db, 1)["data"]["radius"] == 0.9)
aws.set_collision(db, 1, "hull", {"points": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]})
expect("a hull proxy is stored", aws.get_collision(db, 1)["kind"] == "hull")
for kind, payload, label in (
        ("box", {"min": [0, 0], "max": [1, 1, 1]}, "a malformed box is rejected"),
        ("sphere", {"center": [0, 0, 0]}, "a sphere without a radius is rejected"),
        ("hull", {"points": [[0, 0, 0]]}, "a hull needs at least four points"),
        ("nonsense", {}, "an unknown proxy type is rejected")):
    try:
        aws.set_collision(db, 1, kind, payload)
        expect(label, False, "no error raised")
    except aws.WorkstationError:
        expect(label, True)

print("4. notes")
note = aws.add_note(db, 1, "Geometry", "**silhouette** looks wrong\n- check the back", "bob")
expect("a note is stored", note["id"] == 1 and note["category"] == "Geometry")
expect("notes come back newest first",
       [n["id"] for n in aws.list_notes(db, 1)] == [1])
expect("markdown is stored verbatim, not pre-rendered",
       "**silhouette**" in aws.list_notes(db, 1)[0]["note_text"])
for category, text, label in (
        ("Nonsense", "x", "an unknown category is rejected"),
        ("Geometry", "   ", "an empty note is rejected"),
        ("Geometry", "x" * 20_001, "an oversized note is rejected")):
    try:
        aws.add_note(db, 1, category, text, "bob")
        expect(label, False, "no error raised")
    except aws.WorkstationError as exc:
        expect(label, exc.status in (400, 413), f"{exc.status} {exc}")

print("5. HTTP: solo mode")
server = aws.build_server("127.0.0.1", 0, db, data_dir, quiet=True)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.15)
client = Client(port)

status, body = client.json("/api/health")
expect("health responds", status == 200 and body["ok"] is True)
expect("solo mode reports no auth", body["auth"] is False)
status, raw, headers = client.call("/viewer")
expect("the viewer page is served", status == 200 and b"Art Workstation" in raw, str(status))
expect("with the right content type", "text/html" in headers.get("Content-Type", ""))
expect("the viewer cannot be framed", headers.get("X-Frame-Options") == "DENY",
       str(headers.get("X-Frame-Options")))
expect("the viewer sends a CSP",
       "default-src 'none'" in (headers.get("Content-Security-Policy") or ""),
       str(headers.get("Content-Security-Policy")))
expect("the viewer is not sniffed", headers.get("X-Content-Type-Options") == "nosniff")
status, raw, _ = client.call("/static/app.css")
expect("css is served", status == 200 and b"--accent" in raw)
status, _, _ = client.call("/static/../art_workstation_server.py")
expect("static traversal is refused", status == 404, str(status))
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib follows redirects, which would hide the 302 we want to assert."""

    def redirect_request(self, *_args, **_kwargs):
        return None


opener = urllib.request.build_opener(_NoRedirect)
try:
    with opener.open(f"http://127.0.0.1:{port}/", timeout=10) as response:
        status, location = response.status, response.headers.get("Location")
except urllib.error.HTTPError as exc:
    status, location = exc.code, exc.headers.get("Location")
expect("/ redirects to the viewer", status == 302 and location == "/viewer", f"{status} {location}")

status, listing = client.json("/api/assets")
expect("the asset is listed", status == 200 and len(listing["assets"]) == 1)
expect("the listing carries counts",
       listing["assets"][0]["triangle_count"] == 12 and listing["assets"][0]["note_count"] == 1)

status, raw, headers = client.call("/api/assets/1/file")
expect("the raw file is served", status == 200 and raw.startswith(b"# a unit cube"))
expect("obj is served as text", "text/plain" in headers.get("Content-Type", ""))
status, _, _ = client.call("/api/assets/1/lods/LOD1/file")
expect("a LOD file is served", status == 200, str(status))
status, _, _ = client.call("/api/assets/1/lods/LOD9/file")
expect("an unknown LOD is a 404", status == 404, str(status))

status, created = client.json(
    "/api/assets?filename=second.obj&uploader=carol", method="POST", body=OBJ.encode())
expect("upload over HTTP works", status == 201 and created["uploader"] == "carol", str(status))
new_id = created["id"]
status, doc = client.json(f"/api/assets/{new_id}")
expect("the new asset is addressable", status == 200 and doc["filename"] == "second.obj")
status, created = client.json(f"/api/assets/{new_id}/lods", method="POST",
                              body={"ratios": [0.5], "max_tris": [4]})
expect("LOD generation over HTTP works", status == 201 and created["lods"][0]["level"] == "LOD1",
       str(status))
expect("a cap the mesh cannot reach is kept but flagged, never silently exceeded",
       created["lods"][0]["triangle_count"] <= 4 or created["lods"][0]["warning"],
       str(created["lods"][0]))
status, _ = client.json(f"/api/assets/{new_id}/lods", method="POST", body={"ratios": [2.0]})
expect("a bad ratio is a 400", status == 400, str(status))
status, _ = client.json("/api/notes", method="POST",
                        body={"asset_id": new_id, "category": "LOD", "note_text": "too dense"})
expect("a note can be added over HTTP", status == 201, str(status))
status, _ = client.json("/api/notes/1", method="DELETE")
expect("a note can be deleted", status == 200)
status, _ = client.json("/api/assets/999")
expect("an unknown asset is a 404", status == 404)
status, _ = client.json("/api/assets/1", method="DELETE")
expect("an asset can be deleted", status == 200)
expect("its LOD files go with it", not (data_dir / "lods" / "1").exists())
expect("its row goes with it", len(aws.list_assets(db)) == 1)
server.shutdown()
server.server_close()

print("6. HTTP: token mode")
secure_db, secure_data = fresh("secure")
aws.create_asset(secure_db, secure_data, "cube.obj", OBJ.encode())
secured = aws.build_server("127.0.0.1", 0, secure_db, secure_data, token="hunter2", quiet=True)
sport = secured.server_address[1]
threading.Thread(target=secured.serve_forever, daemon=True).start()
time.sleep(0.15)
anon = Client(sport)
right = Client(sport, token="hunter2")

status, _ = anon.json("/api/assets")
expect("api calls need the token", status == 401, str(status))
status, _ = Client(sport, token="wrong").json("/api/assets")
expect("a wrong token is refused", status == 401, str(status))
status, listing = right.json("/api/assets")
expect("the right token works", status == 200 and len(listing["assets"]) == 1)
status, raw, _ = anon.call("/viewer")
expect("the page itself stays open so the UI can ask for a token", status == 200)
status, _ = anon.json("/api/assets?filename=x.obj", method="POST", body=OBJ.encode())
expect("uploads need the token too", status == 401, str(status))
status, _, _ = anon.call("/api/assets/1/file?token=hunter2")
expect("a query token works for a browser tab", status == 200, str(status))
secured.shutdown()
secured.server_close()

print("7. reference auditing (broken links + orphans)")
import base64  # noqa: E402
import struct  # noqa: E402

ref_db, ref_data = fresh("references")
assets_dir = ref_data / "assets"
assets_dir.mkdir(parents=True, exist_ok=True)

# A real one-triangle glTF: positions (36 bytes) + indices (12 bytes), with an
# *external* image so the reference audit has something to resolve.
_positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
_indices = struct.pack("<3I", 0, 1, 2)
_blob = _positions + _indices
GLTF_WITH_IMAGE = json.dumps({
    "asset": {"version": "2.0"},
    "scene": 0,
    "scenes": [{"nodes": [0]}],
    "nodes": [{"mesh": 0}],
    "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1,
                               "material": 0}]}],
    "materials": [{"name": "wood", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
    "textures": [{"source": 0}],
    "images": [{"uri": "wood_Albedo.png"}],
    "buffers": [{"byteLength": len(_blob),
                 "uri": "data:application/octet-stream;base64,"
                        + base64.b64encode(_blob).decode()}],
    "bufferViews": [
        {"buffer": 0, "byteOffset": 0, "byteLength": len(_positions)},
        {"buffer": 0, "byteOffset": len(_positions), "byteLength": len(_indices)},
    ],
    "accessors": [
        {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
         "min": [0, 0, 0], "max": [1, 1, 0]},
        {"bufferView": 1, "componentType": 5125, "count": 3, "type": "SCALAR"},
    ],
}).encode()

ref_asset = aws.create_asset(ref_db, ref_data, "table.gltf", GLTF_WITH_IMAGE)
report = aws.asset_references(ref_db, ref_data, ref_asset["id"])
expect("the reference is listed", [r["uri"] for r in report["references"]] == ["wood_Albedo.png"],
       str(report["references"]))
expect("a missing texture is reported", report["missing"] == ["wood_Albedo.png"])
expect("the report is not ok", report["ok"] is False)

# Ship the texture next to it: the same reference is now satisfied.
from PIL import Image  # noqa: E402

def png_bytes(colour):
    out = _io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(out, format="PNG")
    return out.getvalue()


aws.add_sidecar(ref_db, ref_data, ref_asset["id"], "wood_Albedo.png", png_bytes((120, 100, 90)))
report = aws.asset_references(ref_db, ref_data, ref_asset["id"])
expect("adding the texture clears the missing link", report["missing"] == [])
expect("and the reference is marked present", report["references"][0]["exists"] is True)

aws.add_sidecar(ref_db, ref_data, ref_asset["id"], "nobody_uses_me.png", png_bytes((30, 30, 30)))
report = aws.asset_references(ref_db, ref_data, ref_asset["id"])
expect("an unreferenced texture is an orphan", "nobody_uses_me.png" in report["orphaned"],
       str(report["orphaned"]))
expect("the orphaned texture is not the referenced one",
       "wood_Albedo.png" not in report["orphaned"])
expect("orphans keep the report not-ok", report["ok"] is False)

ref_server = aws.build_server("127.0.0.1", 0, ref_db, ref_data, quiet=True)
rport = ref_server.server_address[1]
threading.Thread(target=ref_server.serve_forever, daemon=True).start()
time.sleep(0.15)
status, body = Client(rport).json(f"/api/assets/{ref_asset['id']}/references")
expect("the endpoint serves the audit", status == 200 and body["references"][0]["exists"] is True,
       str(status))
expect("the endpoint reports orphans", "nobody_uses_me.png" in body["orphaned"])
status, _ = Client(rport).json("/api/assets/999/references")
expect("an unknown asset is a 404", status == 404, str(status))
ref_server.shutdown()
ref_server.server_close()

print("8. live events and the printable report")
live_db, live_data = fresh("live")
live_server = aws.build_server("127.0.0.1", 0, live_db, live_data, quiet=True)
lport = live_server.server_address[1]
threading.Thread(target=live_server.serve_forever, daemon=True).start()
time.sleep(0.15)

stream_events = []


def read_events():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{lport}/api/events", timeout=20) as stream:
            for raw in stream:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    stream_events.append(json.loads(line[5:].strip()))
                    break
    except Exception as exc:
        stream_events.append({"error": str(exc)})


reader = threading.Thread(target=read_events, daemon=True)
reader.start()
time.sleep(0.4)
status, created = Client(lport).json("/api/assets?filename=live.obj&uploader=dev",
                                     method="POST", body=OBJ.encode())
reader.join(10)
event = stream_events[0] if stream_events else {}
expect("the viewer stream receives uploads", event.get("type") == "asset", str(event))
expect("the event names the new asset", event.get("asset_id") == created["id"], str(event))
expect("the stream thread ended with the client", not reader.is_alive())
live_server.shutdown()
live_server.server_close()

report = aws.render_report(live_db, live_data, created["id"])
expect("the report is a standalone document",
       report.startswith("<!DOCTYPE html>") and "<style>" in report)
expect("it carries no external references",
       "http://" not in report and "https://" not in report and "<script" not in report)
expect("it names the asset", "live.obj" in report)
expect("it shows the measured geometry", "12" in report and "8" in report)
expect("it includes a validation verdict",
       "PASSED" in report or "FAILED" in report, report[:0])
expect("it shows the reference section", "Reference integrity" in report)
expect("it shows the LOD section", "LOD breakdown" in report)
expect("it shows the collision proxy", "Collision" in report and "box" in report)
expect("it shows notes", "Review notes" in report)

# Notes are stored verbatim, so they are the real hostile-input vector for the
# report. Filenames are sanitised before they ever reach the database.
aws.add_note(live_db, created["id"], "Other", "<script>alert(1)</script> and **bold**", "eve")
evil_report = aws.render_report(live_db, live_data, created["id"])
expect("note text is escaped in the report", "<script>alert(1)</script>" not in evil_report)
expect("the escaped note is still readable", "&lt;script&gt;alert(1)&lt;/script&gt;" in evil_report)
evil_asset = aws.create_asset(live_db, live_data,
                              "<script>alert(2)</script>.obj", OBJ.encode())
expect("filenames are sanitised on the way in",
       "<" not in evil_asset["filename"] and ">" not in evil_asset["filename"],
       evil_asset["filename"])

report_server = aws.build_server("127.0.0.1", 0, live_db, live_data, quiet=True)
rport = report_server.server_address[1]
threading.Thread(target=report_server.serve_forever, daemon=True).start()
time.sleep(0.15)
status, raw, headers = Client(rport).call(f"/api/assets/{created['id']}/report")
expect("the report route serves HTML", status == 200 and b"<!DOCTYPE html>" in raw, str(status))
expect("inline by default", "Content-Disposition" not in headers, str(headers.get("Content-Disposition")))
status, _, headers = Client(rport).call(f"/api/assets/{created['id']}/report?download=1")
expect("?download=1 forces a file download",
       headers.get("Content-Disposition", "").startswith("attachment"), str(headers.get("Content-Disposition")))
status, _ = Client(rport).json("/api/assets/999/report")
expect("an unknown asset has no report", status == 404, str(status))
report_server.shutdown()
report_server.server_close()

print("9. collision proxy export")

exp_db, exp_data = fresh("export")
exp_asset = aws.create_asset(exp_db, exp_data, "crate.obj", OBJ.encode())

box_text = aws.collision_obj(exp_asset, aws.get_collision(exp_db, exp_asset["id"]))
expect("the derived box exports as 8 vertices", box_text.count("\nv ") == 8,
       str(box_text.count("\nv ")))
expect("the derived box exports as 6 quads", box_text.count("\nf ") == 6)
expect("the export names the asset", exp_asset["filename"] in box_text)
expect("the export is a valid obj header", box_text.startswith("# AssetGatekeeper"))

aws.set_collision(exp_db, exp_asset["id"], "sphere",
                  {"center": [0, 0, 0], "radius": 2.0})
sphere_text = aws.collision_obj(exp_asset, aws.get_collision(exp_db, exp_asset["id"]))
expect("a sphere exports as a closed quad grid", sphere_text.count("\nf ") == 24 * 16,
       str(sphere_text.count("\nf ")))

aws.set_collision(exp_db, exp_asset["id"], "hull",
                  {"points": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 1]]})
hull_text = aws.collision_obj(exp_asset, aws.get_collision(exp_db, exp_asset["id"]))
expect("a hull exports as triangles", hull_text.count("\nf ") >= 4,
       str(hull_text.count("\nf ")))
expect("every hull face is a triangle",
       all(len(line.split()) == 4 for line in hull_text.splitlines() if line.startswith("f ")))

try:
    aws.convex_hull_triangles([[i, 0, 0] for i in range(70)])
    expect("a huge hull point cloud is refused", False)
except aws.WorkstationError as exc:
    expect("a huge hull point cloud is refused", exc.status == 413, str(exc.status))

export_server = aws.build_server("127.0.0.1", 0, exp_db, exp_data, quiet=True)
export_port = export_server.server_address[1]
threading.Thread(target=export_server.serve_forever, daemon=True).start()
time.sleep(0.15)
status, body, headers = Client(export_port).call(f"/api/assets/{exp_asset['id']}/collision.obj")
expect("the export is downloadable", status == 200, str(status))
expect("it downloads as a file",
       "attachment" in (headers.get("Content-Disposition") or ""),
       str(headers.get("Content-Disposition")))
expect("the body is the obj text", body.decode().startswith("# AssetGatekeeper"))
export_server.shutdown()
export_server.server_close()

print("10. CLI wiring")
options = aws.build_parser().parse_args(["--port", "9999", "--db", "x.db",
                                         "--data", "y", "--token", "t"])
ws_port_options = aws.build_parser().parse_args(["--port-auto"])
expect("the workstation CLI can ask for port fallback", ws_port_options.port_auto is True)
expect("the workstation does not fall back by default",
       aws.build_parser().parse_args([]).port_auto is False)
expect("CLI accepts the storage and auth flags",
       (options.port, options.db, options.data, options.token) == (9999, "x.db", "y", "t"))
expect("defaults are loopback and unauthenticated",
       (aws.DEFAULT_HOST, aws.DEFAULT_PORT) == ("127.0.0.1", 8100))
expect("a bad data folder exits 2",
       aws.main(["--db", str(WORK / "missing" / "x.db"), "--data", str(WORK / "d")]) == 2)

print("11. rules inspector endpoint")
# The viewer is served by this server, so /api/rules must be same-origin here -
# the panel can therefore work with no CORS and no second process running.
insp_db, insp_data = fresh("inspector")
insp_rules = WORK / "workstation_rules.toml"
insp_rules.write_text(
    'platform = "n64"\n'
    "\n[mesh]\n"
    "max_triangles = 1500\n"
    "max_material_slots = 2\n"
    "\n[texture]\n"
    "max_edge = 64\n"
    "\n[platforms.n64]\n"
    "max_texture_edge = 32\n"
    "\n[tags.rooms]\n"
    "max_triangles = 8000\n"
    "max_material_slots = 32\n",
    encoding="utf-8")
insp_server = aws.build_server("127.0.0.1", 0, insp_db, insp_data, quiet=True,
                               rules_path=str(insp_rules), rules_root=str(WORK))
threading.Thread(target=insp_server.serve_forever, daemon=True).start()
time.sleep(0.15)
insp_client = Client(insp_server.server_address[1])

status, rules = insp_client.json("/api/rules")
expect("GET /api/rules is served by the workstation itself", status == 200, str(status))
expect("it is flagged read-only", rules.get("read_only") is True)
expect("it carries the platform", rules.get("target_platform") == "n64",
       str(rules.get("target_platform")))
expect("it carries the effective global budget",
       (rules["global_budget"]["max_triangles"], rules["global_budget"]["max_texture_edge"])
       == (1500, 32), str(rules["global_budget"])[:100])
tag_rows = rules.get("tag_budgets") or []
expect("it carries the tag budgets the panel renders",
       [row["tag"] for row in tag_rows] == ["rooms"], str(tag_rows)[:100])
expect("a tag budget reports its own limits",
       bool(tag_rows) and tag_rows[0]["max_triangles"] == 8000
       and tag_rows[0]["max_material_slots"] == 32, str(tag_rows)[:100])
expect("vertex caps are null rather than fabricated",
       rules["global_budget"]["max_vertices"] is None
       and rules.get("vertex_budget_enforced") is False)
expect("the page links the panel to the same endpoint",
       b"Rules &amp; Budgets" in insp_client.call("/viewer")[1]
       and b"rules-table" in insp_client.call("/viewer")[1])
insp_server.shutdown()
insp_server.server_close()

# Token mode must not turn the inspector into an open door.
sec_db, sec_data = fresh("inspector_secure")
sec_server = aws.build_server("127.0.0.1", 0, sec_db, sec_data, token="hunter2", quiet=True,
                              rules_path=str(insp_rules), rules_root=str(WORK))
threading.Thread(target=sec_server.serve_forever, daemon=True).start()
time.sleep(0.15)
expect("a token-protected workstation refuses /api/rules without one",
       Client(sec_server.server_address[1]).json("/api/rules")[0] == 401)
expect("the right token gets the inspector",
       Client(sec_server.server_address[1], token="hunter2").json("/api/rules")[0] == 200)
sec_server.shutdown()
sec_server.server_close()

rules_options = aws.build_parser().parse_args(["--rules", "studio.toml"])
expect("the CLI can point the panel at a rules file",
       rules_options.rules == "studio.toml", rules_options.rules)

print("18. unmeasured assets stay out of the triangle total")
# A .blend/.max/.ma is stored because it is a real source file, but nothing in
# this process can count its triangles. The stored 0 is an absence of
# measurement, so it must never be added into the library total - a project of
# source files would otherwise look free.
um_db, um_data = fresh("unmeasured")
um_cube = aws.create_asset(um_db, um_data, "cube.obj", OBJ.encode())
um_blend = aws.create_asset(um_db, um_data, "crate.blend",
                            b"BLENDER-v403\x00" + b"\x00" * 96)
expect("the .blend is stored, not refused", um_blend["filename"] == "crate.blend",
       str(um_blend)[:120])
expect("and it is marked unverified", um_blend["kind"] == "unverified", um_blend["kind"])
expect("the .obj is still measured", um_cube["kind"] == "mesh" and um_cube["triangle_count"] == 12,
       str(um_cube)[:120])

um_stats = aws.repository_stats(um_db)
expect("the library total counts measured assets only",
       um_stats["triangles"] == 12, str(um_stats))
expect("measured and unverified are reported apart",
       um_stats["measured"] == 1 and um_stats["unverified"] == 1, str(um_stats))
expect("both assets are still listed", um_stats["assets"] == 2, str(um_stats))

um_report = aws.render_report(um_db, um_data, um_blend["id"])
expect("the report does not print a fabricated 0 for the mesh",
       "not measured" in um_report and "<td>0</td>" not in um_report, um_report[:0])
expect("the report says it was not validated",
       "not validated" in um_report and "no violations under the default budget" not in um_report,
       um_report[:0])
expect("the report badge is honest about it", "UNVERIFIED" in um_report, um_report[:0])
expect("the passing asset still reports its numbers",
       "not measured" not in aws.render_report(um_db, um_data, um_cube["id"]))

um_server = aws.build_server("127.0.0.1", 0, um_db, um_data, quiet=True)
um_port = um_server.server_address[1]
threading.Thread(target=um_server.serve_forever, daemon=True).start()
threading.Event().wait(0.05)
um_body = Client(um_port).json("/api/stats")[1]
expect("the stats endpoint exposes both counts",
       um_body["triangles"] == 12 and um_body["unverified"] == 1
       and um_body["measured"] == 1, str(um_body)[:160])
um_server.shutdown()
um_server.server_close()

print("19. phase 1 fixes (found by driving the viewer in a browser, 2026-11-02)")
import math  # noqa: E402
import socket  # noqa: E402
from base64 import b64decode as _b64decode  # noqa: E402

# Filenames keep their extension; Unicode stems survive.
expect("a Unicode stem keeps its name and extension", aws.safe_filename("岩.obj") == "岩.obj",
       aws.safe_filename("岩.obj"))
expect("a Cyrillic name keeps its extension",
       aws.safe_filename("наш_ящик.glb") == "наш_ящик.glb", aws.safe_filename("наш_ящик.glb"))
long_name = aws.safe_filename("a" * 300 + ".glb")
expect("a long name is shortened before the extension",
       long_name.endswith(".glb") and len(long_name) <= aws.MAX_NAME, long_name[-10:])
expect("path parts are still stripped", aws.safe_filename("..\\..\\x/evil.glb") == "evil.glb",
       aws.safe_filename("..\\..\\x/evil.glb"))
expect("a bare extension gets a stem", aws.safe_filename(".glb") == "asset.glb",
       aws.safe_filename(".glb"))
p1_db, p1_data = fresh("phase1")
uni = aws.create_asset(p1_db, p1_data, "岩.obj", OBJ.encode())
expect("a Unicode-named mesh is measured, not unverified",
       uni["kind"] == "mesh" and uni["triangle_count"] == 12, (uni["kind"], uni["triangle_count"]))

# Collision values are validated before they are stored.
def refused(kind, data):
    try:
        aws.set_collision(p1_db, uni["id"], kind, data)
    except aws.WorkstationError as exc:
        return exc.status
    return None


expect("an infinite radius is refused (it was stored as Infinity)",
       refused("sphere", {"center": [0, 0, 0], "radius": 1e999}) == 400)
expect("a zero radius is refused", refused("sphere", {"center": [0, 0, 0], "radius": 0}) == 400)
expect("an inverted box is refused", refused("box", {"min": [1, 1, 1], "max": [0, 0, 0]}) == 400)
expect("a text coordinate is a 400, not a 500",
       refused("box", {"min": ["a", 0, 0], "max": [1, 1, 1]}) == 400)
expect("true is not a number", refused("sphere", {"center": [0, 0, 0], "radius": True}) == 400)
expect("non-object data is a 400", refused("box", [1, 2, 3]) == 400)
stored = aws.get_collision(p1_db, uni["id"])
expect("a refused value leaves the stored proxy alone", stored["kind"] == "box", str(stored)[:80])

# Exported OBJ face numbers point at real vertices.
def obj_indices(text):
    vertices = sum(1 for line in text.splitlines() if line.startswith("v "))
    indices = [int(token) for line in text.splitlines() if line.startswith("f ")
               for token in line.split()[1:]]
    return vertices, min(indices), max(indices)


box_v, box_lo, box_hi = obj_indices(aws.collision_obj(uni, aws.get_collision(p1_db, uni["id"])))
expect("box export face numbers run 1..8 (was up to 9)", (box_lo, box_hi) == (1, box_v) == (1, 8),
       (box_v, box_lo, box_hi))
aws.set_collision(p1_db, uni["id"], "sphere", {"center": [0, 0, 0], "radius": 1.0})
sp_v, sp_lo, sp_hi = obj_indices(aws.collision_obj(uni, aws.get_collision(p1_db, uni["id"])))
expect("sphere export face numbers stay inside the vertex list",
       sp_lo == 1 and sp_hi <= sp_v, (sp_v, sp_lo, sp_hi))

# Hulls: a cube is 12 outward triangles; a dense cloud is reduced, then exportable.
cube = [[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)]
cube_faces, cube_points = aws.convex_hull_triangles(cube)
expect("a cube hull is 12 triangles (was 24 overlapping)", len(cube_faces) == 12, len(cube_faces))


def outward(faces, points):
    centre = [sum(p[i] for p in points) / len(points) for i in range(3)]
    for a, b, c in faces:
        pa, pb, pc = points[a], points[b], points[c]
        n = aws._cross([pb[i] - pa[i] for i in range(3)], [pc[i] - pa[i] for i in range(3)])
        if aws._dot(n, [pa[i] - centre[i] for i in range(3)]) <= 0:
            return False
    return True


expect("every cube hull triangle faces outward", outward(cube_faces, cube_points))
cloud = [[math.cos(i * 0.7) * math.sin(i * 0.3), math.sin(i * 0.7) * math.sin(i * 0.3),
          math.cos(i * 0.3)] for i in range(400)]
aws.set_collision(p1_db, uni["id"], "hull", {"points": cloud})
hull = aws.get_collision(p1_db, uni["id"])
expect("a 400-point hull is stored as at most 64 support points",
       4 <= len(hull["data"]["points"]) <= aws.HULL_POINT_LIMIT, len(hull["data"]["points"]))
hull_faces, hull_points = aws.convex_hull_triangles(hull["data"]["points"])
expect("... and it exports (was always 413)", len(hull_faces) >= 4 and outward(hull_faces, hull_points),
       len(hull_faces))

# LOD inputs.
nan_obj = OBJ.replace("v 1 1 1", "v nan 1 1")
nan_asset = aws.create_asset(p1_db, p1_data, "nan.obj", nan_obj.encode())
try:
    aws.generate_lods(p1_db, p1_data, nan_asset["id"])
    expect("a NaN vertex is a 422, not a 500", False)
except aws.WorkstationError as exc:
    expect("a NaN vertex is a 422, not a 500", exc.status == 422 and "non-finite" in str(exc),
           (exc.status, str(exc)))

expect("the page may fetch data: and blob: (embedded glTF buffers and GLB textures)",
       any(key == "Content-Security-Policy" and "connect-src 'self' data: blob:" in value
           for key, value in aws.WORKSTATION_SECURITY_HEADERS))
expect("a stalled client cannot hold a thread forever", (aws.Handler.timeout or 0) > 0)

p1_server = aws.build_server("127.0.0.1", 0, p1_db, p1_data, quiet=True,
                             allow_hosts=("review.studio.example",))
p1_port = p1_server.server_address[1]
threading.Thread(target=p1_server.serve_forever, daemon=True).start()
time.sleep(0.15)
p1 = Client(p1_port)
note = {"asset_id": uni["id"], "category": "Other", "note_text": "x"}
status, _, _ = p1.call("/api/notes", "POST", note, {"Origin": "http://evil.example",
                                                     "Content-Type": "text/plain"})
expect("a write from another website is refused", status == 403, status)
status, _, _ = p1.call("/api/notes", "POST", note, {"Origin": f"http://127.0.0.1:{p1_port + 1}"})
expect("... including another port on the same machine", status == 403, status)
status, _, _ = p1.call("/api/notes", "POST", note, {"Origin": "null"})
expect("... and an opaque 'null' origin", status == 403, status)
status, _, _ = p1.call("/api/notes", "POST", note, {"Sec-Fetch-Site": "cross-site"})
expect("... and a browser write with no Origin but Sec-Fetch-Site: cross-site", status == 403, status)
status, _, _ = p1.call(f"/api/assets/{uni['id']}", "DELETE", None, {"Origin": "http://evil.example"})
expect("a cross-site delete is refused", status == 403, status)
status, _, _ = p1.call("/api/notes", "POST", note, {"Origin": f"http://127.0.0.1:{p1_port}"})
expect("the viewer's own page may still write", status == 201, status)
status, _, _ = p1.call("/api/notes", "POST", note, {"Origin": "https://review.studio.example"})
expect("a name passed with --allow-host may write (proxy setups)", status == 201, status)
status, _, _ = p1.call("/api/notes", "POST", note)
expect("non-browser clients (uploader, AssetHealth) are unaffected", status == 201, status)
status, _, headers = p1.call("/api/notes", "OPTIONS", None,
                             {"Origin": "http://evil.example", "Access-Control-Request-Method": "DELETE"})
expect("a CORS preflight gets 405 and no permission",
       status == 405 and not any(k.lower().startswith("access-control") for k in headers), (status, headers))
status, _ = p1.json("/api/assets/9999/notes")
expect("notes of a missing asset are a 404", status == 404, status)
status, _ = p1.json("/api/assets/9999/lods")
expect("LODs of a missing asset are a 404", status == 404, status)
status, body = p1.json(f"/api/assets/{uni['id']}/lods", "POST", {"max_triangles": [100]})
expect("an unknown LOD field is named, not ignored", status == 400 and "max_triangles" in body["error"],
       body)
status, _ = p1.json(f"/api/assets/{uni['id']}/lods", "POST", {"ratios": [0.5] * 5})
expect("more than 4 LOD levels is a 400, not silently cut", status == 400, status)
status, _ = p1.json(f"/api/assets/{uni['id']}/lods", "POST", {"max_tris": [-5]})
expect("a negative cap is a 400", status == 400, status)
status, body = p1.json(f"/api/assets/{uni['id']}/collision", "POST",
                       {"kind": "sphere", "data": {"center": [0, 0, 0], "radius": 1e999}})
expect("an infinite radius over HTTP is a 400", status == 400, (status, body))

# One request, one answer: a bad LOD body used to get a 400 *and* a 201.
raw = socket.create_connection(("127.0.0.1", p1_port))
bad = b"not json"
raw.sendall(b"POST /api/assets/%d/lods HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
            b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (uni["id"], p1_port, len(bad))
            + bad)
raw.settimeout(3)
received = b""
try:
    while True:
        chunk = raw.recv(65536)
        if not chunk:
            break
        received += chunk
except socket.timeout:
    pass
raw.close()
expect("a bad LOD body gets exactly one response", received.count(b"HTTP/1.1 ") == 1,
       received.count(b"HTTP/1.1 "))
expect("... and no LODs are generated behind it", aws.list_lods(p1_db, uni["id"]) == [],
       aws.list_lods(p1_db, uni["id"]))
p1_server.shutdown()
p1_server.server_close()

print("20. phase 2: FBX preview, textures, sidecars, broken files")
import io  # noqa: E402
import contextlib  # noqa: E402
from PIL import Image as _Image  # noqa: E402

p2_db, p2_data = fresh("phase2")
FIXTURES = ROOT / "tests" / "fixtures"

# FBX: measured by the gate's reader, previewed through a converted .glb.
mats5 = aws.create_asset(p2_db, p2_data, "mats5.fbx", (FIXTURES / "fbx" / "mats5.fbx").read_bytes())
expect("an FBX is measured, not 'unverified'",
       (mats5["kind"], mats5["triangle_count"], mats5["material_slots"]) == ("mesh", 12, 5),
       (mats5["kind"], mats5["triangle_count"], mats5["material_slots"]))
detail = aws.asset_detail(p2_db, p2_data, mats5["id"])
expect("it has a converted .glb preview, flagged as converted",
       detail["preview"]["type"] == "glb" and detail["preview"]["converted"]
       and "converted from this FBX" in detail["preview"]["warning"], detail["preview"])
ue_lod = aws.create_asset(p2_db, p2_data, "cube_lod.fbx",
                          (FIXTURES / "fbx_ue" / "cube_lod_binary.fbx").read_bytes())
expect("an Unreal FBX with collision and LOD1+ counts LOD0 only (768)",
       ue_lod["triangle_count"] == 768 and "collision" in ue_lod["analyse_note"]
       and "LOD1+" in ue_lod["analyse_note"], (ue_lod["triangle_count"], ue_lod["analyse_note"]))
preview_glb = p2_data / "assets" / str(ue_lod["id"]) / ".agk" / "preview.glb"
import trimesh  # noqa: E402
scene = trimesh.load(preview_glb, force="scene")
expect("the preview draws exactly what was counted",
       sum(len(g.faces) for g in scene.geometry.values()) == 768)
lods = aws.generate_lods(p2_db, p2_data, ue_lod["id"], [0.5])
expect("an FBX gets LODs (from its preview), written as .glb",
       len(lods) == 1 and lods[0]["file_path"].endswith(".glb") and 0 < lods[0]["triangle_count"] < 768,
       lods)
broken_fbx = aws.create_asset(p2_db, p2_data, "half.fbx",
                              (FIXTURES / "fbx" / "mats5.fbx").read_bytes()[:400])
expect("a truncated FBX is broken, with the reason", broken_fbx["kind"] == "broken"
       and "cannot be read" in broken_fbx["analyse_note"], broken_fbx["analyse_note"])
stats = aws.repository_stats(p2_db)
expect("broken files are counted apart and kept out of the triangle total",
       stats["broken"] == 1 and stats["triangles"] == 12 + 768, stats)

# Textures: shown as images; formats a browser cannot draw get a PNG preview.
tga_bytes = io.BytesIO()
_Image.new("RGB", (48, 40), (90, 120, 60)).save(tga_bytes, format="TGA")
tga = aws.create_asset(p2_db, p2_data, "stone.tga", tga_bytes.getvalue())
tga_detail = aws.asset_detail(p2_db, p2_data, tga["id"])
expect("a TGA is a texture with its size and a power-of-two note",
       tga["kind"] == "texture" and "48x40" in tga["analyse_note"] and "power of two" in tga["analyse_note"],
       tga["analyse_note"])
expect("... previewed as a converted PNG", tga_detail["preview"]["type"] == "image"
       and tga_detail["preview"]["converted"], tga_detail["preview"])
junk_png = aws.create_asset(p2_db, p2_data, "junk.png", b"not a png at all")
expect("an unreadable image is broken", junk_png["kind"] == "broken", junk_png["kind"])

# Sidecars: a .gltf waits for its .bin, then measures.
tri = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0) + struct.pack("<3I", 0, 1, 2)
rock_gltf = json.dumps({
    "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
    "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
    "buffers": [{"byteLength": len(tri), "uri": "rock.bin"}],
    "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36},
                    {"buffer": 0, "byteOffset": 36, "byteLength": 12}],
    "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                   "min": [0, 0, 0], "max": [1, 1, 0]},
                  {"bufferView": 1, "componentType": 5125, "count": 3, "type": "SCALAR"}]}).encode()
p2_server = aws.build_server("127.0.0.1", 0, p2_db, p2_data, quiet=True)
p2_port = p2_server.server_address[1]
threading.Thread(target=p2_server.serve_forever, daemon=True).start()
time.sleep(0.15)
c2 = Client(p2_port)
status, rock = c2.json("/api/assets?filename=rock.gltf", "POST", rock_gltf)
expect("a .gltf without its .bin waits for it, and says which file",
       status == 201 and rock["kind"] == "unverified" and rock["missing"] == ["rock.bin"]
       and "waiting for its files" in rock["analyse_note"], (status, rock.get("missing"), rock.get("analyse_note")))
status, rock = c2.json(f"/api/assets/{rock['id']}/files?filename=rock.bin", "POST", tri)
expect("adding the .bin measures it", status == 201 and rock["kind"] == "mesh"
       and rock["triangle_count"] == 1 and rock["missing"] == [], (status, rock.get("kind")))
status, raw, _ = c2.call(f"/api/assets/{rock['id']}/files/rock.bin")
expect("the viewer can fetch the .bin from the asset's folder", status == 200 and raw == tri, status)
for bad in (".agk/preview.glb", "..%2F..%2Fws.db", "nope.bin"):
    status, _, _ = c2.call(f"/api/assets/{rock['id']}/files/{bad}")
    expect(f"files/{bad} is not served", status == 404, status)
status, _ = c2.json(f"/api/assets/{rock['id']}/files?filename=tool.exe", "POST", b"MZ")
expect("an executable sidecar is refused", status == 415, status)
status, _ = c2.json(f"/api/assets/{rock['id']}/files?filename=rock.gltf", "POST", rock_gltf)
expect("a sidecar cannot overwrite the asset itself", status == 409, status)
status, body = c2.json(f"/api/assets/{rock['id']}/files?filename=textures/wood.png", "POST", b"png")
expect("a sidecar keeps the folder the asset references it from",
       status == 201 and "textures/wood.png" in body["files"], body.get("files"))
status, body = c2.json(f"/api/assets/{rock['id']}/files?filename=../../escape.png", "POST", b"png")
expect("a sidecar cannot climb out of the folder", status == 201 and "escape.png" in body["files"]
       and not (p2_data / "escape.png").exists(), body.get("files"))
status, _, _ = c2.call(f"/api/assets/{rock['id']}/files?filename=x.bin", "POST", b"x",
                       {"Origin": "http://evil.example"})
expect("a cross-site sidecar upload is refused", status == 403, status)

# Old flat-layout assets move into a folder the first time a sidecar arrives.
legacy_file = p2_data / "assets" / "999_old.gltf"
legacy_file.write_bytes(rock_gltf)
with aws.open_db(p2_db) as connection:
    connection.execute(
        "INSERT INTO assets (id, filename, file_path, filesize, triangle_count, vertex_count,"
        " material_slots, uploaded_at, kind) VALUES (999, 'old.gltf', 'assets/999_old.gltf',"
        " 10, 0, 0, 0, '2026-01-01T00:00:00+00:00', 'unverified')")
legacy = aws.add_sidecar(p2_db, p2_data, 999, "rock.bin", tri)
expect("a legacy asset is moved into its folder and measured",
       legacy["file_path"] == "assets/999/old.gltf" and legacy["kind"] == "mesh"
       and not legacy_file.exists(), (legacy["file_path"], legacy["kind"]))

# Unicode names download (the header used to raise inside send_header).
uni2 = aws.create_asset(p2_db, p2_data, "岩.obj", OBJ.encode())
status, _, headers = c2.call(f"/api/assets/{uni2['id']}/file")
expect("a Unicode filename downloads with an RFC 5987 header",
       status == 200 and "filename*=UTF-8''%E5%B2%A9.obj" in headers.get("Content-Disposition", ""),
       (status, headers.get("Content-Disposition")))
status, raw, _ = c2.call(f"/api/assets/{mats5['id']}/preview")
expect("the FBX preview is served as a GLB", status == 200 and raw[:4] == b"glTF", status)
status, raw, _ = c2.call(f"/api/assets/{tga['id']}/preview")
expect("the TGA preview is served as a PNG", status == 200 and raw[:4] == b"\x89PNG", status)
status, _, _ = c2.call(f"/api/assets/{uni2['id']}/preview")
expect("an .obj has no separate preview (404)", status == 404, status)

# Delete takes the whole folder: the asset, its sidecars and its previews.
folder = p2_data / "assets" / str(rock["id"])
status, _ = c2.json(f"/api/assets/{rock['id']}", "DELETE")
expect("deleting an asset removes its folder and sidecars", status == 200 and not folder.exists(), status)

# The bulk uploader sends sidecars with their asset (and accepts the documented flags).
sys.path.insert(0, str(ROOT / "tools"))
import upload_assets  # noqa: E402
bulk = WORK / "bulk"
bulk.mkdir()
(bulk / "rock.gltf").write_bytes(rock_gltf)
(bulk / "rock.bin").write_bytes(tri)
(bulk / "lone_Albedo.png").write_bytes(png_bytes((1, 2, 3)))
out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = upload_assets.main([str(bulk), "--workstation", f"http://127.0.0.1:{p2_port}", "--recursive"])
names = sorted(row["filename"] for row in aws.list_assets(p2_db))
expect("the uploader accepts --workstation and --recursive (as the docs say)", code == 0, out.getvalue()[-200:])
expect("it sends rock.bin with rock.gltf, not as its own asset",
       "rock.bin" not in names and "+1 file(s)" in out.getvalue(), out.getvalue()[-300:])
expect("an unreferenced texture still goes up as an asset", "lone_Albedo.png" in names, names)
p2_server.shutdown()
p2_server.server_close()

print("21. phase 3: LOD jobs, uploaded LODs, warnings")
p3_db, p3_data = fresh("phase3")
box = aws.create_asset(p3_db, p3_data, "box.obj", OBJ.encode())
lods = aws.generate_lods(p3_db, p3_data, box["id"], [0.5, 0.25])
expect("a LOD that saves nothing is flagged (was silent)",
       all("same triangle count as the source" in lod["warning"] for lod in lods),
       [lod["warning"] for lod in lods])
try:
    aws.generate_lods(p3_db, p3_data, box["id"], [0.1, 0.5])
    expect("ratios that grow are refused", False)
except aws.WorkstationError as exc:
    expect("ratios that grow are refused", exc.status == 400 and "smaller level by level" in str(exc))

ball = lod_manager.Mesh([(math.cos(i * 0.37) * math.sin(i * 0.11), math.sin(i * 0.37) * math.sin(i * 0.11),
                          math.cos(i * 0.11)) for i in range(400)],
                        [(i, i + 1, i + 2) for i in range(0, 396)])
lod_manager.write_glb(WORK / "ball.glb", ball, "ball")
ball_asset = aws.create_asset(p3_db, p3_data, "ball.glb", (WORK / "ball.glb").read_bytes())

# Background jobs: one per asset, with progress.
release = threading.Event()
runner = aws.JobRunner()
blocked = runner.submit(1, lambda progress: (progress("LOD1", 0, 2), release.wait(5), {"ok": True})[-1])
try:
    runner.submit(1, lambda progress: None)
    expect("a second job for the same asset is refused while one runs", False)
except aws.WorkstationError as exc:
    expect("a second job for the same asset is refused while one runs", exc.status == 409, str(exc))
other = runner.submit(2, lambda progress: {"ok": 2})
time.sleep(0.2)
expect("a running job reports its level and step",
       runner.get(blocked["id"])["state"] == "running" and runner.get(blocked["id"])["level"] == "LOD1"
       and runner.get(blocked["id"])["steps"] == 2, runner.get(blocked["id"]))
release.set()
for _ in range(50):
    if runner.get(other["id"])["state"] == "done":
        break
    time.sleep(0.05)
expect("jobs finish and keep their result", runner.get(blocked["id"])["state"] == "done"
       and runner.get(other["id"])["result"] == {"ok": 2}, runner.get(other["id"]))
failing = runner.submit(3, lambda progress: (_ for _ in ()).throw(aws.WorkstationError("nope", 422)))
time.sleep(0.2)
expect("a failed job says why", runner.get(failing["id"])["state"] == "failed"
       and runner.get(failing["id"])["error"] == "nope", runner.get(failing["id"]))

p3_server = aws.build_server("127.0.0.1", 0, p3_db, p3_data, quiet=True)
p3_port = p3_server.server_address[1]
threading.Thread(target=p3_server.serve_forever, daemon=True).start()
time.sleep(0.15)
c3 = Client(p3_port)
status, body = c3.json(f"/api/assets/{ball_asset['id']}/lods?async=1", "POST", {"ratios": [0.5, 0.25]})
expect("LOD generation can run as a job (202)", status == 202 and body["job"]["state"] in ("queued", "running"),
       (status, body))
job_id = body["job"]["id"]
for _ in range(100):
    status, job = c3.json(f"/api/jobs/{job_id}")
    if job["state"] in ("done", "failed"):
        break
    time.sleep(0.05)
expect("the job finishes with the LODs", job["state"] == "done" and len(job["result"]["lods"]) == 2
       and job["steps"] == 2, job)
status, _ = c3.json("/api/jobs/9999")
expect("an unknown job is a 404", status == 404, status)
status, _ = c3.json("/api/assets/9999/lods?async=1", "POST", {"ratios": [0.5]})
expect("a job for a missing asset is a 404 before it is queued", status == 404, status)

# An artist's own LOD files.
lod_manager.write_glb(WORK / "one_tri.glb",
                      lod_manager.Mesh([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)]), "lod")
status, body = c3.json(f"/api/assets/{ball_asset['id']}/lods/LOD3?filename=ball_LOD3.glb", "POST",
                       (WORK / "one_tri.glb").read_bytes())
lod3 = next((lod for lod in body.get("lods", []) if lod["level"] == "LOD3"), {})
expect("an uploaded LOD is stored as a level, not a new asset",
       status == 201 and lod3.get("origin") == "uploaded" and lod3.get("triangle_count") == 1
       and len(aws.list_assets(p3_db)) == 2, (status, lod3))
status, raw, _ = c3.call(f"/api/assets/{ball_asset['id']}/lods/LOD3/file")
expect("... and served for the viewer", status == 200 and raw[:4] == b"glTF", status)
status, body = c3.json(f"/api/assets/{ball_asset['id']}/lods?async=0", "POST", {"ratios": [0.5]})
expect("regenerating fewer levels keeps the uploaded LOD3",
       status == 201 and any(l["level"] == "LOD3" and l["origin"] == "uploaded" for l in body["lods"]),
       [(l["level"], l["origin"]) for l in body.get("lods", [])])
status, body = c3.json(f"/api/assets/{ball_asset['id']}/lods", "POST", {"ratios": [0.5, 0.3, 0.1]})
expect("generation will not overwrite an uploaded level (409)",
       status == 409 and "LOD3" in body.get("error", ""), (status, body))
status, body = c3.json(f"/api/assets/{ball_asset['id']}/lods/LOD2?filename=mats5.fbx", "POST",
                       (FIXTURES / "fbx" / "mats5.fbx").read_bytes())
lod2 = next((lod for lod in body.get("lods", []) if lod["level"] == "LOD2"), {})
expect("an FBX LOD is measured from the FBX and viewed as a converted .glb",
       status == 201 and lod2.get("triangle_count") == 12 and lod2.get("file_path", "").endswith(".glb"),
       (status, lod2))
status, _ = c3.json(f"/api/assets/{ball_asset['id']}/lods/LOD4?filename=notes.txt", "POST", b"hello")
expect("a LOD must be a mesh file (415)", status == 415, status)
before_files = sorted(path.name for path in (p3_data / "lods" / str(ball_asset["id"])).iterdir())
status, _ = c3.json(f"/api/assets/{ball_asset['id']}/lods/LOD4?filename=bad.glb", "POST", b"garbage!")
after_files = sorted(path.name for path in (p3_data / "lods" / str(ball_asset["id"])).iterdir())
expect("an unreadable LOD is a 422 and leaves nothing behind", status == 422 and before_files == after_files,
       (status, set(after_files) - set(before_files)))
status, _ = c3.json(f"/api/assets/{ball_asset['id']}/lods/LOD5?filename=x.glb", "POST", b"x")
expect("only LOD1-LOD4 exist", status == 404, status)
status, body, _ = c3.call(f"/api/assets/{ball_asset['id']}/lods/LOD3", "DELETE", None,
                          {"Origin": "http://evil.example"})
expect("a cross-site LOD delete is refused", status == 403, status)
status, body = c3.json(f"/api/assets/{ball_asset['id']}/lods/LOD3", "DELETE")
expect("a single LOD can be deleted, with its files",
       status == 200 and all(l["level"] != "LOD3" for l in body["lods"])
       and not list((p3_data / "lods" / str(ball_asset["id"])).glob("LOD3_*")), body)
status, _ = c3.json(f"/api/assets/{ball_asset['id']}/lods/LOD3", "DELETE")
expect("deleting a LOD that is not there is a 404", status == 404, status)
p3_server.shutdown()
p3_server.server_close()

print("22. phase 4: LOD accuracy, speed and placement")
import trimesh as _tm  # noqa: E402
p4_dir = WORK / "phase4"
p4_dir.mkdir()
_tm.creation.icosphere(subdivisions=6).export(p4_dir / "ball82k.glb")
started = time.time()
p4_lods = lod_manager.generate(p4_dir / "ball82k.glb", p4_dir / "out", (0.5, 0.25, 0.125))
elapsed = time.time() - started
expect("each level lands at its target, within 2% over and never far under (was up to +34%)",
       all(lod.target_triangles * 0.9 <= lod.triangle_count <= lod.target_triangles * 1.02
           for lod in p4_lods), [(lod.target_triangles, lod.triangle_count) for lod in p4_lods])
expect("an 82k-triangle ladder takes seconds, not minutes", elapsed < 30, f"{elapsed:.1f}s")
capped = lod_manager.generate(p4_dir / "ball82k.glb", p4_dir / "capped", (0.5, 0.25), [5000, 900])
expect("a --max-tris cap is a ceiling (was overshot by 17-46%)",
       capped[0].triangle_count <= 5000 and capped[1].triangle_count <= 900,
       [(lod.target_triangles, lod.triangle_count) for lod in capped])

doc = json.loads(json.dumps({"nodes": [{"children": [1], "translation": [10, 0, 0]},
                                       {"mesh": 0, "scale": [2, 2, 2]}],
                             "scenes": [{"nodes": [0]}], "scene": 0}))
lod_manager.write_gltf(p4_dir / "raw.gltf", lod_manager.load_mesh(p4_dir / "ball82k.glb"), "raw")
placed = json.loads((p4_dir / "raw.gltf").read_text())
placed.update(doc)
(p4_dir / "placed.gltf").write_text(json.dumps(placed))
placed_lods = lod_manager.generate(p4_dir / "placed.gltf", p4_dir / "placed_out", (0.5,))
lod_bounds = lod_manager.load_mesh(placed_lods[0].path).bounds()
expect("a LOD sits where the node puts the source (was at the raw mesh's place and size)",
       abs(lod_bounds[0][0] - 8.0) < 0.05 and abs(lod_bounds[1][0] - 12.0) < 0.05, lod_bounds)
lod_doc = json.loads(Path(placed_lods[0].path).read_text())
accessor = lod_doc["accessors"][0]
import array as _array  # noqa: E402
_raw = _b64decode(lod_doc["buffers"][0]["uri"].split(",", 1)[1])
_floats = _array.array("f")
_floats.frombytes(_raw[:accessor["count"] * 12])
expect("LOD accessor min/max equal the stored float32 data (validator-clean)",
       accessor["min"][0] == min(_floats[0::3]) and accessor["max"][1] == max(_floats[1::3]))
p4_db, p4_data = fresh("phase4_ws")
placed_asset = aws.create_asset(p4_db, p4_data, "placed.gltf", (p4_dir / "placed.gltf").read_bytes())
seeded = aws.get_collision(p4_db, placed_asset["id"])["data"]
expect("the seeded collision box follows the node transform too",
       abs(seeded["min"][0] - 8.0) < 0.05 and abs(seeded["max"][0] - 12.0) < 0.05, seeded)

print("23. phase 4: network exposure and verdict provenance")
for bind, token, expected in (("127.0.0.1", "", "local"), ("0.0.0.0", "", "open"),
                              ("0.0.0.0", "t0ken", "token")):
    e_db, e_data = fresh(f"exposure_{expected}")
    e_server = aws.build_server(bind, 0, e_db, e_data, token=token, quiet=True)
    e_port = e_server.server_address[1]
    threading.Thread(target=e_server.serve_forever, daemon=True).start()
    time.sleep(0.1)
    e_client = Client(e_port, token=token)
    status, health = e_client.json("/api/health")
    expect(f"health says whether {bind} is reachable from the network",
           health.get("exposed") is (bind != "127.0.0.1"), health)
    status, body = e_client.json("/api/assets?filename=v.obj&verdict=passed&source=ci", "POST",
                                 OBJ.encode())
    expect(f"a verdict sent to a {expected!r} server is recorded as {expected!r}",
           body.get("provenance_auth") == expected, body.get("provenance_auth"))
    e_server.shutdown()
    e_server.server_close()

u_db, u_data = fresh("unicode_export")
u_asset = aws.create_asset(u_db, u_data, "岩石_rock.obj", OBJ.encode())
u_server = aws.build_server("127.0.0.1", 0, u_db, u_data, quiet=True)
u_port = u_server.server_address[1]
threading.Thread(target=u_server.serve_forever, daemon=True).start()
time.sleep(0.1)
status, body, headers = Client(u_port).call(f"/api/assets/{u_asset['id']}/collision.obj")
expect("a non-English asset's collision export downloads (was a 500)", status == 200, str(status))
expect("... named after the asset, RFC 5987-encoded",
       "filename*=UTF-8''" in (headers.get("Content-Disposition") or ""),
       str(headers.get("Content-Disposition")))
status, body, headers = Client(u_port).call(f"/api/assets/{u_asset['id']}/report")
expect("the printable report opens", status == 200 and b"<html" in body.lower(), str(status))
u_server.shutdown()
u_server.server_close()

print("24. healing: Check for fixes makes a verified copy, never touches the original")
h_db, h_data = fresh("heal")
_buf = _io.BytesIO()
Image.new("RGB", (1000, 600), (110, 100, 90)).save(_buf, format="PNG")
h_png = aws.create_asset(h_db, h_data, "bark.png", _buf.getvalue())
h_original = (h_data / "assets" / str(h_png["id"]) / "bark.png").read_bytes()
h_server = aws.build_server("127.0.0.1", 0, h_db, h_data, quiet=True)
h_port = h_server.server_address[1]
threading.Thread(target=h_server.serve_forever, daemon=True).start()
time.sleep(0.1)
h_client = Client(h_port)
status, preview = h_client.json(f"/api/assets/{h_png['id']}/heal")
expect("GET .../heal says what a fix would change", status == 200 and preview["healable"]
       and "renamed bark.png -> bark_Albedo.png" in preview["changes"]
       and "resized 1000x600 -> 1024x1024" in preview["changes"], preview)
expect("... names the budget it checked against", "default budget" in preview["budget"], preview)
expect("... and stores nothing",
       len(aws.list_assets(h_db)) == 1
       and (h_data / "assets" / str(h_png["id"]) / "bark.png").read_bytes() == h_original)
status, made = h_client.json(f"/api/assets/{h_png['id']}/heal", "POST", b"")
expect("POST .../heal makes the fixed copy as a new asset", status == 201
       and made["asset"]["filename"] == "bark_Albedo.png" and made["copy_id"] != h_png["id"],
       (status, made.get("error")))
copy = aws.get_asset(h_db, made.get("copy_id", 0)) if status == 201 else {}
expect("... linked to the original, with what changed",
       copy.get("healed_from") == str(h_png["id"]) and "resized" in copy.get("heal_notes", ""), copy)
expect("... verified by the gate, and says so",
       copy.get("verdict_status") == "passed" and copy.get("source") == "workstation healer")
expect("... measured as 1024x1024", copy.get("analyse_note", "").startswith("1024x1024"),
       copy.get("analyse_note"))
expect("the original is byte-identical",
       (h_data / "assets" / str(h_png["id"]) / "bark.png").read_bytes() == h_original)
status, again = h_client.json(f"/api/assets/{made.get('copy_id', 0)}/heal")
expect("the fixed copy has nothing left to fix", status == 200 and not again["healable"]
       and "already passes" in again["reason"], again)
status, body = h_client.json(f"/api/assets/{made.get('copy_id', 0)}/heal", "POST", b"")
expect("... and a second fix is refused", status == 409, (status, body))

h_obj = aws.create_asset(h_db, h_data, "glass.obj",
                         b"mtllib glass.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nv 2 0 0\nf 1 2 3\nf 1 2 4\n")
aws.add_sidecar(h_db, h_data, h_obj["id"], "glass.mtl", b"newmtl g\n")
status, made_obj = h_client.json(f"/api/assets/{h_obj['id']}/heal", "POST", b"")
expect("an .obj's zero-area faces are fixed, and its .mtl comes with the copy",
       status == 201 and "Stripped 1 degenerate face" in made_obj["changes"]
       and aws.asset_files(h_data, aws.get_asset(h_db, made_obj["copy_id"])) == ["glass.mtl", "glass.obj"],
       made_obj)
h_glb = aws.create_asset(h_db, h_data, "x.glb", b"glTF-not-really")
status, body = h_client.json(f"/api/assets/{h_glb['id']}/heal")
expect("other formats say what the healer can fix", status == 200 and not body["healable"]
       and ".png" in body["reason"], body)
status, body = h_client.json(f"/api/assets/{h_glb['id']}/heal", "POST", b"")
expect("... and refuse a POST", status == 422, (status, body))
_dark = _io.BytesIO()
Image.new("RGB", (1000, 1000), (0, 0, 0)).save(_dark, format="PNG")
h_dark = aws.create_asset(h_db, h_data, "dark.png", _dark.getvalue())
status, body = h_client.json(f"/api/assets/{h_dark['id']}/heal")
expect("a fix that would still fail says why",
       status == 200 and not body["healable"] and "luminance" in body["reason"], body)
status, body = h_client.call(f"/api/assets/{h_png['id']}/heal", "POST", b"",
                             headers={"Origin": "https://evil.example"})[:2]
expect("another website cannot trigger a fix", status == 403, str(status))
expect("no backups are left in the workstation's data folder",
       not list(h_data.rglob(".agk-backups")) and not list(h_data.rglob(".agk-heal-*")))
_rules_dir = WORK / "heal_rules"
_rules_dir.mkdir(exist_ok=True)
(_rules_dir / "rules.toml").write_text("[texture]\nmax_edge = 512\n", encoding="utf-8")
status_budget = aws.heal_asset(h_db, h_data, h_png["id"], apply=False, rules_root=str(_rules_dir))
expect("with a project rules.toml, the fix is checked against it (and says so)",
       "rules.toml" in status_budget["budget"] and not status_budget["healable"]
       and "512" in status_budget["reason"], status_budget)
h_server.shutdown()
h_server.server_close()

print("29. textured FBX previews (W7)")
t_db, t_data = fresh("fbx_textured")
_fx = ROOT / "tests" / "fixtures" / "fbx"
t_asset = aws.create_asset(t_db, t_data, "tex_relative.fbx", (_fx / "tex_relative.fbx").read_bytes())
aws.add_sidecar(t_db, t_data, t_asset["id"], "tex_relative_wood_Albedo.png",
                (_fx / "tex_relative_wood_Albedo.png").read_bytes())
_glb = (t_data / "assets" / str(t_asset["id"]) / aws.PREVIEW_DIR / "preview.glb")
_glb = _glb if _glb.is_file() else next((t_data / "assets" / str(t_asset["id"])).rglob("*.glb"))
_raw = _glb.read_bytes()
_doc = json.loads(_raw[20:20 + struct.unpack("<I", _raw[12:16])[0]])
_attrs = _doc["meshes"][0]["primitives"][0]["attributes"]
expect("the converted preview carries UVs and normals", "TEXCOORD_0" in _attrs and "NORMAL" in _attrs, _attrs)
expect("... and the base-colour texture, by the path it is stored under",
       _doc.get("images") == [{"uri": "tex_relative_wood_Albedo.png"}]
       and "baseColorTexture" in _doc["materials"][0]["pbrMetallicRoughness"], _doc.get("images"))
expect("the triangle count is still the gate's", aws.get_asset(t_db, t_asset["id"])["triangle_count"] == 12)
expect("the banner says what the preview now shows",
       "base-colour textures" in aws.fbx_preview.PREVIEW_WARNING and "Geometry only" not in aws.fbx_preview.PREVIEW_WARNING)

import os  # noqa: E402
import subprocess  # noqa: E402
print("30. notes pinned to the model (W5)")
pin_db, pin_data = fresh("pins")
pin_asset = aws.create_asset(pin_db, pin_data, "crate.obj", OBJ.encode())
_shot = "data:image/png;base64," + base64.b64encode(png_bytes((200, 40, 40))).decode()
_view = {"point": [0.5, 1.0, 0.5], "normal": [0, 1, 0],
         "camera": {"position": [2.4, 1.8, 3.2], "target": [0.5, 0.5, 0.5], "fov": 45},
         "level": "source"}
_first_pin = aws.add_note(pin_db, pin_asset["id"], "Geometry", "seam on the lid", "ana",
                          pin=_view, screenshot=_shot, data_dir=pin_data)
_plain = aws.add_note(pin_db, pin_asset["id"], "Other", "no pin here", "ana")
_second_pin = aws.add_note(pin_db, pin_asset["id"], "Texture", "stretched corner", "ana",
                           pin=dict(_view, point=[1, 0, 1]), data_dir=pin_data)
_listed = {note["id"]: note for note in aws.list_notes(pin_db, pin_asset["id"])}
expect("a pinned note keeps its point, normal, camera and level",
       _listed[_first_pin["id"]]["pin"]["point"] == [0.5, 1.0, 0.5]
       and _listed[_first_pin["id"]]["pin"]["camera"]["target"] == [0.5, 0.5, 0.5]
       and _listed[_first_pin["id"]]["pin"]["camera"]["fov"] == 45
       and _listed[_first_pin["id"]]["pin"]["level"] == "source", _listed[_first_pin["id"]])
expect("pins are numbered oldest first, skipping unpinned notes",
       _listed[_first_pin["id"]]["pin"]["number"] == 1 and _listed[_second_pin["id"]]["pin"]["number"] == 2
       and _listed[_plain["id"]]["pin"] is None)
expect("the screenshot is stored and served by URL, a note without one has none",
       _listed[_first_pin["id"]]["screenshot"] == f"/api/notes/{_first_pin['id']}/screenshot"
       and _listed[_second_pin["id"]]["screenshot"] == "")
_shot_file = aws.note_screenshot(pin_db, pin_data, _first_pin["id"])
expect("... as the PNG that was sent", _shot_file.read_bytes() == png_bytes((200, 40, 40)))
for _bad_pin, _why in ((dict(_view, point=[1, 2]), "two numbers"),
                       (dict(_view, point=[1, 2, float("nan")]), "NaN"),
                       (dict(_view, camera={"position": [0, 0, 1]}), "no camera target"),
                       (dict(_view, camera=dict(_view["camera"], fov=500)), "fov 500"),
                       (dict(_view, level="../x"), "a made-up level"),
                       (dict(_view, colour="red"), "an unknown field"),
                       ("here", "not an object")):
    try:
        aws.add_note(pin_db, pin_asset["id"], "Other", "x", pin=_bad_pin, data_dir=pin_data)
        expect(f"a bad pin is refused ({_why})", False)
    except aws.WorkstationError as exc:
        expect(f"a bad pin is refused ({_why})", exc.status == 400, str(exc))
for _bad_shot, _status, _why in (("data:image/gif;base64,R0lGODlhAQABAAAAACw=", 400, "a GIF"),
                                 ("data:image/png;base64," + base64.b64encode(b"<svg/>").decode(), 400,
                                  "PNG by name, not by its bytes"),
                                 ("data:image/png;base64,***", 400, "not base64"),
                                 ("data:image/jpeg;base64," + base64.b64encode(
                                     b"\xff\xd8\xff" + b"0" * 700_000).decode(), 413, "too large")):
    try:
        aws.add_note(pin_db, pin_asset["id"], "Other", "x", pin=_view, screenshot=_bad_shot,
                     data_dir=pin_data)
        expect(f"a bad screenshot is refused ({_why})", False)
    except aws.WorkstationError as exc:
        expect(f"a bad screenshot is refused ({_why})", exc.status == _status, str(exc))
expect("... and nothing was stored for them",
       len(aws.list_notes(pin_db, pin_asset["id"])) == 3)
_pin_report = aws.render_report(pin_db, pin_data, pin_asset["id"])
expect("the printable report shows the pin number and embeds the view (one self-contained file)",
       "#1</span>" in _pin_report and "data:image/png;base64," in _pin_report
       and "/api/notes/" not in _pin_report)
pin_server = aws.build_server("127.0.0.1", 0, pin_db, pin_data, quiet=True)
pin_port = pin_server.server_address[1]
threading.Thread(target=pin_server.serve_forever, daemon=True).start()
time.sleep(0.1)
_status, _raw, _headers = Client(pin_port).call(f"/api/notes/{_first_pin['id']}/screenshot")
expect("GET /api/notes/<id>/screenshot serves the image",
       _status == 200 and _raw == png_bytes((200, 40, 40)) and _headers.get("Content-Type", "").startswith("image/png"),
       (_status, _headers.get("Content-Type")))
_status, _ = Client(pin_port).json(f"/api/notes/{_plain['id']}/screenshot")
expect("... and 404 for a note without one", _status == 404)
_status, _posted = Client(pin_port).json("/api/notes", method="POST", body={
    "asset_id": pin_asset["id"], "category": "LOD", "note_text": "over HTTP", "pin": _view,
    "screenshot": _shot})
expect("POST /api/notes takes a pin and a screenshot",
       _status == 201 and _posted["pin"]["number"] == 3 and _posted["screenshot"], _posted)
_status, _posted_bad = Client(pin_port).json("/api/notes", method="POST", body={
    "asset_id": pin_asset["id"], "category": "LOD", "note_text": "bad", "pin": {"point": "x"}})
expect("... and answers a bad pin with 400", _status == 400, _posted_bad)
_status, _ = Client(pin_port).json(f"/api/notes/{_first_pin['id']}", method="DELETE")
expect("deleting a note deletes its screenshot",
       _status == 200 and not _shot_file.exists())
_left = aws.note_screenshot(pin_db, pin_data, _posted["id"])
_status, _ = Client(pin_port).json(f"/api/assets/{pin_asset['id']}", method="DELETE")
expect("deleting the asset deletes its notes' screenshots", _status == 200 and not _left.exists())
pin_server.shutdown()
pin_server.server_close()
_old_db = WORK / "pins_old.db"
_old_db.unlink(missing_ok=True)
import sqlite3 as _sqlite3  # noqa: E402
with _sqlite3.connect(_old_db) as _old:
    _old.execute("CREATE TABLE reviews_and_notes (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " asset_id INTEGER NOT NULL, author TEXT NOT NULL DEFAULT '', category TEXT NOT NULL,"
                 " note_text TEXT NOT NULL, created_at TEXT NOT NULL)")
    _old.execute("INSERT INTO reviews_and_notes (asset_id, category, note_text, created_at)"
                 " VALUES (1, 'Other', 'from 1.1', '2026-11-01T10:00:00+00:00')")
aws.init_db(_old_db)
with aws.open_db(_old_db) as _conn:
    _cols = {row["name"] for row in _conn.execute("PRAGMA table_info(reviews_and_notes)")}
    _kept = _conn.execute("SELECT note_text FROM reviews_and_notes").fetchone()[0]
expect("an older library gains the pin columns and keeps its notes",
       {"pin_json", "screenshot"} <= _cols and _kept == "from 1.1", _cols)

print("25. versions (W1) and where a file came from (W8)")
v_db, v_data = fresh("versions")
v1 = aws.create_asset(v_db, v_data, "rock.obj", OBJ.encode())
v2 = aws.create_asset(v_db, v_data, "rock.obj", OBJ.encode() + b"\nv 9 9 9\n")
expect("the same name uploaded again becomes v2 of the same asset (was a stranger)",
       v2["version"] == 2 and v2["family_id"] == v1["family_id"], (v1["family_id"], v2))
expect("... and the library shows the newest version once",
       [a["id"] for a in aws.list_assets(v_db)] == [v2["id"]]
       and aws.list_assets(v_db)[0]["version_count"] == 2)
expect("... while every version is still there on request",
       len(aws.list_assets(v_db, all_versions=True)) == 2)
same = aws.create_asset(v_db, v_data, "rock.obj", OBJ.encode() + b"\nv 9 9 9\n")
expect("re-sending the newest version's exact bytes adds nothing",
       same.get("unchanged") and same["id"] == v2["id"]
       and len(aws.list_assets(v_db, all_versions=True)) == 2)
again_v1 = aws.create_asset(v_db, v_data, "rock.obj", OBJ.encode())
expect("... but going back to v1's bytes is a new version (v3)", again_v1["version"] == 3)
apart = aws.create_asset(v_db, v_data, "rock.obj", OBJ.encode() + b"\n# other\n", new_asset=True)
expect("new_asset forces a separate asset", apart["family_id"] != v1["family_id"]
       and apart["version"] == 1)
detail = aws.asset_detail(v_db, v_data, v2["id"])
expect("an asset's detail lists every version, oldest first",
       [item["version"] for item in detail["versions"]] == [1, 2, 3], detail["versions"])
expect("old links still open their own version",
       aws.asset_detail(v_db, v_data, v1["id"])["version"] == 1)
stats = aws.repository_stats(v_db)
expect("stats count the newest version of each asset, not every re-export",
       stats["assets"] == 2 and stats["versions"] == 4, stats)
d_props = aws.create_asset(v_db, v_data, "rock.obj", OBJ.encode() + b"# props\n",
                           source={"path": "//depot/Art/Props/rock.obj", "changelist": "1234"})
d_env = aws.create_asset(v_db, v_data, "rock.obj", OBJ.encode() + b"# env\n",
                         source={"path": "//depot/Art/Env/rock.obj"})
expect("with depot paths, two rock.obj in different folders are different assets",
       d_props["family_id"] != d_env["family_id"] and d_props["family_id"] != v1["family_id"])
expect("the depot path and changelist are stored",
       d_props["source_path"] == "//depot/Art/Props/rock.obj" and d_props["changelist"] == "1234")
d_props2 = aws.create_asset(v_db, v_data, "rock.obj", OBJ.encode() + b"# props 2\n",
                            source={"path": "//depot/Art/Props/rock.obj", "changelist": "1240"})
expect("the next submit of that depot path is its v2", d_props2["version"] == 2
       and d_props2["family_id"] == d_props["family_id"] and d_props2["changelist"] == "1240")
expect("control characters never reach the stored path",
       aws.create_asset(v_db, v_data, "x.obj", OBJ.encode() + b"#x\n",
                        source={"path": "//depot/a\x1b[31m.obj"})["source_path"] == "//depot/a[31m.obj")
aws.delete_asset(v_db, v_data, again_v1["id"])
expect("deleting the newest version shows the one before it again",
       any(a["id"] == v2["id"] for a in aws.list_assets(v_db)))
# a library from before versions existed
legacy_db = WORK / "legacy_versions.db"
legacy_db.unlink(missing_ok=True)
import sqlite3 as _sqlite3  # noqa: E402
_legacy = _sqlite3.connect(legacy_db)
_legacy.executescript("CREATE TABLE assets (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,"
                      " file_path TEXT NOT NULL, filesize INTEGER NOT NULL, triangle_count INTEGER NOT NULL,"
                      " vertex_count INTEGER NOT NULL, material_slots INTEGER NOT NULL,"
                      " uploader TEXT NOT NULL DEFAULT '', uploaded_at TEXT NOT NULL);"
                      "INSERT INTO assets (filename, file_path, filesize, triangle_count, vertex_count,"
                      " material_slots, uploaded_at) VALUES ('old.obj', 'old.obj', 1, 12, 8, 1, '2026-01-01');")
_legacy.commit()
_legacy.close()
aws.init_db(legacy_db)
_old = aws.list_assets(legacy_db)
expect("an old library opens with every asset at v1 of its own family",
       len(_old) == 1 and _old[0]["version"] == 1 and _old[0]["family_id"] == _old[0]["id"], _old)

v_server = aws.build_server("127.0.0.1", 0, v_db, v_data, quiet=True)
v_port = v_server.server_address[1]
threading.Thread(target=v_server.serve_forever, daemon=True).start()
time.sleep(0.1)
vc = Client(v_port)
status, up = vc.json("/api/assets?filename=crate.obj&source_path=//depot/Art/crate.obj&changelist=77",
                     "POST", OBJ.encode())
expect("the API takes source_path and changelist", status == 201
       and up["source_path"] == "//depot/Art/crate.obj" and up["changelist"] == "77", up)
status, again = vc.json("/api/assets?filename=crate.obj&source_path=//depot/Art/crate.obj", "POST",
                        OBJ.encode())
expect("an unchanged re-send answers 200 with unchanged: true", status == 200 and again["unchanged"],
       (status, again.get("unchanged")))
status, nv = vc.json(f"/api/assets?filename=crate_fixed.obj&version_of={up['id']}", "POST",
                     OBJ.encode() + b"# fixed\n")
expect("version_of adds a version under another file name", status == 201
       and nv["family_id"] == up["family_id"] and nv["version"] == 2, nv)
status, body = vc.json("/api/assets?filename=a.obj&version_of=x", "POST", OBJ.encode())
expect("a bad version_of is a 400", status == 400, (status, body))
status, every = vc.json("/api/assets?all_versions=1")
expect("GET /api/assets?all_versions=1 lists every version",
       len(every["assets"]) > len(vc.json("/api/assets")[1]["assets"]))

print("26. sign-off (W2)")
status, body = vc.json(f"/api/assets/{nv['id']}/review", "POST", {"status": "approved"})
expect("without accounts, a sign-off needs a typed name", status == 400 and "reviewer" in body["error"],
       body)
status, review = vc.json(f"/api/assets/{nv['id']}/review", "POST",
                         {"status": "changes_requested", "note": "seam on the **left**",
                          "reviewer": "Alex"})
expect("a typed-name sign-off is recorded, marked not verified",
       status == 201 and review["status"] == "changes_requested" and review["verified"] is False
       and review["reviewer"] == "Alex", review)
status, body = vc.json(f"/api/assets/{nv['id']}/review", "POST",
                       {"status": "shipped", "reviewer": "Alex"})
expect("an unknown status is refused", status == 400)
vc.json(f"/api/assets/{nv['id']}/review", "POST", {"status": "approved", "reviewer": "Alex"})
d = aws.asset_detail(v_db, v_data, nv["id"])
expect("the newest decision wins and the history stays",
       d["review"]["status"] == "approved" and [r["status"] for r in d["reviews"]]
       == ["approved", "changes_requested"], d["reviews"])
expect("the library shows each asset's review status",
       next(a for a in aws.list_assets(v_db) if a["id"] == nv["id"])["review_status"] == "approved")
expect("the version list carries review status too",
       next(item for item in d["versions"] if item["id"] == nv["id"])["review_status"] == "approved")
expect("stats count assets by review status", aws.repository_stats(v_db)["reviews"].get("approved") == 1)
import hashlib as _hashlib  # noqa: E402
_approved = _hashlib.sha256(OBJ.encode() + b"# fixed\n").hexdigest()
status, approval = vc.json(f"/api/approvals/{_approved}")
expect("GET /api/approvals/<sha256> says an approved file is approved",
       status == 200 and approval["approved"] and approval["reviewer"] == "Alex", approval)
status, approval = vc.json(f"/api/approvals/{_hashlib.sha256(OBJ.encode()).hexdigest()}")
expect("... and why another file is not", status == 200 and not approval["approved"]
       and "not been reviewed" in approval["reason"], approval)
status, approval = vc.json(f"/api/approvals/{'0' * 64}")
expect("... including a file it has never seen", not approval["approved"]
       and "never been sent" in approval["reason"])
status, _ = vc.call(f"/api/assets/{nv['id']}/review", "POST", b'{"status":"approved","reviewer":"x"}',
                    headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})[:2]
expect("another website cannot sign off", status == 403)

print("27. named reviewers (W3)")
a_db, a_data = fresh("accounts")
for _bad, _why in ((("ab", "viewer", "short"), "short password"),
                   (("a b", "viewer", "long enough pw"), "space in the name"),
                   (("lead1", "boss", "long enough pw"), "unknown role")):
    try:
        aws.add_account(a_db, *_bad)
        expect(f"a bad account is refused ({_why})", False)
    except aws.WorkstationError:
        expect(f"a bad account is refused ({_why})", True)
_proc = subprocess.run([sys.executable, str(ROOT / "server" / "art_workstation_server.py"),
                        "--db", str(a_db), "--data", str(a_data), "--add-user", "lead1",
                        "--role", "lead", "--password-stdin"],
                       input="lead-password-1\n", capture_output=True, text=True, timeout=60)
expect("--add-user creates an account from the command line",
       _proc.returncode == 0 and "added lead1 (lead)" in _proc.stdout, _proc.stdout + _proc.stderr)
aws.add_account(a_db, "rev1", "reviewer", "rev-password-1", display_name="Rita Reviewer")
aws.add_account(a_db, "view1", "viewer", "view-password-1")
_stored = aws.list_accounts(a_db)
expect("passwords are never stored as typed",
       "lead-password-1" not in (a_db.read_bytes() if a_db.exists() else b"").decode("latin-1"))
a_asset = aws.create_asset(a_db, a_data, "crate.obj", OBJ.encode())
a_server = aws.build_server("127.0.0.1", 0, a_db, a_data, token="svc-token", quiet=True)
a_port = a_server.server_address[1]
threading.Thread(target=a_server.serve_forever, daemon=True).start()
time.sleep(0.1)


class Browser(Client):
    """A client that keeps the session cookie, like a browser tab."""

    def __init__(self, port):
        super().__init__(port)
        self.cookie = ""

    def call(self, path, method="GET", body=None, headers=None):
        merged = dict(headers or {})
        if self.cookie:
            merged["Cookie"] = self.cookie
        status, raw, response_headers = super().call(path, method, body, merged)
        cookie = response_headers.get("Set-Cookie", "")
        if cookie.startswith(aws.SESSION_COOKIE + "="):
            self.cookie = cookie.split(";", 1)[0]
        return status, raw, response_headers


anon = Browser(a_port)
status, me = anon.json("/api/me")
expect("/api/me says accounts are on and nobody is signed in",
       status == 200 and me["accounts"] and not me["signed_in"] and me["role"] is None, me)
status, _ = anon.json("/api/assets")
expect("with accounts on, the library needs a sign-in", status == 401)
status, raw, headers = anon.call("/api/session", "POST", {"username": "rev1", "password": "wrong-pass"})
expect("a wrong password is refused without saying which part was wrong",
       status == 401 and b"wrong username or password" in raw)
status, raw, headers = anon.call("/api/session", "POST", {"username": "nobody", "password": "wrong-pass"})
expect("... and an unknown user gets the same answer", status == 401 and b"wrong username or password" in raw)
rita = Browser(a_port)
status, raw, headers = rita.call("/api/session", "POST", {"username": "REV1", "password": "rev-password-1"})
cookie_header = headers.get("Set-Cookie", "")
expect("signing in sets an HttpOnly, SameSite=Strict session cookie (usernames ignore case)",
       status == 200 and "HttpOnly" in cookie_header and "SameSite=Strict" in cookie_header, cookie_header)
status, me = rita.json("/api/me")
expect("/api/me names the signed-in reviewer and what they can do",
       me["signed_in"] and me["user"]["display_name"] == "Rita Reviewer" and "review" in me["can"]
       and "delete" not in me["can"], me)
status, note = rita.json("/api/notes", "POST", {"asset_id": a_asset["id"], "category": "Geometry",
                                                "note_text": "ok", "author": "Somebody Else"})
expect("a signed-in note is written by the account, whatever name is typed",
       status == 201 and note["author"] == "Rita Reviewer", note)
status, review = rita.json(f"/api/assets/{a_asset['id']}/review", "POST",
                           {"status": "approved", "reviewer": "Somebody Else"})
expect("a signed-in sign-off is verified and carries the account's name",
       status == 201 and review["verified"] and review["reviewer"] == "Rita Reviewer", review)
status, body = rita.json(f"/api/assets/{a_asset['id']}", "DELETE")
expect("a reviewer cannot delete assets", status == 403 and "reviewer" in body["error"], body)
vera = Browser(a_port)
vera.call("/api/session", "POST", {"username": "view1", "password": "view-password-1"})
status, body = vera.json(f"/api/assets/{a_asset['id']}/review", "POST", {"status": "approved"})
expect("a viewer cannot sign off", status == 403, body)
status, _ = vera.json("/api/notes", "POST", {"asset_id": a_asset["id"], "category": "Other",
                                             "note_text": "looks fine"})
expect("... but can write notes", status == 201)
status, _ = vera.call("/api/assets?filename=v.obj", "POST", OBJ.encode())[:2]
expect("... and cannot upload", status == 403)
service = Client(a_port, token="svc-token")
status, up = service.json("/api/assets?filename=from_script.obj", "POST", OBJ.encode() + b"#s\n")
expect("with accounts on, the token still uploads (scripts, AssetHealth, P4)", status == 201, up)
status, body = service.json(f"/api/assets/{a_asset['id']}/review", "POST",
                            {"status": "approved", "reviewer": "bot"})
expect("... but the token can never sign off", status == 403, body)
status, _ = service.json(f"/api/assets/{a_asset['id']}", "DELETE")
expect("... or delete", status == 403)
lena = Browser(a_port)
lena.call("/api/session", "POST", {"username": "lead1", "password": "lead-password-1"})
status, _ = lena.json(f"/api/assets/{up['id']}", "DELETE")
expect("a lead can delete", status == 200)
status, _ = rita.call("/api/session", "DELETE")[:2]
status, _ = rita.json("/api/assets")
expect("signing out ends the session", status == 401)
aws.set_account(a_db, "lead1", password="lead-password-2")
status, _ = lena.json("/api/assets")
expect("a new password signs the account out everywhere", status == 401)
_flood = Browser(a_port)
for _ in range(aws.LOGIN_FAILURES):
    _flood.call("/api/session", "POST", {"username": "view1", "password": "guess-guess"})
status, raw, _ = _flood.call("/api/session", "POST", {"username": "view1", "password": "view-password-1"})
expect("after repeated failures, sign-in is paused (429) even with the right password",
       status == 429, (status, raw[:80]))
status, _ = anon.call("/api/session", "POST", {"username": "view1", "password": "x" * 12},
                      headers={"Origin": "https://evil.example"})[:2]
expect("another website cannot sign anyone in", status == 403)
_listed = subprocess.run([sys.executable, str(ROOT / "server" / "art_workstation_server.py"),
                          "--db", str(a_db), "--list-users"], capture_output=True, text=True, timeout=60)
expect("--list-users lists accounts and roles", "rev1" in _listed.stdout and "reviewer" in _listed.stdout
       and "password" not in _listed.stdout.lower(), _listed.stdout)
for _name in ("lead1", "rev1", "view1"):
    aws.remove_account(a_db, _name)
status, _ = Client(a_port, token="svc-token").json("/api/assets")
expect("with every account removed, the server is back in token mode", status == 200)
a_server.shutdown()
a_server.server_close()

print("28. the bulk uploader and the P4 trigger record where files came from (W8)")
_bulk = WORK / "bulk_depot"
(_bulk / "Props").mkdir(parents=True, exist_ok=True)
(_bulk / "Props" / "barrel.obj").write_text(OBJ)
_bulk_run = subprocess.run([sys.executable, str(ROOT / "tools" / "upload_assets.py"), str(_bulk),
                            "--workstation", f"http://127.0.0.1:{v_port}",
                            "--depot-root", "//depot/Art", "--changelist", "991"],
                           capture_output=True, text=True, timeout=120)
_barrel = next((a for a in aws.list_assets(v_db) if a["filename"] == "barrel.obj"), {})
expect("--depot-root records //depot/Art/<relative path> and the changelist",
       _barrel.get("source_path") == "//depot/Art/Props/barrel.obj" and _barrel.get("changelist") == "991",
       (_barrel, _bulk_run.stdout[-200:], _bulk_run.stderr[-200:]))
_bulk_again = subprocess.run([sys.executable, str(ROOT / "tools" / "upload_assets.py"), str(_bulk),
                              "--workstation", f"http://127.0.0.1:{v_port}", "--depot-root", "//depot/Art"],
                             capture_output=True, text=True, timeout=120)
expect("running it again reports the file unchanged", "unchanged" in _bulk_again.stdout,
       _bulk_again.stdout[-200:])
v_server.shutdown()
v_server.server_close()



def startup_stderr(*extra):
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "art_workstation_server.py"), "--port", "0",
         "--port-auto", "--db", str(WORK / "warn.db"), "--data", str(WORK / "warn_data"), *extra],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={key: value for key, value in os.environ.items() if key != "AUTH_TOKEN"})
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.terminate()
    return process.communicate(timeout=10)[1]


expect("starting on the network without a token prints a warning",
       "WARNING: listening on the network with no token" in startup_stderr("--host", "0.0.0.0"))
expect("... and not on loopback", "WARNING" not in startup_stderr("--host", "127.0.0.1"))
expect("... and not with a token",
       "WARNING" not in startup_stderr("--host", "0.0.0.0", "--token", "s3cret"))

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
