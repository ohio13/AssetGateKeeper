"""Drive the Art Workstation viewer in a real browser (Chromium via Playwright).

The API suite (test_workstation.py) passed while the viewer showed the wrong
model, refused embedded glTF buffers and crashed on Geometry -> Textured: none
of that is visible without a browser. Every check here is a bug found by
driving the page on 2026-11-02.

Needs ``pip install playwright`` and ``python -m playwright install chromium``.
Without them the suite says so and exits 0 (CI installs both and runs it).
"""
import http.server
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
    expect("several files upload in one go", "second.glb" in names and names.count("rock_Albedo.png") == 2,
           status())

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
    upload([WORK / "rock.gltf"])
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
    page.wait_for_timeout(800)
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
