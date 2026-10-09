#!/usr/bin/env python3
"""Smoke test for remind-journal-owed.py.

Drives the hook's `run()` against a throwaway git vault, with the REAL
`vardemem owed` from this repo standing in for the installed one, so every
assertion is about what the hook says and when, never about a real journal.
Time is passed in, so the cooldown is tested without sleeping.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
spec = importlib.util.spec_from_file_location(
    "remind_journal_owed", HERE.parent / "hooks" / "remind-journal-owed.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


def git(root, *argv):
    subprocess.run(["git", "-C", str(root), *argv], check=True,
                   capture_output=True)


T = 1_700_000_000


def commit(root, name, when):
    """Pinned commit times: commits made within one real second would tie
    with the journal commit and the cutoff, and the test would flake."""
    (root / name).write_text(name, encoding="utf-8")
    stamp = "@%d +0000" % when
    os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = stamp
    git(root, "add", "-A")
    git(root, "commit", "-qm", name)


def fire(spine, root, event, now, session="s1"):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        hook.run({"hook_event_name": event, "cwd": str(root),
                  "session_id": session}, spine, now)
    return out.getvalue()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="remind-journal-owed-"))
    root, home = tmp / "vault", tmp / "home"
    root.mkdir()
    home.mkdir()
    (root / ".vardemem.json").write_text('{"journal": "log"}', encoding="utf-8")
    (root / "log").mkdir()
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t"),
                 ("AGENT_VAULT", str(root))):
        os.environ[k] = v
    os.environ.pop("WRITER_SURFACE", None)
    spine = types.SimpleNamespace(
        trusted_home=lambda _log: str(home), log=lambda _msg: None,
        resolve_vardemem=lambda: str(REPO / "vardemem.py"))
    try:
        git(root, "init", "-q")
        commit(root, "log/journal-1.md", T)
        for i in range(5):
            commit(root, "before%d" % i, T + 10 + i)
        t0 = T + 100
        check(fire(spine, root, "UserPromptSubmit", t0) == "",
              "work from before the session's first prompt is not its debt")

        for i in range(5):
            commit(root, "w%d" % i, T + 110 + i)
        out = fire(spine, root, "UserPromptSubmit", T + 140)
        ctx = json.loads(out or "{}").get("hookSpecificOutput", {})
        check(ctx.get("hookEventName") == "UserPromptSubmit"
              and "5 commits" in ctx.get("additionalContext", ""),
              "five commits in the session inject one reminder line")

        for i in range(5, 10):
            commit(root, "w%d" % i, T + 150 + i)
        check(fire(spine, root, "UserPromptSubmit", T + 180) == "",
              "the cooldown holds the next prompt silent")
        check("Journal is owed" in fire(spine, root, "PreCompact", T + 190),
              "PreCompact ignores the cooldown and writes plain instructions")
        check(fire(spine, root, "UserPromptSubmit", T + 190 + hook.COOLDOWN_S) == "",
              "after a reminder, only newer work counts")

        plain = tmp / "plain"
        plain.mkdir()
        check("Journal is owed" in fire(spine, plain, "PreCompact", t0, session="s4"),
              "a session outside any repo still counts its vault commits")
        real = hook._spine_module()
        check(all(callable(getattr(real, n, None)) for n in
                  ("trusted_home", "log", "resolve_vardemem", "_disabled")),
              "the real preload-spine module exposes what the hook calls")

        check(fire(spine, root, "UserPromptSubmit", t0, session="../x") == "",
              "a session id that is not a plain token is silent")
        broken = types.SimpleNamespace(**vars(spine))
        broken.resolve_vardemem = lambda: None
        check(fire(broken, root, "UserPromptSubmit", t0, session="s2") == "",
              "no installed vardemem is silent")
        os.environ["AGENT_VAULT"] = str(tmp / "nowhere")
        check(fire(spine, tmp, "UserPromptSubmit", t0, session="s3") == ""
              and fire(spine, tmp, "UserPromptSubmit", t0 + 9999, session="s3") == "",
              "an unresolvable journal is silent")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%s" % ("ALL PASS" if not fails else "%d FAILED" % len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
