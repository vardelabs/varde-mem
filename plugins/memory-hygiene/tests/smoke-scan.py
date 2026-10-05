#!/usr/bin/env python3
"""Smoke test for scan.py (memory-hygiene consolidate-memory scanner).

Runs scan.py as a subprocess against a throwaway $HOME and memory dir. Covers the
verdict logic (bloat -> NEEDS_RUN, healthy -> SKIP, missing dir -> ERROR) and two
config-resolution invariants: misroute detection is OFF without a routing config
(opt-in, like the hook), and a present-but-unreadable canonical config does not
fall back to a stale legacy config.

Usage: python3 tests/smoke-scan.py   (exit 0 = all pass)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCAN = Path(__file__).resolve().parent.parent / "skills" / "consolidate-memory" / "scan.py"


def run_scan(home: Path, memdir: Path) -> tuple:
    env = dict(os.environ)
    env["HOME"] = str(home)
    p = subprocess.run([sys.executable, str(SCAN), "--dir", str(memdir)],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout


def verdict(out: str) -> str:
    for line in reversed(out.splitlines()):
        if line.startswith("VERDICT:"):
            return line.split()[1]
    return "?"


def write_mems(memdir: Path, n: int, ftype: str) -> None:
    memdir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (memdir / f"m{i}.md").write_text(f"---\ntype: {ftype}\n---\nb\n", encoding="utf-8")


def write_cfg(home: Path, *, legacy: bool = False) -> None:
    d = home / (".config/harness-management" if legacy else ".config/memory-hygiene")
    d.mkdir(parents=True, exist_ok=True)
    (d / "memory-routing.json").write_text(
        json.dumps({"enabled": True, "trip_types": {"project": "x", "user": "y"}}), encoding="utf-8")


def main() -> int:
    cases = []

    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 22, "reference")
        rc, out = run_scan(home, md)
        cases.append(("bloat (22 files) -> NEEDS_RUN", rc == 0 and verdict(out) == "NEEDS_RUN"))

    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 18, "reference")
        rc, out = run_scan(home, md)
        cases.append(("file-count trip (18 files) -> NEEDS_RUN", rc == 0 and verdict(out) == "NEEDS_RUN"))

    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 17, "reference")
        rc, out = run_scan(home, md)
        cases.append(("just under every trip (17 small files) -> SKIP", rc == 0 and verdict(out) == "SKIP"))

    # One over-grown file trips a run on its own, though the dir is small.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 3, "reference")
        (md / "big.md").write_text("---\ntype: reference\n---\n" + "x" * 8_000, encoding="utf-8")
        rc, out = run_scan(home, md)
        cases.append(("one file >= 8000 B -> NEEDS_RUN, named as oversized",
                      rc == 0 and verdict(out) == "NEEDS_RUN" and "big.md" in out))

    # Many files each under the per-file trip still trip on total size.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; md.mkdir(parents=True)
        for i in range(10):
            (md / f"m{i}.md").write_text("---\ntype: reference\n---\n" + "x" * 6_500, encoding="utf-8")
        rc, out = run_scan(home, md)
        cases.append(("total >= 60000 B with no oversized file -> NEEDS_RUN",
                      rc == 0 and verdict(out) == "NEEDS_RUN" and "oversized: none" in out))

    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 5, "reference")
        rc, out = run_scan(home, md)
        cases.append(("healthy (5 files) -> SKIP", rc == 0 and verdict(out) == "SKIP"))

    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        rc, out = run_scan(home, home / "nope")
        cases.append(("missing dir -> ERROR", rc == 0 and verdict(out) == "ERROR"))

    # F1: misroute detection is opt-in. No config + 2 project memories stays SKIP.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 2, "project")
        rc, out = run_scan(home, md)
        cases.append(("F1: no config, project memories -> SKIP (misroute off)",
                      rc == 0 and verdict(out) == "SKIP"))

    # F1: with a routing config, the same 2 project memories trip a run.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 2, "project"); write_cfg(home)
        rc, out = run_scan(home, md)
        cases.append(("F1: with config, project memories -> NEEDS_RUN (misroute on)",
                      rc == 0 and verdict(out) == "NEEDS_RUN"))

    # F2: a present-but-unreadable canonical (here a directory) must NOT fall back
    # to a stale enabled legacy config, so misroute detection stays off -> SKIP.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 2, "project")
        write_cfg(home, legacy=True)  # legacy would trip if wrongly served
        (home / ".config/memory-hygiene/memory-routing.json").mkdir(parents=True)  # canonical is a DIR
        rc, out = run_scan(home, md)
        cases.append(("F2: unreadable (directory) canonical does not use legacy -> SKIP",
                      rc == 0 and verdict(out) == "SKIP" and "served from legacy" not in out))

    # scanner honors `enabled`: an enabled:false config yields no trip set, so
    # misroute detection is off (routing is not done on a schedule when disabled).
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 2, "project")
        d = home / ".config/memory-hygiene"; d.mkdir(parents=True, exist_ok=True)
        (d / "memory-routing.json").write_text(
            json.dumps({"enabled": False, "trip_types": {"project": "x"}}), encoding="utf-8")
        rc, out = run_scan(home, md)
        cases.append(("enabled:false config -> SKIP (routing honors enabled)",
                      rc == 0 and verdict(out) == "SKIP"))

    # A DISABLED canonical must suppress an ENABLED legacy fallback (scanner half): with
    # no env kill-switch, enabled:false at the canonical path is a migrating tenant's only stop.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 2, "project")
        d = home / ".config/memory-hygiene"; d.mkdir(parents=True, exist_ok=True)
        (d / "memory-routing.json").write_text(
            json.dumps({"enabled": False, "trip_types": {"project": "x"}}), encoding="utf-8")
        write_cfg(home, legacy=True)  # enabled legacy, must NOT serve
        rc, out = run_scan(home, md)
        cases.append(("disabled canonical suppresses enabled legacy -> SKIP",
                      rc == 0 and verdict(out) == "SKIP" and "served from legacy" not in out))

    # a hyphenated trip_type key + a hyphenated memory type trips (parser accepts hyphens/digits).
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; write_mems(md, 2, "project-fact")
        d = home / ".config/memory-hygiene"; d.mkdir(parents=True, exist_ok=True)
        (d / "memory-routing.json").write_text(
            json.dumps({"enabled": True, "trip_types": {"project-fact": "x"}}), encoding="utf-8")
        rc, out = run_scan(home, md)
        cases.append(("hyphenated type + matching config -> NEEDS_RUN",
                      rc == 0 and verdict(out) == "NEEDS_RUN"))

    # a trailing YAML inline comment on the type line is still parsed and detected.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td); md = home / "m"; md.mkdir(parents=True, exist_ok=True)
        for i in range(2):
            (md / f"m{i}.md").write_text("---\ntype: project # vault-bound\n---\nb\n", encoding="utf-8")
        write_cfg(home)
        rc, out = run_scan(home, md)
        cases.append(("inline-comment type + config -> NEEDS_RUN",
                      rc == 0 and verdict(out) == "NEEDS_RUN"))

    ok = True
    for label, passed in cases:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("smoke-scan:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
