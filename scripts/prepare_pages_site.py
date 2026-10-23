#!/usr/bin/env python3
"""Add a verified signed wheel snapshot to a cumulative Pages site."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import tempfile
from pathlib import Path

try:
    from .promote_snapshot import promote
    from .sign_manifest import validate_manifest, verify_signature
except ImportError:
    from promote_snapshot import promote
    from sign_manifest import validate_manifest, verify_signature


RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CATALOG_NAME = "catalog.json"


def _check_release_id(release_id: str) -> None:
    if not RELEASE_ID_RE.fullmatch(release_id) or ".." in release_id:
        raise ValueError("release ID must be 1-64 safe characters and cannot contain '..'")


def _check_tree(root: Path, description: str) -> Path:
    if root.is_symlink():
        raise ValueError(f"{description} must not be a symbolic link")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{description} must be a directory")
    for path in resolved.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"{description} contains a symbolic link: {path.relative_to(resolved)}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _verify_release(release_dir: Path, public_key: Path) -> tuple[dict, str]:
    release_dir = _check_tree(release_dir, "published release")
    manifest_path = release_dir / "manifest.json"
    signature_path = release_dir / "manifest.json.sig"
    manifest = validate_manifest(manifest_path)
    verify_signature(manifest_path, public_key, signature_path)
    return manifest, _sha256(manifest_path)


def _read_catalog(site_dir: Path, public_key: Path) -> dict:
    catalog_path = site_dir / CATALOG_NAME
    releases_root = site_dir / "releases"
    if releases_root.is_symlink():
        raise ValueError("release store must not be a symbolic link")
    if releases_root.exists() and not releases_root.is_dir():
        raise ValueError("release store must be a directory")
    if not catalog_path.exists():
        if catalog_path.is_symlink():
            raise ValueError("catalog must not be a symbolic link")
        if releases_root.exists() and any(releases_root.iterdir()):
            raise ValueError("release directories exist without a catalog")
        return {"schemaVersion": 1, "latestRelease": None, "releases": []}
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ValueError("catalog must be a regular file")

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if (
        not isinstance(catalog, dict)
        or set(catalog) != {"schemaVersion", "latestRelease", "releases"}
        or catalog["schemaVersion"] != 1
        or not isinstance(catalog["releases"], list)
    ):
        raise ValueError("unsupported Pages catalog schema")

    seen: set[str] = set()
    for entry in catalog["releases"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"id", "path", "manifest", "signature", "manifestSha256"}
        ):
            raise ValueError("invalid Pages catalog entry")
        release_id = entry["id"]
        if not isinstance(release_id, str):
            raise ValueError("invalid Pages catalog release ID")
        _check_release_id(release_id)
        expected_prefix = f"releases/{release_id}/"
        if (
            release_id in seen
            or not isinstance(entry["path"], str)
            or not isinstance(entry["manifest"], str)
            or not isinstance(entry["signature"], str)
            or not isinstance(entry["manifestSha256"], str)
            or entry["path"] != expected_prefix
            or entry["manifest"] != f"{expected_prefix}manifest.json"
            or entry["signature"] != f"{expected_prefix}manifest.json.sig"
            or not re.fullmatch(r"[0-9a-f]{64}", entry["manifestSha256"])
        ):
            raise ValueError("invalid or duplicate Pages catalog entry")
        seen.add(release_id)
        manifest, manifest_digest = _verify_release(releases_root / release_id, public_key)
        if manifest_digest != entry["manifestSha256"]:
            raise ValueError(f"catalog manifest digest mismatch for release {release_id}")

    latest = catalog["latestRelease"]
    if (seen and latest not in seen) or (not seen and latest is not None):
        raise ValueError("catalog latestRelease does not name a published release")
    if releases_root.exists():
        actual = {path.name for path in releases_root.iterdir()}
        if actual != seen:
            raise ValueError("release directories do not match the Pages catalog")
    elif seen:
        raise ValueError("catalog lists releases but the release store is missing")
    return catalog


def _atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent,
        prefix=f".{path.name}.", delete=False,
    ) as temporary:
        temporary.write(contents)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _catalog_entry(release_id: str, manifest_digest: str) -> dict[str, str]:
    prefix = f"releases/{release_id}/"
    return {
        "id": release_id,
        "path": prefix,
        "manifest": f"{prefix}manifest.json",
        "signature": f"{prefix}manifest.json.sig",
        "manifestSha256": manifest_digest,
    }


def _write_site_index(site_dir: Path, catalog: dict) -> None:
    latest = catalog["latestRelease"]
    latest_link = (
        f'<p>Latest: <a href="releases/{html.escape(latest)}/">'
        f'{html.escape(latest)}</a></p>'
        if latest is not None
        else "<p>No wheel snapshots have been published yet.</p>"
    )
    release_links = "\n".join(
        f'<li><a href="{html.escape(entry["path"])}">{html.escape(entry["id"])}</a></li>'
        for entry in reversed(catalog["releases"])
    )
    page = (
        "<!doctype html>\n<html lang=\"en\">\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>PyRun Android wheels</title>\n<body>\n"
        "<h1>PyRun Android wheels</h1>\n"
        f"{latest_link}\n<h2>Immutable snapshots</h2>\n<ul>\n{release_links}\n</ul>\n"
        "<p><a href=\"catalog.json\">Machine-readable catalog</a></p>\n"
        "</body>\n</html>\n"
    )
    _atomic_write(site_dir / "index.html", page)
    _atomic_write(
        site_dir / CATALOG_NAME,
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def prepare_pages_site(snapshot: Path, site_dir: Path, release_id: str, public_key: Path) -> None:
    _check_release_id(release_id)
    snapshot = _check_tree(snapshot, "signed snapshot")
    if public_key.is_symlink():
        raise ValueError("public verification key must not be a symbolic link")
    public_key = public_key.resolve(strict=True)
    if not public_key.is_file():
        raise ValueError("public verification key must be a regular file")
    snapshot_manifest = snapshot / "manifest.json"
    manifest = validate_manifest(snapshot_manifest)
    verify_signature(snapshot_manifest, public_key, snapshot / "manifest.json.sig")
    snapshot_digest = _sha256(snapshot_manifest)

    if site_dir.is_symlink():
        raise ValueError("Pages site root must not be a symbolic link")
    site_dir.mkdir(parents=True, exist_ok=True)
    site_dir = site_dir.resolve(strict=True)
    allowed_root_entries = {".git", CATALOG_NAME, "index.html", "releases"}
    unexpected_root_entries = {path.name for path in site_dir.iterdir()} - allowed_root_entries
    if unexpected_root_entries:
        raise ValueError(f"unexpected Pages site entries: {sorted(unexpected_root_entries)}")
    for name in allowed_root_entries:
        entry = site_dir / name
        if entry.is_symlink():
            raise ValueError(f"Pages site entry must not be a symbolic link: {name}")
        if entry.exists() and (
            (name in {CATALOG_NAME, "index.html", ".git"} and not entry.is_file())
            or (name == "releases" and not entry.is_dir())
        ):
            raise ValueError(f"invalid Pages site entry: {name}")
    catalog = _read_catalog(site_dir, public_key)
    existing_ids = {entry["id"] for entry in catalog["releases"]}
    destination = site_dir / "releases" / release_id
    releases_root = site_dir / "releases"
    if releases_root.exists() and (releases_root.is_symlink() or not releases_root.is_dir()):
        raise ValueError("release store must be a real directory")
    if destination.is_symlink():
        raise ValueError("release destination must not be a symbolic link")

    if destination.exists():
        if release_id not in existing_ids:
            raise ValueError("release directory exists but is absent from the catalog")
        _verify_release(destination, public_key)
        if _file_digests(destination) != _file_digests(snapshot):
            raise ValueError(f"release ID {release_id} already exists with different contents")
    else:
        if release_id in existing_ids:
            raise ValueError("catalog entry exists but its release directory is missing")
        promote(snapshot, destination, public_key)
        catalog["releases"].append(_catalog_entry(release_id, snapshot_digest))
        catalog["latestRelease"] = release_id

    _write_site_index(site_dir, catalog)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--site-dir", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--public-key", required=True, type=Path)
    args = parser.parse_args()
    prepare_pages_site(args.snapshot, args.site_dir, args.release_id, args.public_key)
    print(f"Verified and staged immutable Pages snapshot {args.release_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
