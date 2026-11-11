#!/usr/bin/env python3
"""Read-only version gate; never applies a patch or imports Hermes."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Hermes source checkout (not a profile)")
    parser.add_argument("--state", choices=("before", "after"), default="before")
    args = parser.parse_args()
    root = args.root.resolve()
    here = Path(__file__).resolve().parent
    manifest = json.loads((here / "manifest.json").read_text())
    errors = []
    try:
        top = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True).strip()
        revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        print("STOP: root must be a Git source checkout.", file=sys.stderr)
        return 1
    if Path(top).resolve() != root or revision != manifest["head"]:
        errors.append("checkout root or exact HEAD mismatch")
    patch = here / manifest["patch"]
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["patch_sha256"]:
        errors.append("patch SHA256 mismatch")
    for name, states in manifest["files"].items():
        path = root / name
        expected = states[args.state]
        # Reject symlinked files AND parent directories; never follow them into a profile.
        parts = [path, *path.parents]
        if any(p.is_symlink() for p in parts if p != root and root in p.parents):
            errors.append(name + ": symlink rejected")
        elif expected is None:
            if path.exists():
                errors.append(name + ": must be absent")
        elif not path.is_file():
            errors.append(name + ": missing regular file")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(name + ": entire-file SHA256 mismatch")
    if errors:
        for error in errors:
            print("STOP: " + error, file=sys.stderr)
        return 1
    print("OK: exact HEAD, patch, and all 12 complete file states match " + args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
