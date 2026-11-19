#!/usr/bin/env python3
"""Smoke test for consolidate-memory/migrate.py.

Builds a synthetic auto-memory dir and a tree that declares homes, then runs the
migration with the real vardemem. Checks that a dry run writes nothing, that
--apply records each placeable file and reports the rest, that no memory file is
deleted or changed, and that a rerun records nothing new.

Usage: python3 tests/smoke-migrate.py   (exit 0 = all pass)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
MIGRATE = PLUGIN / "skills" / "consolidate-memory" / "migrate.py"
VARDEMEM = PLUGIN.parent / "journal" / "bin" / "vardemem"

MEMORIES = {
    "rule.md": "---\nname: r\ntype: feedback\n---\nAlways run the tests before a pull request.\n",
    "wrapped.md": "---\nname: w\ntype: feedback\n---\nPrefer small commits\nwith one change each.\n",
    "long.md": "---\nname: l\ntype: feedback\n---\nFirst paragraph.\n\nSecond paragraph.\n",
    "list.md": "---\nname: s\ntype: feedback\n---\n- Keep A\n- Keep B\n",
    "repo.md": "---\nname: p\ntype: plugin\n---\nThe example plugin skips empty files.\n",
    "loose.md": "---\nname: u\ntype: reference\n---\nA link to read later.\n",
    "MEMORY.md": "- index\n",
}


def run(home: Path, vault: Path, mem: Path, *flags: str):
    env = dict(os.environ, HOME=str(home), AGENT_VAULT=str(vault))
    return subprocess.run([sys.executable, str(MIGRATE), *flags, str(mem)],
                          capture_output=True, text=True, env=env, cwd=str(vault))


def snapshot(mem: Path) -> dict:
    return {p.name: p.read_text(encoding="utf-8") for p in mem.iterdir()}


def main() -> int:
    cases = []
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        vault = home / "example-vault"
        (vault / "Rules").mkdir(parents=True)
        (vault / ".vardemem.json").write_text(json.dumps({"homes": {
            "agent-rules": {"kinds": ["agent-rule"], "file": "Rules/agent.md"},
            "plugin-rules": {"kinds": ["plugin-rule"], "file": "RULES.md", "repo": "example-plugin"},
        }}), encoding="utf-8")
        cfg = home / ".config" / "memory-hygiene"
        cfg.mkdir(parents=True)
        (cfg / "memory-routing.json").write_text(json.dumps({
            "enabled": True, "trip_types": {"project": "x"}, "vardemem": str(VARDEMEM),
            "route_kinds": {"feedback": "agent-rule", "plugin": "plugin-rule"},
        }), encoding="utf-8")
        mem = home / ".claude" / "projects" / "-x" / "memory"
        mem.mkdir(parents=True)
        for name, text in MEMORIES.items():
            (mem / name).write_text(text, encoding="utf-8")
        before = snapshot(mem)
        rules = vault / "Rules" / "agent.md"

        dry = run(home, vault, mem)
        cases.append(("dry run exits 0 and writes no home", dry.returncode == 0 and not rules.exists()))
        cases.append(("dry run lists what it would put", "would put rule.md as agent-rule" in dry.stdout))

        first = run(home, vault, mem, "--apply")
        out = first.stdout
        recorded = rules.read_text(encoding="utf-8") if rules.exists() else ""
        cases.append(("apply exits 0", first.returncode == 0))
        cases.append(("a one-paragraph rule is recorded",
                      "- Always run the tests before a pull request." in recorded))
        cases.append(("a wrapped paragraph is recorded as one line",
                      "- Prefer small commits with one change each." in recorded))
        cases.append(("two paragraphs are reported", "unplaced  long.md: body is not one paragraph" in out))
        cases.append(("a list body is reported", "unplaced  list.md: body is not one paragraph" in out))
        cases.append(("a repo home is reported", "unplaced  repo.md: its home is in a repo" in out))
        cases.append(("an unmapped type is reported", "unplaced  loose.md: type `reference`" in out))
        cases.append(("the index is not migrated", "MEMORY.md" not in out))
        cases.append(("the count names the reported files", "4 file(s) left for the operator" in out))
        cases.append(("no memory file is deleted or changed", snapshot(mem) == before))

        second = run(home, vault, mem, "--apply")
        cases.append(("a rerun records nothing new",
                      second.returncode == 0 and rules.read_text(encoding="utf-8") == recorded
                      and "Already recorded" in second.stdout))

        (cfg / "memory-routing.json").write_text(json.dumps({"enabled": True, "trip_types": {"p": "x"}}),
                                                 encoding="utf-8")
        cases.append(("no route_kinds is a config error", run(home, vault, mem).returncode == 2))

    ok = True
    for label, passed in cases:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("smoke-migrate:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
