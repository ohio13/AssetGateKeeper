"""Bulk-upload a folder of assets into a running Art Workstation.

The workstation GUI accepts one drag-and-dropped file at a time; this walks a
folder (or a single file) and POSTs each asset to ``/api/assets`` so a whole
project shows up in the library at once.

    python tools/upload_assets.py "C:/path/to/assets"
    python tools/upload_assets.py room.glb --url http://127.0.0.1:8100
    python tools/upload_assets.py assets --dry-run        # just list what would go

Files an asset references - a .gltf's .bin, an .obj's .mtl, textures - are sent
with it into the same asset folder, so it measures and renders as exported;
they are not uploaded again as assets of their own (--no-sidecars turns this
off). The workstation copies the bytes into its own storage - the source files
are only ever read, never modified. Re-running is safe but uploads duplicates
unless you pass --skip-uploaded.

Exit codes: 0 all uploaded, 1 at least one failure, 2 bad arguments/no server.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8100"
SUFFIXES = (".glb", ".gltf", ".obj", ".fbx", ".png", ".jpg", ".jpeg", ".tga")
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def references_of(path: Path) -> list[tuple[str, Path]]:
    """[(as written, local file)] for the files ``path`` references that exist."""
    try:
        import asset_gatekeeper as agk
        pairs = agk.resolve_references(path)
    except Exception:
        return []
    found, seen = [], set()
    for written, target in pairs:
        if written not in seen and target.is_file():
            seen.add(written)
            found.append((written, target))
    return found
#: Used only when the workstation does not report its own limit (older builds).
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def upload_limit(base_url: str, token: str = "") -> int:
    """The workstation's own per-file limit from /api/health (--max-upload-mb)."""
    request = urllib.request.Request(f"{base_url}/api/health")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            value = json.loads(response.read() or b"{}").get("max_upload_bytes")
        return int(value) if value else MAX_UPLOAD_BYTES
    except (OSError, ValueError, TypeError, AttributeError):
        return MAX_UPLOAD_BYTES


def iter_assets(root: Path, recursive: bool, suffixes: tuple[str, ...]) -> list[Path]:
    if root.is_file():
        return [root]
    pattern = "**/*" if recursive else "*"
    found = [p for p in root.glob(pattern) if p.is_file() and p.suffix.lower() in suffixes]
    return sorted(found)


