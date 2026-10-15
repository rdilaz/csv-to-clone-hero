"""Assemble an allowlisted, offline Windows CPython 3.12 companion ZIP.

Run using the already installed project environment. This copies approved
source and installed dependencies; it never invokes pip, downloads music,
reads a catalog, opens a database, or includes local runtime configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

VERSION = "2026.10.15"
ARTIFACT = f"riff-companion-{VERSION}-windows-py312-x64.zip"
DEPENDENCIES = {
    "annotated-types": "0.8.0", "blinker": "1.9.0", "click": "8.4.2",
    "colorama": "0.4.6", "Flask": "3.1.3", "itsdangerous": "2.2.0",
    "Jinja2": "3.1.6", "MarkupSafe": "3.0.3", "pydantic": "2.13.4",
    "pydantic_core": "2.46.4", "PyYAML": "6.0.3", "RapidFuzz": "3.14.5",
    "typing_extensions": "4.16.0", "typing-inspection": "0.4.4",
    "waitress": "3.0.2", "Werkzeug": "3.1.8",
}
MODULES = (
    "__init__.py", "catalog.py", "catalog_validation.py", "companion.py",
    "config.py", "database.py", "exporter.py", "filesystem.py",
    "library_inventory.py", "matching.py", "models.py", "normalization.py",
    "sources/base.py", "server/catalog_browse.py",
)
ASSETS = (
    "companion.css", "companion.js", "catalog-audio.js", "catalog.js",
    "riff-scene.js", "signal-knot.svg",
)
STUB = '"""Local companion distribution: public server and network adapters are not bundled."""\n'


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source: Path, output: Path) -> dict:
    if platform.system() != "Windows" or sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        raise ValueError("Build with the existing Windows x64 CPython 3.12 environment")
    source, output = source.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    staging = output / f"riff-companion-{VERSION}"
    if staging.exists():
        raise ValueError("Staging already exists; choose a fresh output directory to preserve earlier artifacts")
    staging.mkdir()
    app = staging / "app" / "src" / "csv_to_clonehero"
    for relative in MODULES:
        destination = app / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / "src" / "csv_to_clonehero" / relative, destination)
    for package in ("sources", "server"):
        (app / package / "__init__.py").write_text(STUB, encoding="utf-8")
    assets = app / "server" / "static"
    assets.mkdir()
    for name in ASSETS:
        shutil.copyfile(source / "src" / "csv_to_clonehero" / "server" / "static" / name, assets / name)
    templates = app / "server" / "templates"
    templates.mkdir()
    shutil.copyfile(source / "src" / "csv_to_clonehero" / "server" / "templates" / "companion.html", templates / "companion.html")
    for name in ("bootstrap.py", "START_COMPANION.cmd", "CONFIGURE_COMPANION.cmd", "README.txt"):
        shutil.copyfile(source / "distribution" / "companion" / name, staging / name)
    shutil.copyfile(source / "LICENSE", staging / "LICENSE")

    vendor = staging / "vendor"
    notices = ["Riff companion - bundled dependency notices", "", "Original license files and metadata are preserved in vendor/*dist-info/.", ""]
    distribution_info = []
    for name, version in DEPENDENCIES.items():
        dist = importlib.metadata.distribution(name)
        if dist.version != version:
            raise ValueError(f"Expected {name}=={version}; found {dist.version}")
        license_files = []
        for item in dist.files or ():
            relative = Path(str(item))
            if relative.is_absolute() or ".." in relative.parts:
                continue  # Wheel entry points outside site-packages are never included.
            if any(part == "__pycache__" for part in relative.parts) or relative.suffix in {".pyc", ".pth"}:
                continue
            if relative.name in {"direct_url.json", "REQUESTED"}:
                continue
            original = Path(dist.locate_file(item))
            if not original.is_file():
                continue
            destination = vendor / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
            if "license" in relative.name.casefold() or "copying" in relative.name.casefold():
                license_files.append(relative.as_posix())
        if not license_files:
            raise ValueError(f"No installed license notice found for {name}")
        license_name = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See preserved notices"
        license_name = license_name.splitlines()[0][:150]
        distribution_info.append({"name": name, "version": version, "license": license_name, "notices": license_files})
        notices.extend([f"{name}=={version} | {license_name}", *["  vendor/" + value for value in license_files], ""])
    (staging / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notices), encoding="utf-8")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    files = {path.relative_to(staging).as_posix(): sha256(path) for path in sorted(staging.rglob("*")) if path.is_file()}
    forbidden = [name for name in files if name.lower().endswith((".sng", ".ogg", ".opus", ".mp3", ".wav", ".jsonl", ".db", ".sqlite", ".env", ".pyc", ".pth"))]
    if forbidden:
        raise ValueError(f"Forbidden runtime/user artifacts found: {forbidden}")
    changed = bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source, text=True).strip())
    manifest = {
        "version": VERSION,
        "reviewed_source_revision": "e458f27296c0df305ff356cc915556788f770be9",
        "build_repository_revision": revision, "source_worktree_changes": changed,
        "companion_source_sha256": files["app/src/csv_to_clonehero/companion.py"],
        "distribution_source_tree_sha256": hashlib.sha256(json.dumps(
            {name: digest for name, digest in files.items() if not name.startswith("vendor/")},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "target": "Windows x64 CPython 3.12 (existing runtime required)",
        "python_runtime_bundled": False, "music_catalog_or_user_data_bundled": False,
        "network_clients_bundled": False,
        "packaging_stubs": ["app/src/csv_to_clonehero/server/__init__.py", "app/src/csv_to_clonehero/sources/__init__.py"],
        "rapidfuzz_implementation": "python", "dependencies": distribution_info, "files": files,
    }
    (staging / "RELEASE_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    target = output / ARTIFACT
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(staging.name) / path.relative_to(staging)).as_posix())
    digest = sha256(target)
    (output / (ARTIFACT + ".sha256")).write_text(f"{digest}  {ARTIFACT}\n", encoding="ascii")
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {ARTIFACT}\n", encoding="ascii")
    result = {"version": VERSION, "artifacts": [{"filename": ARTIFACT, "sha256": digest, "size_bytes": target.stat().st_size}]}
    (output / "manifest.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), indent=2))


if __name__ == "__main__":
    main()
