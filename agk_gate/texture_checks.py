"""What a texture's pixels say about how it was made (17 Nov 2026).

Three checks that read the image itself, not only its header:

* **Animated images.** A GIF (or APNG, or animated WebP) with more than one
  frame: every engine imports the first frame only.
* **Normal maps** (named as one: ``_Normal``, ``_N``, ``_nrm``, ``_nor_gl``...):
  whether the vectors are unit length (a map saved through an sRGB/gamma
  conversion is not), whether they point out of the surface (blue at or above
  128), and which way green points. Green is found from the pixels alone: a
  tangent-space normal map is the slope of a height field, and a slope field
  has no curl, so the curl measured with green read "up" (OpenGL) and with
  green read "down" (DirectX) tells them apart. It is judged in 32-pixel tiles
  and only tiles with real detail vote, so a flat or noisy map gives no verdict
  rather than a wrong one, and a map whose parts disagree (a mirrored half
  baked the other way) is reported as mixed.
* **Packed masks** (``_ORM``, ``_ARM``, ``_RMA``, ``_MRA``, ``_MaskMap``):
  channels that were never packed (R, G and B the same image), an occlusion
  channel that is nearly black, a metallic channel full of in-between values
  (usually occlusion or roughness in the wrong slot), and a roughness of 0
  everywhere (usually smoothness saved where roughness belongs).

Every finding is a warning unless ``[texture] normal_check`` / ``mask_check``
is ``"fail"``; ``"off"`` turns a check off. Engines and their green: Unreal
reads DirectX, Unity, Godot, Blender and glTF read OpenGL.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from agk_gate.common import np

try:
    from PIL import Image
except ImportError:          # pragma: no cover - Pillow is a hard dependency
    Image = None

#: Engines (as [naming] presets) and the green they read.
ENGINE_CONVENTION = {"unreal": "directx", "unity": "opengl", "godot": "opengl",
                     "gltf": "opengl"}
CONVENTION_WORDS = {"opengl": "OpenGL (green up)", "directx": "DirectX (green down)"}

_NORMAL_NAME = re.compile(
    r"(?:^|_)(?:normal|nrm|nor|n)(?:_?(gl|ogl|opengl|dx|directx))?$", re.IGNORECASE)

#: Packed-mask names (last part of the file name) and what each channel holds.
PACKED_LAYOUTS = {
    "orm": ("occlusion", "roughness", "metallic"),
    "arm": ("occlusion", "roughness", "metallic"),
    "occlusionroughnessmetallic": ("occlusion", "roughness", "metallic"),
    "rma": ("roughness", "metallic", "occlusion"),
    "mra": ("metallic", "roughness", "occlusion"),
    "maskmap": ("metallic", "occlusion", "detail mask"),
}
_CHANNEL = ("red", "green", "blue")

#: Size the pixels are analysed at: enough for 256 tiles of 32 px.
ANALYSIS_EDGE = 512


def normal_name_convention(path: Path) -> tuple[bool, str]:
    """(is it named as a normal map, the convention its name claims or '')."""
    found = _NORMAL_NAME.search(Path(path).stem)
    if not found:
        return False, ""
    hint = (found.group(1) or "").lower()
    if hint in ("gl", "ogl", "opengl"):
        return True, "opengl"
    if hint in ("dx", "directx"):
        return True, "directx"
    return True, ""


def packed_layout(path: Path) -> Optional[tuple]:
    last = re.split(r"[_\-. ]", Path(path).stem)[-1].lower()
    return PACKED_LAYOUTS.get(last)


def expected_convention(budget) -> str:
    """The green a project's engine reads: [texture] normal_convention, else its
    [naming] presets when they name one family of engines; else unknown."""
    explicit = getattr(budget, "normal_convention", "") or ""
    if explicit:
        return explicit
    found = {ENGINE_CONVENTION[key] for key, *_ in getattr(budget, "naming_conventions", ())
             if key in ENGINE_CONVENTION}
    return found.pop() if len(found) == 1 else ""


# - animation - #

def frame_count(path: Path) -> int:
    if Image is None:
        return 1
    try:
        with Image.open(path) as image:
            return int(getattr(image, "n_frames", 1) or 1)
    except Exception:            # unreadable: the size check already says so
        return 1


def animation_warning(path: Path) -> str:
    frames = frame_count(path)
    if frames <= 1:
        return ""
    return (f"animated ({frames} frames): engines import the first frame only; "
            "export a frame sheet or a flipbook if the animation is wanted")


# - pixels - #

def _pixels(path: Path, nearest: bool = False):
    """RGB(A) as floats 0-1, at most ANALYSIS_EDGE on the long side."""
    with Image.open(path) as image:
        image.seek(0)
        mode = "RGBA" if "A" in image.getbands() else "RGB"
        image = image.convert(mode)
        width, height = image.size
        scale = min(1.0, ANALYSIS_EDGE / max(width, height))
        if scale < 1.0:
            size = (max(1, round(width * scale)), max(1, round(height * scale)))
            # A normal map is averaged (BOX keeps slopes); a mask keeps its
            # 0/1 values (NEAREST), so a metallic edge does not turn grey.
            image = image.resize(size, Image.NEAREST if nearest else Image.BOX)
        return np.asarray(image, np.float64) / 255.0


# - normal maps - #

def green_convention(normals) -> tuple[str, float, int]:
    """('opengl' | 'directx' | 'mixed' | '', share of tiles that agree, tiles that voted).

    ``normals``: an (h, w, 3) array of vectors in -1..1.
    """
    nx, ny, nz = normals[..., 0], normals[..., 1], normals[..., 2]
    usable = nz > 0.3
    if not usable.any():
        return "", 0.0, 0
    nz = np.where(usable, nz, 1.0)
    slope_x = -nx / nz                 # dh/dx
    slope_y = ny / nz                  # dh/dy (rows down) if green is up
    d_slope_x = np.gradient(slope_x, axis=0)
    d_slope_y = np.gradient(slope_y, axis=1)
    jump = sum(np.abs(np.gradient(field, axis=axis))
               for field in (slope_x, slope_y) for axis in (0, 1))
    # UV seams and island borders are jumps, not slopes: the top 5% are left out.
    keep = usable & (jump < np.percentile(jump[usable], 95))
    keep[0, :] = keep[-1, :] = keep[:, 0] = keep[:, -1] = False
    tile = 32 if min(keep.shape) >= 128 else 16
    votes = []
    for top in range(0, keep.shape[0] - tile + 1, tile):
        for left in range(0, keep.shape[1] - tile + 1, tile):
            mask = keep[top:top + tile, left:left + tile]
            if mask.sum() < tile * tile * 0.5:
                continue
            a = d_slope_x[top:top + tile, left:left + tile][mask]
            b = d_slope_y[top:top + tile, left:left + tile][mask]
            if np.std(a) + np.std(b) < 0.02:
                continue                 # flat: nothing to judge
            up = np.mean(np.abs(a - b))      # curl if green is up
            down = np.mean(np.abs(a + b))    # curl if green is down
            if up <= 0 or down <= 0:
                continue
            vote = float(np.log(up / down))
            if abs(vote) >= 0.15:            # a tile that cannot tell does not vote
                votes.append(vote)
    if len(votes) < 6:
        return "", 0.0, len(votes)
    votes = np.asarray(votes)
    opengl = float(np.mean(votes < 0))
    if opengl >= 0.75:
        return "opengl", opengl, len(votes)
    if opengl <= 0.25:
        return "directx", 1.0 - opengl, len(votes)
    # Split votes are a finding only when both sides are sure of themselves:
    # noise gives a split of weak votes.
    strong_up = np.median(-votes[votes < 0]) if (votes < 0).any() else 0
    strong_down = np.median(votes[votes > 0]) if (votes > 0).any() else 0
    if min(strong_up, strong_down) >= 0.3:
        return "mixed", max(opengl, 1 - opengl), len(votes)
    return "", max(opengl, 1 - opengl), len(votes)


def normal_findings(path: Path, expected: str) -> list[str]:
    """What is wrong with a normal map, in words (empty when nothing is)."""
    _, claimed = normal_name_convention(path)
    try:
        pixels = _pixels(path)
    except Exception:              # unreadable: the size check already says so
        return []
    rgb = pixels[..., :3] * 2.0 - 1.0
    findings = []
    blue = pixels[..., 2]
    # A two-channel (BC5-style) map keeps X and Y and leaves blue empty; a full
    # map that is mostly flat has blue near 255 everywhere, which is different.
    two_channel = float(np.mean(blue)) < 0.1 and float(np.std(blue)) < 0.01 and (
        float(np.std(pixels[..., 0])) > 0.01 or float(np.std(pixels[..., 1])) > 0.01)
    if not two_channel:
        length = np.sqrt((rgb ** 2).sum(axis=-1))
        typical = float(np.median(length))
        if abs(typical - 1.0) > 0.1:
            findings.append(f"normal vectors are not unit length (typical {typical:.2f}): "
                            "saved through an sRGB/gamma conversion, or not a normal map; "
                            "save it as linear (non-colour) data")
        inward = float(np.mean(rgb[..., 2] < 0))
        if inward > 0.05:
            findings.append(f"{inward:.0%} of pixels point into the surface (blue below 128): "
                            "an object-space map, or not a normal map")
    if findings:
        return findings              # a damaged map's green says nothing
    if two_channel:
        z = np.sqrt(np.clip(1 - rgb[..., 0] ** 2 - rgb[..., 1] ** 2, 0, 1))
        rgb = np.stack([rgb[..., 0], rgb[..., 1], z], axis=-1)
    found, share, _ = green_convention(rgb)
    if found == "mixed":
        findings.append("green points both ways: some parts read as OpenGL and some as "
                        "DirectX, usually a mirrored half baked the other way; rebake those parts")
        return findings
    if not found:
        return findings
    if claimed and claimed != found:
        findings.append(f"named {CONVENTION_WORDS[claimed]} but the pixels read as "
                        f"{CONVENTION_WORDS[found]} ({share:.0%} of detailed areas agree); "
                        "flip the green channel or fix the name")
    elif expected and expected != found and not claimed:
        findings.append(f"green reads as {CONVENTION_WORDS[found]} ({share:.0%} of detailed "
                        f"areas agree) but this project's engine reads "
                        f"{CONVENTION_WORDS[expected]}: lighting on it will look inverted; "
                        "flip the green channel")
    elif expected and claimed and claimed != expected and claimed == found:
        findings.append(f"a {CONVENTION_WORDS[found]} normal map, but this project's engine "
                        f"reads {CONVENTION_WORDS[expected]}; flip the green channel on import "
                        "or export the other version")
    return findings


# - packed masks - #

def mask_findings(path: Path, layout: tuple) -> list[str]:
    try:
        pixels = _pixels(path, nearest=True)
    except Exception:
        return []
    channels = [pixels[..., index] for index in range(3)]
    findings = []
    spread = max(float(np.std(channel)) for channel in channels)
    same = (np.abs(channels[0] - channels[1]) < 2 / 255) & (np.abs(channels[1] - channels[2]) < 2 / 255)
    if spread > 0.02 and float(np.mean(same)) > 0.99:
        return [f"red, green and blue hold the same image: the channels were not packed "
                f"(expected {', '.join(layout)})"]
    for index, role in enumerate(layout):
        channel = channels[index]
        where = f"{_CHANNEL[index]} ({role})"
        if role == "occlusion" and float(np.mean(channel)) < 0.15:
            findings.append(f"{where} is almost black (mean {float(np.mean(channel)):.2f}): "
                            "the whole surface would be in shadow; channels in the wrong order?")
        elif role == "metallic":
            between = float(np.mean((channel > 0.15) & (channel < 0.85)))
            if between > 0.25:
                findings.append(f"{where} has {between:.0%} in-between values; metallic is "
                                "normally 0 or 1, so this is likely occlusion or roughness in "
                                f"the wrong slot (expected {', '.join(layout)})")
        elif role == "roughness" and float(channel.max()) < 0.02:
            findings.append(f"{where} is 0 everywhere, a perfect mirror: usually smoothness "
                            "saved where roughness belongs (invert it)")
    return findings
