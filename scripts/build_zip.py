#!/usr/bin/env python3
"""Package the Decky plugin: out/tcl-me-remo.zip.

The loader wants exactly one top-level folder with plugin.json directly inside it, and
unpacks the archive as it is.  What goes in is listed here and nothing else does: no sources,
tests, preview or node_modules.

The file name is part of the contract.  When a plugin is installed from a ZIP, Decky treats
it as an update of an installed plugin only if the file's name without ".zip" equals the
"name" in plugin.json.  With any other name (a version suffix, a browser's " (1)") it unpacks
the archive over the running plugin instead, without the uninstall step and without clearing
out files the new version no longer has.  So the archive carries no version in its name; the
version is in the package.json inside.

    pnpm install --frozen-lockfile && pnpm run build     # produces dist/index.js
    python3 scripts/build_zip.py                         # prints the path of the zip

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLDER = "tcl-me-remo"
# Files copied from the repository root, in this order.
ROOT_FILES = ("plugin.json", "package.json", "main.py", "LICENSE", "README.md")
BUNDLE = "dist/index.js"
PACKAGE = "py_modules/tcl_me_remo"
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = (".pyc", ".pyo")
# A fixed time stamp, so that the same sources give the same archive.
ZIP_TIME = (2020, 1, 1, 0, 0, 0)


class BuildError(Exception):
    """The plugin cannot be packaged; the message says what is missing."""


def package_files(package: Path) -> list[Path]:
    """Every file of the service's Python package, sorted, without caches."""
    found: list[Path] = []
    for directory, subdirs, names in os.walk(package):
        subdirs[:] = sorted(name for name in subdirs if name not in SKIP_DIRS)
        found.extend(Path(directory) / name for name in sorted(names) if not name.endswith(SKIP_SUFFIXES))
    return found


def collect(root: Path) -> list[tuple[Path, str]]:
    """(source file, path inside the zip) for everything that ships."""
    if not (root / BUNDLE).is_file():
        raise BuildError(f"{BUNDLE} is missing: run 'pnpm install --frozen-lockfile && pnpm run build' first")
    missing = [name for name in ROOT_FILES if not (root / name).is_file()]
    if missing:
        raise BuildError(f"missing from {root}: {', '.join(missing)}")
    if not (root / PACKAGE / "__init__.py").is_file():
        raise BuildError(f"{PACKAGE} is missing from {root}")
    files = [(root / name, f"{FOLDER}/{name}") for name in ROOT_FILES]
    files.append((root / BUNDLE, f"{FOLDER}/{BUNDLE}"))
    files.extend((path, f"{FOLDER}/{path.relative_to(root).as_posix()}") for path in package_files(root / PACKAGE))
    return files


def plugin_version(root: Path) -> str:
    """The version Decky will show (package.json); it must match the service's own."""
    try:
        version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]
        manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        raise BuildError(f"package.json or plugin.json cannot be read: {exc}") from None
    if not isinstance(version, str) or not version:
        raise BuildError("package.json has no version")
    if "root" not in manifest.get("flags", []) or manifest.get("api_version") != 1:
        raise BuildError('plugin.json must have "root" in flags and api_version 1')
    init = (root / PACKAGE / "__init__.py").read_text(encoding="utf-8")
    if f'__version__ = "{version}"' not in init:
        raise BuildError(f"package.json says version {version} but {PACKAGE}/__init__.py does not")
    return version


def build(root: Path = ROOT, out_dir: Path | None = None) -> Path:
    """Write the zip and return its path."""
    files = collect(root)
    plugin_version(root)  # refuses to package inconsistent versions or a missing root flag
    out_dir = out_dir if out_dir is not None else root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{FOLDER}.zip"
    partial = target.with_suffix(".zip.part")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, name in files:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            # The loader sets every file to 755 after unpacking; say the same here.
            info.external_attr = 0o100755 << 16
            archive.writestr(info, source.read_bytes())
    os.replace(partial, target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None, help="directory for the zip (default: out/)")
    args = parser.parse_args(argv)
    try:
        target = build(ROOT, args.out)
    except BuildError as exc:
        print(f"build_zip: {exc}", file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