def _request(url: str, token: str, data: bytes | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    request.add_header("User-Agent", "AssetGatekeeper-uploader/1.0")
    if data is not None:
        request.add_header("Content-Type", "application/octet-stream")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        # 60 s plus 1 s per MB: the workstation measures a mesh before it answers.
        timeout = 60 + len(data or b"") / 1048576
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def existing_names(base_url: str, token: str) -> set[str]:
    status, body = _request(f"{base_url}/api/assets", token)
    if status != 200:
        return set()
    try:
        listing = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return set()
    rows = listing.get("assets", listing) if isinstance(listing, dict) else listing
    names: set[str] = set()
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("filename"):
                names.add(str(row["filename"]))
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload a folder of meshes and textures to the Art Workstation.")
    parser.add_argument("path", help="folder or single asset to upload")
    parser.add_argument("--url", "--workstation", dest="url", default=DEFAULT_URL,
                        help=f"workstation base URL (default {DEFAULT_URL})")
    parser.add_argument("--recursive", action="store_true",
                        help="walk sub-folders (the default; kept for older instructions)")
    parser.add_argument("--no-sidecars", action="store_true",
                        help="do not send the files an asset references with it")
    parser.add_argument("--token", default="", help="bearer token, if --token mode is on")
    parser.add_argument("--depot-root", default="", metavar="PATH",
                        help="depot path of the folder being uploaded (e.g. //depot/Art/Props); "
                             "each asset is recorded as <PATH>/<relative path>, shown in the "
                             "inspector, and re-uploads become new versions of the same asset")
    parser.add_argument("--changelist", default="", metavar="N",
                        help="changelist the files come from, shown next to the depot path")
    parser.add_argument("--flat", action="store_true",
                        help="only look at the top level (default: recurse)")
    parser.add_argument("--ext", default=",".join(SUFFIXES),
                        help="comma separated extensions (default glb,gltf,obj,fbx,png,jpg,jpeg,tga)")
    parser.add_argument("--limit", type=int, default=0, help="stop after N files")
    parser.add_argument("--skip-uploaded", action="store_true",
                        help="skip files whose name is already in the library")
    parser.add_argument("--dry-run", action="store_true", help="list, do not upload")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    root = Path(args.path).expanduser()
    if not root.exists():
        print(f"error: no such path: {root}", file=sys.stderr)
        return 2
    suffixes = tuple(s.strip().lower() for s in args.ext.split(",") if s.strip())
    files = iter_assets(root, not args.flat, suffixes)
    sidecars_of: dict = {}
    if not args.no_sidecars:
        referenced = set()
        for path in files:
            pairs = references_of(path)
            if pairs:
                sidecars_of[path] = pairs
                referenced |= {target.resolve() for _, target in pairs}
        # A texture the .gltf uses travels with it, not as a second asset.
        files = [path for path in files if path.resolve() not in referenced]
    if args.limit > 0:
        files = files[: args.limit]
    if not files:
        print(f"nothing to upload under {root} ({', '.join(suffixes)})", file=sys.stderr)
        return 2

    base_url = args.url.rstrip("/")
    if args.dry_run:
        print(f"{len(files)} file(s) would be uploaded to {base_url}:")
        for path in files:
            print(f"  {path}  ({path.stat().st_size:,} bytes)")
            for written, _ in sidecars_of.get(path, []):
                print(f"      + {written}")
        return 0

    skip = existing_names(base_url, args.token) if args.skip_uploaded else set()
    limit = upload_limit(base_url, args.token)
    uploaded = skipped = failed = 0
    for index, path in enumerate(files, 1):
        name = path.name
        prefix = f"[{index}/{len(files)}] {name}"
        if name in skip:
            skipped += 1
            print(f"{prefix}: skipped (already uploaded)")
            continue
        size = path.stat().st_size
        if size == 0:
            failed += 1
            print(f"{prefix}: FAILED empty file", file=sys.stderr)
            continue
        if size > limit:
            failed += 1
            print(f"{prefix}: FAILED {size / 1048576:,.0f} MB is over the workstation's "
                  f"{limit / 1048576:,.0f} MB limit (start it with --max-upload-mb to raise it)",
                  file=sys.stderr)
            continue
        try:
            relative = path.relative_to(root).as_posix() if root.is_dir() else path.name
        except ValueError:
            relative = path.name
        source_path = (f"{args.depot_root.rstrip('/')}/{relative}" if args.depot_root
                       else relative)
        query = urllib.parse.urlencode({"filename": name, "source_path": source_path,
                                        "changelist": args.changelist})
        try:
            status, body = _request(f"{base_url}/api/assets?{query}", args.token,
                                    data=path.read_bytes())
        except (urllib.error.URLError, OSError) as exc:
            print(f"error: cannot reach {base_url}: {exc}", file=sys.stderr)
            print("       is the workstation running? double-click Start AssetGatekeeper.bat", file=sys.stderr)
            return 2
        if status in (200, 201):
            uploaded += 1
            try:
                asset = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                asset = {}
            if asset.get("unchanged"):
                print(f"{prefix}: unchanged (same file as v{asset.get('version', '?')})")
            elif int(asset.get("version") or 1) > 1:
                print(f"{prefix}: new version v{asset['version']}")
            sent = []
            for written, target in sidecars_of.get(path, []):
                if not isinstance(asset.get("id"), int):
                    break
                if target.stat().st_size > limit:
                    print(f"{prefix}: {written} skipped (over the upload limit)", file=sys.stderr)
                    continue
                query = urllib.parse.urlencode({"filename": written})
                side_status, side_body = _request(
                    f"{base_url}/api/assets/{asset['id']}/files?{query}", args.token,
                    data=target.read_bytes())
                if side_status == 201:
                    sent.append(written)
                    try:
                        asset = json.loads(side_body.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        pass
                else:
                    print(f"{prefix}: {written} not sent ({side_status})", file=sys.stderr)
            tris = asset.get("triangle_count", "?")
            extra = f", +{len(sent)} file(s)" if sent else ""
            print(f"{prefix}: uploaded (id {asset.get('id', '?')}, {tris} tris{extra})")
        elif status == 413:
            failed += 1
            print(f"{prefix}: FAILED too large", file=sys.stderr)
        else:
            failed += 1
            detail = body.decode("utf-8", "replace")[:200]
            print(f"{prefix}: FAILED {status} {detail}", file=sys.stderr)

    print(f"\nuploaded {uploaded}, skipped {skipped}, failed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
