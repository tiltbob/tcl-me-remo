"""The plugin zip has exactly the layout Decky Loader requires, and nothing else in it."""

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def build_zip():
    spec = importlib.util.spec_from_file_location("build_zip", REPO / "scripts" / "build_zip.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tree(tmp_path):
    """A small plugin checkout, with the kind of clutter a real one has."""
    root = tmp_path / "repo"
    files = {
        "plugin.json": json.dumps({"name": "tcl-me-remo", "author": "x", "flags": ["root"], "api_version": 1}),
        "package.json": json.dumps({"name": "tcl-me-remo", "version": "1.2.3", "type": "module"}),
        "main.py": "class Plugin: pass\n",
        "LICENSE": "MIT\n",
        "README.md": "# readme\n",
        "dist/index.js": "export default 1;\n",
        "dist/index.js.map": "{}",
        "py_modules/tcl_me_remo/__init__.py": '__version__ = "1.2.3"\n',
        "py_modules/tcl_me_remo/service.py": "pass\n",
        "py_modules/tcl_me_remo/bluez/gatt.py": "pass\n",
        "py_modules/tcl_me_remo/_vendor/dbus_fast/LICENSE": "MIT\n",
        "py_modules/tcl_me_remo/__pycache__/service.cpython-313.pyc": "x",
        "py_modules/tcl_me_remo/bluez/__pycache__/gatt.cpython-313.pyc": "x",
        "py_modules/tcl_me_remo/stray.pyc": "x",
        "py_modules/other_package/__init__.py": "pass\n",
        "src/index.tsx": "export default 1;\n",
        "tests/test_x.py": "pass\n",
        "preview/main.tsx": "pass\n",
        "node_modules/react/index.js": "pass\n",
        "docs/images/panel.png": "x",
        "install.sh": "#!/bin/sh\n",
        "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_zip_contains_exactly_the_plugin(build_zip, tree, tmp_path):
    target = build_zip.build(tree, tmp_path / "out")
    # No version in the file name: Decky compares it with plugin.json's "name" to tell an
    # update from a fresh install.
    assert target == tmp_path / "out" / "tcl-me-remo.zip"
    assert target.stem == json.loads((tree / "plugin.json").read_text())["name"]
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert archive.testzip() is None
        assert archive.read("tcl-me-remo/dist/index.js") == b"export default 1;\n"
    assert sorted(names) == [
        "tcl-me-remo/LICENSE",
        "tcl-me-remo/README.md",
        "tcl-me-remo/dist/index.js",
        "tcl-me-remo/main.py",
        "tcl-me-remo/package.json",
        "tcl-me-remo/plugin.json",
        "tcl-me-remo/py_modules/tcl_me_remo/__init__.py",
        "tcl-me-remo/py_modules/tcl_me_remo/_vendor/dbus_fast/LICENSE",
        "tcl-me-remo/py_modules/tcl_me_remo/bluez/gatt.py",
        "tcl-me-remo/py_modules/tcl_me_remo/service.py",
    ]


def test_zip_passes_the_loaders_own_check(build_zip, tree, tmp_path):
    # Decky Loader (browser.py): exactly one "<folder>/plugin.json" at depth one.
    target = build_zip.build(tree, tmp_path / "out")
    with zipfile.ZipFile(target) as archive:
        manifests = [name for name in archive.namelist() if name.endswith("/plugin.json") and name.count("/") == 1]
        assert manifests == ["tcl-me-remo/plugin.json"]
        assert {name.split("/")[0] for name in archive.namelist()} == {"tcl-me-remo"}
        manifest = json.loads(archive.read(manifests[0]))
        package = json.loads(archive.read("tcl-me-remo/package.json"))
    assert "root" in manifest["flags"] and manifest["api_version"] == 1
    assert package["type"] == "module"


def test_same_sources_give_the_same_zip(build_zip, tree, tmp_path):
    first = build_zip.build(tree, tmp_path / "a").read_bytes()
    second = build_zip.build(tree, tmp_path / "b").read_bytes()
    assert first == second


def test_fails_without_the_frontend_bundle(build_zip, tree, tmp_path):
    (tree / "dist" / "index.js").unlink()
    with pytest.raises(build_zip.BuildError, match="dist/index.js is missing"):
        build_zip.build(tree, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_fails_when_versions_disagree(build_zip, tree, tmp_path):
    (tree / "py_modules" / "tcl_me_remo" / "__init__.py").write_text('__version__ = "9.9.9"\n', encoding="utf-8")
    with pytest.raises(build_zip.BuildError, match="version 1.2.3"):
        build_zip.build(tree, tmp_path / "out")


def test_fails_without_the_root_flag(build_zip, tree, tmp_path):
    manifest = {"name": "tcl-me-remo", "author": "x", "flags": ["_root"], "api_version": 1}
    (tree / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(build_zip.BuildError, match="root"):
        build_zip.build(tree, tmp_path / "out")


def test_command_reports_a_missing_bundle(build_zip, tree, tmp_path, monkeypatch, capsys):
    (tree / "dist" / "index.js").unlink()
    monkeypatch.setattr(build_zip, "ROOT", tree)
    assert build_zip.main(["--out", str(tmp_path / "out")]) == 1
    assert "dist/index.js is missing" in capsys.readouterr().err


def test_repository_versions_agree(build_zip):
    # The real checkout: package.json, plugin.json and the service's __version__ line up.
    assert build_zip.plugin_version(REPO)
    manifest = json.loads((REPO / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "tcl-me-remo"
