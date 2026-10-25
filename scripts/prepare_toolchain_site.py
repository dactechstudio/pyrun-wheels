"""Add verified signed compiler packs to the cumulative Pages site."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

from scripts.verify_android_toolchain_pack import verify_pack


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract(pack: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(pack, mode="r:xz") as archive:
        for member in archive.getmembers():
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError(f"compiler pack contains an unsafe archive member: {member.name}")
            if member.issym() or member.islnk():
                link = Path(member.linkname)
                if link.is_absolute() or ".." in link.parts:
                    raise ValueError(f"compiler pack contains an unsafe link: {member.name}")
            target = (destination / name).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError(f"compiler pack escapes its root: {member.name}")
        archive.extractall(destination)
    return destination


def _load_catalog(path: Path) -> dict:
    if not path.exists():
        return {"schemaVersion": 1, "latest": {}, "releases": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schemaVersion") != 1 or not isinstance(value.get("latest"), dict) or not isinstance(value.get("releases"), list):
        raise ValueError("toolchain catalog is corrupt")
    return value


def prepare(site: Path, release_id: str, public_key: Path, arm64_pack: Path, x86_64_pack: Path) -> None:
    site = site.resolve()
    if not site.is_dir():
        raise ValueError("Pages site directory is missing")
    if not release_id or "/" in release_id or "\\" in release_id or ".." in release_id:
        raise ValueError("release id is unsafe")
    catalog_path = site / "catalog" / "toolchains.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog = _load_catalog(catalog_path)
    release_entries = []
    with tempfile.TemporaryDirectory(prefix="pyrun-toolchain-site-") as temporary:
        for abi, pack in (("arm64_v8a", arm64_pack), ("x86_64", x86_64_pack)):
            pack = pack.resolve(strict=True)
            extracted = _extract(pack, Path(temporary) / abi)
            manifest = verify_pack(extracted, public_key)
            if manifest["abi"] != abi:
                raise ValueError(f"compiler pack ABI mismatch: expected {abi}, got {manifest['abi']}")
            version = manifest["version"]
            destination = site / "toolchains" / abi / version
            if destination.exists():
                raise FileExistsError(f"refusing to replace published compiler pack: {destination}")
            destination.mkdir(parents=True, exist_ok=False)
            archive_name = f"pyrun-toolchain-{abi}-{version}.tar.xz"
            shutil.copyfile(pack, destination / archive_name)
            shutil.copyfile(extracted / "manifest.json", destination / "manifest.json")
            shutil.copyfile(extracted / "manifest.json.sig", destination / "manifest.json.sig")
            entry = {
                "abi": abi,
                "version": version,
                "releaseId": release_id,
                "pack": f"../toolchains/{abi}/{version}/{archive_name}",
                "packSha256": _sha256(pack),
                "packSize": pack.stat().st_size,
                "manifest": f"../toolchains/{abi}/{version}/manifest.json",
                "signature": f"../toolchains/{abi}/{version}/manifest.json.sig",
            }
            release_entries.append(entry)
            catalog["latest"][abi] = entry
    catalog["releases"] = [entry for entry in catalog["releases"] if entry.get("releaseId") != release_id]
    catalog["releases"].extend(release_entries)
    catalog["releases"].sort(key=lambda entry: (entry["releaseId"], entry["abi"]))
    catalog_path.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--arm64-pack", required=True, type=Path)
    parser.add_argument("--x86-64-pack", required=True, type=Path)
    args = parser.parse_args()
    prepare(args.site, args.release_id, args.public_key, args.arm64_pack, args.x86_64_pack)
    print(f"Published compiler-pack catalog release {args.release_id}")


if __name__ == "__main__":
    main()
