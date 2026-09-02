#!/usr/bin/env python3
"""Smoke test for enforce-journal-entry.py (journal plugin, varde-mem).

Runs the hook as a subprocess against a throwaway $HOME, a fake vault (a
`.vardemem.json` plus a Journal dir, reached through $AGENT_VAULT or the
trusted walk), and a synthetic transcript, so config, ledger, and activity
are all fully controlled. Every subprocess call is time-bounded, so a hang
FAILS instead of stalling the suite.

Case classes:
  - the gate never fires on: no config, a disabled tenant, a continuation
    (`stop_hook_active`), a Q&A session, failed writes, scratch/tmp/memory
    writes, a failed commit, a session with no resolvable journal
  - the gate fires once on: a successful edit, a successful commit
  - satisfiers: a write under the journal dir, a write to a worktree copy of
    the journal (relative-segment match), `vardemem put`, a Bash naming the
    journal path; a DENIED journal write does not satisfy
  - block-once: the second Stop of the same session passes, the ledger
    records one block, observe mode records and never blocks
  - concurrency: overlapping Stop processes take exactly one block slot
  - safety: a traversal session_id creates nothing outside the state dir;
    malformed payloads and transcripts are silent; records from another
    session id are ignored
  - the transcript reader's session slice and result mapping, tested in-process

Usage: python3 tests/smoke-enforce-journal-entry.py   (exit 0 = all pass)
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
HOOK = HOOKS / "enforce-journal-entry.py"
CFG_DIR = ".config/journal"
STATE_DIR = ".local/state/journal"
JOURNAL_REL = "Operations/Activity/Journal"

failures = []


def check(name, cond):
    if cond is True:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (" (HANG)" if cond == "HANG" else ""))
        failures.append(name)


def run(payload, home, env=None, timeout=5):
    """Run the hook; return (blocked: bool, reason: str). A hang is a FAILURE."""
    e = dict(os.environ)
    e["HOME"] = str(home)
    # Config is disk-located via $JOURNAL_CONFIG_DIR now, not derived from
    # $HOME (see #2310). Point it at the throwaway home's config dir so the
    # suite stays hermetic and never reads the developer's real config.
    e["JOURNAL_CONFIG_DIR"] = str(Path(home) / CFG_DIR)
    e.pop("JOURNAL_DISABLED", None); e.pop("HARNESS_MANAGEMENT_DISABLED", None)
    e.pop("AGENT_VAULT", None)
    e.pop("TMPDIR", None)
    if env:
        e.update(env)
    try:
        p = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload) if not isinstance(payload, str) else payload,
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=e, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "HANG", "hook exceeded its timeout"
    assert p.returncode == 0, f"hook must always exit 0, got {p.returncode}: {p.stderr}"
    if not p.stdout.strip():
        return False, ""
    out = json.loads(p.stdout)
    return out.get("decision") == "block", out.get("reason", "")


def write_cfg(home, cfg):
    d = Path(home) / CFG_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "journal-entry.json").write_text(json.dumps(cfg), encoding="utf-8")


def make_vault(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".vardemem.json").write_text(json.dumps({"journal": JOURNAL_REL}), encoding="utf-8")
    j = root / JOURNAL_REL
    j.mkdir(parents=True, exist_ok=True)
    (j / "journal-2026-09-01.md").write_text("# 2026-09-01\n", encoding="utf-8")
    return j


class Transcript:
    """Builds a Claude Code style JSONL transcript."""

    def __init__(self, path: Path, session_id: str):
        self.path, self.sid, self.lines = path, session_id, []

    def _rec(self, rtype, content, sid=None, **extra):
        rec = {"type": rtype, "uuid": str(uuid.uuid4()), "sessionId": sid or self.sid,
               "message": {"role": rtype, "content": content}}
        rec.update(extra)
        self.lines.append(json.dumps(rec))
        return rec

    def prompt(self, text="do the thing", sid=None):
        return self._rec("user", text, sid=sid)

    def text(self, text, sid=None):
        return self._rec("assistant", [{"type": "text", "text": text}], sid=sid)

    def call(self, name, inp, ok=True, sid=None, sidechain=False):
        tid = "toolu_" + uuid.uuid4().hex[:12]
        self._rec("assistant", [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
                  sid=sid, isSidechain=sidechain)
        if ok is not None:
            self._rec("user", [{"type": "tool_result", "tool_use_id": tid,
                                "content": "x", "is_error": not ok}], sid=sid)
        return tid

    def raw(self, line):
        self.lines.append(line)

    def write(self):
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        return str(self.path)


def payload(sid, transcript, cwd, active=False):
    return {"session_id": sid, "transcript_path": transcript, "cwd": cwd,
            "hook_event_name": "Stop", "stop_hook_active": active}


def new_session(tmp, name):
    sid = f"s-{name}-{uuid.uuid4().hex[:8]}"
    return sid, Transcript(Path(tmp) / f"{sid}.jsonl", sid)


with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp) / "home"
    home.mkdir()
    vault = Path(tmp) / "vault"
    journal = make_vault(vault)
    # A path OUTSIDE every tmp root: the suite's own tempdir is under
    # /var/folders on macOS, which the hook rightly ignores as scratch.
    repo = Path("/srv/work/repo")
    env_vault = {"AGENT_VAULT": str(vault)}
    write_cfg(home, {"enabled": True})

    print("gate never fires")
    sid, t = new_session(tmp, "noconfig")
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    bare_home = Path(tmp) / "bare"; bare_home.mkdir()
    check("no config: silent", run(payload(sid, t.write(), str(repo)), bare_home, env_vault) == (False, ""))

    check("disabled by name in $JOURNAL_DISABLED: silent",
          run(payload(sid, str(t.path), str(repo)), home,
              {**env_vault, "JOURNAL_DISABLED": "x,enforce-journal-entry"}) == (False, ""))
    check("disabled via legacy $HARNESS_MANAGEMENT_DISABLED: silent",
          run(payload(sid, str(t.path), str(repo)), home,
              {**env_vault, "HARNESS_MANAGEMENT_DISABLED": "enforce-journal-entry"}) == (False, ""))
    check("disabled via legacy $HARNESS_MANAGEMENT_DISABLED=stop-gate umbrella: silent",
          run(payload(sid, str(t.path), str(repo)), home,
              {**env_vault, "HARNESS_MANAGEMENT_DISABLED": "x,stop-gate"}) == (False, ""))
    check("$JOURNAL_DISABLED=stop-gate does NOT disable (umbrella is legacy-only): BLOCK",
          run(payload(sid, str(t.path), str(repo)), home,
              {**env_vault, "JOURNAL_DISABLED": "stop-gate"})[0] is True)
    check("stop_hook_active: silent",
          run(payload(sid, str(t.path), str(repo), active=True), home, env_vault) == (False, ""))

    sid, t = new_session(tmp, "qa")
    t.prompt("what does this do?"); t.call("Read", {"file_path": str(repo / "a.py")})
    t.call("Bash", {"command": "ls -la"}); t.call("Grep", {"pattern": "x"}); t.text("It lists.")
    check("Q&A session (reads, ls, grep): silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))

    sid, t = new_session(tmp, "failedwrite")
    t.prompt(); t.call("Write", {"file_path": str(repo / "a.py"), "content": "x"}, ok=False)
    t.call("Edit", {"file_path": str(repo / "b.py"), "old_string": "a", "new_string": "b"}, ok=None)
    check("failed write and interrupted edit: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))

    sid, t = new_session(tmp, "scratch")
    t.prompt()
    t.call("Write", {"file_path": "/tmp/notes.md", "content": "x"})
    t.call("Write", {"file_path": "/private/tmp/claude-501/x/scratchpad/plan.md", "content": "x"})
    t.call("Write", {"file_path": str(home / ".claude/projects/-x-y/memory/fact.md"), "content": "x"})
    # Literal paths only: the suite's own tempdir is wherever this host's
    # TMPDIR points, which need not be one of the hook's fixed tmp roots.
    t.call("Write", {"file_path": "/var/folders/zz/abc/T/under-var-folders.md", "content": "x"})
    check("tmp, scratchpad, auto-memory, macOS /var/folders writes: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    sid, t = new_session(tmp, "tmpdirenv")
    t.prompt(); t.call("Write", {"file_path": "/srv/mytmp/scratch.md", "content": "x"})
    check("a write under the session's own $TMPDIR is scratch: silent",
          run(payload(sid, t.write(), str(repo)), home, {**env_vault, "TMPDIR": "/srv/mytmp/"}) == (False, ""))
    check("the same write with no $TMPDIR is work: BLOCK",
          run(payload(sid, str(t.path), str(repo)), home, env_vault)[0] is True)

    sid, t = new_session(tmp, "failedcommit")
    t.prompt(); t.call("Bash", {"command": "git commit -m x"}, ok=False)
    check("failed commit: silent", run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))

    sid, t = new_session(tmp, "agentonly")
    t.prompt(); t.call("Agent", {"prompt": "go"}); t.call("Bash", {"command": "git status"})
    check("Agent dispatch and git status: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))

    sid, t = new_session(tmp, "nojournal")
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    check("no .vardemem.json and no AGENT_VAULT: silent",
          run(payload(sid, t.write(), str(repo)), home) == (False, ""))
    check("untrusted walk root, no AGENT_VAULT: silent",
          run(payload(sid, str(t.path), str(vault)), home) == (False, ""))
    broken = Path(tmp) / "broken-cwd"; broken.mkdir()
    (broken / ".vardemem.json").symlink_to(broken / "missing-config")
    check("a dangling .vardemem.json symlink in cwd stops resolution (vardemem fails closed): silent",
          run(payload(sid, str(t.path), str(broken)), home, env_vault) == (False, ""))

    print("gate fires once")
    sid, t = new_session(tmp, "edit")
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    t.text("Done.")
    blocked, reason = run(payload(sid, t.write(), str(repo)), home, env_vault)
    check("successful edit, no journal write: BLOCK", blocked is True)
    check("reason names the journal dir", str(journal) in reason)
    check("reason names the newest day file", "journal-2026-09-01.md" in reason)
    check("reason says it fires once", "at most once per session" in reason)
    ledger = home / STATE_DIR / f"{sid}.json"
    data = json.loads(ledger.read_text(encoding="utf-8", errors="replace")) if ledger.exists() else {}
    check("ledger records one block", data.get("enforce-journal-entry", {}).get("blocks") == 1)
    t.text("Ending anyway.")
    check("second Stop of the same session: silent (block-once)",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    check("ledger still one block", json.loads(ledger.read_text(encoding="utf-8", errors="replace"))["enforce-journal-entry"]["blocks"] == 1)

    sid, t = new_session(tmp, "commit")
    t.prompt(); t.call("Bash", {"command": "git add x && git commit -m 'feat: x'"})
    check("successful commit, no journal write: BLOCK",
          run(payload(sid, t.write(), str(repo)), home, env_vault)[0] is True)

    sid, t = new_session(tmp, "walk")
    # The trust file lives under the PASSWD home, not $HOME, so the suite
    # points at its own copy through the absolute override vardemem honors.
    trust_file = Path(tmp) / "trust.json"
    trust_file.write_text(json.dumps([str(vault)]), encoding="utf-8")
    t.prompt(); t.call("Bash", {"command": "git commit -m 'rules: x'"})
    check("trusted walk from inside the vault resolves the journal: BLOCK",
          run(payload(sid, t.write(), str(vault / "Rules")), home,
              {"VARDEMEM_TRUST_FILE": str(trust_file)})[0] is True)

    # #2310 guard: config is disk-located via $JOURNAL_CONFIG_DIR, never derived
    # from $HOME. A spoofed $HOME carrying an enabled config must not force a
    # block when the absolute override points at a dir that has none.
    sid, t = new_session(tmp, "homespoof")
    attacker = Path(tmp) / "attacker-home"
    (attacker / CFG_DIR).mkdir(parents=True, exist_ok=True)
    (attacker / CFG_DIR / "journal-entry.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8")
    empty_cfg = Path(tmp) / "empty-cfg"; empty_cfg.mkdir(parents=True, exist_ok=True)
    t.prompt(); t.call("Bash", {"command": "git commit -m 'x'"})
    check("enabled config under a spoofed $HOME is ignored; the override wins: silent",
          run(payload(sid, t.write(), str(repo)), attacker,
              {**env_vault, "JOURNAL_CONFIG_DIR": str(empty_cfg)}) == (False, ""))

    sid, t = new_session(tmp, "threshold")
    write_cfg(home, {"enabled": True, "min_work_events": 2})
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    check("one edit under min_work_events 2: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    t.call("Bash", {"command": "gh issue close 12"})
    check("edit plus issue close reaches 2: BLOCK",
          run(payload(sid, t.write(), str(repo)), home, env_vault)[0] is True)
    write_cfg(home, {"enabled": True})

    print("satisfiers")
    def satisfied(name, extra):
        sid, t = new_session(tmp, name)
        t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
        extra(t)
        return run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, "")

    check("Write under the journal dir satisfies",
          satisfied("jwrite", lambda t: t.call("Write", {"file_path": str(journal / "journal-2026-09-02.md"), "content": "x"})))
    check("Edit to a worktree copy of the journal (relative segments) satisfies",
          satisfied("jworktree", lambda t: t.call("Edit", {"file_path": f"/elsewhere/wt/{JOURNAL_REL}/journal-2026-09-02.md", "old_string": "a", "new_string": "b"})))
    check("MultiEdit under the journal dir satisfies",
          satisfied("jmulti", lambda t: t.call("MultiEdit", {"file_path": str(journal / "journal-2026-09-02.md"), "edits": []})))
    check("`vardemem put <date>` satisfies",
          satisfied("jput", lambda t: t.call("Bash", {"command": 'vardemem put 2026-09-02 "shipped the gate"'})))
    check("`vardemem.py put <date>` by absolute path after && satisfies",
          satisfied("jputpy", lambda t: t.call("Bash", {"command": 'cd /v && /x/varde-mem/vardemem.py put 2026-09-02 "x"'})))
    check("Bash appending into the journal path satisfies",
          satisfied("jbash", lambda t: t.call("Bash", {"command": f"cat >> {journal}/journal-2026-09-02.md <<'EOF'\n- x\nEOF"})))
    check("Bash tee into a worktree copy of the journal satisfies",
          satisfied("jtee", lambda t: t.call("Bash", {"command": f"printf x | tee -a /wt/{JOURNAL_REL}/journal-2026-09-02.md"})))
    check("a DENIED journal write does not satisfy",
          not satisfied("jdenied", lambda t: t.call("Write", {"file_path": str(journal / "journal-2026-09-02.md"), "content": "x"}, ok=False)))
    check("a grep that mentions the journal path does not satisfy",
          not satisfied("jgrep", lambda t: t.call("Bash", {"command": f"grep -n Journal x.md; ls {journal} 2>/dev/null | tail -3"})))
    check("a heredoc that quotes `vardemem put <YYYY-MM-DD>` does not satisfy",
          not satisfied("jquote", lambda t: t.call("Bash", {"command": "cat > /srv/issue.md <<'EOF'\nrun `vardemem put <YYYY-MM-DD> \"line\"` at day close\nEOF"})))
    check("a heredoc that names the journal path with no redirect into it does not satisfy",
          not satisfied("jbody", lambda t: t.call("Bash", {"command": f"cat > /srv/issue.md <<'EOF'\nthe store is {JOURNAL_REL}/\nEOF"})))
    sid, t = new_session(tmp, "whichvarde")
    t.prompt(); t.call("Bash", {"command": "which varde-issue; varde-issue --help"})
    check("`which varde-issue` and `--help` are not work: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    t.call("Bash", {"command": "varde-issue --title 'x' --type Task"})
    check("`varde-issue --title` is work: BLOCK",
          run(payload(sid, t.write(), str(repo)), home, env_vault)[0] is True)

    print("work verbs count only at command position")
    for label, cmd in [
        ("grep for 'git commit' piped to head", "grep -rn 'git commit' docs/ | head -20"),
        ("git commit --dry-run", "git commit --dry-run"),
        ("echo mentioning git push", "echo 'next step: git push origin main'"),
        ("gh issue create --help", "gh issue create --help"),
        ("gh issue create -h at end of command", "gh issue create -h"),
        ("git log --grep='git commit'", "git log --oneline --grep='git commit' -n 5"),
        ("man git commit", "man git commit | head"),
        # A work verb must end on a token boundary: these are different git
        # commands that a bare prefix test read as `git commit`.
        ("git commit-tree", "git commit-tree -p HEAD $(git write-tree)"),
        ("git commit-graph write", "git commit-graph write --reachable"),
    ]:
        sid, t = new_session(tmp, "verb")
        t.prompt(); t.call("Bash", {"command": cmd})
        check(f"{label}: silent", run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    sid, t = new_session(tmp, "threereads")
    t.prompt()
    for cmd in ("grep -n 'git commit' README", "man git commit | head", "echo 'git push'"):
        t.call("Bash", {"command": cmd})
    check("three read-only mentions in one session: silent (zero work events)",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    for label, cmd in [
        ("git commit after cd and env assignment", "cd /srv && FOO=1 git commit -m x"),
        ("sudo gh issue close", "sudo gh issue close 4"),
        ("git push in a subshell", "(git push origin main)"),
    ]:
        sid, t = new_session(tmp, "verbpos")
        t.prompt(); t.call("Bash", {"command": cmd})
        check(f"{label}: BLOCK", run(payload(sid, t.write(), str(repo)), home, env_vault)[0] is True)
    check("cp into the journal satisfies",
          satisfied("jcp", lambda t: t.call("Bash", {"command": f"cp /srv/draft.md {journal}/journal-2026-09-02.md"})))
    check("`env FOO=1 vardemem put <date>` satisfies",
          satisfied("jenvput", lambda t: t.call("Bash", {"command": 'env FOO=1 vardemem put 2026-09-02 "x"'})))
    check("a subagent's edit (isSidechain) counts as work: BLOCK",
          (lambda s_t: run(payload(s_t[0], s_t[1].write(), str(repo)), home, env_vault)[0] is True)(
              (lambda s_t: (s_t[1].prompt(), s_t[1].call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"}, sidechain=True), s_t)[2])(new_session(tmp, "side"))))
    # A journal-only session is silent because the write SATISFIES the gate;
    # whether it also counts as work is unreachable, since satisfaction is
    # checked first, so this case claims no more than silence.
    sid, t = new_session(tmp, "jonly")
    t.prompt(); t.call("Write", {"file_path": str(journal / "j.md"), "content": "x"})
    check("journal-only session: silent (satisfied)",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))

    print("heredoc bodies are data")
    check("a `vardemem put` satisfier AFTER an UNQUOTED heredoc still satisfies (no double-strip false fire)",
          satisfied("hdthenput", lambda t: t.call("Bash", {"command": "cat > /srv/issue.md <<EOF\ncontext for the issue\nEOF\nvardemem put 2026-09-02 \"shipped the gate\""})))
    check("a `cp` into the journal AFTER an UNQUOTED heredoc still satisfies",
          satisfied("hdthencp", lambda t: t.call("Bash", {"command": f"cat > /srv/x.md <<EOF\nbody\nEOF\ncp /srv/x.md {journal}/journal-2026-09-02.md"})))
    check("a heredoc body holding a real `vardemem put <date>` line does not satisfy",
          not satisfied("hdput", lambda t: t.call("Bash", {"command": "cat > /srv/issue.md <<'EOF'\nvardemem put 2026-09-02 'example only'\nEOF"})))
    sid, t = new_session(tmp, "hdcommit")
    t.prompt(); t.call("Bash", {"command": "cat > /srv/issue.md <<'EOF'\ngit commit -m 'example only'\nEOF"})
    check("a heredoc body holding `git commit` is not work: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    sid, t = new_session(tmp, "hdafter")
    t.prompt(); t.call("Bash", {"command": "cat > /srv/x.md <<EOF\nbody\nEOF\ngit commit -m real"})
    check("a real `git commit` AFTER a heredoc still counts: BLOCK",
          run(payload(sid, t.write(), str(repo)), home, env_vault)[0] is True)

    two_heredocs = (f"cat <<A <<B\nfirst body\nA\nprintf x >> {journal}/journal-2026-09-01.md\nB")
    check("the second of two heredocs on one line is still data (does not satisfy)",
          not satisfied("hd2", lambda t: t.call("Bash", {"command": two_heredocs})))
    sid, t = new_session(tmp, "hd2work")
    t.prompt(); t.call("Bash", {"command": "cat <<A <<B\nfirst body\nA\ngit commit -m 'example only'\nB"})
    check("the second of two heredocs holding `git commit` is not work: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    check("cp OUT of the journal does not satisfy",
          not satisfied("cpout", lambda t: t.call("Bash", {"command": f"cp {journal}/journal-2026-09-01.md /srv/backup.md"})))
    check("`vardemem put \"$DAY\" ...` with a quoted variable date satisfies",
          satisfied("putvar", lambda t: t.call("Bash", {"command": 'DAY=2026-09-01; vardemem put "$DAY" "recorded work"'})))
    sid, t = new_session(tmp, "hdindent")
    t.prompt(); t.call("Bash", {"command": "cat <<EOF\n  EOF\ngit commit -m x\nEOF"})
    check("an indented terminator under <<EOF is body text, so the commit inside is not work: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    sid, t = new_session(tmp, "hddash")
    t.prompt(); t.call("Bash", {"command": "cat <<-EOF\n\tbody\n\tEOF\ngit commit -m x"})
    check("<<-EOF forgives a tab-indented terminator, so the commit after it counts: BLOCK",
          run(payload(sid, t.write(), str(repo)), home, env_vault)[0] is True)
    check("a QUOTED redirect target in the journal satisfies (the target is unwrapped, not dropped)",
          satisfied("quotedtarget", lambda t: t.call("Bash", {"command": f'echo "- x" >> "$VAULT/{JOURNAL_REL}/journal-2026-09-02.md"'})))
    check("`<<` inside a quoted span opens no heredoc, so a later `vardemem put` still satisfies",
          satisfied("shiftput", lambda t: t.call("Bash", {"command": 'echo "shift is 1 << 2"\nvardemem put 2026-09-02 "x"'})))
    check("a redirect into the journal inside a quoted string does not satisfy",
          not satisfied("quotedredir", lambda t: t.call("Bash", {"command": f"printf 'documentation: > {JOURNAL_REL}/journal-2026-09-01.md'"})))
    check("a heredoc with a hyphenated delimiter is still data",
          not satisfied("hdhyphen", lambda t: t.call("Bash", {"command": "cat > /srv/d.md <<'END-OF-DOC'\nvardemem put 2026-09-02 x\nEND-OF-DOC"})))
    sid, t = new_session(tmp, "quotedcommit")
    t.prompt(); t.call("Bash", {"command": "printf 'documentation says; git commit -m x'"})
    check("`git commit` inside a quoted string is not work: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    check("`vardemem put <date>` inside a quoted string does not satisfy",
          not satisfied("quotedput", lambda t: t.call("Bash", {"command": "printf 'documentation says; vardemem put 2026-09-02 x'"})))
    sid, t = new_session(tmp, "quotedreal")
    t.prompt(); t.call("Bash", {"command": "git commit -m 'say; nothing'"})
    check("a real commit with a quoted message still counts: BLOCK",
          run(payload(sid, t.write(), str(repo)), home, env_vault)[0] is True)

    print("journal path must end a segment")
    check("a Write whose path climbs out of the journal with .. does not satisfy",
          not satisfied("dotdot", lambda t: t.call("Write", {"file_path": f"/tmp/{JOURNAL_REL}/../../not-a-journal.md", "content": "x"})))
    check("redirect into Journal_evil does not satisfy",
          not satisfied("evilredir", lambda t: t.call("Bash", {"command": f"printf x > /srv/{JOURNAL_REL}_evil/not-a-journal.md"})))
    check("cp into Journal_evil does not satisfy",
          not satisfied("evilcp", lambda t: t.call("Bash", {"command": f"cp /srv/draft.md /srv/{JOURNAL_REL}_evil/not-a-journal.md"})))
    check("Write to Journal_evil does not satisfy",
          not satisfied("evilwrite", lambda t: t.call("Write", {"file_path": f"/srv/{JOURNAL_REL}_evil/x.md", "content": "x"})))
    check("a journal Write with NO result does not satisfy",
          not satisfied("noresult", lambda t: t.call("Write", {"file_path": str(journal / "journal-2026-09-02.md"), "content": "x"}, ok=None)))

    print("trust file override")
    alt_trust = Path(tmp) / "alt-trust.json"
    alt_trust.write_text(json.dumps([str(vault)]), encoding="utf-8")
    sid, t = new_session(tmp, "trustenv")
    t.prompt(); t.call("Bash", {"command": "git commit -m 'rules: x'"})
    check("absolute $VARDEMEM_TRUST_FILE trusts the walk root: BLOCK",
          run(payload(sid, t.write(), str(vault / "Rules")), home, {"VARDEMEM_TRUST_FILE": str(alt_trust)})[0] is True)
    check("relative $VARDEMEM_TRUST_FILE is ignored: silent",
          run(payload(sid, str(t.path), str(vault / "Rules")), home, {"VARDEMEM_TRUST_FILE": "alt-trust.json"}) == (False, ""))
    sid, t = new_session(tmp, "trustpad")
    t.prompt(); t.call("Bash", {"command": "git commit -m 'rules: y'"})
    check("$VARDEMEM_TRUST_FILE with surrounding spaces is trimmed, as vardemem trims it: BLOCK",
          run(payload(sid, t.write(), str(vault / "Rules")), home, {"VARDEMEM_TRUST_FILE": f"  {alt_trust}  "})[0] is True)
    sid, t = new_session(tmp, "vaultspace")
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    check("$AGENT_VAULT is read literally (a padded value resolves nothing): silent",
          run(payload(sid, t.write(), str(repo)), home, {"AGENT_VAULT": f" {vault} "}) == (False, ""))

    sid, t = new_session(tmp, "nosid")
    stale = dict(t.prompt())
    t.lines.pop(); stale.pop("sessionId"); t.lines.append(json.dumps(stale))
    tid = "toolu_stale"
    for r in ({"type": "assistant", "uuid": "u1", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "Edit", "input": {"file_path": str(repo / "a.py")}}]}},
              {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": False}]}}):
        t.raw(json.dumps(r))
    t.prompt(); t.call("Read", {"file_path": str(repo / "a.py")})
    check("an edit in a record with NO sessionId is not this session's work: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))

    print("observe mode and composition")
    write_cfg(home, {"enabled": True, "max_blocks_per_session": 0})
    sid, t = new_session(tmp, "observe")
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    check("observe mode: silent", run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))
    data = json.loads((home / STATE_DIR / f"{sid}.json").read_text(encoding="utf-8", errors="replace"))
    check("observe mode: ledger records the observation, zero blocks",
          data["enforce-journal-entry"]["observed"] == 1 and data["enforce-journal-entry"]["blocks"] == 0)
    check("observe mode: hook log has the would-block line",
          "observe: would block" in (home / ".local/log/enforce-journal-entry-hook.log").read_text(encoding="utf-8", errors="replace"))

    write_cfg(home, {"enabled": True, "max_blocks_per_session": 2})
    sid, t = new_session(tmp, "budget2")
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    first = run(payload(sid, t.write(), str(repo)), home, env_vault)[0]
    t.text("again")
    second = run(payload(sid, t.write(), str(repo)), home, env_vault)[0]
    t.text("and again")
    third = run(payload(sid, t.write(), str(repo)), home, env_vault)[0]
    check("max_blocks_per_session 2: blocks twice, then passes", (first, second, third) == (True, True, False))
    # Same-Stop dedupe at budget>1: the SAME transcript (same stop_key) run
    # twice must block once, not twice, even with budget to spare. Without the
    # per-Stop claim, one Stop invoked twice would take two slots.
    sid, t = new_session(tmp, "budget2samestop")
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    tp = t.write()
    a1 = run(payload(sid, tp, str(repo)), home, env_vault)[0]
    a2 = run(payload(sid, tp, str(repo)), home, env_vault)[0]
    check("budget 2, same Stop run twice: blocks once (per-Stop dedupe)", (a1, a2) == (True, False))
    write_cfg(home, {"enabled": True})

    # Reset to the default budget: the concurrency tests below assert exactly
    # one block, which is the budget-1 property. The numbered O_EXCL slots
    # (block0..) are the serialization point, so overlapping processes on the
    # same session take at most one, whatever their Stop keys.
    write_cfg(home, {"enabled": True})

    sid, t = new_session(tmp, "concurrent")
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    tp = t.write()
    e = dict(os.environ); e.update({"HOME": str(home), "JOURNAL_CONFIG_DIR": str(Path(home) / CFG_DIR), **env_vault}); e.pop("JOURNAL_DISABLED", None); e.pop("HARNESS_MANAGEMENT_DISABLED", None)
    def race(payloads):
        """Start every process, feed all stdins, THEN collect, so they overlap."""
        procs = [subprocess.Popen([sys.executable, str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=e) for _ in payloads]
        for p, pl in zip(procs, payloads):
            p.stdin.write(json.dumps(pl)); p.stdin.close()
        outs = [p.stdout.read() for p in procs]
        for p in procs:
            p.wait(timeout=10)
        return sum(1 for o in outs if o.strip()), all(p.returncode == 0 for p in procs)
    check("eight overlapping processes on one Stop: exactly one block",
          race([payload(sid, tp, str(repo))] * 8) == (1, True))
    write_cfg(home, {"enabled": True})

    sid, _ = new_session(tmp, "twokeys")
    tps = []
    for k in range(6):
        tk = Transcript(Path(tmp) / f"{sid}-{k}.jsonl", sid)
        tk.prompt(); tk.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
        tps.append(tk.write())
    check("six overlapping Stops with DISTINCT keys for one session, budget 1: exactly one block",
          race([payload(sid, tp, str(repo)) for tp in tps]) == (1, True))

    sid, t = new_session(tmp, "ledgerdir")
    (home / STATE_DIR / f"{sid}.json").mkdir(parents=True, exist_ok=True)
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    first = run(payload(sid, t.write(), str(repo)), home, env_vault)
    t.text("again")
    second = run(payload(sid, t.write(), str(repo)), home, env_vault)
    check("ledger JSON path is a directory: the slot still bounds it to one block, no nag",
          first[0] is True and second == (False, ""))
    check("ledger JSON path is a directory: the log records the lost audit entry",
          "audit entry lost" in (home / ".local/log/enforce-journal-entry-hook.log").read_text(encoding="utf-8", errors="replace"))

    print("safety")
    sid, t = new_session(tmp, "traversal")
    t.prompt(); t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    tp = t.write()
    before = set(os.listdir(home / ".local/state"))
    bad = payload("../../escape", tp, str(repo))
    check("traversal session_id: silent", run(bad, home, env_vault) == (False, ""))
    check("traversal session_id: no file created outside the state dir",
          set(os.listdir(home / ".local/state")) == before
          and not (home / ".local/state/escape.json").exists())

    check("malformed payload (not JSON): silent", run("{not json", home, env_vault) == (False, ""))
    check("payload is a list: silent", run([1, 2], home, env_vault) == (False, ""))
    check("missing transcript: silent",
          run(payload(sid, str(Path(tmp) / "nope.jsonl"), str(repo)), home, env_vault) == (False, ""))
    sid, t = new_session(tmp, "badlines")
    t.prompt(); t.raw("{not json"); t.raw("[1,2]"); t.raw('{"type":"assistant"}')
    t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"})
    check("malformed transcript lines are skipped, real edit still counted: BLOCK",
          run(payload(sid, t.write(), str(repo)), home, env_vault)[0] is True)

    sid, t = new_session(tmp, "othersid")
    t.prompt(sid="older-session")
    t.call("Edit", {"file_path": str(repo / "a.py"), "old_string": "a", "new_string": "b"}, sid="older-session")
    t.prompt(); t.call("Read", {"file_path": str(repo / "a.py")})
    check("edits from another session id are ignored: silent",
          run(payload(sid, t.write(), str(repo)), home, env_vault) == (False, ""))

    fifo = Path(tmp) / "fifo.jsonl"
    os.mkfifo(fifo)
    sid = "s-fifo"
    check("FIFO transcript: silent, no hang", run(payload(sid, str(fifo), str(repo)), home, env_vault) == (False, ""))

    print("reader and ledger (in-process)")
    import importlib.util
    spec = importlib.util.spec_from_file_location("ej", HOOK)
    ej = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ej  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(ej)
    sid, t = new_session(tmp, "split")
    t.prompt("first"); t.call("Edit", {"file_path": "/r/a.py"}); t.text("done one")
    t.prompt("second"); t.call("Bash", {"command": "ls"}, ok=False); t.call("Write", {"file_path": "/r/b"}, ok=None)
    t.text("I'll open the PR next.")
    act = ej.inspect_transcript(t.write(), sid)
    # Every call of the SESSION, across both turns: a journal write in an
    # earlier turn still satisfies the gate.
    check("the reader holds every call of the session", [c.name for c in act.calls] == ["Edit", "Bash", "Write"])
    check("ok mapping: True / False / None", [c.ok for c in act.calls] == [True, False, None])
    check("last assistant uuid is set", isinstance(act.last_assistant_uuid, str) and len(act.last_assistant_uuid) > 0)
    check("stop_key is the last assistant uuid", ej.stop_key_for(act, str(t.path)) == act.last_assistant_uuid)
    led = ej.Ledger("bad/../id", Path(tmp) / "state")
    check("Ledger refuses a traversal id", led.valid is False and led.claim_block_slot(1) is False)
    led = ej.Ledger("good-id", Path(tmp) / "state")
    check("a block slot is exclusive", led.claim_block_slot(1) is True and led.claim_block_slot(1) is False)
    led_c = ej.Ledger("claim-id", Path(tmp) / "state")
    check("claim_stop is exclusive per stop_key", led_c.claim_stop("k1") is True and led_c.claim_stop("k1") is False)
    check("claim_stop admits a different stop_key", led_c.claim_stop("k2") is True)
    check("claim_stop refuses a traversal id", ej.Ledger("bad/../id", Path(tmp) / "state").claim_stop("k") is False)
    check("blocks() counts the taken slot", led.blocks() == 1)
    # The home-resolution branch of _trust_file_path (the real production path
    # when no absolute $VARDEMEM_TRUST_FILE override is set) is otherwise
    # untested, since every trust-walk case above uses the override. Prime
    # agent_home's cache with a temp home and assert the trust file lands under it.
    import agent_home
    os.environ.pop("VARDEMEM_TRUST_FILE", None)
    fake_home = Path(tmp) / "faux-home"
    agent_home._CACHED = str(fake_home)
    try:
        check("_trust_file_path resolves under the OS home when no override is set",
              ej._trust_file_path() == fake_home / ".config" / "vardemem" / "trust.json")
    finally:
        agent_home._CACHED = agent_home._UNSET
    ro = Path(tmp) / "ro-state"; ro.mkdir(); os.chmod(ro, 0o500)
    try:
        led_ro = ej.Ledger("ro-sess", ro)
        check("claim_block_slot fails open on an unwritable state dir",
              led_ro.claim_block_slot(1) is False and led_ro.blocks() == 0)
    finally:
        os.chmod(ro, 0o700)  # restore so the tempdir can be cleaned up
    old = Path(tmp) / "state" / "ancient.json"; old.write_text("{}", encoding="utf-8")
    os.utime(old, (0, 0))
    led.prune()
    check("prune removes files older than the window",
          not old.exists() and (Path(tmp) / "state" / f"good-id.{ej.HOOK_NAME}.block0").exists())

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all passed")
