#!/usr/bin/env python3
"""Smoke test for enforce-journal-tags.py (journal), moved and renamed from the
monolith's enforce-daily-note-tags.py.

Runs the hook as a subprocess against a throwaway $HOME and a real on-disk note, so the
per-tenant config AND the before-document are both fully controlled.

EVERY finding from review has a named regression
case below, built from the reviewer's own concrete input. The classes:

  - a Write that OVERWRITES a note holding legacy untagged entries must NOT deny
  - non-entries are never gated: bold-only headers, fenced code (honoring delimiter char
    and length), HTML comments (including a mid-line opener), <pre> blocks, pipe-bearing
    lines, and column-0 bold sub-points sitting directly under their entry
  - identity is the WHOLE LINE, so none of the three multiset bugs can recur
    (order-dependence in both directions, and net-count cancellation)
  - `old_string` cannot launder a new untagged entry
  - block boundaries: an entry right after a closed fence / comment / <pre> IS gated
  - the deny message must be TRUE and actionable (it once claimed existing entries are
    never flagged while denying an edited one, and showed a [decision] example to a tenant
    whose tags were ["call", "chore"])
  - apply_call refuses to replay anything ambiguous rather than guessing
  - fail open on malformed payload / config / unreplayable edit, always
  - no hang: no user regex, entries() is a single linear pass, and both an outsized
    MultiEdit and an outsized document are refused rather than replayed
  - CommonMark fidelity where it changes the answer: strong-emphasis delimiter validity,
    space/tab (not any-unicode-space) separators, inline code spans, and block-level vs
    inline raw HTML

Every subprocess call is time-bounded, so a hang FAILS instead of stalling the suite.

Usage: python3 tests/smoke-enforce-journal-tags.py   (exit 0 = all pass)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "enforce-journal-tags.py"
CFG_DIR = ".config/journal"

failures = []


def run(payload, home, env=None, timeout=5):
    """Run the hook; return (denied: bool, reason: str). A hang is a FAILURE."""
    e = dict(os.environ)
    e["HOME"] = str(home)
    # Config is disk-located via $JOURNAL_CONFIG_DIR now, not derived from $HOME
    # (see #2310). Point it at the throwaway home's config dir so the suite stays
    # hermetic and never reads the developer's real config.
    e["JOURNAL_CONFIG_DIR"] = str(Path(home) / CFG_DIR)
    e.pop("JOURNAL_DISABLED", None)
    e.pop("HARNESS_MANAGEMENT_DISABLED", None)
    if env:
        e.update(env)
    try:
        p = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload) if not isinstance(payload, str) else payload,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=e, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "HANG", "hook exceeded its timeout"
    assert p.returncode == 0, f"hook must always exit 0, got {p.returncode}: {p.stderr}"
    if not p.stdout.strip():
        return False, ""
    out = json.loads(p.stdout)
    hso = out.get("hookSpecificOutput", {})
    return hso.get("permissionDecision") == "deny", hso.get("permissionDecisionReason", "")


def write_cfg(home, cfg):
    d = Path(home) / CFG_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "journal-tags.json").write_text(json.dumps(cfg), encoding="utf-8")


def check(name, cond):
    if cond is True:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (" (HANG)" if cond == "HANG" else ""))
        failures.append(name)


UNTAGGED = "**Shipped the thing.** It went out today."
TAGGED = "[decision] **Shipped the thing.** Sam's call."
LEGACY = "**Legacy entry.** Written before the hook existed."

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    vault = home / "vault" / "Daily Notes"
    vault.mkdir(parents=True)
    note = vault / "2026-07-13.md"
    NOTE = str(note)

    def w(content, path=None):
        return {"tool_name": "Write",
                "tool_input": {"file_path": path or NOTE, "content": content}}

    def ed(new, old, path=None):
        return {"tool_name": "Edit",
                "tool_input": {"file_path": path or NOTE,
                               "new_string": new, "old_string": old}}

    # ---- opt-in gate ----
    print("opt-in gate")
    note.write_text("", encoding="utf-8")
    check("no config -> no-op even on an untagged entry", run(w(UNTAGGED), home)[0] is False)

    write_cfg(home, {"enabled": True})

    # ---- core deny / allow (new note) ----
    print("core deny / allow")
    note.write_text("", encoding="utf-8")
    denied, reason = run(w(UNTAGGED), home)
    check("untagged entry -> DENY", denied is True)
    check("deny reason names the tags", "[decision]" in reason and "[ops]" in reason)
    check("tagged entry -> allow", run(w(TAGGED), home)[0] is False)
    for tag in ("decision", "fix", "architecture", "ops"):
        check(f"[{tag}] allowed", run(w(f"[{tag}] **Thing.** Body."), home)[0] is False)
    check("unknown tag -> DENY", run(w("[bogus] **Thing.** Body."), home)[0] is True)
    check("unknown tag, NO space ([bogus]**X.**) -> DENY",
          run(w("[bogus]**Launch.** shipped today."), home)[0] is True)

    # ---- REGRESSION: Write overwriting legacy untagged entries ----
    print("Write over an existing note with legacy untagged entries")
    note.write_text(f"# 2026-07-13\n\n## Session Log\n\n{LEGACY}\n", encoding="utf-8")
    check("same-content overwrite -> allow (no entry ADDED)",
          run(w(f"# 2026-07-13\n\n## Session Log\n\n{LEGACY}\n"), home)[0] is False)
    check("overwrite appending a TAGGED entry -> allow (legacy stays untagged)",
          run(w(f"# 2026-07-13\n\n## Session Log\n\n{LEGACY}\n\n{TAGGED}\n"), home)[0] is False)
    check("overwrite appending an UNTAGGED entry -> DENY",
          run(w(f"# 2026-07-13\n\n## Session Log\n\n{LEGACY}\n\n{UNTAGGED}\n"), home)[0] is True)
    check("rewriting the BODY of a legacy untagged entry -> DENY (line changed; tag it)",
          run(w("**Legacy entry.** Body rewritten, typo fixed.\n"), home)[0] is True)
    check("voluntarily TAGGING a legacy entry in place -> allow",
          run(w("[ops] **Legacy entry.** Written before the hook existed.\n"), home)[0] is False)
    check("a SECOND entry with the same title as a legacy one -> DENY (counted, not matched)",
          run(w(f"{LEGACY}\n\n**Legacy entry.** A genuinely different, newly added one.\n"),
              home)[0] is True)

    # ---- REGRESSION: non-entries misread as entries ----
    print("non-entries are not entries")
    note.write_text("", encoding="utf-8")
    for label, content in [
        ("bold-only header (**Agenda**)", "**Agenda**\n\n- first item"),
        ("bold-only header (**Session Log**)", "**Session Log**\n"),
        ("fenced code block with bold", "```markdown\n**Example output.** not an entry\n```"),
        ("tilde-fenced code block", "~~~\n**Example.** not an entry\n~~~"),
        ("bold-led table row", "**Name** | Value\n--- | ---\nfoo | bar"),
        ("markdown headings / prose / list", "# 2026-07-13\n\n## Session Log\n\nProse.\n- item"),
        ("mid-line bold", "Some text with **bold** mid-line."),
    ]:
        check(f"{label} -> allow", run(w(content), home)[0] is False)

    print("Edit fragment adding mid-paragraph emphasis")
    note.write_text("This is TOKEN text.\n", encoding="utf-8")
    check("emphasis edit inside prose -> allow", run(ed("**important**", "TOKEN"), home)[0] is False)

    # ---- REGRESSION: old_string laundering ----
    print("old_string cannot launder a new untagged entry")
    note.write_text("Reference: **Launch.**\n", encoding="utf-8")
    check("inline bold in old_string does NOT exempt a new entry -> DENY",
          run(ed("Reference: **Launch.**\n\n**Launch.** shipped today.",
                 "Reference: **Launch.**"), home)[0] is True)

    note.write_text(f"{LEGACY}\n", encoding="utf-8")
    check("duplicate lead of an existing entry, new untagged entry -> DENY",
          run(ed(f"{LEGACY}\n\n**Legacy entry.** A DIFFERENT new one.",
                 LEGACY), home)[0] is True)

    # ---- adding via Edit ----
    print("Edit add paths")
    note.write_text("# 2026-07-13\n", encoding="utf-8")
    check("append an untagged entry -> DENY", run(ed(f"# 2026-07-13\n\n{UNTAGGED}", "# 2026-07-13"), home)[0] is True)
    check("append a tagged entry -> allow", run(ed(f"# 2026-07-13\n\n{TAGGED}", "# 2026-07-13"), home)[0] is False)
    check("old_string not in file -> allow (tool will error, not our call)",
          run(ed(UNTAGGED, "NOT PRESENT ANYWHERE"), home)[0] is False)

    print("MultiEdit")
    # NOTE: edits must target text that exists in the ORIGINAL file. Claude Code rejects
    # an edit whose old_string is a substring of a previous edit's new_string
    # ("old_string is a substring of a new_string from a previous edit"), so chaining
    # edits through text the first edit introduced would test a call the real tool
    # refuses. An earlier version of this test did exactly that: it
    # passed, but only proved the simulator accepts something the tool would not.
    note.write_text("# 2026-07-13\n\n## Session Log\n\n## Evening\n", encoding="utf-8")
    me = {"tool_name": "MultiEdit", "tool_input": {"file_path": NOTE, "edits": [
        {"old_string": "## Session Log", "new_string": f"## Session Log\n\n{TAGGED}"},
        {"old_string": "## Evening", "new_string": f"## Evening\n\n{UNTAGGED}"},
    ]}}
    check("MultiEdit whose net result adds an untagged entry -> DENY", run(me, home)[0] is True)
    note.write_text("# 2026-07-13\n\n## Session Log\n\n## Evening\n", encoding="utf-8")
    me_ok = {"tool_name": "MultiEdit", "tool_input": {"file_path": NOTE, "edits": [
        {"old_string": "## Session Log", "new_string": f"## Session Log\n\n{TAGGED}"},
        {"old_string": "## Evening", "new_string": f"## Evening\n\n[ops] **Second.** Body."},
    ]}}
    check("MultiEdit where every added entry is tagged -> allow", run(me_ok, home)[0] is False)

    # ---- path scoping ----
    print("path scoping")
    note.write_text("", encoding="utf-8")
    check("non-Daily-Note path untouched",
          run(w(UNTAGGED, path=str(home / "vault" / "Rules" / "foo.md")), home)[0] is False)
    check("'Not Daily Notes/' does NOT match (segment, not substring)",
          run(w(UNTAGGED, path="/vault/Not Daily Notes/2026-07-13.md"), home)[0] is False)
    check("Windows separators DO match",
          run(w(UNTAGGED, path=r"C:\vault\Daily Notes\2026-07-13.md"), home)[0] is True)
    check("non-.md file in Daily Notes ignored",
          run(w(UNTAGGED, path="/vault/Daily Notes/notes.txt"), home)[0] is False)

    # ---- disable env ----
    print("disable env")
    note.write_text("", encoding="utf-8")
    check("named in JOURNAL_DISABLED -> no-op",
          run(w(UNTAGGED), home, env={"JOURNAL_DISABLED": "enforce-journal-tags"})[0] is False)
    check("a DIFFERENT hook named in JOURNAL_DISABLED -> still denies",
          run(w(UNTAGGED), home, env={"JOURNAL_DISABLED": "some-other-hook"})[0] is True)
    check("legacy HARNESS_MANAGEMENT_DISABLED still honored -> no-op",
          run(w(UNTAGGED), home, env={"HARNESS_MANAGEMENT_DISABLED": "enforce-journal-tags"})[0] is False)
    check("a DIFFERENT hook named in legacy HARNESS_MANAGEMENT_DISABLED -> still denies",
          run(w(UNTAGGED), home, env={"HARNESS_MANAGEMENT_DISABLED": "some-other-hook"})[0] is True)
    # The OLD monolith name disables ONLY through the legacy var, so a tenant who
    # set it there before the rename stays disabled.
    check("legacy name enforce-daily-note-tags in legacy var -> no-op",
          run(w(UNTAGGED), home, env={"HARNESS_MANAGEMENT_DISABLED": "enforce-daily-note-tags"})[0] is False)
    check("legacy name in the NEW JOURNAL_DISABLED -> still denies (new var, new name only)",
          run(w(UNTAGGED), home, env={"JOURNAL_DISABLED": "enforce-daily-note-tags"})[0] is True)

    # ---- config hardening ----
    print("config hardening")
    write_cfg(home, {"enabled": True, "tags": ["call", "chore"]})
    check("custom tag list honored (allow)", run(w("[call] **Thing.** Body."), home)[0] is False)
    check("default tag rejected under a custom list", run(w("[decision] **Thing.** Body."), home)[0] is True)

    write_cfg(home, {"enabled": True, "tags": [None]})
    check("tags:[null] -> falls back to defaults, [decision] still allowed",
          run(w("[decision] **Thing.** Body."), home)[0] is False)
    write_cfg(home, {"enabled": True, "tags": [""]})
    check("tags:[''] -> falls back, bare '[] **Entry.**' is NOT categorized",
          run(w("[] **Thing.** Body."), home)[0] is True)

    write_cfg(home, {"enabled": True, "daily_notes_dirs": ["Journal"]})
    note2 = home / "vault" / "Journal"
    note2.mkdir(parents=True, exist_ok=True)
    (note2 / "x.md").write_text("", encoding="utf-8")
    check("custom dir honored", run(w(UNTAGGED, path=str(note2 / "x.md")), home)[0] is True)
    check("custom dir REPLACES the default (Daily Notes now silent)",
          run(w(UNTAGGED), home)[0] is False)

    # #2310 guard: config is disk-located via $JOURNAL_CONFIG_DIR, never derived
    # from $HOME. An enabled config under a spoofed $HOME must not deny when the
    # absolute override points at a dir that has none.
    spoof = home / "attacker-home"
    (spoof / CFG_DIR).mkdir(parents=True, exist_ok=True)
    (spoof / CFG_DIR / "journal-tags.json").write_text(
        json.dumps({"enabled": True}), encoding="utf-8")
    empty_cfg = home / "empty-cfg"; empty_cfg.mkdir(parents=True, exist_ok=True)
    check("enabled config under a spoofed $HOME is ignored; the override wins: allow",
          run(w(UNTAGGED), spoof, env={"JOURNAL_CONFIG_DIR": str(empty_cfg)})[0] is False)
    write_cfg(home, {"enabled": True})

    # ---- further edge cases ----
    print("fence handling (accepted limitation, see threat model)")
    write_cfg(home, {"enabled": True})
    note.write_text("", encoding="utf-8")
    # ACCEPTED BYPASS, named on purpose. A single linear pass means an UNCLOSED fence
    # runs to end-of-document, hiding entries below it. The alternative paired
    # fences by lookahead, which desynchronized against the HTML-comment scanner and was
    # QUADRATIC (15s on a 10k-line note, past the 5s timeout, stalling every write).
    # A bypass costs one missing tag; a hang or a false deny blocks real work. Trade taken.
    check("an UNCLOSED fence hides entries below it -> allow (accepted bypass)",
          run(w("```\n" + UNTAGGED), home)[0] is False)
    check("a PROPERLY closed fence exempts its contents -> allow",
          run(w("```\n" + UNTAGGED + "\n```\n"), home)[0] is False)
    check("an entry AFTER a properly closed fence is still gated -> DENY",
          run(w("```\ncode\n```\n\n" + UNTAGGED), home)[0] is True)

    print("self-review: unreplayable edits fail open, never guess")
    note.write_text("alpha\n", encoding="utf-8")
    check("empty old_string -> allow (semantics not derivable, do not guess)",
          run(ed(UNTAGGED, ""), home)[0] is False)
    note.write_text("dup\ndup\n", encoding="utf-8")
    check("non-unique old_string without replace_all -> allow (the tool itself errors)",
          run(ed(f"dup\n{UNTAGGED}", "dup"), home)[0] is False)
    note.write_text("dup\ndup\n", encoding="utf-8")
    me2 = {"tool_name": "Edit", "tool_input": {
        "file_path": NOTE, "old_string": "dup",
        "new_string": f"dup\n\n{UNTAGGED}", "replace_all": True}}
    check("non-unique old_string WITH replace_all -> replayed, and DENIES",
          run(me2, home)[0] is True)

    # ---- the multiset had a THIRD bug, so identity is now whole-line ----
    print("net-count cancellation")
    write_cfg(home, {"enabled": True})
    note.write_text("**Legacy.** old\n", encoding="utf-8")
    check("retro-tag one occurrence WHILE adding an untagged twin -> DENY (was a bypass)",
          run(ed("[ops] **Legacy.** old\n\n**Legacy.** NEW", "**Legacy.** old"), home)[0] is True)
    note.write_text("**A   B.** old\n", encoding="utf-8")
    check("title-normalization collision cannot launder either -> DENY",
          run(ed("[ops] **A   B.** old\n\n**A B.** NEW", "**A   B.** old"), home)[0] is True)

    print("markdown state machine")
    note.write_text("", encoding="utf-8")
    check("fence inside an HTML comment does not desync the scanners -> DENY (was a bypass)",
          run(w("<!--\n```\n-->\n```\n```\n\n**New.** body\n```\n"), home)[0] is True)
    check("HTML comment opened mid-line -> allow (was a false deny)",
          run(w("Template follows <!--\n**Example.** commented out\n-->\n"), home)[0] is False)
    check("invalid backtick info string (```foo`bar) is not a fence -> DENY (was a bypass)",
          run(w("```foo`bar\n\n**New.** body\n```\n"), home)[0] is True)
    check("table delimiter across a blank line is not a table -> allow (pipe rule)",
          run(w("**New.** body | value\n\n--- | ---\n"), home)[0] is False)

    print("pathological fence document cannot hang (was quadratic, 15s at 10k lines)")
    r, _ = run(w("```x\n" * 6000), home, timeout=5)
    check("6000 unpaired fence-like lines parse without hanging", r != "HANG")

    print("MultiEdit chaining a prior edit's output is refused")
    note.write_text("A\n", encoding="utf-8")
    me_bad = {"tool_name": "MultiEdit", "tool_input": {"file_path": NOTE, "edits": [
        {"old_string": "A", "new_string": "A\nX"},
        {"old_string": "X", "new_string": "**New.** body"},
    ]}}
    check("old_string from a prior edit's new_string -> allow (the tool rejects this call)",
          run(me_bad, home)[0] is False)

    print("column-0 bold sub-points inside one entry")
    note.write_text("", encoding="utf-8")
    check("sub-points under a tagged entry are not separate entries -> allow (was a false deny)",
          run(w("[ops] **Ran the migration.** Steps:\n"
                "**Step 1.** backed up\n"
                "**Step 2.** ran the script\n"), home)[0] is False)
    check("a blank-separated entry after a tagged one IS gated -> DENY",
          run(w(f"[ops] **First.** Body.\n\n{UNTAGGED}\n"), home)[0] is True)
    # ACCEPTED BYPASS, named: an entry must start a block (follow a blank line, a heading,
    # or the top of the file). One jammed directly under prose is not gated. Real notes are
    # blank-separated, so the gate fires on the normal path; this trades a narrow bypass for
    # killing the sub-point false deny above.
    check("entry jammed directly under prose is not gated (accepted bypass)",
          run(w("Some prose line.\n" + UNTAGGED), home)[0] is False)
    # A BLANK-SEPARATED bold sub-point is positionally identical to an entry;
    # no signal distinguishes them, so it is gated. Documented in the USER-GUIDE as the
    # limit of the block-start rule rather than papered over.
    check("blank-separated bold sub-point IS an entry -> DENY (grammar, documented)",
          run(w("[ops] **Investigated the outage.** Findings:\n\n"
                "**Root cause.** A stale cache entry.\n"), home)[0] is True)

    print("CommonMark delimiter validity")
    write_cfg(home, {"enabled": True})
    note.write_text("", encoding="utf-8")
    check("'** not bold**' cannot OPEN emphasis, so it is not an entry -> allow",
          run(w("** not bold** ordinary prose"), home)[0] is False)
    check("'**title **' cannot CLOSE emphasis, so it is not an entry -> allow",
          run(w("**title ** ordinary prose"), home)[0] is False)
    check("valid bold is still an entry -> DENY", run(w("**Title.** prose"), home)[0] is True)

    print("CommonMark separators are space/tab, not any unicode space")
    check("NBSP after '#' is NOT a heading, so the line below is not a block start -> allow",
          run(w("#\u00a0launch\n**Owner.** Sam will coordinate.\n"), home)[0] is False)
    check("NBSP-suffixed fence line does NOT close the fence -> allow",
          run(w("```text\ncode\n```\u00a0\n**Literal.** still inside the fence\n"),
              home)[0] is False)

    print("inline CODE SPAN is literal, not raw HTML")
    check("documenting `<!--` in inline code does not swallow the note -> DENY",
          run(w("Syntax reminder: type `<!--` to open a comment.\n\n**Shipped.** body\n"),
              home)[0] is True)
    check("documenting `<pre>` in inline code does not swallow the note -> DENY",
          run(w("Syntax reminder: type `<pre>` to open a pre block.\n\n**Shipped.** body\n"),
              home)[0] is True)

    print("duplicate-occurrence contract")
    note.write_text("**Repeated.** Same body.\n", encoding="utf-8")
    check("adding a SECOND copy of an existing untagged entry -> DENY (occurrence-counted)",
          run(w("**Repeated.** Same body.\n\n**Repeated.** Same body.\n"), home)[0] is True)

    print("whitespace-only edit is not a change")
    note.write_text("**Legacy.** body\n", encoding="utf-8")
    check("whitespace-only edit to an untagged entry -> allow (normalized identity)",
          run(ed("**Legacy.**  body", "**Legacy.** body"), home)[0] is False)

    print("an outsized DOCUMENT cannot hang the hook")
    note.write_text("x" * 3_000_000, encoding="utf-8")
    r, _ = run(ed("**New.** untagged", "xxxx"), home, timeout=5)
    check("3MB existing note -> refused, not parsed; no hang", r is False)
    # The ceiling must cover AFTER, not just BEFORE. A Write to an EMPTY note
    # carries the whole document in its payload, so `before` passes trivially while the
    # thing actually parsed is arbitrarily large.
    note.write_text("", encoding="utf-8")
    big_write = ("ordinary prose line\n" * 300_000) + "\n**New.** body\n"
    r, _ = run(w(big_write), home, timeout=5)
    check("6MB Write to an EMPTY note -> refused, not parsed; no hang", r is False)
    note.write_text("", encoding="utf-8")

    print("inline markers are prose, not block boundaries")
    write_cfg(home, {"enabled": True})
    note.write_text("", encoding="utf-8")
    check("inline <!-- --> in an entry does not fake a boundary -> allow (was a false deny)",
          run(w("[ops] **Investigated the outage.** Findings: <!-- source note -->\n"
                "**Root cause.** A stale cache entry.\n"), home)[0] is False)
    check("inline <pre> in an entry does not fake a boundary -> allow (was a false deny)",
          run(w("[ops] **Documented HTML.** Use <pre>...</pre> for literal output.\n"
                "**Example.** This is a sub-point.\n"), home)[0] is False)
    check("an entry MENTIONING <!-- --> is still an entry -> DENY (was a bypass)",
          run(w("**Documented syntax.** HTML comments use <!-- text --> in templates.\n"),
              home)[0] is True)
    check("an entry MENTIONING <pre> is still an entry -> DENY (was a bypass)",
          run(w("**Documented syntax.** HTML literal output uses <pre>text</pre>.\n"),
              home)[0] is True)

    print("context change must not flag an UNTOUCHED line")
    note.write_text("Session notes\n**Owner.** Sam will coordinate.\n", encoding="utf-8")
    check("promoting a line to a heading does not flag the untouched entry below -> allow",
          run(ed("## Session notes", "Session notes"), home)[0] is False)
    note.write_text("```text\nsample\n**Legacy.** Existing line.\n", encoding="utf-8")
    check("closing a previously-unclosed fence does not flag the untouched line -> allow",
          run(ed("sample\n```\n", "sample\n"), home)[0] is False)

    print("ATX heading recognition")
    note.write_text("", encoding="utf-8")
    check("'#launch' is prose, not a heading, so the next line is not an entry -> allow",
          run(w("#launch\n**Owner.** Sam will coordinate.\n"), home)[0] is False)
    check("a REAL heading still starts a block -> DENY",
          run(w("# 2026-07-13\n**Owner.** Sam will coordinate.\n"), home)[0] is True)

    print("an outsized MultiEdit cannot hang the hook")
    note.write_text("".join(f"x{i:05d}\n" for i in range(16000)), encoding="utf-8")
    big = {"tool_name": "MultiEdit", "tool_input": {"file_path": NOTE, "edits": [
        {"old_string": f"x{i:05d}", "new_string": f"y{i:05d}"} for i in range(16000)]}}
    r, _ = run(big, home, timeout=5)
    check("16000-edit MultiEdit -> refused, not replayed; no hang", r is False)

    print("block boundaries")
    note.write_text("", encoding="utf-8")
    check("entry right after a CLOSED fence is gated -> DENY (was an accidental bypass)",
          run(w("```text\noutput\n```\n**Recorded the result.** Successful.\n"), home)[0] is True)
    check("entry right after a single-line HTML comment is gated -> DENY",
          run(w("<!-- generated checkpoint -->\n**Recorded the result.** Successful.\n"),
              home)[0] is True)
    check("entry right after a closed <pre> is gated -> DENY",
          run(w("<pre>\nout\n</pre>\n**Recorded the result.** Successful.\n"), home)[0] is True)

    print("raw <pre> is literal, not markdown")
    check("bold inside a <pre> block -> allow (was a false deny)",
          run(w("<pre>\n\n**Expected output.** literal text, not Markdown\n\n</pre>\n"),
              home)[0] is False)

    print("the deny message must be true and actionable")
    note.write_text("**Legacy entry.** Original body.\n", encoding="utf-8")
    d, reason = run(ed("Corrected body.", "Original body."), home)
    check("editing a legacy untagged entry -> DENY", d is True)
    check("deny reason does NOT claim existing entries are never flagged",
          "already in the file are never flagged" not in reason)
    check("deny reason says ADDS or EDITS", "ADDS or EDITS" in reason)

    write_cfg(home, {"enabled": True, "tags": ["call", "chore"]})
    note.write_text("", encoding="utf-8")
    d, reason = run(w(UNTAGGED), home)
    check("custom tags: deny reason lists them", d is True and "[call]" in reason)
    check("custom tags: the EXAMPLE uses a configured tag, not [decision]",
          "[call] **" in reason and "[decision]" not in reason)
    write_cfg(home, {"enabled": True})

    # ---- order-dependence broke BOTH ways ----
    print("untagged-only comparison is order-independent")
    write_cfg(home, {"enabled": True})

    # Inserting a TAGGED entry above a legacy untagged twin false-denied.
    note.write_text("**Legacy.** old\n", encoding="utf-8")
    check("tagged dup inserted ABOVE a legacy untagged twin -> allow (was a false deny)",
          run(ed("[ops] **Legacy.** new tagged occurrence\n**Legacy.** old",
                 "**Legacy.** old"), home)[0] is False)

    # The mirror image was a bypass.
    note.write_text("[ops] **Sync.** old tagged occurrence\n", encoding="utf-8")
    check("untagged dup inserted ABOVE a tagged twin -> DENY (was a bypass)",
          run(ed("**Sync.** new untagged occurrence\n[ops] **Sync.** old tagged occurrence",
                 "[ops] **Sync.** old tagged occurrence"), home)[0] is True)

    print("fence delimiter identity and length")
    note.write_text("", encoding="utf-8")
    check("``` inside a ```` block does NOT close it -> allow",
          run(w("````markdown\n```\n**Example.** this is code\n````\n"), home)[0] is False)
    check("~~~ does not close a ``` block -> allow",
          run(w("```\n~~~\n**Example.** this is code\n```\n"), home)[0] is False)
    check("4-space indented pseudo-fence is NOT a fence -> DENY the entry below it",
          run(w("    ```\n\n**Shipped.** untagged real entry\n"), home)[0] is True)

    print("raw HTML and table context")
    check("bold inside an HTML comment -> allow",
          run(w("<!--\n**Example.** commented-out template\n-->\n"), home)[0] is False)
    # Conservative by design: ANY pipe-bearing line is left alone. Distinguishing a table
    # row from an entry needs lookahead/lookbehind this parser got wrong twice, so a narrow
    # bypass is traded for the removal of a whole class of false deny. See the threat model.
    check("table HEADER row -> allow",
          run(w("**Name** | Value\n--- | ---\nfoo | bar\n"), home)[0] is False)
    check("table BODY row with a bold first cell -> allow",
          run(w("Name | Value\n--- | ---\nAlice | one\n**Bob** | two\n"), home)[0] is False)
    check("pipe-bearing line is never gated (accepted bypass, documented)",
          run(w("**Shipped.** | released today\n"), home)[0] is False)

    print("path resolution")
    rules = home / "vault" / "Rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "foo.md").write_text("", encoding="utf-8")
    check("'Daily Notes/../Rules/foo.md' resolves out of scope -> allow (was a false deny)",
          run(w("**Rule.** Ordinary untagged rule.",
                path=str(vault / ".." / "Rules" / "foo.md")), home)[0] is False)
    link = home / "vault" / "Today"
    if not link.exists():
        link.symlink_to(vault)
    (vault / "linked.md").write_text("", encoding="utf-8")
    check("a symlink INTO Daily Notes still gates -> DENY (was a bypass)",
          run(w(UNTAGGED, path=str(link / "linked.md")), home)[0] is True)

    print("config strings are stripped on store")
    write_cfg(home, {"enabled": True, "tags": [" ops "]})
    note.write_text("", encoding="utf-8")
    check("tags:[' ops '] -> '[ops]' is accepted (was a false deny)",
          run(w("[ops] **Routine work.** completed"), home)[0] is False)

    print("a FIFO note cannot hang the hook")
    write_cfg(home, {"enabled": True})
    fifo = vault / "fifo.md"
    try:
        os.mkfifo(fifo)
        r, _ = run(w(UNTAGGED, path=str(fifo)), home, timeout=5)
        check("FIFO target -> allow, no hang (blocking read refused before open)", r is False)
    except (AttributeError, OSError):
        print("  SKIP  FIFO test (mkfifo unavailable on this platform)")

    # ---- ReDoS is structurally impossible now ----
    print("no user regex -> no ReDoS")
    write_cfg(home, {"enabled": True, "daily_notes_dirs": ["^(a+)+$"]})
    r, _ = run(w("plain", path="/" + "a" * 40 + "!/x.md"), home, timeout=5)
    check("pathological dir string cannot hang the hook", r is False)

    # ---- fail open ----
    print("fail open")
    write_cfg(home, {"enabled": True})
    for bad in ('{"tool_name":"Write"}', '[]', 'null', 'not json', '{"tool_name":"Write","tool_input":{"file_path":123}}'):
        r, _ = run(bad, home)
        check(f"malformed payload {bad[:22]!r} -> no deny", r is False)

    write_cfg(home, {"enabled": True, "tags": "not-a-list"})
    check("non-list tags -> defaults, still gates", run(w(UNTAGGED), home)[0] is True)

    (Path(home) / CFG_DIR / "journal-tags.json").write_text("{ not json", encoding="utf-8")
    check("malformed config -> no-op (opt-in gate fails open)", run(w(UNTAGGED), home)[0] is False)

    # ---- in-process: production fallback path + unresolvable-home visibility ----
    # run() sets an absolute $JOURNAL_CONFIG_DIR on every subprocess case, so the
    # fallback branch (no override, config under the trusted home's
    # .config/journal) that every real install uses is otherwise untested. Load
    # the hook in-process and exercise load_config directly.
    print("in-process config resolution")
    import importlib.util
    spec = importlib.util.spec_from_file_location("et", HOOK)
    et = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = et  # importlib hygiene; not load-bearing here (no dataclasses)
    spec.loader.exec_module(et)
    import agent_home

    cfg_home = Path(tmp) / "cfg-home"
    (cfg_home / CFG_DIR).mkdir(parents=True, exist_ok=True)
    (cfg_home / CFG_DIR / "journal-tags.json").write_text(
        json.dumps({"enabled": True, "tags": ["ops"]}), encoding="utf-8")
    _saved_home = os.environ.get("HOME")
    _saved_jcd = os.environ.pop("JOURNAL_CONFIG_DIR", None)
    os.environ["HOME"] = str(Path(tmp) / "no-config-home")
    agent_home._CACHED = str(cfg_home)
    try:
        cfg = et.load_config()
        check("load_config reads config from the trusted-home fallback (no override), not $HOME",
              isinstance(cfg, dict) and cfg.get("enabled") is True)
    finally:
        agent_home._CACHED = agent_home._UNSET
        if _saved_jcd is not None:
            os.environ["JOURNAL_CONFIG_DIR"] = _saved_jcd
        if _saved_home is not None:
            os.environ["HOME"] = _saved_home

    if os.name != "nt":
        # Visibility (#2310): an unresolvable home must be RECORDED, not silently
        # read as "nothing configured". Drive the real trusted_home failure path
        # (pwd.getpwuid raising) with no override, and assert load_config no-ops
        # AND logs. Fails if load_config drops the log= it passes.
        agent_home._CACHED = agent_home._UNSET
        _orig_pw = agent_home.pwd.getpwuid
        _saved_jcd2 = os.environ.pop("JOURNAL_CONFIG_DIR", None)
        logged = []
        _orig_log = et._log
        agent_home.pwd.getpwuid = lambda uid: (_ for _ in ()).throw(KeyError("no pwent"))
        et._log = lambda m: logged.append(m)
        try:
            r = et.load_config()
            check("unresolvable home: load_config no-ops AND records it (not silent)",
                  r == {} and logged != [])
        finally:
            et._log = _orig_log
            agent_home.pwd.getpwuid = _orig_pw
            agent_home._CACHED = agent_home._UNSET
            if _saved_jcd2 is not None:
                os.environ["JOURNAL_CONFIG_DIR"] = _saved_jcd2

print()
if failures:
    print(f"FAILED ({len(failures)}): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
