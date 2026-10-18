#!/usr/bin/env python3
r"""Smoke test for enforce-memory-routing.py (memory-hygiene).

Runs the hook as a subprocess against a throwaway $HOME so the config and memory
file are fully controlled. Covers the opt-in gate (the config is the only gate),
trip-type match, silent cases, the true memory_path_regex override, a null
config, Windows-path parity, and
the hot-path no-disk-write guarantee.

Windows cases use write_raw_named to create each fixture on disk under a name
byte-identical to the raw file_path, so a "trips" assertion proves BOTH that the
path matches (via the normalized fallback) AND that read_text() opened the raw
path: an implementation that mutated file_path before I/O would read a name that
was never created and the case would fail to trip.

Usage: python3 tests/smoke-enforce-memory-routing.py   (exit 0 = all pass)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "enforce-memory-routing.py"

CFG_DIR = ".config/memory-hygiene"


def run(home: Path, file_path: str) -> str:
    """Run the hook against a controlled $HOME."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["AGENT_VAULT"] = str(home / "agent-vault")
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": file_path}})
    p = subprocess.run([sys.executable, str(HOOK)], input=payload,
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, f"hook exited {p.returncode}: {p.stderr}"
    return p.stdout.strip()


def run_rc(home: Path, payload: str, *, cfg=None) -> tuple:
    """Run with a RAW payload and optional RAW config (written to canonical);
    return (returncode, stdout) without asserting, for fail-open cases."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["AGENT_VAULT"] = str(home / "agent-vault")
    if cfg is not None:
        cfg_dir = home / CFG_DIR
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "memory-routing.json").write_text(cfg, encoding="utf-8")
    p = subprocess.run([sys.executable, str(HOOK)], input=payload,
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout.strip()


def write_config(home: Path, enabled: bool = True) -> None:
    """Write the opt-in config to its path."""
    cfg_dir = home / CFG_DIR
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "memory-routing.json").write_text(json.dumps({
        "enabled": enabled,
        "trip_types": {
            "project": "a project/company fact. Fold it into your vault.",
            "user": "a person fact. Fold it into that contact file.",
        },
    }), encoding="utf-8")


def clear_configs(home: Path) -> None:
    f = home / CFG_DIR / "memory-routing.json"
    if f.exists():
        f.unlink()


def write_mem(home: Path, name: str, ftype: str, nested: bool = False) -> str:
    mem = home / ".claude/projects/-x/memory"
    mem.mkdir(parents=True, exist_ok=True)
    f = mem / name
    if nested:
        f.write_text(f"---\nname: t\nmetadata:\n  type: {ftype}\n---\nbody\n", encoding="utf-8")
    else:
        f.write_text(f"---\nname: t\ntype: {ftype}\n---\nbody\n", encoding="utf-8")
    return str(f)


def write_raw_named(home: Path, raw_suffix: str, ftype: str) -> str:
    """Create a fixture whose on-disk name is byte-identical to raw_suffix, and
    return the file_path to hand the hook. A real '/' in raw_suffix is a POSIX
    separator (creating nested dirs); a '\\' is a legal filename byte on POSIX, so
    it stays inside one component. See the module docstring for why this proves
    raw-path I/O."""
    f = home / raw_suffix
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"---\nname: t\ntype: {ftype}\n---\nbody\n", encoding="utf-8")
    return str(f)


def main() -> int:
    cases = []  # (label, predicate)
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)

        # opt-in gate + trip-type match
        write_config(home)
        out = run(home, write_mem(home, "proj.md", "project"))
        cases.append(("project trips with canonical config", '"additionalContext"' in out and "vault" in out))
        out = run(home, write_mem(home, "ref.md", "reference"))
        cases.append(("reference stays silent", out == ""))

        # Windows-path parity: each trips via the normalized fallback AND is read
        # back from the raw path (write_raw_named pins both). Extended-length paths
        # are the case that must keep literal backslashes for I/O.
        for label, raw in [
            ("drive-letter (C:\\)", r"C:\Users\alex\.claude\projects\-x\memory\drive.md"),
            ("UNC (\\\\server\\share)", r"\\server\share\Users\alex\.claude\projects\-x\memory\unc.md"),
            ("extended-length (\\\\?\\C:\\)", r"\\?\C:\Users\alex\.claude\projects\-x\memory\ext.md"),
            ("mixed separators", r"C:\Users\alex/.claude/projects\-x/memory\mixed.md"),
        ]:
            out = run(home, write_raw_named(home, raw, "project"))
            cases.append((f"Windows {label} trips and reads the raw path",
                          '"additionalContext"' in out and "vault" in out))

        # POSIX slug with a literal backslash: matches via the RAW attempt alone (no regression).
        posix_bs = home / ".claude" / "projects" / "na\\me" / "memory"
        posix_bs.mkdir(parents=True, exist_ok=True)
        (posix_bs / "note.md").write_text("---\nname: t\ntype: project\n---\nbody\n", encoding="utf-8")
        out = run(home, str(posix_bs / "note.md"))
        cases.append(("POSIX path with a literal backslash still trips", '"additionalContext"' in out and "vault" in out))

        # Backslash path outside a memory dir stays silent (no-match side).
        non_memory = home / "windows_notes.md"
        non_memory.write_text("---\ntype: project\n---\nbody\n", encoding="utf-8")
        out = run(home, str(non_memory).replace("/", "\\"))
        cases.append(("Windows backslash path outside a memory dir stays silent", out == ""))

        # opt-in gate: disabled config = no-op even for a tripping type
        write_config(home, enabled=False)
        out = run(home, write_mem(home, "proj2.md", "project"))
        cases.append(("disabled config = no-op", out == ""))

        clear_configs(home)

        # non-memory path + nested metadata.type
        write_config(home, enabled=True)
        other = home / "notes.md"
        other.write_text("---\ntype: project\n---\n", encoding="utf-8")
        out = run(home, str(other))
        cases.append(("non-memory path ignored", out == ""))
        out = run(home, write_mem(home, "nested.md", "user", nested=True))
        cases.append(("nested metadata.type trips", '"additionalContext"' in out and "vault" in out))

        # a quoted YAML type value (`type: "project"`) is equivalent to the bare form.
        mem = home / ".claude/projects/-x/memory"
        mem.mkdir(parents=True, exist_ok=True)
        (mem / "quoted.md").write_text('---\nname: t\ntype: "project"\n---\nbody\n', encoding="utf-8")
        out = run(home, str(mem / "quoted.md"))
        cases.append(("quoted frontmatter type trips", '"additionalContext"' in out and "vault" in out))

        # a hyphenated/digit type value is parsed and matches a hyphenated trip_type key.
        clear_configs(home)
        hy_cfg = json.dumps({"enabled": True, "trip_types": {"project-fact": "a project fact. Fold it into your vault."}})
        (mem / "hyphen.md").write_text("---\nname: t\ntype: project-fact\n---\nbody\n", encoding="utf-8")
        rc, out = run_rc(home, json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(mem / "hyphen.md")}}), cfg=hy_cfg)
        cases.append(("hyphenated frontmatter type trips a hyphenated key",
                      rc == 0 and '"additionalContext"' in out and "vault" in out))
        clear_configs(home)
        write_config(home, enabled=True)  # back to the default {project, user} config

        # a trailing YAML inline comment on the type line still parses the value.
        (mem / "commented.md").write_text("---\nname: t\ntype: project # vault-bound\n---\nbody\n", encoding="utf-8")
        out = run(home, str(mem / "commented.md"))
        cases.append(("inline-comment type trips", '"additionalContext"' in out and "vault" in out))

        # the filename is sanitized before it reaches the advisory: a backtick in the
        # name must not appear raw in additionalContext (it could otherwise break the
        # code span and inject text into the next turn's context).
        (mem / "inj`x.md").write_text("---\nname: t\ntype: project\n---\nbody\n", encoding="utf-8")
        out = run(home, str(mem / "inj`x.md"))
        cases.append(("backtick in filename is stripped from the advisory",
                      '"additionalContext"' in out and "injx.md" in out and "inj`x.md" not in out))
        clear_configs(home)
        write_config(home, enabled=True)

        # fail-open: a bad payload/config must not crash. A malformed memory_path_regex
        # must DEGRADE to the default matcher and keep working, so assert the advisory
        # fires (a crash would also exit 0, so rc==0 alone cannot tell them apart).
        mp = write_mem(home, "fo.md", "project")
        bad_cfg = json.dumps({"enabled": True, "trip_types": {"project": "x"}, "memory_path_regex": 123})
        rc, out = run_rc(home, json.dumps({"tool_name": "Write", "tool_input": {"file_path": mp}}), cfg=bad_cfg)
        cases.append(("non-string regex degrades to default matcher (still advises)",
                      rc == 0 and '"additionalContext"' in out))
        rc, out = run_rc(home, json.dumps({"tool_name": "Write", "tool_input": {"file_path": 123}}))
        cases.append(("fail-open: non-string file_path", rc == 0 and out == ""))

        # trip_types keys are matched case/space-insensitively, so scanner (which
        # lowercases keys) and hook agree; a "Project " key trips a `project` memory.
        clear_configs(home)
        cap_cfg = json.dumps({"enabled": True, "trip_types": {"Project ": "a project fact. Fold it into your vault."}})
        mp = write_mem(home, "capkey.md", "project")
        rc, out = run_rc(home, json.dumps({"tool_name": "Write", "tool_input": {"file_path": mp}}), cfg=cap_cfg)
        cases.append(("trip_types key normalized (capitalized/padded key trips)",
                      rc == 0 and '"additionalContext"' in out and "vault" in out))

        # an undecodable (invalid UTF-8) canonical config must fail open, not crash;
        # it is present-authoritative-unusable, so the hook stays silent (no fallback).
        clear_configs(home)
        (home / CFG_DIR).mkdir(parents=True, exist_ok=True)
        (home / CFG_DIR / "memory-routing.json").write_bytes(b"\xff\xfe not utf8")
        out = run(home, write_mem(home, "badenc.md", "project"))
        cases.append(("undecodable canonical config: fail-open, silent", out == ""))
        clear_configs(home)

        # memory_path_regex is a TRUE override: broadening advises on a non-default path...
        broad_cfg = json.dumps({
            "enabled": True,
            "trip_types": {"project": "a project fact. Fold it into your vault."},
            "memory_path_regex": r"/tenant-memory/[^/]+\.md$",
        })
        tmem = home / "tenant-memory"
        tmem.mkdir(parents=True, exist_ok=True)
        (tmem / "custom.md").write_text("---\ntype: project\n---\nbody\n", encoding="utf-8")
        rc, out = run_rc(home, json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": str(tmem / "custom.md")}}), cfg=broad_cfg)
        cases.append(("broadening memory_path_regex override advises", rc == 0 and '"additionalContext"' in out and "vault" in out))
        # ...and narrowing stays silent on a default path it excludes.
        narrow_cfg = json.dumps({
            "enabled": True,
            "trip_types": {"project": "x"},
            "memory_path_regex": r"/never-matches-this-dir/[^/]+\.md$",
        })
        mp = write_mem(home, "narrow.md", "project")
        rc, out = run_rc(home, json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": mp}}), cfg=narrow_cfg)
        cases.append(("narrowing memory_path_regex override stays silent", rc == 0 and out == ""))

        clear_configs(home)
        mp = write_mem(home, "nullcfg.md", "project")
        rc, out = run_rc(home, json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": mp}}), cfg="null")
        cases.append(("null config stays silent", rc == 0 and out == ""))

    # Hot-path no-disk-write: an ordinary non-memory write must do zero disk work,
    # so the log file must not even be created (fresh $HOME).
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        log_file = home / ".local/log/memory-routing-hook.log"
        write_config(home)
        other = home / "notes.md"
        other.write_text("---\ntype: project\n---\n", encoding="utf-8")
        out = run(home, str(other))
        cases.append(("non-memory write: silent, no log write", out == "" and not log_file.exists()))

    ok = True
    for label, passed in cases:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("smoke-enforce-memory-routing:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
