"""Drive the Art Workstation viewer in a real browser (Chromium via Playwright).

The API suite (test_workstation.py) passed while the viewer showed the wrong
model, refused embedded glTF buffers and crashed on Geometry -> Textured: none
of that is visible without a browser. Every check here is a bug found by
driving the page on 2026-11-02.

Needs ``pip install playwright`` and ``python -m playwright install chromium``.
Without them the suite says so and exits 0 (CI installs both and runs it).
"""
import http.server
import io
import json
import shutil
import socketserver
import struct
import sys
import threading
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

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIPPED: playwright is not installed (pip install playwright && "
          "python -m playwright install chromium)")
    raise SystemExit(0)

import art_workstation_server as aws  # noqa: E402
import lod_manager  # noqa: E402

WORK = Path("scratch") / "viewer_browser"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)
failures = []


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(label)


# --- fixtures: every kind of asset the audit tripped over --------------------
CUBE = lod_manager.Mesh(
    [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)],
    [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
     (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)])


def sphere(rings=24, segments=48):
    import math
    points = [(0.0, 1.0, 0.0)]
    for ring in range(1, rings):
        phi = math.pi * ring / rings
        for segment in range(segments):
            theta = 2 * math.pi * segment / segments
            points.append((math.sin(phi) * math.cos(theta), math.cos(phi),
                           math.sin(phi) * math.sin(theta)))
    points.append((0.0, -1.0, 0.0))
    faces = []
    for segment in range(segments):
        faces.append((0, 1 + (segment + 1) % segments, 1 + segment))
    for ring in range(rings - 2):
        for segment in range(segments):
            a = 1 + ring * segments + segment
            b = 1 + ring * segments + (segment + 1) % segments
            faces += [(a, b, b + segments), (a, b + segments, a + segments)]
    last = len(points) - 1
    base = 1 + (rings - 2) * segments
    for segment in range(segments):
        faces.append((last, base + segment, base + (segment + 1) % segments))
    return lod_manager.Mesh(points, faces)


lod_manager.write_glb(WORK / "cube.glb", CUBE, "cube")
lod_manager.write_gltf(WORK / "embedded.gltf", sphere(), "embedded")   # data: URI buffer
lod_manager.write_obj(WORK / "ball.obj", sphere(), "ball")


def png(width, height):
    import zlib
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


(WORK / "rock_Albedo.png").write_bytes(png(64, 64))
(WORK / "tool.exe").write_bytes(b"MZ")
(WORK / "second.glb").write_bytes((WORK / "cube.glb").read_bytes())
lod_manager.write_glb(WORK / "one_tri.glb",
                      lod_manager.Mesh([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)]), "lod")
(WORK / "ball_LOD3.glb").write_bytes((WORK / "one_tri.glb").read_bytes())
FBX = ROOT / "tests" / "fixtures" / "fbx" / "cube_subd3.fbx"
(WORK / "scene.blend").write_bytes(b"BLENDER-v300" + b"\x00" * 64)
(WORK / "junk.glb").write_bytes(b"\x13\x37" * 300)

# A .gltf whose buffer is a separate rock.bin, as Blender exports it.
import base64  # noqa: E402
_doc = json.loads((WORK / "embedded.gltf").read_text())
_blob = base64.b64decode(_doc["buffers"][0]["uri"].split(",", 1)[1])
_doc["buffers"][0]["uri"] = "rock.bin"
(WORK / "rock.gltf").write_text(json.dumps(_doc))
(WORK / "rock.bin").write_bytes(_blob)
# The same geometry under another name: a .gltf uploaded without its .bin.
(WORK / "boulder.gltf").write_text(json.dumps(_doc))

# An .obj with its .mtl and texture.
_obj = (WORK / "ball.obj").read_text().replace("v ", "mtllib crate.mtl\nusemtl planks\nv ", 1)
(WORK / "crate.obj").write_text(_obj)
(WORK / "crate.mtl").write_text("newmtl planks\nKd 0.8 0.5 0.2\nmap_Kd planks_Albedo.png\n")
(WORK / "planks_Albedo.png").write_bytes(png(32, 32))

from PIL import Image  # noqa: E402
Image.new("RGB", (32, 32), (90, 120, 60)).save(WORK / "stone_Albedo.tga")

db = WORK / "ws.db"
data = WORK / "data"
aws.init_db(db)
ids = {}
ids["cube"] = aws.create_asset(db, data, "cube.glb", (WORK / "cube.glb").read_bytes())["id"]
ids["embedded"] = aws.create_asset(db, data, "embedded.gltf",
                                   (WORK / "embedded.gltf").read_bytes())["id"]
ids["ball"] = aws.create_asset(db, data, "ball.obj", (WORK / "ball.obj").read_bytes(),
                               provenance={"verdict": "FAILED", "problems": "too heavy",
                                           "source": "cli"})["id"]
ids["fbx"] = aws.create_asset(db, data, "cube_subd3.fbx", FBX.read_bytes())["id"]
ids["png"] = aws.create_asset(db, data, "rock_Albedo.png",
                              (WORK / "rock_Albedo.png").read_bytes())["id"]
aws.generate_lods(db, data, ids["ball"], [0.5, 0.25])
aws.generate_lods(db, data, ids["embedded"], [0.5])
aws.generate_lods(db, data, ids["fbx"], [0.5])
ids["blend"] = aws.create_asset(db, data, "scene.blend", (WORK / "scene.blend").read_bytes())["id"]
ids["junk"] = aws.create_asset(db, data, "junk.glb", (WORK / "junk.glb").read_bytes())["id"]
ids["tga"] = aws.create_asset(db, data, "stone_Albedo.tga",
                              (WORK / "stone_Albedo.tga").read_bytes())["id"]

