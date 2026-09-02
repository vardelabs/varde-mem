#!/usr/bin/env python3
r"""Smoke test for enforce-memory-routing.py (vardemem).

Runs the hook as a subprocess against a throwaway $HOME so the per-tenant config
and the memory file are fully controlled. Asserts the opt-in gate, the trip-type
match, the silent cases, the config-path canonical/legacy fallback, the dual-honor
disable env (canonical VARDEMEM_DISABLED plus the legacy HARNESS_MANAGEMENT_DISABLED),
the TRUE memory_path_regex override (a broadening override advises on a non-default
path, a narrowing one stays silent), the null-canonical sentinel (a present-but-null
canonical config is authoritative and does NOT fall back to legacy), and the
hot-path no-disk-write guarantee (a disabled hook or an ordinary non-memory write
creates no log file, since load_config is a pure read and every disk WRITE is
deferred to a confirmed trip).

Windows-path parity: drive-letter case (c:\ vs C:\), a UNC path (\\server\share\...),
extended-length paths (\\?\C:\... and \\?\UNC\...), and mixed separators within one
path all trip via the FALLBACK normalized-matcher attempt, AND read_text() succeeds
by opening the RAW file_path exactly as delivered, never a mutated copy. Each fixture
is created ON DISK under a byte-identical name to the raw file_path string (POSIX
allows backslash, colon, and '?' as ordinary filename bytes), so a regression back to
mutating file_path before I/O would point read_text() at a path that does not exist
and the case would silently fail to trip -- this is what makes these cases a real
I/O-preservation proof, not just a regex-matching one. A POSIX path whose slug
legitimately contains a literal backslash character is also covered as an explicit
non-regression case: it must match via the RAW (first) attempt alone.

Usage: python3 tests/smoke-enforce-memory-routing.py   (exit 0 = all pass)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "enforce-memory-routing.py"

CANONICAL_CFG_DIR = ".config/vardemem"
LEGACY_CFG_DIR = ".config/harness-management"


def run(home: Path, file_path: str, *, disable_env: str = "") -> str:
    """Run the hook. disable_env, if set, names the env var to populate with the
    hook name (one of VARDEMEM_DISABLED / HARNESS_MANAGEMENT_DISABLED)."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["AGENT_VAULT"] = str(home / "agent-vault")
    env.pop("VARDEMEM_DISABLED", None)
    env.pop("HARNESS_MANAGEMENT_DISABLED", None)
    if disable_env:
        env[disable_env] = "enforce-memory-routing"
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": file_path}})
    p = subprocess.run([sys.executable, str(HOOK)], input=payload,
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, f"hook exited {p.returncode}: {p.stderr}"
    return p.stdout.strip()


def run_rc(home: Path, payload: str, *, cfg=None) -> tuple:
    """Run with a RAW payload string and optional RAW config string (written to the
    canonical path); return (returncode, stdout) WITHOUT asserting, for fail-open
    (must-not-crash) cases."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["AGENT_VAULT"] = str(home / "agent-vault")
    env.pop("VARDEMEM_DISABLED", None)
    env.pop("HARNESS_MANAGEMENT_DISABLED", None)
    if cfg is not None:
        cfg_dir = home / CANONICAL_CFG_DIR
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "memory-routing.json").write_text(cfg, encoding="utf-8")
    p = subprocess.run([sys.executable, str(HOOK)], input=payload,
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout.strip()


def write_config(home: Path, enabled: bool = True, *, legacy: bool = False) -> None:
    """Write the opt-in config to the canonical path, or the legacy path when
    legacy=True (to exercise the legacy fallback read)."""
    cfg_dir = home / (LEGACY_CFG_DIR if legacy else CANONICAL_CFG_DIR)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "memory-routing.json").write_text(json.dumps({
        "enabled": enabled,
        "trip_types": {
            "project": "a project/company fact. Fold it into your vault.",
            "user": "a person fact. Fold it into that contact file.",
        },
    }), encoding="utf-8")


def clear_configs(home: Path) -> None:
    for d in (CANONICAL_CFG_DIR, LEGACY_CFG_DIR):
        f = home / d / "memory-routing.json"
        if f.exists():
            f.unlink()


def write_mem(home: Path, name: str, ftype: str, nested: bool = False) -> str:
    mem = home / ".claude/projects/-x/memory"
    mem.mkdir(parents=True, exist_ok=True)
    f = mem / name
    if nested:
        # The real memory shape: type nested under metadata: (per the spec).
        f.write_text(f"---\nname: t\nmetadata:\n  type: {ftype}\n---\nbody\n", encoding="utf-8")
    else:
        f.write_text(f"---\nname: t\ntype: {ftype}\n---\nbody\n", encoding="utf-8")
    return str(f)


def write_raw_named(home: Path, raw_suffix: str, ftype: str) -> str:
    """Create a fixture file directly under `home` whose on-disk name is
    BYTE-IDENTICAL to `raw_suffix`, then return the exact file_path string to
    hand to the hook (home's absolute path joined with `raw_suffix`).

    `raw_suffix` may contain characters that are path separators on Windows
    (backslash) but are ordinary, legal filename bytes on POSIX (backslash,
    colon, '?'). Any REAL '/' in `raw_suffix` is a genuine POSIX separator (it
    creates real nested directories, e.g. for the mixed-separator case); any
    '\\' in it is never a separator here, so it lands inside a single path
    component's literal name.

    This is the mechanism that turns "does read_text() open the RAW path" into
    something provable on a POSIX test runner: if the hook were to mutate
    file_path (e.g. rewrite backslashes to forward slashes) before doing I/O,
    Path(mutated).read_text() would look for a DIFFERENT name than the one
    this function actually created on disk, fail with OSError, and the case
    would silently fail to trip."""
    f = home / raw_suffix
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"---\nname: t\ntype: {ftype}\n---\nbody\n", encoding="utf-8")
    return str(f)


def main() -> int:
    cases = []  # (label, predicate)
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)

        # 1. canonical config + type project -> advisory emitted
        write_config(home)
        out = run(home, write_mem(home, "proj.md", "project"))
        cases.append(("project trips with canonical config", '"additionalContext"' in out and "vault" in out))

        # 2. type reference -> silent
        out = run(home, write_mem(home, "ref.md", "reference"))
        cases.append(("reference stays silent", out == ""))

        # 2a-2h. Windows-path parity and its I/O-preservation guarantee:
        # DEFAULT_MEM_RE is forward-slash only, so the path guard tries the RAW
        # file_path first (byte-for-byte the original behavior) and only falls
        # back to a SEPARATE normalized copy (never assigned back to file_path)
        # if the raw attempt fails. file_path itself is NEVER mutated, so
        # read_text() always opens exactly what was delivered. Each "trips" case
        # below proves BOTH halves at once: the fixture is created ON DISK under
        # a name byte-identical to the raw file_path string (write_raw_named), so
        # a regression back to mutating file_path before I/O would point
        # read_text() at a name that was never created and the case would
        # silently fail to trip.

        # 2a. Drive-letter path, uppercase (C:\...) -> trips via the fallback
        #     normalized attempt; read_text() opens the exact raw name.
        fp = write_raw_named(home, r"C:\Users\alex\.claude\projects\-x\memory\upper.md", "project")
        out = run(home, fp)
        cases.append(("Windows drive-letter path (C:) trips and reads the raw path",
                      '"additionalContext"' in out and "vault" in out))

        # 2b. Drive-letter path, lowercase (c:\...) -> drive-letter case must not
        #     matter (the regex never inspects the drive letter at all).
        fp = write_raw_named(home, r"c:\Users\alex\.claude\projects\-x\memory\lower.md", "project")
        out = run(home, fp)
        cases.append(("Windows drive-letter path (c:, lowercase) trips and reads the raw path",
                      '"additionalContext"' in out and "vault" in out))

        # 2c. UNC path (\\server\share\...) -> trips via the fallback attempt.
        fp = write_raw_named(home, r"\\server\share\Users\alex\.claude\projects\-x\memory\unc.md", "project")
        out = run(home, fp)
        cases.append(("Windows UNC path trips and reads the raw path",
                      '"additionalContext"' in out and "vault" in out))

        # 2d. Extended-length path (\\?\C:\...). Per Microsoft's documented
        #     behavior this form REQUIRES literal backslashes; rewriting
        #     file_path itself to forward slashes for I/O would make the read
        #     fail. Proven here because the fixture's real on-disk name is the
        #     raw \\?\C:\... string itself; an implementation that mutated the
        #     path would look for the forward-slash rewrite instead, find
        #     nothing, and stay silent.
        fp = write_raw_named(home, r"\\?\C:\Users\alex\.claude\projects\-x\memory\extlen.md", "project")
        out = run(home, fp)
        cases.append(("Windows extended-length path (\\\\?\\C:\\...) trips: raw path is what gets opened",
                      '"additionalContext"' in out and "vault" in out))

        # 2e. Extended-length UNC path (\\?\UNC\server\share\...) -> same proof
        #     as 2d for the UNC extended-length form.
        fp = write_raw_named(home, r"\\?\UNC\server\share\Users\alex\.claude\projects\-x\memory\extunc.md", "project")
        out = run(home, fp)
        cases.append(("Windows extended-length UNC path (\\\\?\\UNC\\...) trips: raw path is what gets opened",
                      '"additionalContext"' in out and "vault" in out))

        # 2f. Mixed separators within one path (both `\` and `/` present) ->
        #     trips via the fallback attempt; the fixture's real directory
        #     structure follows the REAL '/' boundaries in the raw string
        #     (write_raw_named), so this also proves raw-path I/O for a path
        #     that is not uniformly one style or the other.
        fp = write_raw_named(home, r"C:\Users\alex/.claude/projects\-x/memory\mixed.md", "project")
        out = run(home, fp)
        cases.append(("Mixed-separator path trips and reads the raw path",
                      '"additionalContext"' in out and "vault" in out))

        # 2g. POSIX-literal-backslash non-regression case: a REAL POSIX directory
        #     whose name legitimately contains a literal backslash character (an
        #     ordinary, legal, non-separator filename byte on POSIX) must match
        #     via the RAW (first) attempt alone, exactly as it always has via
        #     [^/]+ -- no normalization involved at all, so this is a case that
        #     never involved Windows.
        posix_bs_dir = home / ".claude" / "projects" / "na\\me" / "memory"
        posix_bs_dir.mkdir(parents=True, exist_ok=True)
        posix_bs_file = posix_bs_dir / "note.md"
        posix_bs_file.write_text("---\nname: t\ntype: project\n---\nbody\n", encoding="utf-8")
        out = run(home, str(posix_bs_file))
        cases.append(("POSIX path with a literal backslash in a component still trips (no regression)",
                      '"additionalContext"' in out and "vault" in out))

        # 2h. no-match side: a backslash-style file_path that does NOT live under
        #     an auto-memory dir -> stays silent, same as the forward-slash case.
        non_memory = home / "windows_notes.md"
        non_memory.write_text("---\ntype: project\n---\nbody\n", encoding="utf-8")
        out = run(home, str(non_memory).replace("/", "\\"))
        cases.append(("Windows backslash path outside a memory dir stays silent (no-match side)",
                      out == ""))

        # 3. opt-in gate: enabled=false -> silent even for project
        write_config(home, enabled=False)
        out = run(home, write_mem(home, "proj2.md", "project"))
        cases.append(("disabled config = no-op", out == ""))

        # 4a. canonical VARDEMEM_DISABLED opt-out -> silent
        write_config(home, enabled=True)
        out = run(home, write_mem(home, "proj3.md", "project"), disable_env="VARDEMEM_DISABLED")
        cases.append(("VARDEMEM_DISABLED opt-out", out == ""))

        # 4b. legacy HARNESS_MANAGEMENT_DISABLED still disables (dual-honor) -> silent
        out = run(home, write_mem(home, "proj4.md", "project"), disable_env="HARNESS_MANAGEMENT_DISABLED")
        cases.append(("legacy HARNESS_MANAGEMENT_DISABLED dual-honored", out == ""))

        # 4c. both disable envs absent -> runs (advisory emitted)
        out = run(home, write_mem(home, "proj5.md", "project"))
        cases.append(("both disable envs absent -> runs", '"additionalContext"' in out and "vault" in out))

        # 5. config-path fallback: config ONLY at legacy path, canonical absent -> trips
        clear_configs(home)
        write_config(home, legacy=True)
        out = run(home, write_mem(home, "proj6.md", "project"))
        cases.append(("legacy config-path fallback serves", '"additionalContext"' in out and "vault" in out))

        # 6. canonical path preferred over legacy when both present: canonical enabled
        #    shadows a legacy enabled=false (canonical read first).
        write_config(home, enabled=True)                 # canonical: enabled
        write_config(home, enabled=False, legacy=True)   # legacy: disabled (should be ignored)
        out = run(home, write_mem(home, "proj7.md", "project"))
        cases.append(("canonical config preferred over legacy", '"additionalContext"' in out and "vault" in out))
        clear_configs(home)

        # 7. non-memory path -> silent
        write_config(home, enabled=True)
        other = home / "notes.md"
        other.write_text("---\ntype: project\n---\n", encoding="utf-8")
        out = run(home, str(other))
        cases.append(("non-memory path ignored", out == ""))

        # 8. nested metadata.type (the real memory shape) -> advisory emitted
        out = run(home, write_mem(home, "nested.md", "user", nested=True))
        cases.append(("nested metadata.type trips", '"additionalContext"' in out and "vault" in out))

        # 9. fail open: a non-string memory_path_regex in the config must not crash
        mp = write_mem(home, "fo.md", "project")
        bad_cfg = json.dumps({"enabled": True, "trip_types": {"project": "x"}, "memory_path_regex": 123})
        rc, _ = run_rc(home, json.dumps({"tool_name": "Write", "tool_input": {"file_path": mp}}), cfg=bad_cfg)
        cases.append(("fail-open: non-string regex config", rc == 0))

        # 10. fail open: a non-string tool_input.file_path must not crash
        write_config(home, enabled=True)
        rc, out = run_rc(home, json.dumps({"tool_name": "Write", "tool_input": {"file_path": 123}}))
        cases.append(("fail-open: non-string file_path", rc == 0 and out == ""))

        # 11. TRUE override: a memory_path_regex matching a NON-default path, plus
        #     a file at that path with a tripping type, MUST advise. A guard that
        #     checked DEFAULT_MEM_RE before the config would silently drop a
        #     broadening override.
        clear_configs(home)
        broad_cfg = json.dumps({
            "enabled": True,
            "trip_types": {"project": "a project fact. Fold it into your vault."},
            "memory_path_regex": r"/tenant-memory/[^/]+\.md$",
        })
        tmem = home / "tenant-memory"
        tmem.mkdir(parents=True, exist_ok=True)
        custom = tmem / "custom.md"
        custom.write_text("---\ntype: project\n---\nbody\n", encoding="utf-8")
        rc, out = run_rc(home, json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": str(custom)}}), cfg=broad_cfg)
        cases.append(("broadening memory_path_regex override advises on a non-default path",
                      rc == 0 and '"additionalContext"' in out and "vault" in out))

        # 12. narrowing override still works: a memory_path_regex a default
        #     auto-memory path fails -> silent.
        narrow_cfg = json.dumps({
            "enabled": True,
            "trip_types": {"project": "x"},
            "memory_path_regex": r"/never-matches-this-dir/[^/]+\.md$",
        })
        mp = write_mem(home, "narrow.md", "project")
        rc, out = run_rc(home, json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": mp}}), cfg=narrow_cfg)
        cases.append(("narrowing memory_path_regex override stays silent on a non-matching path",
                      rc == 0 and out == ""))

        # 13. null canonical config + valid enabled legacy -> canonical is
        #     authoritative (null = degenerate empty), hook no-ops; it does NOT
        #     fall through to legacy (which would have advised). Pins the sentinel.
        clear_configs(home)
        write_config(home, enabled=True, legacy=True)  # legacy would advise if served
        mp = write_mem(home, "nullcfg.md", "project")
        rc, out = run_rc(home, json.dumps(
            {"tool_name": "Write", "tool_input": {"file_path": mp}}), cfg="null")
        cases.append(("null canonical config is authoritative (no legacy fallback)",
                      rc == 0 and out == ""))

    # Hot-path disk-write cases (fresh $HOME so the log-file assertions are clean).
    # A disabled hook OR an ordinary non-memory write must do ZERO disk work, so
    # the log file must not even be created. load_config is a pure read; every disk
    # WRITE (mkdir + append) is deferred to a confirmed trip.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        log_file = home / ".local/log/memory-routing-hook.log"

        # 14. legacy config present + disable env set + ordinary NON-memory write
        #     -> silent AND no log file (the disable check precedes the pure config
        #     read and the deferred legacy-fallback log / mkdir).
        write_config(home, legacy=True)
        other = home / "notes.md"
        other.write_text("---\ntype: project\n---\n", encoding="utf-8")
        out = run(home, str(other), disable_env="HARNESS_MANAGEMENT_DISABLED")
        cases.append(("disabled + legacy config + non-memory write: silent, no log write",
                      out == "" and not log_file.exists()))

        # 15. ENABLED (no disable env), legacy config serving, ordinary NON-memory
        #     write -> silent AND no log file (the path guard short-circuits the
        #     trip, so the deferred legacy-fallback log never fires either).
        out = run(home, str(other))
        cases.append(("enabled + legacy config + non-memory write: silent, no log write",
                      out == "" and not log_file.exists()))

    ok = True
    for label, passed in cases:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("smoke-enforce-memory-routing:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
