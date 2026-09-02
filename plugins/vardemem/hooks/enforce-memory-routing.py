#!/usr/bin/env python3
"""Write-time routing reflex for the Claude Code auto-memory dir.

A PostToolUse hook (wired in hooks.json on Edit|Write|MultiEdit) that fires when
a memory file is written under any `.claude/projects/<slug>/memory/` dir. If the
new memory looks mis-routed (a fact that belongs in the agent's vault, not in
cwd-scratch), it injects a NON-BLOCKING advisory via additionalContext so the
agent re-routes it in the same turn.

This is the deterministic prevention half of a memory-hygiene pair; the periodic
cleanup half (merge / promote / dedupe) lives in the `consolidate-memory` skill
in this same plugin. It exists because the routing rule ("auto-memory is
cwd-scratch only; person/project/company/agent/framework/environment facts go to
the vault") is otherwise probabilistic recall, and dirs bloat when it slips.

REFLECTIVE, never a hard block: it only advises. It trips on the highest-precision
signal (frontmatter `type:` in the config's trip_types, default `project`/`user`),
so it stays quiet on normal `reference`/`feedback` cwd-scratch writes.

OPT-IN PER TENANT via a config file. With no config (or `enabled` not true) the
hook no-ops, so a tenant uses it only if they want to:

    $HOME/.config/vardemem/memory-routing.json
    {
      "enabled": true,
      "trip_types": {
        "project": "a project/company fact. Fold it into <your vault path>, ...",
        "user": "a person fact. Fold it into <your contact-file path>, ..."
      },
      "memory_path_regex": "optional override; default matches any auto-memory dir"
    }

The canonical config path is `~/.config/vardemem/memory-routing.json`. When that
path is absent the hook falls back to `~/.config/harness-management/memory-routing.json`
(the path this pair used before it moved into vardemem), logging a one-line note
when the fallback serves.

Ordering (hot-path safety): the disable-env check runs FIRST (pure), so a disabled
hook does ZERO disk work. load_config() is then a PURE READ (no mkdir, no log
append, no write), so it is safe to read the config before the path guard and honor
a tenant's memory_path_regex as a TRUE override of the default matcher (it can
broaden or replace the default, not merely narrow it). All disk WRITES (the mkdir +
log append) are deferred to a confirmed trip: an enabled tenant writing a memory
file whose frontmatter type is in trip_types. A disabled hook, an ordinary
(non-memory) write, or a non-tripping type therefore never writes to disk. See the
main() comment for the ordering rationale (disable-env and the pure config read
precede any write).

See hooks/memory-routing.config.example.json. Disable entirely (even with a config
present) by naming `enforce-memory-routing` in $VARDEMEM_DISABLED. The legacy
$HARNESS_MANAGEMENT_DISABLED is dual-honored for this hook.

Hook input: JSON on stdin (PostToolUse payload).
Hook output: JSON on stdout with optional additionalContext; always exit 0.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# Canonical config path; the pre-move path is kept as a fallback so a tenant that
# has not migrated its config keeps working.
CONFIG_PATH = Path.home() / ".config/vardemem/memory-routing.json"
LEGACY_CONFIG_PATH = Path.home() / ".config/harness-management/memory-routing.json"
LOG_FILE = Path.home() / ".local/log/memory-routing-hook.log"
# Forward-slash only by design: a Windows-shaped file_path is matched via a
# SEPARATE normalized copy built ONLY for this regex (see the path guard in
# main() and normalize_seps() below), never via a mutated file_path, so the
# pattern itself stays readable instead of growing a [\\/] separator class.
DEFAULT_MEM_RE = r"/\.claude/projects/[^/]+/memory/[^/]+\.md$"
SKIP_NAMES = {"MEMORY.md", ".gitignore"}
# NOTE: the SKIP_NAMES fast path checks Path(file_path).name with HOST pathlib
# semantics, so on a POSIX host a Windows-shaped (backslash) path never takes
# this skip (.name is the whole string). That is backstopped, not load-bearing:
# .gitignore never satisfies the \.md$ anchor and MEMORY.md has no type:
# frontmatter, so both are independently excluded downstream.


def normalize_seps(path: str) -> str:
    """Return `path` with backslashes normalized to forward slashes.

    A Windows harness delivers tool_input.file_path with backslashes (e.g.
    "C:\\Users\\alex\\.claude\\projects\\...\\memory\\x.md"), which
    DEFAULT_MEM_RE (forward-slash only) cannot see.

    This is a MATCHER-ONLY helper: the caller uses its return value solely to
    build a second regex attempt in the path guard (see main()). It must NEVER
    be used for file I/O or any other side effect, and `file_path` itself is
    NEVER reassigned to this value. Two cases explain why the split matters:

    (a) A Windows extended-length path (`\\\\?\\C:\\...`, `\\\\?\\UNC\\...`) is,
    per Microsoft's documented behavior, REQUIRED to use literal backslashes;
    rewriting it to forward slashes for I/O makes the read fail (the hook
    then silently no-ops on the exact machine this handling targets).

    (b) A POSIX path whose slug legitimately contains a literal backslash
    character (a normal, non-separator filename byte on POSIX) matches fine via
    `[^/]+`, which allows backslash. Blanket-rewriting file_path itself would
    both mis-split that segment for matching AND point read_text() at a path
    that no longer exists on disk, a regression on a case that never involved
    Windows at all.

    So `file_path` stays byte-for-byte as delivered for every downstream
    operation (the path guard's RAW attempt, the SKIP_NAMES name check, and
    read_text()); only this function's return value, used exclusively as the
    path guard's second (fallback) regex attempt, is normalized."""
    return path.replace("\\", "/")


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with LOG_FILE.open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


