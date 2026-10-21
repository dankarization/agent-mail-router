#!/usr/bin/env python3
"""Offline source/test gate. No package installation or production access."""
import ast
import json
import os
from pathlib import Path
import sys
import sysconfig
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
os.environ["MAIL_ROUTER_CONFIG"] = str(ROOT / "examples/settings.example.json")
sys.path[:0] = [str(ROOT / "tools/tests"), str(ROOT / "tools")]
manifest = json.loads((ROOT / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
import hashlib
for row in manifest["files"]:
    path = ROOT / row["path"]
    assert path.is_file() and not path.is_symlink(), row["path"]
    assert path.stat().st_size == row["bytes"], row["path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"], row["path"]
actual = {str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
          if p.is_file() and not any(part in (".git", ".local", "__pycache__") for part in p.relative_to(ROOT).parts)}
assert actual == {row["path"] for row in manifest["files"]} | {"SOURCE_MANIFEST.json"}, "Unexpected source paths"
local_modules = {p.stem for p in (ROOT / "tools").glob("*.py")}
local_modules.update(p.stem for p in (ROOT / "tools/tests").glob("*.py"))
for path in sorted((ROOT / "tools").rglob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    compile(tree, str(path), "exec")
    for node in ast.walk(tree):
        names = ([n.name for n in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
        for name in names:
            head = name.split(".")[0]
            if head not in sys.stdlib_module_names and head not in local_modules:
                raise AssertionError("Non-stdlib or missing local dependency: " + name)

(ROOT / ".local").mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="mail-router-check-", dir=ROOT / ".local") as scratch:
    previous_temp = tempfile.tempdir
    tempfile.tempdir = scratch
    read_roots = [ROOT, Path(scratch), Path(sysconfig.get_path("stdlib")),
                  Path("/usr/share/zoneinfo")]

    def guard(event, args):
        if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.posix_spawn"):
            raise AssertionError("External boundary forbidden: " + event)
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path != Path(os.devnull) and not any(path.is_relative_to(p) for p in read_roots):
                raise AssertionError("External file access forbidden: " + str(path))
    # The audit open event omits dir_fd. Normalize os.open calls first so
    # shutil's fd-relative cleanup is checked against the actual directory,
    # independently of the process working directory.
    original_open = os.open
    def absolute_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and not os.path.isabs(path):
            path = os.path.join(os.readlink("/proc/self/fd/" + str(dir_fd)), path)
            dir_fd = None
        return original_open(path, flags, mode, dir_fd=dir_fd)
    os.open = absolute_open
    sys.addaudithook(guard)
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tools/tests"), pattern="test_syncopia_agent_mail_*.py")
    for name, module in list(sys.modules.items()):
        if name.startswith(("syncopia_agent_mail_", "test_syncopia_agent_mail_")):
            assert Path(module.__file__).resolve().is_relative_to(ROOT), name
        if name.startswith("test_syncopia_agent_mail_") and hasattr(module, "m"):
            assert Path(module.m.__file__).resolve().is_relative_to(ROOT), name
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    tempfile.tempdir = previous_temp
print(json.dumps({"tests": result.testsRun, "successful": result.wasSuccessful(),
                  "compile": "PASS", "dependencies": "stdlib and repository only",
                  "network_subprocess_external_files": "forbidden"}))
raise SystemExit(0 if result.wasSuccessful() else 1)
