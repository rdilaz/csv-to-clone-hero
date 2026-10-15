"""Offline launcher for the Windows / CPython 3.12 x64 companion distribution."""
from __future__ import annotations

import argparse
import json
import os
import platform
import struct
import sys
from pathlib import Path


def parse_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) - {"catalog", "library_roots", "artifact_roots", "port"}:
            raise ValueError("Unknown settings")
        for key in ("library_roots", "artifact_roots"):
            value = data.get(key, [])
            if not isinstance(value, list) or len(value) > 10 or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{key} must contain at most ten paths")
        if not isinstance(data.get("catalog", ""), str):
            raise ValueError("catalog must be a path")
        port = data.get("port", 8947)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        return data
    except (OSError, ValueError) as exc:
        raise ValueError("Settings could not be read. Run CONFIGURE_COMPANION.cmd to replace them.") from exc


def configure(data_dir: Path) -> None:
    print("Riff companion - local configuration")
    print("Nothing is downloaded. Your library is read-only. Press Enter to skip a path.")
    chosen = {}
    for key, label, is_file in (
        ("catalog", "Existing checksum-valid catalog .jsonl file", True),
        ("artifact_roots", "Existing export-artifacts cache folder (optional)", False),
        ("library_roots", "Existing Songs folder to inspect read-only (optional)", False),
    ):
        value = input(label + ": ").strip().strip('"')
        if value:
            path = Path(value).expanduser().absolute()
            if not (path.is_file() if is_file else path.is_dir()):
                raise ValueError(f"The selected {label.lower()} does not exist; settings were not saved.")
            chosen[key] = str(path) if is_file else [str(path)]
    for value in chosen.get("library_roots", []) + chosen.get("artifact_roots", []):
        library = Path(value).resolve()
        settings_root = data_dir.resolve()
        if settings_root == library or settings_root.is_relative_to(library) or library.is_relative_to(settings_root):
            raise ValueError("Choose a settings/data folder separate from the read-only input folders.")
    if chosen.get("catalog"):
        catalog_folder = Path(chosen["catalog"]).resolve().parent
        if data_dir.resolve() == catalog_folder or data_dir.resolve().is_relative_to(catalog_folder):
            raise ValueError("Choose a settings/data folder outside the read-only catalog folder.")
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = data_dir / "settings.json"
    settings.write_text(json.dumps(chosen, indent=2) + "\n", encoding="utf-8")
    print("Settings saved locally. Start START_COMPANION.cmd to open the companion.")


def main() -> int:
    if platform.system() != "Windows" or sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        print("This download requires an existing Windows x64 CPython 3.12 installation.", file=sys.stderr)
        print("It includes dependencies, not the Python runtime. No installation was attempted.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".riff-companion")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--check", action="store_true", help="Verify local runtime and imports without opening the server")
    args, extra = parser.parse_known_args()
    data_dir = args.data_dir.expanduser().absolute()
    try:
        if args.configure:
            configure(data_dir)
            return 0
        settings = parse_settings(data_dir / "settings.json")
        base = Path(__file__).resolve().parent
        sys.path[:0] = [str(base / "vendor"), str(base / "app" / "src")]
        # Optional RapidFuzz acceleration has a separate MSVC runtime
        # prerequisite. The pure Python implementation avoids installing it.
        os.environ["RAPIDFUZZ_IMPLEMENTATION"] = "python"
        from csv_to_clonehero.companion import create_app, main as companion_main
        import waitress
        if args.check:
            app = create_app(catalog_path=data_dir / "catalog" / "enchor.jsonl", sandbox_root=data_dir / "sandbox")
            response = app.test_client().get("/api/companion/status")
            if response.status_code != 200 or response.get_json()["network_downloads"] is not False:
                raise ValueError("Companion self-check failed")
            print("Runtime and offline companion imports verified. No catalog or music is bundled.")
            return 0
        forwarded = ["--data-dir", str(data_dir)]
        if settings.get("catalog"):
            forwarded.extend(["--catalog", settings["catalog"]])
        for key, flag in (("library_roots", "--library-root"), ("artifact_roots", "--artifact-root")):
            for value in settings.get(key, []):
                forwarded.extend([flag, value])
        if "port" in settings:
            forwarded.extend(["--port", str(settings["port"])])
        sys.argv = [str(base / "bootstrap.py"), *forwarded, *extra]
        companion_main()
        return 0
    except (OSError, ValueError, ImportError) as exc:
        print(f"Riff could not start: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
