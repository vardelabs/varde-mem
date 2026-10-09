#!/usr/bin/env python3
"""Non-blocking Journal reminder for a session with no scheduled tick.

A scheduled agent is reminded by its own periodic tick. A local interactive
session has no tick, so these two events are its tick: UserPromptSubmit adds one line to
the next turn, and PreCompact puts the same line into the compaction
instructions, since the cue would otherwise be summarized away.

Whether the Journal is owed is NOT decided here. It is `vardemem owed`, the
one definition the scheduled reminder also calls, so a local session and a
scheduled agent trip on the same rule. This hook only scopes the question to its own
session and keeps the cooldown, which the check leaves to its caller:

  - `--since` is the later of the session's first prompt and the last
    reminder, so work from before this session, or already nagged about,
    does not count twice.
  - `--repo` is the session's cwd plus `$AGENT_VAULT`. Every surface of one
    agent commits as one author, so a count by author would let one busy session
    trip every other one; repo HEAD plus `--surface $WRITER_SURFACE` is the
    narrowest scope git can answer.

A host whose scheduled tick already reminds names this hook in
$JOURNAL_DISABLED, or its sessions are nagged twice.

Every failure is silence. A missed reminder costs one Journal line; a broken
prompt hook costs the turn. Disable by naming `remind-journal-owed` in
$JOURNAL_DISABLED.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPONENT = "remind-journal-owed"
COOLDOWN_S = 3600
OWED_TIMEOUT = 3
EVENTS = ("UserPromptSubmit", "PreCompact")


def _spine_module():
    """preload-spine's resolver, by file path: its hyphenated name cannot be
    imported, and a second copy of a trusted-path resolver is how two copies
    of a security boundary drift apart."""
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location(
        "journal_preload_spine", HERE / "preload-spine.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _state_path(spine, session_id):
    home = spine.trusted_home(spine.log)
    if not home or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        return None
    return Path(home) / ".local" / "state" / "journal" / ("owed-%s.json" % session_id)


def _load(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, state):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _in_git_repo(path):
    """`owed` fails the whole check on a path that is not a repo, and a
    session started in a plain directory still commits to the vault."""
    p = Path(path)
    return any((d / ".git").exists() for d in (p, *p.parents))


def _ask_owed(script, cwd, since):
    # --uncommitted: a local session's own uncommitted Journal edit is its write.
    args = [sys.executable, script, "owed", "--since", "@%d" % since,
            "--uncommitted"]
    if _in_git_repo(cwd):
        args += ["--repo", cwd]
    vault = os.environ.get("AGENT_VAULT", "").strip()
    if vault:
        args += ["--repo", vault]
    surface = os.environ.get("WRITER_SURFACE", "").strip()
    if surface:
        args += ["--surface", surface, "--journal-surface", surface]
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=OWED_TIMEOUT)
        verdict = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return verdict if isinstance(verdict, dict) and verdict.get("owed") is True else None


def _message(event, verdict):
    if event == "PreCompact":
        return ("Carry this into the summary: the Journal is owed (%s). The "
                "first turn after compaction adds a Journal line for this "
                "session's work." % verdict.get("reason", ""))
    return ("Journal reminder: %s. Add a Journal line for this session's work "
            "when you reach a clean break." % verdict.get("reason", ""))


def _emit(event, text):
    if event == "PreCompact":
        # PreCompact stdout becomes the compaction's custom instructions.
        sys.stdout.write(text)
    else:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": event, "additionalContext": text}}))


def run(payload, spine, now):
    event, cwd = payload.get("hook_event_name"), payload.get("cwd")
    if event not in EVENTS or not isinstance(cwd, str) or not cwd:
        return
    path = _state_path(spine, str(payload.get("session_id", "")))
    script = spine.resolve_vardemem()
    if path is None or script is None:
        return
    state = _load(path)
    start = state.setdefault("start", now)
    reminded = state.get("reminded", 0)
    # The compaction erases any earlier reminder, so it ignores the cooldown.
    if event == "UserPromptSubmit" and now - reminded < COOLDOWN_S:
        _save(path, state)
        return
    verdict = _ask_owed(script, cwd, max(start, reminded))
    if verdict is not None:
        _emit(event, _message(event, verdict))
        state["reminded"] = now
    _save(path, state)


def main():
    try:
        spine = _spine_module()
        if spine._disabled(COMPONENT):
            return 0
        payload = json.loads(sys.stdin.read() or "{}")
        if isinstance(payload, dict):
            run(payload, spine, int(time.time()))
    except Exception:  # noqa: BLE001 -- a reminder never breaks the prompt
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