# Distinct sentinel: the canonical config file could not be read AT ALL
# (absent / unreadable). Kept separate from a `None` that a present config
# yields (a literal `null`, or a malformed parse), so only an ABSENT canonical
# triggers the legacy fallback; a present-but-null/unusable canonical is
# authoritative and does NOT fall back.
_CANONICAL_ABSENT = object()


def load_config() -> tuple:
    """Per-tenant opt-in config. Returns (cfg, served_from_legacy).

    PURE READ: never writes, mkdirs, or logs. A config read is cheap and has no
    accumulating side effect, so it is safe to call before the trip is confirmed.
    The one-line "served from legacy" note is the CALLER's job, emitted only on a
    real trip (so it never fires on an ordinary write); `served_from_legacy`
    reports whether the legacy path actually supplied the returned config.

    `cfg` is the usable opt-in dict, or {} when there is no usable config (absent,
    disabled, or missing a `trip_types` map -> the hook no-ops).

    Read-order (shared in spirit with `consolidate-memory/scan.py` so scanner and
    hook agree on which frontmatter types are vault-bound for any present, usable
    config): try the canonical `~/.config/vardemem/memory-routing.json`. A PRESENT
    canonical file is authoritative even when it is a literal `null`, malformed, or
    disables the hook (`enabled: false`); only an ABSENT (unreadable) canonical file
    falls back to the legacy `~/.config/harness-management/memory-routing.json`. The
    distinct `_CANONICAL_ABSENT` sentinel is what separates "canonical absent"
    (-> try legacy) from "canonical present but null/unusable" (-> authoritative, no
    fallback), so a config whose entire content is `null` is a present-authoritative
    degenerate config, not a trigger to serve the legacy path."""
    try:
        ctext = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        cfg = _CANONICAL_ABSENT  # canonical absent/unreadable -> try legacy
    else:
        try:
            cfg = json.loads(ctext)  # present: authoritative (may be None/{}/...)
        except (json.JSONDecodeError, ValueError):
            cfg = None  # present but malformed -> authoritative-but-unusable

    served_from_legacy = False
    if cfg is _CANONICAL_ABSENT:
        try:
            ltext = LEGACY_CONFIG_PATH.read_text(encoding="utf-8")
        except OSError:
            return {}, False
        try:
            cfg = json.loads(ltext)
        except (json.JSONDecodeError, ValueError):
            return {}, False
        served_from_legacy = True

    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return {}, served_from_legacy
    trip = cfg.get("trip_types")
    if not isinstance(trip, dict) or not trip:
        return {}, served_from_legacy
    return cfg, served_from_legacy


