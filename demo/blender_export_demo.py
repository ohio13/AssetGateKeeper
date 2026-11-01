"""Blender-side demo: export assets that deliberately pass and fail the gate.

Run headless:

    blender -b --factory-startup --python demo/blender_export_demo.py -- ^
        <watch-folder> [--delay 7] [--lead 15]

Each step rebuilds a scene, exports it into the watch folder, and waits, so a
running dashboard fills up one verdict at a time. Nothing here talks to the gate:
it only writes files, exactly like an artist pressing Export.
"""

import sys
import time

import bpy

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_mesh(kind, **kwargs):
    """Create the base object and triangulate it, so counts are honest."""
    if kind == "cube":
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    elif kind == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(
            segments=kwargs.get("segments", 32), ring_count=kwargs.get("ring", 16),
            radius=1.0, location=(0, 0, 0))
    elif kind == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=kwargs.get("vertices", 24), radius=0.5, depth=2.0)
    else:
        raise ValueError(kind)

    obj = bpy.context.object
    modifier = obj.modifiers.new(name="Triangulate", type="TRIANGULATE")
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    return obj


def assign_materials(obj, count):
    """One material per slot, faces spread across them like a real export."""
    obj.data.materials.clear()
    for index in range(count):
        material = bpy.data.materials.new(name=f"material_{index}")
        material.use_nodes = True
        material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (
            0.55, 0.25 + index * 0.12, 0.18, 1.0)
        obj.data.materials.append(material)
    for polygon in obj.data.polygons:
        polygon.material_index = polygon.index % count


def export_gltf(path):
    bpy.ops.export_scene.gltf(
        filepath=str(path),
        export_format="GLTF_SEPARATE",
        use_selection=False,
        export_apply=True,
    )


def save_texture(path, width, height):
    """Write a real PNG through Blender's image API (no bake needed)."""
    image = bpy.data.images.new("demo_tex", width=width, height=height, alpha=False)
    try:
        import numpy as np

        xs = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
        ys = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
        pixels = np.empty((height, width, 4), dtype=np.float32)
        pixels[..., 0] = 0.15 + 0.6 * xs
        pixels[..., 1] = 0.10 + 0.5 * ys
        pixels[..., 2] = 0.35 + 0.25 * (xs * ys)
        pixels[..., 3] = 1.0
        image.pixels.foreach_set(pixels.ravel())
    except ImportError:  # pragma: no cover - Blender always ships numpy
        image.pixels.foreach_set([0.5, 0.4, 0.3, 1.0] * (width * height))

    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)


# --------------------------------------------------------------------------- #
# The scripted export pass
# --------------------------------------------------------------------------- #

def step_crate(watch):
    reset_scene()
    crate = make_mesh("cube")
    crate.name = "export_crate"
    assign_materials(crate, 1)
    export_gltf(watch / "crate_01.gltf")
    return f"{len(crate.data.polygons)} tris / 1 material"


def step_statue(watch):
    reset_scene()
    statue = make_mesh("sphere", segments=200, ring=260)
    statue.name = "hero_statue"
    assign_materials(statue, 4)
    export_gltf(watch / "hero_statue.gltf")
    return f"{len(statue.data.polygons):,} tris / 4 materials"


def step_albedo_good(watch):
    save_texture(watch / "stone_Albedo.png", 1024, 1024)
    return "1024x1024 _Albedo"


def step_icon(watch):
    save_texture(watch / "ui_icon.png", 300, 300)
    return "300x300, no suffix"


def step_hull(watch):
    save_texture(watch / "hull_Albedo.png", 4096, 1024)
    return "4096x1024"


def step_unnamed(watch):
    save_texture(watch / "stone_texture.png", 1000, 1000)
    return "1000x1000, no suffix (healable)"


def step_recover(watch):
    """The artist fixes the statue: a light prop, back inside budget."""
    reset_scene()
    prop = make_mesh("cylinder", vertices=600)
    prop.name = "prop_fixed"
    assign_materials(prop, 1)
    export_gltf(watch / "prop_fixed.gltf")
    return f"{len(prop.data.polygons)} tris / 1 material"


SCRIPT = [
    (step_crate, "a clean export crate - should PASS"),
    (step_statue, "a dense hero statue - should FAIL on triangles and material slots"),
    (step_albedo_good, "a correct 1024 albedo - should PASS"),
    (step_icon, "a 300x300 icon, badly named - should FAIL twice"),
    (step_hull, "a 4096x1024 hull map - should FAIL on resolution only"),
    (step_unnamed, "a 1000x1000 unnamed texture - FAILS raw, HEALS with --auto-heal"),
    (step_recover, "the artist's fix, a light prop - should PASS"),
]


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        print("[blender] usage: -- <watch-folder> [--delay 7] [--lead 15]")
        return 1

    from pathlib import Path

    watch = Path(argv[0])
    delay, lead = 7.0, 15.0
    if "--delay" in argv:
        delay = float(argv[argv.index("--delay") + 1])
    if "--lead" in argv:
        lead = float(argv[argv.index("--lead") + 1])
    watch.mkdir(parents=True, exist_ok=True)

    print(f"[blender] exporting into {watch}", flush=True)
    print(f"[blender] first export in {lead:.0f}s, then every {delay:.0f}s", flush=True)
    time.sleep(lead)

    for index, (function, description) in enumerate(SCRIPT, start=1):
        print(f"[blender] [{index}/{len(SCRIPT)}] {description}", flush=True)
        try:
            detail = function(watch)
            print(f"[blender]          wrote {detail}", flush=True)
        except Exception as exc:  # a demo must not die half way through
            print(f"[blender]          FAILED: {type(exc).__name__}: {exc}", flush=True)
        if index < len(SCRIPT):
            time.sleep(delay)

    print("[blender] export pass complete", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
