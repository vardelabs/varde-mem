#!/usr/bin/env python3
"""Write-time routing reflex for the Claude Code auto-memory dir.

PostToolUse hook (Edit|Write|MultiEdit) that fires when a memory file is written
under a `.claude/projects/<slug>/memory/` dir. If the memory's frontmatter `type:`
is vault-bound (in the config's trip_types), it injects a NON-BLOCKING advisory
via additionalContext so the agent re-routes it in the same turn. It only advises.

The prevention half of a memory-hygiene pair; the periodic cleanup half is the
`consolidate-memory` skill in this same plugin.

OPT-IN PER TENANT. With no config (or `enabled` not true) the hook no-ops:

    $HOME/.config/memory-hygiene/memory-routing.json
    {
      "enabled": true,
      "trip_types": {"project": "...routing advice...", "user": "..."},
      "memory_path_regex": "optional; default matches any auto-memory dir"
    }

BLOCKING, when routing is on. A config that also sets `"block": true` turns the
PreToolUse half on: a Write that would CREATE a new memory file is denied when
`vardemem` answers from the session's tree with at least one declared home. The
reason names `vardemem route <kind>`, with the kind from the config's
`route_kinds` (frontmatter type -> rule kind) and the home it returns, or lists
the kinds `route_kinds` names. Edits and rewrites of an existing memory file
still pass, because the consolidate-memory skill merges and trims in place.
Anything that keeps the hook from confirming a declared home (no `vardemem`, no
homes, an error, a timeout) falls back to the advisory below. `vardemem` is
`cfg["vardemem"]` when set, else the first one on PATH.

The config path is `~/.config/memory-hygiene/memory-routing.json`. See
hooks/memory-routing.config.example.json. The config is the only
gate: with no usable config the hook no-ops, and there is no separate off-switch.
Always exits 0.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONFIG_PATH = Path.home() / ".config/memory-hygiene/memory-routing.json"
LOG_FILE = Path.home() / ".local/log/memory-routing-hook.log"
DEFAULT_MEM_RE = r"/\.claude/projects/[^/]+/memory/[^/]+\.md$"
SKIP_NAMES = {"MEMORY.md", ".gitignore"}
VARDEMEM_TIMEOUT_SECONDS = 3


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


def load_config() -> dict:
    """Per-tenant opt-in config: the usable dict, or {} when absent, unreadable,
    malformed, disabled, or without trip_types.

    PURE READ (no write/mkdir/log), so it is safe to call before the trip is
    confirmed. Shared read rules with scan.py so scanner and hook agree on
    trip_types.
    """
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return {}
    trip = cfg.get("trip_types")
    if not isinstance(trip, dict) or not trip:
        return {}
    return cfg


def frontmatter_type(text: str) -> str:
    """The frontmatter `type:` value (top-level or under metadata:), or ''.

    Deliberately line-oriented, not a full YAML parse. A memory's frontmatter is a
    simple name / description / metadata.type shape, so matching `type:` at any
    indent is enough and keeps the plugin dependency-free. The known cost: a
    `type:` line embedded in a block-scalar value would false-match. It is accepted
    rather than pulling in a YAML parser for a shape memories do not have, and it is
    bounded -- the hook only advises (the agent re-decides) and the scanner's
    verdict is re-judged by reading the actual file.
    """
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


def safe_display(name: str) -> str:
    r"""The filename is DATA, not instructions. Strip every line boundary
    (str.splitlines covers \n, \r, vertical tab, form feed, U+2028/U+2029, NEL,
    etc.) and backticks before embedding it in model-facing text, so a crafted
    name can neither start a new instruction line nor close the code span."""
    return "".join(name.splitlines()).replace("`", "")[:200]


def vardemem_cmd(cfg: dict) -> list | None:
    configured = cfg.get("vardemem")
    if isinstance(configured, str) and configured:
        return [configured] if os.path.isfile(configured) else None
    found = shutil.which("vardemem")
    return [found] if found else None


def run_vardemem(cmd: list, *args: str) -> tuple | None:
    """(returncode, stdout) from vardemem in the session's directory, or None."""
    try:
        proc = subprocess.run(
            [*cmd, *args], capture_output=True, text=True,
            timeout=VARDEMEM_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode, proc.stdout


def route_kinds(cfg: dict) -> dict:
    table = cfg.get("route_kinds")
    if not isinstance(table, dict):
        return {}
    return {str(k).strip().lower(): v for k, v in table.items()
            if isinstance(v, str) and re.fullmatch(r"[a-z0-9-]+", v)}


def block_reason(cfg: dict, cmd: list, name: str, ftype: str) -> str | None:
    """The deny reason, or None when vardemem does not confirm a declared home."""
    kinds = route_kinds(cfg)
    kind = kinds.get(ftype)
    head = (f"Routing is on, so a new auto-memory file (`{safe_display(name)}`) is "
            "refused: auto-memory stays on this device and never reaches the "
            "homes the tree declares.")
    if kind:
        answer = run_vardemem(cmd, "route", kind)
        if not answer or answer[0] != 0:
            return None
        try:
            home = json.loads(answer[1])
        except ValueError:
            return None
        if not isinstance(home, dict) or not home.get("home"):
            return None
        route = f"{head} `vardemem route {kind}` sends a `{ftype}` memory to home `{home['home']}`"
        if home.get("written_here"):
            return f'{route}. Record it with `vardemem rule {kind} "<one line>"`.'
        return (f"{route}, which lives in repo `{safe_display(str(home.get('repo')))}`. Add it "
                f"to `{safe_display(str(home.get('file')))}` through a change to that repo.")
    answer = run_vardemem(cmd, "rules", "--budget", "2")
    if not answer or answer[0] != 0:
        return None
    listed = ", ".join(sorted(set(kinds.values()))) or "none in route_kinds"
    return (f"{head} Pick the kind of fact, run `vardemem route <kind>`, then "
            f'`vardemem rule <kind> "<one line>"`. Kinds this tenant maps: {listed}.')


def pre_tool_use(payload: dict, cfg: dict, file_path: str) -> int:
    """Deny a Write that creates a new memory file, when routing is on."""
    if not cfg or cfg.get("block") is not True or payload.get("tool_name") != "Write":
        return 0
    # A link could carry the Write to a file outside the memory dir.
    if os.path.isfile(file_path) and not os.path.islink(file_path):
        return 0
    content = (payload.get("tool_input") or {}).get("content")
    ftype = frontmatter_type(content) if isinstance(content, str) else ""
    cmd = vardemem_cmd(cfg)
    if not cmd:
        return 0
    reason = block_reason(cfg, cmd, Path(file_path).name, ftype)
    if not reason:
        return 0
    log(f"blocked new memory: {Path(file_path).name} (type={ftype or 'none'})")
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.stdout.flush()
    return 0


def memory_path(cfg: dict, file_path: str) -> bool:
    """True for an auto-memory file this hook judges."""
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
        return False
    return matched and Path(file_path).name not in SKIP_NAMES


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

    # Pure config read before the path guard, so a tenant's memory_path_regex can
    # act as a TRUE override of the default matcher. All disk writes stay deferred
    # to a confirmed trip below.
    cfg = load_config()
    if not memory_path(cfg, file_path):
        return 0
    if payload.get("hook_event_name") == "PreToolUse":
        return pre_tool_use(payload, cfg, file_path)

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

    # Confirmed trip: only now touch the disk.
    vault_env = os.environ.get("AGENT_VAULT", "").strip()
    vault_name = Path(vault_env).name if vault_env else "your vault"
    name = Path(file_path).name
    # The log keeps the raw name (a local file, never model context).
    safe_name = safe_display(name)
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