server = aws.build_server("127.0.0.1", 0, db, data, quiet=True)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"


class Evil(http.server.SimpleHTTPRequestHandler):
    """Another site the reviewer happens to have open."""

    def do_GET(self):  # noqa: N802
        body = ("<!doctype html><script>"
                f"fetch('{BASE}/api/notes', {{method: 'POST', mode: 'no-cors',"
                " headers: {'Content-Type': 'text/plain'},"
                f" body: JSON.stringify({{asset_id: {ids['cube']}, category: 'Other',"
                " note_text: 'written by another website'})});"
                "</script>evil").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class QuietServer(socketserver.TCPServer):
    def handle_error(self, request, client_address):   # the browser hanging up is fine
        pass


evil = QuietServer(("127.0.0.1", 0), Evil)
threading.Thread(target=evil.serve_forever, daemon=True).start()


def notes_of(asset_id):
    with urllib.request.urlopen(f"{BASE}/api/assets/{asset_id}/notes") as response:
        return [note["note_text"] for note in json.loads(response.read())["notes"]]


with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page_errors, console_errors = [], []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("console", lambda message: console_errors.append(message.text)
            if message.type == "error" else None)

    def status():
        return page.locator("#status").inner_text()

    def readout():
        return page.locator("#readout").inner_text().replace("\n", " ")

    def hint_visible():
        return page.locator("#viewport-hint").is_visible()

    def pick(asset_id):
        # Wait for *this* asset: the status bar names it once it has settled.
        name = page.locator(f'#asset-list .asset[data-id="{asset_id}"] .name').inner_text()
        name = name.replace("FAILED", "").replace("PASSED", "").strip()
        page.locator(f'#asset-list .asset[data-id="{asset_id}"]').click()
        page.wait_for_function(
            "(name) => { const s = document.getElementById('status').textContent;"
            " return s.includes(name) && !s.startsWith('loading'); }",
            arg=name, timeout=15000)

    page.goto(f"{BASE}/viewer?live=0")
    page.wait_for_selector("#asset-list .asset")

    print("1. every previewable format loads")
    pick(ids["cube"])
    expect("a .glb loads", status() == "cube.glb" and not hint_visible(), status())
    expect("its readout is its own (12 tris)", "12 tris" in readout(), readout())
    pick(ids["embedded"])
    expect("a .gltf with an embedded data: buffer loads (CSP refused it)",
           status() == "embedded.gltf" and not hint_visible(), status())
    page.locator("#lod-buttons button[data-level='LOD1']").click()
    page.wait_for_timeout(800)
    expect("a .gltf LOD loads", not status().startswith("error"), status())
    pick(ids["ball"])
    page.locator("#lod-buttons button[data-level='LOD1']").click()
    page.wait_for_timeout(800)
    expect("an .obj LOD loads (was parsed as glTF)", not status().startswith("error")
           and "LOD1" in readout(), (status(), readout()))

    print("2. FBX: a converted .glb, and it says so")
    pick(ids["cube"])
    pick(ids["fbx"])
    warning = page.locator("#preview-warning")
    expect("an FBX is drawn (through its converted preview)", not hint_visible()
           and status() == "cube_subd3.fbx", status())
    expect("the conversion warning is on screen", warning.is_visible()
           and ".glb converted from this FBX" in warning.inner_text(), warning.inner_text()[:80])
    expect("its numbers are the gate's, from the FBX (768 tris)", "768 tris" in readout(), readout())
    page.locator("#lod-buttons button[data-level='LOD1']").click()
    page.wait_for_timeout(800)
    expect("an FBX LOD loads and keeps the warning", warning.is_visible()
           and not status().startswith("error") and "LOD1" in readout(), (status(), readout()))
    pick(ids["cube"])
    expect("the warning goes away for a real .glb", not warning.is_visible())

    print("3. an asset that cannot be drawn never shows the previous one")
    pick(ids["blend"])
    expect("a .blend shows the no-preview panel", hint_visible()
           and "no 3D preview" in page.locator("#hint-title").inner_text(),
           page.locator("#hint-title").inner_text())
    expect("the readout no longer shows the cube's 12 tris", "12 tris" not in readout(), readout())
    expect("the header names the .blend", "scene.blend" in status(), status())
    expect("the cube's material sliders are gone",
           "no 3d model" in page.locator("#materials").inner_text().lower(),
           page.locator("#materials").inner_text())
    expect("a note can be added to an unmeasured asset", page.locator("#note-submit").is_enabled())
    page.fill("#note-text", "needs a re-export")
    page.locator("#note-submit").click()
    page.wait_for_timeout(600)
    expect("... and it is saved", "needs a re-export" in notes_of(ids["blend"]), notes_of(ids["blend"]))
    pick(ids["junk"])
    expect("a corrupt .glb is called broken, not unverified",
           "is broken" in page.locator("#hint-title").inner_text()
           and "broken" in page.locator(f'#asset-list .asset[data-id="{ids["junk"]}"]').inner_text(),
           page.locator("#hint-title").inner_text())
    page.click("button.filter[data-filter='attention']")
    expect("... and it needs attention",
           page.locator(f'#asset-list .asset[data-id="{ids["junk"]}"]').is_visible())
    page.click("button.filter[data-filter='all']")

    print("4. textures are shown")
    pick(ids["png"])
    image = page.locator("#texture-img")
    page.wait_for_function("() => document.getElementById('texture-img').naturalWidth > 0", timeout=10000)
    expect("a PNG is drawn as an image", page.locator("#texture-view").is_visible()
           and image.evaluate("img => img.naturalWidth") == 64)
    expect("with its size", "64x64" in page.locator("#texture-caption").inner_text()
           and "64x64" in readout(), page.locator("#texture-caption").inner_text())
    expect("its library card shows its size, not '0 tris'",
           "0 tris" not in page.locator(f'#asset-list .asset[data-id="{ids["png"]}"]').inner_text())
    pick(ids["tga"])
    page.wait_for_function("() => document.getElementById('texture-img').naturalWidth === 32",
                           timeout=10000)
    expect("a TGA is converted to PNG for preview, and says so",
           "converted to PNG" in page.locator("#texture-caption").inner_text(),
           page.locator("#texture-caption").inner_text())
    pick(ids["cube"])
    expect("the texture view is cleared for a mesh", not page.locator("#texture-view").is_visible())

    print("5. materials")
    pick(ids["cube"])
    page.click("#mode-geometry")
    page.click("#mode-textured")
    page.click("#mode-geometry")
    page.click("#mode-textured")
    expect("Geometry -> Textured does not throw", not page_errors, page_errors[:1])

    print("6. verdicts")
    page.goto(f"{BASE}/viewer?live=0&asset={ids['ball']}")
    page.wait_for_function(
        "() => !document.getElementById('status').textContent.startsWith('loading')"
        " && document.getElementById('status').textContent !== 'loading…'", timeout=15000)
    page.wait_for_timeout(500)
    verdict = page.locator("#verdict-panel").inner_text()
    expect("a deep link shows the asset's verdict (was 'No asset selected')",
           "FAILED" in verdict and "too heavy" in verdict, verdict.replace("\n", " | "))
    page.click("button.filter[data-filter='attention']")
    visible = page.locator("#asset-list .asset:visible").count()
    expect("an upper-case FAILED counts as needing attention",
           page.locator(f'#asset-list .asset[data-id="{ids["ball"]}"]').is_visible()
           and visible == 2, visible)            # ball.obj (FAILED) + junk.glb (broken)
    page.click("button.filter[data-filter='all']")

    print("7. uploads")
    page.set_input_files("#upload", str(WORK / "tool.exe"))
    page.wait_for_function("() => !document.getElementById('status').textContent.startsWith('uploading')",
                           timeout=15000)
    expect("a refused file is reported, not left on 'uploading'",
           "refused" in status() and "tool.exe" in status(), status())
    page.set_input_files("#upload", [str(WORK / "second.glb"), str(WORK / "rock_Albedo.png")])
    page.wait_for_function("() => !document.getElementById('status').textContent.startsWith('uploading')",
                           timeout=15000)
    page.wait_for_timeout(500)
    names = page.locator("#asset-list").inner_text()
    expect("several files upload in one go; the unchanged one is not duplicated (W1)",
           "second.glb" in names and names.count("rock_Albedo.png") == 1, status())
    page.set_input_files("#upload", str(WORK / "rock_Albedo.png"))
    page.wait_for_function("() => !document.getElementById('status').textContent.startsWith('uploading')",
                           timeout=15000)
    expect("re-uploading the same file says it is unchanged", "unchanged" in status(), status())

    print("7b. a different file with a name already in the library (audit 2026-11-02)")
    (WORK / "dup").mkdir(exist_ok=True)
    (WORK / "dup" / "second.glb").write_bytes((WORK / "one_tri.glb").read_bytes())
    page.set_input_files("#upload", str(WORK / "dup" / "second.glb"))
    page.wait_for_selector("#version-choice:not([hidden])", timeout=10000)
    expect("the viewer asks: next version, or a separate asset?",
           "already in the library" in page.locator("#version-choice").inner_text())
    page.click("#vc-new")
    page.wait_for_function("() => !document.getElementById('status').textContent.startsWith('uploading')",
                           timeout=15000)
    page.wait_for_timeout(500)
    expect("Separate asset keeps both in the library",
           page.locator("#asset-list").inner_text().count("second.glb") == 2,
           page.locator("#asset-list").inner_text()[:300])
    (WORK / "dup" / "second.glb").write_bytes((WORK / "ball_LOD3.glb").read_bytes()[:-4] + b"    ")
    page.set_input_files("#upload", str(WORK / "dup" / "second.glb"))
    page.wait_for_selector("#version-choice:not([hidden])", timeout=10000)
    page.click("#vc-version")
    page.wait_for_function("() => !document.getElementById('status').textContent.startsWith('uploading')",
                           timeout=15000)
    page.wait_for_timeout(500)
    expect("New version makes it the next version of the newest same-named asset",
           "v2" in status() or "version" in status(), status())

    print("8. collision")
    pick(ids["cube"])
    page.select_option("#collision-kind", "hull")
    page.click("#collision-derive")
    page.wait_for_timeout(800)
    with urllib.request.urlopen(f"{BASE}/api/assets/{ids['cube']}/collision.obj") as response:
        hull_obj = response.read().decode()
    expect("a hull rebuilt from the mesh exports (was 413)",
           sum(line.startswith("f ") for line in hull_obj.splitlines()) == 12,
           sum(line.startswith("f ") for line in hull_obj.splitlines()))

    print("8b. files an asset references travel with it")
    def upload(paths):
        page.set_input_files("#upload", [str(path) for path in paths])
        page.wait_for_function(
            "() => { const s = document.getElementById('status').textContent;"
            " return !s.startsWith('uploading') && !s.startsWith('adding') && !s.startsWith('loading'); }",
            timeout=20000)
        page.wait_for_timeout(600)

    upload([WORK / "rock.bin", WORK / "rock.gltf"])
    expect("a .gltf dropped with its .bin loads (was 'could not read mesh: rock.bin')",
           not hint_visible() and "1 file added alongside" in status(), status())
    expect("the .bin is not a second asset", "rock.bin" not in page.locator("#asset-list").inner_text())
    upload([WORK / "boulder.gltf"])
    expect("a .gltf alone says what it is waiting for",
           "waiting for its files" in page.locator("#hint-title").inner_text()
           and "rock.bin" in page.locator("#hint-body").inner_text(),
           page.locator("#hint-title").inner_text())
    upload([WORK / "rock.bin"])
    expect("dropping the .bin onto it completes it", not hint_visible()
           and "added alongside" in status(), status())
    upload([WORK / "crate.obj", WORK / "crate.mtl", WORK / "planks_Albedo.png"])
    expect("an .obj with its .mtl and texture uses its material",
           "planks" in page.locator("#materials").inner_text(), page.locator("#materials").inner_text())
    expect("... and the texture is not a separate asset",
           "planks_Albedo.png" not in page.locator("#asset-list").inner_text())

    print("8c. LODs: generate, upload, warnings, delete")
    page.goto(f"{BASE}/viewer?live=0")
    page.wait_for_selector("#asset-list .asset")
    verdict_box = page.locator("#verdict-panel").bounding_box()
    covering = page.evaluate(
        "([x, y]) => { const el = document.elementFromPoint(x, y);"
        " return !!(el && el.closest('#verdict-panel')); }",
        [verdict_box["x"] + 20, verdict_box["y"] + 10]) if verdict_box else False
    expect("the gate verdict panel is on screen (another panel drew over it)", covering, verdict_box)
    pick(ids["cube"])
    page.select_option("#lod-preset", "0.5,0.25,0.125")
    page.click("#lod-generate")
    page.wait_for_function("() => /generated|error/.test(document.getElementById('lod-progress').textContent)",
                           timeout=30000)
    progress_text = page.locator("#lod-progress").inner_text()
    expect("Generate LODs runs from the viewer", "3 LODs generated" in progress_text, progress_text)
    expect("... and says the 12-triangle box's LODs save nothing",
           "save nothing" in progress_text
           and page.locator("#lod-buttons button.warned").count() == 3
           and "same triangle count as the source" in page.locator("#lod-list").inner_text(),
           page.locator("#lod-list").inner_text()[:160])
    pick(ids["ball"])
    page.select_option("#lod-upload-level", "LOD4")
    page.set_input_files("#lod-upload", str(WORK / "one_tri.glb"))
    page.wait_for_function("() => /is now LOD4|error/.test(document.getElementById('lod-progress').textContent)",
                           timeout=15000)
    expect("Upload your own LOD stores it as a level",
           "is now LOD4" in page.locator("#lod-progress").inner_text()
           and "uploaded" in page.locator("#lod-list .lod-row[data-level='LOD4']").inner_text(),
           page.locator("#lod-progress").inner_text())
    count_before = page.locator("#asset-list .asset").count()
    page.set_input_files("#upload", str(WORK / "ball_LOD3.glb"))
    page.wait_for_function("() => /LOD added|refused/.test(document.getElementById('status').textContent)",
                           timeout=15000)
    expect("a file named ball_LOD3 becomes LOD3 of ball.obj, not a new asset",
           "LOD3 of ball.obj" in status() and page.locator("#asset-list .asset").count() == count_before,
           status())
    page.locator("#lod-buttons button[data-level='LOD4']").click()
    page.wait_for_timeout(600)
    expect("viewing an uploaded LOD says so", "your LOD (uploaded)" in readout(), readout())
    page.locator("#lod-list .lod-row[data-level='LOD4'] .del").click()
    # Wait for the result, not a fixed time: 800 ms was too short on a busy CI runner.
    try:
        page.wait_for_function(
            "() => !document.querySelector(\"#lod-list .lod-row[data-level='LOD4']\")"
            " && document.getElementById('readout').textContent.trim().startsWith('source')",
            timeout=15000)
    except Exception:  # noqa: BLE001 - the expect below reports what was on screen
        pass
    expect("deleting the LOD on screen goes back to the source",
           page.locator("#lod-list .lod-row[data-level='LOD4']").count() == 0
           and readout().startswith("source"), readout())
    report_href = page.locator("#asset-report").get_attribute("href") or ""
    expect("the inspector links the printable report",
           page.locator("#asset-report").is_visible() and report_href.endswith(f"/api/assets/{ids['ball']}/report"),
           report_href)
    report = page.request.get(BASE + report_href)
    expect("... and it opens", report.status == 200 and "ball.obj" in report.text(), str(report.status))
    page.click("#asset-delete")
    expect("Delete asset asks for a second click", "Click again" in page.locator("#asset-delete").inner_text()
           and page.locator(f'#asset-list .asset[data-id="{ids["ball"]}"]').count() == 1)
    page.click("#asset-delete")
    page.wait_for_function("(id) => !document.querySelector(`#asset-list .asset[data-id='${id}']`)",
                           arg=ids["ball"], timeout=10000)
    expect("... and the second click deletes it",
           page.locator(f'#asset-list .asset[data-id="{ids["ball"]}"]').count() == 0 and hint_visible(),
           status())

    print("8d. Check for fixes: a verified copy, the original kept")
    _npot = io.BytesIO()
    Image.new("RGB", (100, 60), (110, 100, 90)).save(_npot, format="PNG")
    ids["npot"] = aws.create_asset(db, data, "bark.png", _npot.getvalue())["id"]
    page.goto(f"{BASE}/viewer?live=0&asset={ids['npot']}")
    page.wait_for_function("() => document.getElementById('status').textContent === 'bark.png'",
                           timeout=15000)
    expect("a .png offers Check for fixes", page.locator("#asset-heal").is_visible())
    stored_before = len(aws.list_assets(db))
    page.click("#asset-heal")
    page.wait_for_selector("#heal-result:not([hidden])", timeout=10000)
    heal_text = page.locator("#heal-result").inner_text()
    expect("... which lists what the healer would change, and the budget",
           "renamed bark.png -> bark_Albedo.png" in heal_text and "resized 100x60 -> 128x64" in heal_text
           and "default budget" in heal_text and "this one is not changed" in heal_text, heal_text)
    expect("... and offers to make the copy",
           page.locator("#asset-heal").inner_text() == "Make fixed copy")
    expect("checking stored nothing", len(aws.list_assets(db)) == stored_before)
    page.click("#asset-heal")
    page.wait_for_function("() => document.getElementById('status').textContent"
                           ".startsWith('fixed copy of bark.png')", timeout=15000)
    verdict_text = page.locator("#verdict-body").inner_text()
    expect("the fixed copy is selected, and says where it came from",
           f"Fixed copy of #{ids['npot']}" in verdict_text and "PASSED" in verdict_text
           and "bark_Albedo.png" in status(), verdict_text)
    expect("... and has nothing left to fix", "Check for fixes" in page.locator("#asset-heal").inner_text())
    expect("the fixed copy is v2 of the same asset, and v1 is kept (W1)",
           page.locator("#version-list .version-row").count() == 2
           and "healed" in page.locator("#version-list").inner_text()
           and "v2 vs v1" in page.locator("#version-list").inner_text(),
           page.locator("#version-list").inner_text())
    page.locator("#verdict-body a[data-asset]").click()
    page.wait_for_function("() => document.getElementById('status').textContent === 'bark.png'",
                           timeout=15000)
    expect("the link goes back to the original", "bark.png" == status())
    pick(ids["cube"])
    expect("a .glb does not offer it (the healer cannot fix glTF)",
           not page.locator("#asset-heal").is_visible())

    print("8e. versions, compare, sign-off and the depot path (W1, W2, W8)")
    _ball = (WORK / "ball.obj").read_bytes()
    ids["vase1"] = aws.create_asset(db, data, "vase.obj", _ball,
                                    source={"path": "//depot/Art/vase.obj", "changelist": "501"})["id"]
    ids["vase2"] = aws.create_asset(db, data, "vase.obj", _ball + b"\nf 1 2 3\n",
                                    source={"path": "//depot/Art/vase.obj", "changelist": "502"})["id"]
    page.goto(f"{BASE}/viewer?live=0&asset={ids['vase2']}")
    page.wait_for_function("() => document.getElementById('status').textContent === 'vase.obj'",
                           timeout=15000)
    versions_text = page.locator("#version-list").inner_text()
    expect("the inspector lists both versions and what changed",
           page.locator("#version-list .version-row").count() == 2 and "v2 vs v1" in versions_text
           and "triangles +1" in versions_text, versions_text)
    expect("the library card says v2", "v2" in page.locator(
        f'#asset-list .asset[data-id="{ids["vase2"]}"]').inner_text())
    expect("the depot path and changelist are shown, with a copy button",
           page.locator("#source-text").get_attribute("data-raw") == "//depot/Art/vase.obj @502"
           and page.locator("#source-copy").is_visible(), page.locator("#source-text").get_attribute("data-raw"))
    page.click("#compare-overlay")
    page.wait_for_function("() => document.getElementById('status').textContent.includes('wireframe over')",
                           timeout=15000)
    expect("Overlay previous draws v1 over v2", "active" in (page.locator("#compare-overlay").get_attribute("class") or ""),
           status())
    page.click("#compare-side")
    page.wait_for_function("() => document.getElementById('status').textContent.includes('beside')",
                           timeout=15000)
    expect("Side by side switches to v1 beside v2",
           "active" in (page.locator("#compare-side").get_attribute("class") or "")
           and "active" not in (page.locator("#compare-overlay").get_attribute("class") or ""), status())
    page.click("#compare-side")
    page.wait_for_timeout(300)
    expect("a second click turns the compare off",
           "active" not in (page.locator("#compare-side").get_attribute("class") or ""))
    page.fill("#review-name", "Alex")
    page.fill("#review-note", "seam on the **left**")
    page.click("#review-changes")
    page.wait_for_function("() => document.getElementById('status').textContent.includes('Changes requested')",
                           timeout=15000)
    page.click("#review-approve")
    page.wait_for_function("() => document.getElementById('status').textContent.includes('Approved')",
                           timeout=15000)
    review_text = page.locator("#review-status").inner_text()
    expect("signing off shows who, and that the name was typed, not signed in",
           "Approved" in review_text and "Alex" in review_text and "not signed in" in review_text, review_text)
    expect("the history keeps the earlier decision",
           "Changes requested" in page.locator("#review-history").inner_text())
    page.click("button.filter[data-filter='approved']")
    page.wait_for_timeout(300)
    expect("the Approved filter shows it",
           page.locator(f'#asset-list .asset[data-id="{ids["vase2"]}"]').is_visible())
    page.click("button.filter[data-filter='to-review']")
    page.wait_for_timeout(300)
    expect("... and To review does not",
           not page.locator(f'#asset-list .asset[data-id="{ids["vase2"]}"]').is_visible())
    page.click("button.filter[data-filter='all']")
    page.locator("#version-list .version-row").nth(1).click()
    page.wait_for_function("(id) => document.querySelector('#version-list .version-row.current')"
                           " && document.querySelector('#version-list .version-row.current').dataset.id == id",
                           arg=str(ids["vase1"]), timeout=15000)
    expect("clicking v1 opens v1, with its own changelist",
           page.locator("#source-text").get_attribute("data-raw") == "//depot/Art/vase.obj @501")

    print("8f. inspection views (W4)")
    (WORK / "plane.obj").write_text("v -0.5 0 -0.5\nv 0.5 0 -0.5\nv 0.5 0 0.5\nv -0.5 0 0.5\n"
                                    "vt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nvn 0 1 0\n"
                                    "f 1/1/1 2/2/1 3/3/1\nf 1/1/1 3/3/1 4/4/1\n")
    ids["plane"] = aws.create_asset(db, data, "plane.obj", (WORK / "plane.obj").read_bytes())["id"]
    page.goto(f"{BASE}/viewer?live=0&asset={ids['plane']}")
    page.wait_for_function("() => document.getElementById('status').textContent === 'plane.obj'",
                           timeout=15000)
    expect("a single version with no depot path shows no compare buttons and no empty Copy",
           not page.locator("#compare-overlay").is_visible() and not page.locator("#source-copy").is_visible())
    page.click("#mode-uv")
    expect("UV check draws the numbered checker and says how to read it",
           "UV checker" in readout() and "active" in (page.locator("#mode-uv").get_attribute("class") or ""),
           readout())
    page.click("#mode-texel")
    expect("Texel shows the density in px/m, the gate's maths (1 m plane, 1024 texture = 1,024)",
           "median 1,024 px/m (assuming a 1024 texture)" in readout(), readout())
    expect("... and the Materials panel still lists the asset's own material",
           "texel density" not in page.locator("#materials").inner_text()
           and "UV checker" not in page.locator("#materials").inner_text(),
           page.locator("#materials").inner_text())
    page.click("#mode-textured")
    expect("Textured goes back", "UV checker" not in readout() and "texel" not in readout())
    page.click("#scale")
    expect("Scale gives the size in metres beside a 1.8 m person",
           "size 1.00 x 0.00 x 1.00 m (person: 1.8 m)" in readout(), readout())
    page.click("#normals")
    expect("Normals toggles on", "active" in (page.locator("#normals").get_attribute("class") or ""))
    pick(ids["cube"])
    page.click("#mode-uv")
    expect("a mesh without UVs is shown pink, and the readout says so", "pink = no UVs" in readout())
    page.click("#mode-textured")
    page.click("#normals")
    page.click("#scale")

    print("8g. a textured FBX preview (W7)")
    _fx = Path(__file__).resolve().parent / "fixtures" / "fbx"
    ids["texfbx"] = aws.create_asset(db, data, "tex_relative.fbx", (_fx / "tex_relative.fbx").read_bytes())["id"]
    aws.add_sidecar(db, data, ids["texfbx"], "tex_relative_wood_Albedo.png",
                    (_fx / "tex_relative_wood_Albedo.png").read_bytes())
    texture_hits = []
    page.on("response", lambda response: texture_hits.append(response.status)
            if response.url.endswith("/files/tex_relative_wood_Albedo.png") else None)
    page.goto(f"{BASE}/viewer?live=0&asset={ids['texfbx']}")
    page.wait_for_function("() => document.getElementById('status').textContent === 'tex_relative.fbx'",
                           timeout=15000)
    page.wait_for_timeout(500)
    expect("the FBX preview loads its texture from the asset's files", 200 in texture_hits, texture_hits)
    expect("... and the banner still says it is a converted preview, now with textures",
           "base-colour textures" in page.locator("#preview-warning").inner_text())

    print("8h. notes pinned to the model (W5)")
    ids["pinned"] = aws.create_asset(db, data, "pinned_cube.glb", (WORK / "cube.glb").read_bytes())["id"]
    page.goto(f"{BASE}/viewer?live=0&asset={ids['pinned']}")
    page.wait_for_function("() => document.getElementById('status').textContent === 'pinned_cube.glb'",
                           timeout=15000)
    page.wait_for_timeout(800)              # let the framing damping settle

    def canvas_box():
        return page.locator("#canvas-host canvas").bounding_box()

    def marker_tip(selector=".pin-marker:not(.draft)"):
        box = page.locator(selector).first.bounding_box()
        return (box["x"] + box["width"] / 2, box["y"] + box["height"]) if box else None

    _box = canvas_box()
    _click = (_box["x"] + _box["width"] / 2, _box["y"] + _box["height"] / 2)
    expect("Pin to model is ready once the model is on screen", page.locator("#note-pin").is_enabled())
    page.click("#note-pin")
    page.mouse.click(*_click)
    expect("a click on the model places a draft pin there",
           page.locator(".pin-marker.draft").count() == 1
           and "pinned here" in page.locator("#note-pin-state").inner_text(),
           page.locator("#note-pin-state").inner_text())
    page.fill("#note-text", "seam on this face")
    page.click("#note-submit")
    page.wait_for_selector(".pin-marker:not(.draft)", timeout=10000)
    with urllib.request.urlopen(f"{BASE}/api/assets/{ids['pinned']}/notes") as _response:
        _pinned = json.loads(_response.read())["notes"][0]
    expect("the note is stored with its point, camera and a screenshot",
           _pinned["pin"] and _pinned["pin"]["number"] == 1 and _pinned["screenshot"]
           and len(_pinned["pin"]["camera"]["position"]) == 3, _pinned)
    with urllib.request.urlopen(f"{BASE}{_pinned['screenshot']}") as _response:
        _shot_bytes = _response.read()
        _shot_type = _response.headers.get("Content-Type", "")
    import io as _io
    from PIL import Image as _Image
    _shot_image = _Image.open(_io.BytesIO(_shot_bytes))
    expect("... the screenshot is a JPEG of the view, at most 800 px wide, not a blank frame",
           _shot_type.startswith("image/jpeg") and _shot_image.width <= 800
           and len(set(_shot_image.convert("L").resize((32, 32)).getdata())) > 4,
           (_shot_type, _shot_image.size))
    _tip = marker_tip()
    expect("the numbered pin sits where the model was clicked",
           _tip and abs(_tip[0] - _click[0]) < 4 and abs(_tip[1] - _click[1]) < 4, (_tip, _click))
    expect("the note shows its number and the view's picture",
           "#1" in page.locator("#note-list .note").first.inner_text()
           and page.locator("#note-list .note .shot").count() == 1)
    page.mouse.move(_click[0] - 200, _click[1] - 120)
    page.mouse.down()
    page.mouse.move(_click[0] + 60, _click[1] - 40, steps=8)
    page.mouse.up()
    page.wait_for_timeout(900)
    _moved = marker_tip()
    expect("orbiting away moves the pin with the model",
           _moved is None or abs(_moved[0] - _click[0]) > 15 or abs(_moved[1] - _click[1]) > 15,
           (_moved, _click))
    page.locator("#note-list .note [data-view]").first.click()
    page.wait_for_timeout(1200)
    _back = marker_tip()
    expect("view flies the camera back to the pinned view",
           _back and abs(_back[0] - _click[0]) < 4 and abs(_back[1] - _click[1]) < 4, (_back, _click))
    _elsewhere = browser.new_page(viewport={"width": 1440, "height": 900})
    _elsewhere.goto(f"{BASE}/viewer?live=0&asset={ids['pinned']}&note={_pinned['id']}")
    _elsewhere.wait_for_selector(".pin-marker", timeout=15000)
    _elsewhere.wait_for_timeout(1500)
    _box2 = _elsewhere.locator(".pin-marker").first.bounding_box()
    _tip2 = (_box2["x"] + _box2["width"] / 2, _box2["y"] + _box2["height"])
    expect("a ?note= link opens the same view, with the same pin, in another browser",
           abs(_tip2[0] - _click[0]) < 4 and abs(_tip2[1] - _click[1]) < 4
           and "active" in (_elsewhere.locator("#note-list .note").first.get_attribute("class") or ""),
           (_tip2, _click))
    _elsewhere.close()
    page.click("#note-pin")
    page.mouse.click(_box["x"] + 5, _box["y"] + 5)
    expect("a click beside the model places nothing and says so",
           page.locator(".pin-marker.draft").count() == 0
           and "missed" in page.locator("#note-pin-state").inner_text())
    page.click("#note-pin")

    print("8i. shippable LODs (W6)")
    import math as _math
    import numpy as _np
    import trimesh as _trimesh
    _verts, _uvs, _faces = [], [], []
    for _i in range(25):
        for _j in range(49):
            _t, _p = _math.pi * _i / 24, 2 * _math.pi * _j / 48
            _verts.append((_math.sin(_t) * _math.cos(_p), _math.cos(_t), _math.sin(_t) * _math.sin(_p)))
            _uvs.append((_j / 48, 1 - _i / 24))
    for _i in range(24):
        for _j in range(48):
            _a, _c = _i * 49 + _j, (_i + 1) * 49 + _j
            if _i:
                _faces.append([_a, _c, _a + 1])
            if _i != 23:
                _faces.append([_a + 1, _c, _c + 1])
    _material = _trimesh.visual.material.PBRMaterial(
        name="Checker", baseColorTexture=Image.new("RGB", (256, 256), (200, 80, 40)))
    _globe = _trimesh.Scene(_trimesh.Trimesh(
        _np.array(_verts), _np.array(_faces), vertex_normals=_np.array(_verts),
        visual=_trimesh.visual.TextureVisuals(uv=_np.array(_uvs), material=_material),
        process=False)).export(file_type="glb")
    ids["globe"] = aws.create_asset(db, data, "globe.glb", _globe)["id"]
    page.goto(f"{BASE}/viewer?live=0&asset={ids['globe']}")
    page.wait_for_function("() => document.getElementById('status').textContent === 'globe.glb'",
                           timeout=15000)
    page.check("#lod-shippable")
    page.click("#lod-generate")
    page.wait_for_function("() => /generated|error/.test(document.getElementById('lod-progress').textContent)",
                           timeout=60000)
    _rows = page.locator("#lod-list .lod-row").all_inner_texts()
    expect("Shippable generates levels marked shippable, with their surface error",
           len(_rows) == 3 and all("shippable" in row and "mm" in row for row in _rows), _rows)
    expect("... each with a download link", page.locator("#lod-list a.get").count() == 3
           and page.locator("#lod-list a.get").first.get_attribute("href").endswith("/lods/LOD1/file"))
    page.locator('#lod-buttons [data-level="LOD2"]').click()
    page.wait_for_function("() => document.getElementById('status').textContent.includes('LOD2')",
                           timeout=15000)
    page.wait_for_timeout(300)
    expect("viewing a shippable LOD keeps its material and texture (not the grey preview)",
           "Checker" in page.locator("#materials").inner_text()
           and "no materials" not in page.locator("#materials").inner_text(),
           page.locator("#materials").inner_text()[:200])
    page.uncheck("#lod-shippable")

    print("9. deleted while open")
    page.goto(f"{BASE}/viewer?asset={ids['embedded']}")
    page.wait_for_function("() => document.getElementById('status').textContent === 'embedded.gltf'",
                           timeout=15000)
    request = urllib.request.Request(f"{BASE}/api/assets/{ids['embedded']}", method="DELETE")
    urllib.request.urlopen(request).read()
    page.wait_for_timeout(2500)
    expect("deleting the open asset clears the screen",
           hint_visible() and "No asset selected" in page.locator("#collision-info").inner_text()
           and page.locator("#lod-buttons button").count() == 0,
           page.locator("#collision-info").inner_text())

    print("9b. open to the network, and on a phone")
    open_server = aws.build_server("0.0.0.0", 0, db, data, quiet=True)
    open_port = open_server.server_address[1]
    threading.Thread(target=open_server.serve_forever, daemon=True).start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{open_port}/api/assets?filename=claimed.glb&verdict=passed&source=ci",
        data=(WORK / "cube.glb").read_bytes(), method="POST")
    claimed = json.loads(urllib.request.urlopen(request).read())
    page.goto(f"http://127.0.0.1:{open_port}/viewer?live=0&asset={claimed['id']}")
    page.wait_for_function("() => document.getElementById('status').textContent === 'claimed.glb'",
                           timeout=15000)
    expect("an open, tokenless workstation says so on every page",
           page.locator("#exposure-warning").is_visible(), page.locator("#exposure-warning").inner_text()[:80])
    expect("a verdict that arrived while it was open is marked unauthenticated",
           "Not authenticated" in page.locator("#verdict-panel").inner_text(),
           page.locator("#verdict-panel").inner_text().replace("\n", " | "))
    open_server.shutdown()
    open_server.server_close()
    page.goto(f"{BASE}/viewer?live=0&asset={ids['cube']}")
    page.wait_for_function("() => document.getElementById('status').textContent === 'cube.glb'",
                           timeout=15000)
    expect("a loopback workstation shows no exposure warning", not page.locator("#exposure-warning").is_visible())
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(600)
    canvas = page.locator("#canvas-host").bounding_box()
    expect("on a phone the 3D view is on screen and usable (it disappeared)",
           canvas is not None and canvas["width"] >= 360 and canvas["height"] >= 300
           and canvas["y"] + canvas["height"] <= 844,
           canvas)
    scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
    expect("... with no sideways scrolling", scroll_width <= 390, scroll_width)
    page.set_viewport_size({"width": 1440, "height": 900})

    print("9c. named reviewers (W3)")
    acc_db, acc_data = WORK / "accounts.db", WORK / "accounts_data"
    acc_db.unlink(missing_ok=True)
    aws.init_db(acc_db)
    aws.add_account(acc_db, "lead1", "lead", "lead-password-1", display_name="Lena Lead")
    acc_asset = aws.create_asset(acc_db, acc_data, "crate.obj", (WORK / "ball.obj").read_bytes())
    acc_server = aws.build_server("127.0.0.1", 0, acc_db, acc_data, quiet=True)
    threading.Thread(target=acc_server.serve_forever, daemon=True).start()
    ACC = f"http://127.0.0.1:{acc_server.server_address[1]}"
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{ACC}/viewer?live=0")
    page.wait_for_selector("#signin:not([hidden])", timeout=10000)
    expect("with accounts on, the page asks to sign in and shows no library",
           page.locator("#asset-list .asset").count() == 0 and "sign in" in status())
    page.fill("#signin-user", "lead1")
    page.fill("#signin-password", "not-the-password")
    page.click("#signin button[type=submit]")
    page.wait_for_function("() => document.getElementById('signin-message').textContent.length > 0",
                           timeout=10000)
    expect("a wrong password says so on the form",
           "wrong username or password" in page.locator("#signin-message").inner_text())
    page.fill("#signin-password", "lead-password-1")
    page.click("#signin button[type=submit]")
    page.wait_for_selector("#asset-list .asset", timeout=15000)
    expect("signed in: the account and role are shown, the typed-name fields are gone",
           "Lena Lead" in page.locator("#account").inner_text() and "lead" in page.locator("#account").inner_text()
           and not page.locator("#note-author").is_visible(), page.locator("#account").inner_text())
    page.locator(f'#asset-list .asset[data-id="{acc_asset["id"]}"]').click()
    page.wait_for_function("() => document.getElementById('status').textContent === 'crate.obj'", timeout=15000)
    expect("a lead sees the sign-off buttons without a name field",
           page.locator("#review-approve").is_visible() and not page.locator("#review-name").is_visible())
    page.click("#review-approve")
    page.wait_for_function("() => document.getElementById('status').textContent.includes('Approved')",
                           timeout=15000)
    expect("the sign-off carries the account's name, verified",
           "Lena Lead" in page.locator("#review-status").inner_text()
           and "not signed in" not in page.locator("#review-status").inner_text(),
           page.locator("#review-status").inner_text())
    page.click("#signout")
    page.wait_for_selector("#signin:not([hidden])", timeout=10000)
    expect("Sign out returns to the sign-in form", page.locator("#asset-list .asset").count() == 0)
    acc_server.shutdown()
    acc_server.server_close()

    print("10. another website")
    other = browser.new_page()
    other.goto(f"http://127.0.0.1:{evil.server_address[1]}/")
    other.wait_for_timeout(1500)
    expect("a page on another origin cannot write a note",
           "written by another website" not in notes_of(ids["cube"]), notes_of(ids["cube"]))

    blocked = [line for line in console_errors if "Content Security Policy" in line
               or "Fetch API cannot load" in line]
    expect("no load was blocked by the page's security policy", not blocked, blocked[:1])
    expect("no uncaught page errors", not page_errors, page_errors[:2])
    browser.close()

server.shutdown()
server.server_close()
evil.shutdown()
evil.server_close()
print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
