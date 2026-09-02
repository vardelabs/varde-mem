#!/usr/bin/env python3
"""Smoke test for snapshot.sh (vardemem consolidate-memory undo helper).

Runs snapshot.sh against a throwaway $HOME so its hardcoded snapshot root
(~/.local/state/memory-snapshots) is controlled. Covers a normal snapshot, the
portable-unique-suffix guarantee (two same-second passes get distinct, non-nested
dirs -- the fix does not depend on `date +%N`, absent on BSD/macOS), pruning to
the last 10, and that the prune never follows a symlinked entry into its target.

Usage: python3 tests/smoke-snapshot.py   (exit 0 = all pass)
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SNAP = Path(__file__).resolve().parent.parent / "skills" / "consolidate-memory" / "snapshot.sh"


def run_snap(home: Path, memdir: Path):
    env = dict(os.environ)
    env["HOME"] = str(home)
    return subprocess.run(["bash", str(SNAP), str(memdir)], capture_output=True, text=True, env=env)


def snaproot(home: Path) -> Path:
    return home / ".local/state/memory-snapshots"


def make_memdir(base: Path) -> Path:
    md = base / "memory"
    md.mkdir(parents=True, exist_ok=True)
    (md / "a.md").write_text("aaa", encoding="utf-8")
    (md / "b.md").write_text("bbb", encoding="utf-8")
    return md


def main() -> int:
    cases = []

    # 1. normal snapshot mirrors the memory dir (no nesting).
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = make_memdir(home / "work")
        p = run_snap(home, md)
        dirs = list(snaproot(home).iterdir())
        ok = (p.returncode == 0 and len(dirs) == 1
              and (dirs[0] / "a.md").is_file() and (dirs[0] / "b.md").is_file()
              and not (dirs[0] / "memory").exists())
        cases.append(("normal snapshot mirrors the memory dir (no nesting)", ok))

    # 2. two same-second passes get distinct, non-nested dirs (no reliance on %N).
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = make_memdir(home / "work")
        p1 = run_snap(home, md); p2 = run_snap(home, md)
        dirs = sorted(snaproot(home).iterdir())
        ok = (p1.returncode == 0 and p2.returncode == 0 and len(dirs) == 2
              and all((d / "a.md").is_file() and not (d / "memory").exists() for d in dirs))
        cases.append(("two same-second passes -> 2 distinct, non-nested dirs", ok))

    # 3. prune keeps the last 10.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = make_memdir(home / "work")
        for _ in range(12):
            run_snap(home, md)
        cases.append(("prune keeps the last 10", len(list(snaproot(home).iterdir())) == 10))

    # 4. the prune idiom does not follow a symlinked entry into its target.
    with tempfile.TemporaryDirectory() as td:
        sr = Path(td) / "sr"; (sr / "target").mkdir(parents=True)
        (sr / "target" / "sentinel").write_text("x", encoding="utf-8")
        os.symlink(sr / "target", sr / "oldlink")
        # exactly snapshot.sh's fixed prune step applied to the symlinked entry.
        subprocess.run(["bash", "-c", f'old="{sr}/oldlink/"; rm -rf "${{old%/}}"'], check=True)
        idiom_ok = (sr / "target" / "sentinel").exists() and not (sr / "oldlink").exists()
        script_uses_idiom = 'rm -rf "${old%/}"' in SNAP.read_text(encoding="utf-8")
        cases.append(("prune does not follow a symlinked entry (and snapshot.sh uses the idiom)",
                      idiom_ok and script_uses_idiom))

    ok = True
    for label, passed in cases:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("smoke-snapshot:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
