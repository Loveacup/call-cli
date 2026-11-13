#!/usr/bin/env python3
"""Run narrow compatibility regressions on a disposable source copy; never patch it."""
import argparse
import os
from pathlib import Path
import socket
import sys
import types

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path)
parser.add_argument("run", type=Path, help="new scratch output directory")
parser.add_argument("targets", nargs="*", help="relative tests/gateway paths")
args = parser.parse_args()
root, run = args.root.resolve(), args.run.resolve()
run.mkdir(parents=True, exist_ok=False)
home = run / "home"
(home / ".hermes").mkdir(parents=True)
(run / "tmp").mkdir()
os.environ.clear()
os.environ.update(PATH="/usr/bin:/bin", LANG="en_US.UTF-8", TZ="UTC",
    HOME=str(home), HERMES_HOME=str(home / ".hermes"),
    HERMES_TEST_ISOLATION=str(home / ".hermes"), TMPDIR=str(run / "tmp"),
    PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
os.chdir(run)
sys.path.insert(0, str(root))
sys.dont_write_bytecode = True
sys._hermes_relaunched = True
sys.modules["hermes_bootstrap"] = types.ModuleType("hermes_bootstrap")


def blocked(*args, **kwargs):
    raise RuntimeError("TEST_NETWORK_BLOCKED")


socket.socket.connect = socket.socket.connect_ex = blocked
socket.create_connection = blocked
sys.modules["hermes_bootstrap"]._happy_eyeballs_create_connection = blocked


def audit(event, args):
    paths = []
    if event == "open":
        path, mode, flags = args
        if ((isinstance(mode, str) and any(c in mode for c in "wax+")) or
                (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))):
            paths = [path]
    if event in ("os.mkdir", "os.remove", "os.rmdir", "os.rename"):
        paths = args[:2] if event == "os.rename" else args[:1]
    for path in paths:
        if isinstance(path, (str, bytes, os.PathLike)):
            if not Path(os.fsdecode(path)).resolve().is_relative_to(run):
                raise PermissionError("TEST_WRITE_OUTSIDE_SCRATCH")
    if event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"):
        raise PermissionError("TEST_CHILD_PROCESS_BLOCKED")


sys.addaudithook(audit)
import pytest

targets = args.targets or ["test_leading_silence.py", "test_mixed_turn_silence.py",
                           "test_stream_consumer.py", "test_response_filters.py",
                           "test_gateway_silence_tokens.py"]
print("Source:", root, "Python:", sys.version.split()[0], "pytest:", pytest.__version__, flush=True)
raise SystemExit(pytest.main(["-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider",
    "--confcutdir", str(root / "tests"), "--basetemp", str(run / "tmp/pytest"),
    "--log-file", str(run / "pytest.log"), "-q",
    *[str(root / "tests/gateway" / name) for name in targets]]))
