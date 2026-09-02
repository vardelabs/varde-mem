#!/usr/bin/env python3
"""Write-time routing reflex for the Claude Code auto-memory dir.

PostToolUse hook (Edit|Write|MultiEdit) that fires when a memory file is written
under a `.claude/projects/<slug>/memory/` dir. If the memory's frontmatter `type:`
is vault-bound (in the config's trip_types), it injects a NON-BLOCKING advisory
via additionalContext so the agent re-routes it in the same turn. It only advises.

The prevention half of a memory-hygiene pair; the periodic cleanup half is the
`consolidate-memory` skill in this same plugin.

OPT-IN PER TENANT. With no config (or `enabled` not true) the hook no-ops:

    $HOME/.config/vardemem/memory-routing.json
    {
      "enabled": true,
      "trip_types": {"project": "...routing advice...", "user": "..."},
      "memory_path_regex": "optional; default matches any auto-memory dir"
    }

The canonical path is `~/.config/vardemem/memory-routing.json`; when absent the
hook falls back to `~/.config/harness-management/memory-routing.json` (the pair's
pre-move path). See hooks/memory-routing.config.example.json. Disable by naming
`enforce-memory-routing` in $VARDEMEM_DISABLED (legacy $HARNESS_MANAGEMENT_DISABLED
dual-honored). Always exits 0.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

CONFIG_PATH = Path.home() / ".config/vardemem/memory-routing.json"
LEGACY_CONFIG_PATH = Path.home() / ".config/harness-management/memory-routing.json"
LOG_FILE = Path.home() / ".local/log/memory-routing-hook.log"
DEFAULT_MEM_RE = r"/\.claude/projects/[^/]+/memory/[^/]+\.md$"
SKIP_NAMES = {"MEMORY.md", ".gitignore"}

# Sentinel for "canonical config MISSING" (FileNotFoundError), kept distinct from
# a present config that parses to None. Only a MISSING canonical falls back to
# legacy; a present-but-unreadable/null/malformed canonical is authoritative.
_CANONICAL_ABSENT = object()


def normalize_seps(path: str) -> str:
    """Backslashes to forward slashes, for MATCHING ONLY.

    A Windows harness delivers file_path with backslashes, which the forward-slash
    DEFAULT_MEM_RE cannot see. This value is used only to build a second regex
    attempt; file_path itself is never reassigned, because it must stay byte-exact
    for read_text(): a Windows extended-length path (`\\\\?\\C:\\...`) requires
    literal backslashes, and a POSIX slug may legitimately contain a backslash.
    """
    return path.replace("\\", "/")


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with LOG_FILE.open("a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def load_config() -> tuple:
    """Per-tenant opt-in config. Returns (cfg, served_from_legacy).

    PURE READ (no write/mkdir/log), so it is safe to call before the trip is
    confirmed. `cfg` is the usable opt-in dict, or {} when unusable (absent,
    disabled, or no trip_types). Canonical is read first; only a MISSING canonical
    (FileNotFoundError) falls back to legacy, so a present-but-unreadable or null
    canonical is authoritative and does not silently prefer a legacy config. Shared
    read-order with scan.py so scanner and hook agree on trip_types.
    """
    try:
        ctext = CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        cfg = _CANONICAL_ABSENT
    except (OSError, UnicodeDecodeError):
        cfg = None  # present but unreadable/undecodable: authoritative-unusable, no fallback
    else:
        try:
            cfg = json.loads(ctext)
        except (json.JSONDecodeError, ValueError):
            cfg = None

    served_from_legacy = False
    if cfg is _CANONICAL_ABSENT:
        try:
            ltext = LEGACY_CONFIG_PATH.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
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
    """The frontmatter `type:` value at any indent (top-level or under metadata:), or ''."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    # Optional surrounding quotes and a trailing YAML inline comment (which needs
    # whitespace before the #, so a # inside the value is not mistaken for one); the
    # value may contain digits and hyphens (`type: project-fact`, `type: "project"`,
    # and `type: project # note` all read as the bare form).
    m = re.search(r"""^\s*type:\s*["']?([A-Za-z0-9_-]+)["']?(?:\s+#.*)?\s*$""", text[3:end], re.MULTILINE)
    return m.group(1).strip().lower() if m else ""


def _disabled_in(env_name: str) -> bool:
    return "enforce-memory-routing" in {
        s.strip() for s in os.environ.get(env_name, "").split(",") if s.strip()
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0  # missing/non-string path (e.g. NotebookEdit's notebook_path)

    # Disable-env first, so a disabled hook does ZERO disk work.
    if _disabled_in("VARDEMEM_DISABLED") or _disabled_in("HARNESS_MANAGEMENT_DISABLED"):
        return 0

    # Pure config read before the path guard, so a tenant's memory_path_regex can
    # act as a TRUE override of the default matcher. All disk writes stay deferred
    # to a confirmed trip below.
    cfg, served_from_legacy = load_config()

    # A valid memory_path_regex overrides the default (can broaden, not just
    # narrow); a malformed one falls back to the default rather than dropping the guard.
    mem_re = DEFAULT_MEM_RE
    override = cfg.get("memory_path_regex") if cfg else None
    if isinstance(override, str) and override:
        try:
            re.compile(override)
        except re.error:
            pass
        else:
            mem_re = override

    # Match the RAW path first (byte-exact original behavior), then a
    # normalized copy as a fallback for Windows-shaped paths. file_path is never
    # reassigned; see normalize_seps.
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
    if not cfg:
        return 0  # opt-in: no usable config

    # Read the resulting file from disk (robust to Write vs Edit payload shape).
    try:
        text = Path(file_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0

    # Normalize the config's trip_types keys the same way scan.py does
    # (strip + lowercase), so scanner and hook agree on a key like "Project".
    ftype = frontmatter_type(text)
    trip = {str(k).strip().lower(): v for k, v in cfg["trip_types"].items()}
    guidance = trip.get(ftype)
    if not guidance:
        return 0

    # Confirmed trip: now the deferred disk writes.
    if served_from_legacy:
        log(f"config fallback: served from legacy path {LEGACY_CONFIG_PATH} "
            f"(canonical {CONFIG_PATH} absent)")

    vault_env = os.environ.get("AGENT_VAULT", "").strip()
    vault_name = Path(vault_env).name if vault_env else "your vault"
    name = Path(file_path).name
    # The filename is DATA, not instructions. Strip every line boundary
    # (str.splitlines covers \n, \r, vertical tab, form feed, U+2028/U+2029, NEL,
    # etc.) and backticks before embedding it in the model-facing advisory, so a
    # crafted name can neither start a new instruction line nor close the code span
    # and inject text into the next turn's context. The log keeps the raw name (a
    # local file, never model context).
    safe_name = "".join(name.splitlines()).replace("`", "")[:200]
    log(f"flagged mis-route: {name} (type={ftype})")
    advisory = (
        f"Routing check: the memory you just wrote (`{safe_name}`) has frontmatter "
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
    # Fail open: a PostToolUse hook must never crash a tool call.
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
