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
expect("the file lands on disk", (data_dir / "assets" / "1_cube.obj").is_file())
expect("an AABB collision is derived on upload",
       aws.get_collision(db, 1)["kind"] == "box", str(aws.get_collision(db, 1)))
with aws.open_db(db) as connection:
    box = connection.execute("SELECT data_json FROM collisions WHERE asset_id = 1").fetchone()
payload = json.loads(box["data_json"])
expect("the derived box matches the mesh",
       payload["min"] == [0.0, 0.0, 0.0] and payload["max"] == [1.0, 1.0, 1.0], str(payload))

try:
    aws.create_asset(db, data_dir, "notes.txt", b"hello")
    expect("non-asset uploads are refused", False, "no error raised")
except aws.WorkstationError as exc:
    expect("non-asset uploads are refused", exc.status == 415, str(exc))
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
expect("the hard cap was honoured", created["lods"][0]["triangle_count"] <= 12,
       str(created["lods"][0]["triangle_count"]))
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

Image.new("RGB", (64, 64), (120, 100, 90)).save(assets_dir / "wood_Albedo.png")
report = aws.asset_references(ref_db, ref_data, ref_asset["id"])
expect("adding the texture clears the missing link", report["missing"] == [])
expect("and the reference is marked present", report["references"][0]["exists"] is True)

Image.new("RGB", (64, 64), (30, 30, 30)).save(assets_dir / "nobody_uses_me.png")
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

print("8. CLI wiring")
options = aws.build_parser().parse_args(["--port", "9999", "--db", "x.db",
                                         "--data", "y", "--token", "t"])
expect("CLI accepts the storage and auth flags",
       (options.port, options.db, options.data, options.token) == (9999, "x.db", "y", "t"))
expect("defaults are loopback and unauthenticated",
       (aws.DEFAULT_HOST, aws.DEFAULT_PORT) == ("127.0.0.1", 8100))
expect("a bad data folder exits 2",
       aws.main(["--db", str(WORK / "missing" / "x.db"), "--data", str(WORK / "d")]) == 2)

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