def frontmatter_type(text: str) -> str:
    """Return the frontmatter `type:` value, or ''. Matches a `type:` key at any
    indent inside the `---` frontmatter block; in practice memories carry it
    top-level or nested under `metadata:`."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    m = re.search(r"^\s*type:\s*([A-Za-z_]+)\s*$", text[3:end], re.MULTILINE)
    return m.group(1).strip().lower() if m else ""


def _disabled_in(env_name: str) -> bool:
    """True if `enforce-memory-routing` is named in the comma-separated env var."""
    return "enforce-memory-routing" in {
        s.strip() for s in os.environ.get(env_name, "").split(",") if s.strip()
    }


def main() -> int:
    # Parse the payload and extract file_path FIRST (pure: no disk work).
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0  # fail open on a valid-but-non-dict payload ([], null, "x", 1)
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0  # fail open on a missing or non-string path (NotebookEdit carries
                  # notebook_path, not file_path, so it lands here as a no-op too)
    # file_path is NEVER mutated. It is used byte-for-byte for every downstream
    # I/O and side effect (the SKIP_NAMES name check, read_text(), log/advisory
    # text) below. Only the path guard (FOURTH, below) also tries a SEPARATE
    # normalized copy, purely for regex matching. See normalize_seps() for why
    # the split is load-bearing.

    # Disable-env check SECOND, before ANY disk work. A tenant that named this hook
    # in VARDEMEM_DISABLED (or, still honored, the legacy HARNESS_MANAGEMENT_DISABLED)
    # must incur zero filesystem cost, so this precedes load_config() / log() / any
    # mkdir.
    if _disabled_in("VARDEMEM_DISABLED") or _disabled_in("HARNESS_MANAGEMENT_DISABLED"):
        return 0

    # Config read THIRD, as a PURE READ (no mkdir, no log, no write). Split from
    # the fallback LOGGING: the disk WRITE (mkdir + log append) was the hot-path
    # problem, NOT the config READ. A cheap side-effect-free read can precede the
    # path guard, which is what lets a tenant's memory_path_regex act as a TRUE
    # override of the default matcher rather than a narrowing-only filter. The
    # "served from legacy" note is deferred to a confirmed trip below (so it fires
    # only when the legacy config actually supplied a trip, never on an ordinary
    # write). Disable-env and the path guard still precede any disk WRITE.
    cfg, served_from_legacy = load_config()

    # Determine the EFFECTIVE memory-path matcher: the tenant's memory_path_regex
    # if present and valid is a TRUE override (authoritative -- it can broaden or
    # replace the default, not merely narrow it), else DEFAULT_MEM_RE. A malformed
    # override falls back to the default rather than dropping the guard.
    mem_re = DEFAULT_MEM_RE
    override = cfg.get("memory_path_regex") if cfg else None
    if isinstance(override, str) and override:
        try:
            re.compile(override)
        except re.error:
            pass  # malformed override -> DEFAULT_MEM_RE stands
        else:
            mem_re = override

    # Path guard FOURTH, using the EFFECTIVE matcher. Try the RAW file_path
    # first: this is byte-for-byte the original behavior, so a plain POSIX
    # path (with or without a literal backslash inside a component, which
    # [^/]+ has always allowed) matches exactly as it always has, with zero
    # semantic change. Only if the raw attempt fails do we try a SEPARATE
    # forward-slash-normalized copy (normalize_seps(), never assigned back to
    # file_path) as a fallback: this is what catches a Windows-shaped path
    # (backslash-separated, mixed separators, a UNC \\server\share\... path,
    # or an extended-length \\?\C:\...\ / \\?\UNC\...\ path) that the raw
    # attempt cannot see. file_path itself is NEVER reassigned, so read_text()
    # below always opens the path exactly as delivered:
    # mutating file_path for I/O broke Windows extended-length paths (which
    # Microsoft's docs require to keep literal backslashes) and could point
    # read_text() at a nonexistent rewritten path for a POSIX slug containing
    # a literal backslash.
    try:
        matched = bool(re.search(mem_re, file_path)) or bool(
            re.search(mem_re, normalize_seps(file_path))
        )
    except (re.error, TypeError):
        return 0
    if not matched:
        return 0
    if Path(file_path).name in SKIP_NAMES:
        return 0

    # Config-enabled gate FIFTH: a disabled/absent config no-ops (still no write).
    if not cfg:
        return 0  # opt-in: no usable config, no-op

    # It is a memory-file write on an enabled tenant. Read the resulting file from
    # disk (robust to Write `content` vs Edit before-state field-name differences;
    # the file exists post-tool). This is a READ, not the accumulating WRITE.
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except OSError:
        return 0

    ftype = frontmatter_type(text)
    guidance = cfg["trip_types"].get(ftype)
    if not guidance:
        return 0  # not a tripped type for this tenant (no fallback log either)

    # Confirmed trip. NOW do the disk WRITEs: the fallback note ONLY if the legacy
    # config actually supplied this trip, then the mis-route advisory.
    if served_from_legacy:
        log(f"config fallback: served from legacy path {LEGACY_CONFIG_PATH} "
            f"(canonical {CONFIG_PATH} absent)")

    vault_env = os.environ.get("AGENT_VAULT", "").strip()
    vault_name = Path(vault_env).name if vault_env else "your vault"
    name = Path(file_path).name
    log(f"flagged mis-route: {name} (type={ftype})")
    advisory = (
        f"Routing check: the memory you just wrote (`{name}`) has frontmatter "
        f"`type: {ftype}`, which is {guidance} The auto-memory dir is cwd-scratch "
        f"only; consider folding this into {vault_name} rather than leaving it to "
        "bloat the session-start index."
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": advisory,
        }
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    # Fail open: a PostToolUse hook must never crash a tool call. Any unexpected
    # error (malformed payload / config / file) degrades to a silent no-op.
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
