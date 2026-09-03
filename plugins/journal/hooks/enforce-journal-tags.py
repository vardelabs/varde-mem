#!/usr/bin/env python3
"""Write-time tag gate for Daily Notes entries.

A PreToolUse hook (wired in hooks.json on Edit|Write|MultiEdit) that DENIES a write
which ADDS an untagged entry to a Daily Note, so the agent re-issues it tagged in the
same turn. An entry is a bold lead-in at column 0 followed by prose:

    [decision] **Retired the charter model.** Kai's call; issues are the hub now.
    [fix] **Corrected the stale PreCompact target.** It pointed at a dead script.
    [architecture] **Folded handoff-protocol into harness-management.** One domain.
    [ops] **Ran the weekly board reconcile.** No surprises.

Why a HARD BLOCK and not an advisory: daily-note tagging is a habit, not a
judgment, and it demonstrably does not form on its own: over the month preceding
this hook (2026-06-14
to 2026-07-12), 20 consecutive Daily Notes carried ZERO tags despite the convention
being proposed ten weeks earlier (corvus-vault#24). An advisory ignored 100% of the
time is a no-op with overhead. The block is what makes the tag exist; the AGENT still
supplies the judgment of WHICH tag, because that cannot be inferred from a diff.

DOCUMENT DIFF, NOT FRAGMENT MATCH (the load-bearing design decision).
---------------------------------------------------------------------
The obvious implementation, classifying lines in `content` / `new_string`, is WRONG,
and every false-deny in review came from it. A tool-input fragment carries no markdown
context: you cannot tell whether a `**bold**` line is an entry, a section header, a
table cell, or a line inside a fenced code block; and a `Write` that OVERWRITES an
existing note is indistinguishable from one that authors a fresh one, so every legacy
untagged entry in the file gets flagged and the write is denied.

So this hook does not classify fragments. It reconstructs BOTH documents:

    before = the file currently on disk (empty string when it does not exist)
    after  = before with this tool call applied (Write replaces; Edit/MultiEdit patch)

parses `after` into entries WITH markdown context, and denies an untagged one only if its
text is not already present in `before`. Precisely: each occurrence of a line in `before`
exempts one identical occurrence in `after`, so adding a SECOND copy of an existing
untagged entry is still gated. (Stated exactly because "present anywhere" is the loose
version and would imply a duplicate gets in free. It does not.)

Identity is NORMALIZED text (whitespace collapsed), not bytes, so a whitespace-only edit
to an existing entry is not treated as a change.

Two deliberate choices in that sentence, both bought with bugs:

  - Identity is the WHOLE normalized line, not the bold title. Identifying entries by
    title and counting occurrences produced three separate bugs: order-dependence in both
    directions (a tagged line could consume an untagged line's slot, and vice versa), and
    then a net-count cancellation, where retro-tagging one occurrence while adding an
    untagged twin netted to zero and the new entry passed. The multiset was the problem.
  - The exemption is keyed on `before`'s LINES, not on `before`'s parsed ENTRIES. Whether
    a line parses as an entry depends on parser CONTEXT, so a line's classification can
    change without the line changing:

        Session notes                     ->   ## Session notes      (the edit)
        **Owner.** Kai will coordinate.        **Owner.** Kai will coordinate.

    The `**Owner.**` line is byte-identical and untouched, but the new heading makes it
    start a block, so it becomes an entry. Keying on entries would deny it; keying on the
    text present in `before` cannot, which is what makes the guarantee below literally
    true rather than merely usually true.

Consequences that fall out of this for free, rather than needing a special case each:

  - Overwriting a note full of legacy untagged entries is fine: their text is in `before`,
    so they are not "added". Retro-tagging the back catalog is explicitly not this hook's
    job, and enabling the hook never demands it.
  - An untagged entry cannot be laundered by planting matching bold text in `old_string`:
    `before` is read from disk, never from tool input.
  - A bold line inside a fenced code block is not an entry, because the fence is tracked
    across the real document.

The one thing this DOES ask, stated plainly because an earlier version of this docstring
promised otherwise: EDITING an existing untagged entry (its body or its lead-in) produces
a line that is not in `before`, so the gate asks you to tag it. Entries you do not touch
are never flagged; only the one you are already editing is.

THREAT MODEL (this is what licenses the trades below, so read it before "fixing" them).
------------------------------------------------------------------------------------
The adversary is the agent's own forgetfulness, NOT an attacker. Nobody is trying to
smuggle an untagged entry past this hook; the hook exists so that a busy agent does not
FORGET to tag one. That makes the two failure modes wildly asymmetric:

  - A FALSE DENY blocks real work. It is loud, it is expensive, and after two rounds of
    review it is the failure that kept blocking release. It is the thing to avoid.
  - A BYPASS just means one entry goes untagged. The cost is one missing tag in a grep.

Named, accepted bypasses (all consequences of biasing toward allow): any line containing a
`|` is never gated; an unclosed fence, an unterminated `<!--`, or an unterminated `<pre>`
runs to end-of-document and hides entries below it; an entry jammed directly under prose,
rather than starting a block, is not gated.

Known limitations, judged non-blocking in review and recorded rather than quietly dropped:

  - A Setext heading underline (`**Release status.** summary` followed by `---`) makes the
    bold line a heading, not an entry, but detecting it needs lookahead. It is gated.
  - Raw HTML blocks other than `<!--` and `<pre>` (e.g. `<script>`) are not tracked, so a
    blank-separated bold line inside one is classified as an entry.
  - The inline-code scrubber handles single-backtick spans only, so a marker inside a
    multi-backtick span (``` ``<!--`` ```) can still open comment state.

Each needs deliberately unusual markdown in a daily note. Fixing them means more lookahead
and more parser state, which is exactly what produced the false denies this design exists
to avoid. The trade is deliberate; revisit only if one is hit in practice.

So this parser is CONSERVATIVE ON PURPOSE: when it is not confident a line is an entry,
it says nothing. Concretely, any line containing a `|` is left alone rather than gated,
because telling a table row from an entry needs lookahead and lookbehind that this parser
got wrong twice. That accepts a narrow bypass to delete an entire class of false deny.
Do not "tighten" these without re-reading this paragraph.

Markdown context is a SINGLE LINEAR PASS (comment state, then fence state, then entry).
An earlier cut ran two independent scanners and paired fences by lookahead; they
desynchronized (a ``` inside an HTML comment paired with a real fence after it, hiding
everything between), and the pairing search was QUADRATIC: a 10k-line note of unpaired
fence-like lines took 15s, blowing the hook's 5s timeout and stalling every subsequent
write to that file. One pass, one state machine, no lookahead.

NO USER-SUPPLIED REGEX. An earlier cut let a tenant override the path matcher with an
arbitrary regex, which is a ReDoS foot-gun: `^(a+)+$` against a long non-matching path
backtracks for longer than the hook's 5s timeout, and Python cannot interrupt a regex
mid-match, so the hook hangs and stalls EVERY write. Scoping is therefore a directory
NAME matched as a path segment (separator-normalized, so Windows paths work), which is
not a regex and cannot backtrack.

OPT-IN PER TENANT via a config file. With no config (or `enabled` not true) the hook
no-ops, so it never fires for a tenant or client who keeps no Daily Notes. The config
is disk-located, never derived from a spoofable $HOME: an absolute $JOURNAL_CONFIG_DIR
overrides, else it sits under the trusted (passwd/token) home:

    <trusted home>/.config/journal/journal-tags.json
    {
      "enabled": true,
      "tags": ["decision", "fix", "architecture", "ops"],
      "daily_notes_dirs": ["Daily Notes"]
    }

Disable entirely (even with a config present) by naming `enforce-journal-tags` in
$JOURNAL_DISABLED. The legacy $HARNESS_MANAGEMENT_DISABLED, under which this hook
shipped in the monolith as `enforce-daily-note-tags`, still disables it under EITHER
name, so a tenant who disabled the old name stays disabled after the rename.

FAIL OPEN, ALWAYS. A PreToolUse deny blocks real work, so every uncertainty resolves to
"allow": a malformed payload, an unusable config, an unreadable file, or any unexpected
exception degrades to a silent no-op, never a spurious deny.

Hook input: JSON on stdin (PreToolUse payload).
Hook output: JSON on stdout with a deny decision when the write ADDS or EDITS an
untagged entry; otherwise silence. Always exit 0.
"""

import json
import os
import re
import stat
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_home import env_override_or_home  # noqa: E402  (sibling module)

HOOK_NAME = "enforce-journal-tags"
# The name this hook shipped under in the monolith. Honored ONLY in the legacy
# disable var, so a tenant who disabled it there stays disabled after the rename.
LEGACY_HOOK_NAME = "enforce-daily-note-tags"
CONFIG_FILE = "journal-tags.json"
# Config is disk-located, never derived from a spoofable $HOME: an absolute
# $JOURNAL_CONFIG_DIR overrides, else <trusted home>/.config/journal. Resolved
# LAZILY in load_config, not at import: this hook fires on every candidate write
# and the home lookup is not free (see agent_home.config_path, #2310).
CONFIG_DIR_ENV = "JOURNAL_CONFIG_DIR"

DEFAULT_TAGS = ("decision", "fix", "architecture", "ops")
DEFAULT_DIRS = ("Daily Notes",)

# Above this, a MultiEdit is not simulated at all (fail open). Replay cost grows with the
# edit count times document size, and a large enough payload would outrun the hook's 5s
# timeout and stall the write. A real note edit is a handful of edits, never hundreds.
MAX_REPLAY_EDITS = 100

# And a document-size ceiling, because the edit cap alone does not bound replay cost:
# every accepted edit still scans the whole evolving document, and 100 edits over a 50MiB
# note measured 8.4s against the hook's 5s timeout. A daily note is tens of KB; anything
# past this is not a note we need to gate, so fail open rather than risk stalling a write.
MAX_DOC_BYTES = 2_000_000

# An entry candidate: column-0 bold, optionally preceded by a `[tag]`, with a closing
# `**` and trailing content. The optional tag is part of the pattern on purpose: an
# entry carrying a WRONG tag (`[bogus] **X.** body`, or `[bogus]**X.** body` with no
# space) must still register AS an entry, or it slips the gate unseen. `\s*` (not `\s+`)
# after the bracket is what catches the no-space form.
ENTRY_RE = re.compile(
    r"^(?:\[(?P<tag>[^\]]*)\]\s*)?\*\*(?P<title>\S(?:.*?\S)?)\*\*(?P<rest>.*)$")

# A fence opener/closer: at most 3 leading spaces (4+ is an indented code line, NOT a
# fence), then a run of >=3 backticks or tildes. Delimiter char and length both matter:
# a ``` line does not close a ```` block, and ~~~ does not close a ``` block.
FENCE_RE = re.compile(r"^ {0,3}(?P<char>`{3,}|~{3,})(?P<info>.*)$")

HTML_COMMENT_OPEN = "<!--"
HTML_COMMENT_CLOSE = "-->"

# BLOCK-LEVEL raw HTML: the marker must OPEN the line (CommonMark allows up to 3 leading
# spaces). An `<!--` or `<pre>` appearing INLINE in prose is not a block boundary, and
# treating it as one is wrong in both directions: it false-denies the line after an entry
# whose text happens to mention a comment, and it makes the entry itself vanish from
# classification (a bypass), since ordinary technical notes say things like
# `**Documented syntax.** HTML comments use <!-- text --> in templates.`
BLOCK_COMMENT_RE = re.compile(r"^ {0,3}<!--")
BLOCK_PRE_RE = re.compile(r"^ {0,3}<pre\b", re.I)
PRE_OPEN_INLINE_RE = re.compile(r"<pre\b", re.I)
PRE_CLOSE_RE = re.compile(r"</pre\s*>", re.I)

# An ATX heading: up to 3 leading spaces, 1 to 6 `#`, then whitespace or end-of-line.
# A bare `startswith("#")` treats `#launch` (ordinary prose, or a tag) as a heading, which
# makes the following line a false block start and denies it.
HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")

# An inline code span is literal text, so `<!--` inside backticks is NOT raw HTML.
# Without this, a note that merely DOCUMENTS the syntax opens comment state and hides
# every entry below it. That is an ordinary accidental path, not a crafted evasion.
CODE_SPAN_RE = re.compile(r"`[^`]*`")


def _disabled() -> bool:
    """True when this hook is disabled by name. $JOURNAL_DISABLED takes the
    current name only; the legacy $HARNESS_MANAGEMENT_DISABLED it shipped under
    in the monolith honors EITHER name, so a tenant who disabled the old
    `enforce-daily-note-tags` there stays disabled after the rename."""
    for var, names in (
        ("JOURNAL_DISABLED", (HOOK_NAME,)),
        ("HARNESS_MANAGEMENT_DISABLED", (HOOK_NAME, LEGACY_HOOK_NAME)),
    ):
        raw = os.environ.get(var, "")
        present = {p.strip() for p in raw.split(",") if p.strip()}
        if present.intersection(names):
            return True
    return False


def _log(msg: str) -> None:
    """Stderr, because this gate keeps no log file. It exists so an unresolvable
    home is VISIBLE: "could not determine where to look" and "nothing is
    configured" are different states, and silently reporting the first as the
    second is the conflation agent_home was written to end (#2310)."""
    try:
        sys.stderr.write("[" + HOOK_NAME + "] " + msg + "\n")
    except Exception:
        pass


def load_config() -> dict:
    """Per-tenant opt-in config. PURE READ of config: never writes or mkdirs.

    It DOES log once (to stderr, via _log) when the home cannot be resolved, so
    that state is visible rather than read as "nothing configured" (#2310); that
    is the whole reason agent_home takes a `log`.

    Returns the usable opt-in dict, or {} when there is no usable config (absent,
    malformed, or `enabled` not true), in which case the hook no-ops.
    """
    base = env_override_or_home(CONFIG_DIR_ENV, ".config", "journal", log=_log)
    if base is None:
        return {}
    raw = read_regular(Path(base) / CONFIG_FILE)  # a FIFO config would otherwise block forever
    if raw is None:
        return {}
    try:
        cfg = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return {}
    return cfg


def clean_str_list(value, default: tuple) -> list:
    """Keep only non-empty strings. A config of `[null]` or `[""]` would otherwise
    stringify to a `[None]` tag or accept `[] **Entry.**` as categorized, so a
    semantically empty list falls back to the default rather than creating either a
    systematic false-deny or a silent bypass."""
    if not isinstance(value, list):
        return list(default)
    # STRIP on store, not just on test. Validating `v.strip()` while appending the raw
    # `v` means a config of `[" ops "]` accepts nothing: the tag parsed from the note is
    # `ops`, which is not `" ops "`, so every entry false-denies.
    out = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return out or list(default)


def in_daily_notes(file_path: str, dirs) -> bool:
    """True when `file_path` is a markdown file inside one of the named directories.

    A path-SEGMENT match, not a substring one: `/vault/Not Daily Notes/x.md` must not
    match `Daily Notes`. The path is RESOLVED first (`..` collapsed, symlinks followed),
    because a purely lexical check is wrong in both directions:

      - false deny: `/vault/Daily Notes/../Rules/foo.md` really targets `Rules/`, but a
        lexical scan sees a `Daily Notes` segment and gates an unrelated file.
      - bypass: if `/vault/Today` symlinks to `/vault/Daily Notes`, then writing
        `/vault/Today/x.md` edits a Daily Note while never showing the configured
        segment.

    Resolution is lexical-plus-symlink only; it never reads the file, so it cannot block.
    """
    if not file_path.lower().endswith(".md"):
        return False
    try:
        resolved = os.path.realpath(file_path)
    except (OSError, ValueError):
        resolved = os.path.normpath(file_path)
    parts = {p for p in resolved.replace("\\", "/").split("/") if p}
    return any(d in parts for d in dirs)


def read_regular(path: Path):
    """Read a REGULAR file, or return None.

    Guards the blocking-read hole: `open()` on a FIFO blocks forever when no writer is
    connected, and the top-level `except Exception` cannot interrupt a blocking syscall,
    so the hook would hang until its 5s timeout and stall EVERY write. Anything that is
    not a regular file is therefore refused before it is opened, rather than caught after.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def entries(text: str) -> list:
    """Every entry in a document, in order, as `(tag, line)` with the line normalized.

    An entry is: a line that STARTS A BLOCK (follows a blank line, a heading, or the top
    of the file), at column 0, leading with `**bold**`, optionally prefixed by a `[tag]`,
    and carrying prose after the bold.

    ONE LINEAR PASS: comment state, then fence state, then the entry test. Two independent
    scanners desynchronized (a fence marker inside an HTML comment paired with a real fence
    after it, hiding everything between), and pairing fences by lookahead was quadratic.

    NOT entries, each because calling one an entry is a false deny:
      - anything inside a fenced code block, honoring CommonMark delimiter char and length
        (a ``` line does not close a ```` block) and the max-3-space indent rule
      - anything inside an HTML comment, whose opener may appear mid-line
      - a bold-only line (`**Agenda**`), which is a section header
      - ANY line containing a `|` (see the threat model: table rows take this shape, and
        telling one from an entry needs lookahead this parser got wrong twice)
      - a column-0 bold SUB-POINT inside an entry, which is what the start-a-block rule
        exists to exclude

    Accepted bypasses, all consequences of biasing toward allow: an unclosed fence or an
    unterminated `<!--` (including one inside an entry's own text) runs to end-of-document
    and hides entries below it; an entry jammed directly under prose is not gated.
    """
    found = []
    fence = None       # (delimiter char, length) of the open fence, or None
    in_comment = False
    in_pre = False
    # An entry must START A BLOCK. True at top-of-document, after a blank line, after a
    # heading, and after any block CLOSES (a fence, an HTML comment, a <pre>). Tracking a
    # real block boundary rather than "was the previous raw line blank" is what stops an
    # entry immediately following a closed fence from slipping the gate.
    block_start = True

    for raw in (text or "").splitlines():
        # Raw-HTML and comment state win over everything, including fences: a fence marker
        # INSIDE a comment is comment text, and letting the fence scanner see it
        # desynchronizes the two and silently hides real content below.
        if in_comment:
            if HTML_COMMENT_CLOSE in raw:
                in_comment = False
                block_start = True
            continue

        if in_pre:
            if PRE_CLOSE_RE.search(raw):
                in_pre = False
                block_start = True
            continue

        if fence is not None:
            m = FENCE_RE.match(raw)
            if m:
                d = m.group("char")
                # a closer matches the opener's char, is at least as long, and carries
                # no info string
                if d[0] == fence[0] and len(d) >= fence[1] and not m.group("info").strip(" \t"):
                    fence = None
                    block_start = True
            continue

        # BLOCK-level raw HTML only: the marker must open the line. An inline `<!--` or
        # `<pre>` inside an entry's prose is ordinary text and is handled with the content
        # lines below, so it neither hides the entry nor fakes a block boundary after it.
        if BLOCK_COMMENT_RE.match(raw):
            if HTML_COMMENT_CLOSE not in raw.split(HTML_COMMENT_OPEN, 1)[1]:
                in_comment = True
                block_start = False
            else:
                block_start = True  # a standalone `<!-- x -->` line is its own block
            continue

        if BLOCK_PRE_RE.match(raw):
            if not PRE_CLOSE_RE.search(raw):
                in_pre = True
                block_start = False
            else:
                block_start = True
            continue

        m = FENCE_RE.match(raw)
        if m:
            d = m.group("char")
            info = m.group("info")
            # A backtick fence's info string may not itself contain a backtick, so
            # ```foo`bar is not a valid opener.
            if not (d[0] == "`" and "`" in info):
                fence = (d[0], len(d))
                block_start = False
                continue
            # not a valid opener: fall through and treat it as ordinary content

        if not raw.strip():
            block_start = True
            continue

        was_block_start = block_start
        # A heading opens the next block; any other content line continues this one.
        block_start = bool(HEADING_RE.match(raw))

        # An INLINE, unterminated `<!--` or `<pre>` really does open a block in markdown,
        # even though the line it sits on is still ordinary content. Classify this line
        # normally (below), but remember that the following lines are inside it. Code spans
        # are stripped first: a marker inside backticks is literal text being documented,
        # not raw HTML.
        scrubbed = CODE_SPAN_RE.sub("", raw)
        tail = scrubbed.split(HTML_COMMENT_OPEN, 1)
        if len(tail) > 1 and HTML_COMMENT_CLOSE not in tail[1]:
            in_comment = True
        elif PRE_OPEN_INLINE_RE.search(scrubbed) and not PRE_CLOSE_RE.search(scrubbed):
            in_pre = True

        # This is what separates a real entry from a column-0 bold SUB-POINT inside one:
        #
        #     [ops] **Ran the migration.** Steps:
        #     **Step 1.** backed up          <- a sub-point, NOT a second entry
        #     **Step 2.** ran the script     <- likewise
        #
        # Without it, that single legitimate write is denied twice over, for two "untagged
        # entries" the author never meant to write.
        if not was_block_start:
            continue

        em = ENTRY_RE.match(raw)
        if not em:
            continue
        rest = em.group("rest").strip()
        if not rest:
            continue  # bold-only line: a section header, not an entry
        # Any line carrying a pipe is left alone. Table rows (header AND body) take this
        # shape, and distinguishing a table row from an entry needs lookahead / lookbehind
        # that this parser got wrong twice. Refusing to gate ANY pipe-bearing line trades
        # a narrow bypass (`**Shipped.** | released today` goes untagged) for the removal
        # of a whole class of false deny. Under this hook's threat model that is the right
        # trade: see the module docstring.
        if "|" in raw:
            continue
        tag = em.group("tag")
        found.append((tag.strip() if tag is not None else None, " ".join(raw.split())))
    return found


def added_untagged(before: str, after: str, tags) -> list:
    """The untagged entry LINES `after` introduces that `before` did not already have.

    Identity is the whole normalized line, compared as a multiset. Deliberately dumb, and
    the third design here, because both cleverer ones were wrong:

      1. Matching on the whole line, but computing "added" from the tool-input fragment,
         could not tell an overwrite from a fresh authoring.
      2. Matching on the bold TITLE and counting occurrences was order-dependent (a
         tagged line could consume an untagged line's slot, and vice versa), and even
         once that was fixed it still had a NET-COUNT CANCELLATION hole: retro-tagging
         one occurrence of a title while adding an untagged twin of the same title left
         the untagged count unchanged, so the new untagged entry passed. Three separate
         bugs came out of that multiset. The multiset was the problem.

    Whole-line identity has no cancellation and no ordering: a line is either already in
    the file or it is not.

    The tradeoff, stated honestly: EDITING an existing untagged entry (its body or its
    lead-in) now produces a line that is not in `before`, so the gate asks for a tag. That
    is a real behavior change from the title-identity version, and it is acceptable. The
    guarantee that actually matters is preserved exactly: entries you do not touch are
    byte-identical in `before` and `after`, so they are never flagged. You can overwrite,
    append to, and rewrite a note full of legacy untagged entries all day. You are only
    asked to tag an old entry on the rare occasion you actually edit one, which improves
    the corpus rather than punishing you.
    """
    # Compare against EVERY line already in the file, not merely the lines that PARSED as
    # entries. Entry-ness depends on parser context, so a line's classification can change
    # without the line itself changing:
    #
    #     Session notes                     ->   ## Session notes      (the edit)
    #     **Owner.** Kai will coordinate.        **Owner.** Kai will coordinate.
    #
    # The `**Owner.**` line is byte-identical, and untouched, but the new heading makes it
    # start a block, so it becomes an entry and would be denied. Same class when closing a
    # previously-unclosed fence. That breaks the guarantee this hook is sold on, that
    # entries you do not touch are never flagged.
    #
    # Keying the exemption on the TEXT PRESENT IN `before` makes that guarantee literally
    # true: if the line was already in the file, it cannot be something this write "added",
    # no matter what context moved around it.
    present = Counter(" ".join(ln.split()) for ln in (before or "").splitlines())
    out = []
    for tag, line in entries(after):
        if tag is not None and tag in tags:
            continue  # tagged: fine
        if present[line] > 0:
            present[line] -= 1  # this exact text was already in the file
            continue
        out.append(line)
    return out


def apply_call(tool_name: str, tool_input: dict, before: str):
    """The document as it will exist AFTER this tool call, or None if not derivable.

    None means "cannot reason about the result" and the caller fails open.
    """
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None

    def _patch(text: str, edit: dict):
        """Replay one edit, or None when the result is not confidently derivable.

        Every `None` here is a deliberate fail-open. A wrong simulation is worse than no
        simulation: it yields a WRONG `after` document, and both false denies and silent
        bypasses follow from that. So this only models the cases whose semantics are
        unambiguous, and refuses to guess the rest.
        """
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        if old == "":
            # An empty old_string has no unambiguous meaning to replay. Python's
            # str.replace would INSERT at position 0; the Edit tool rejects it outright.
            # An earlier cut here guessed "append", which matches neither. Refuse.
            return None
        hits = text.count(old)
        if hits == 0:
            return None  # the tool will error; nothing for us to judge
        if hits > 1 and edit.get("replace_all") is not True:
            # The Edit tool errors on a non-unique old_string rather than picking one.
            # Replaying "first occurrence" would let us deny a write that was never
            # going to land, masking the tool's real uniqueness error with a bogus
            # tag-deny.
            return None
        return text.replace(old, new, -1 if edit.get("replace_all") is True else 1)

    if tool_name == "Edit":
        return _patch(before, tool_input)

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
        # Replay is inherently superlinear (each edit rescans the evolving document, and
        # the substring-of-a-prior-new_string guard rescans prior edits). A 16k-edit
        # MultiEdit measured 7.5s, past the hook's 5s timeout, which would stall the write.
        # Refuse to simulate an outsized payload rather than hang: fail open, as always.
        if len(edits) > MAX_REPLAY_EDITS:
            return None
        text = before
        produced = []
        for e in edits:
            if not isinstance(e, dict):
                return None
            old = e.get("old_string")
            # The real MultiEdit rejects an edit whose old_string is a substring of an
            # earlier edit's new_string ("old_string is a substring of a new_string from
            # a previous edit"). Replaying it anyway would let us deny a call the tool
            # was never going to execute.
            if isinstance(old, str) and any(old in p for p in produced):
                return None
            text = _patch(text, e)
            if text is None:
                return None
            new = e.get("new_string")
            if isinstance(new, str):
                produced.append(new)
        return text

    return None


def main() -> int:
    # Payload parse FIRST (pure: no disk work).
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    tool_name = payload.get("tool_name")
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return 0

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0

    # Disable-env check SECOND, before ANY disk work.
    if _disabled():
        return 0

    # Config read THIRD (pure read).
    cfg = load_config()
    if not cfg:
        return 0  # opt-in: no usable config, no-op

    dirs = clean_str_list(cfg.get("daily_notes_dirs"), DEFAULT_DIRS)

    # Path guard FOURTH. Segment match, no regex, so it cannot backtrack.
    if not in_daily_notes(file_path, dirs):
        return 0

    tags = clean_str_list(cfg.get("tags"), DEFAULT_TAGS)

    # Reconstruct BOTH documents. `before` comes from DISK, never from tool input, which
    # is what makes the added-entry set impossible to launder.
    target = Path(file_path)
    if target.exists():
        before = read_regular(target)
        if before is None:
            return 0  # not a regular file, or unreadable: cannot reason about it, allow
    else:
        before = ""  # a brand-new note: every entry in it is genuinely new

    if len(before) > MAX_DOC_BYTES:
        return 0  # outsized document: replay/parse could outrun the timeout, so fail open

    after = apply_call(tool_name, tool_input, before)
    if not isinstance(after, str):
        return 0  # not derivable: fail open

    # The ceiling must cover `after`, not just `before`. A Write to an EMPTY note carries
    # the whole document in its payload, so `before` passes the check trivially while the
    # thing actually parsed is arbitrarily large: a 120MB Write parsed past the 5s timeout
    # and stalled the write. Check the document we are about to parse.
    if len(after) > MAX_DOC_BYTES:
        return 0

    untagged = added_untagged(before, after, tags)
    if not untagged:
        return 0

    shown = untagged[0][:90]
    more = f" (and {len(untagged) - 1} more)" if len(untagged) > 1 else ""
    joined = " | ".join(f"[{t}]" for t in tags)
    # The example MUST use a configured tag. Hardcoding `[decision]` told a tenant whose
    # tags are ["call", "chore"] to use a tag the hook would reject, so following the
    # deny message got them denied again.
    example = f"[{tags[0]}] **Retired the charter model.** Kai's call."
    meanings = {
        "decision": "a call was made",
        "fix": "something broken was corrected",
        "architecture": "a system's structure changed",
        "ops": "routine work (the usual default)",
    }
    hints = [f"[{t}] {meanings[t]}" for t in tags if t in meanings]
    hint_line = ("\nPick the tag that fits: " + "; ".join(hints) + ".\n") if hints else "\n"
    reason = (
        f"This Daily Note entry has no category tag{more}:\n\n"
        f"  {shown}\n\n"
        f"Prefix its bold lead-in with one of: {joined}\n"
        f"For example: {example}\n"
        f"{hint_line}"
        f"Then re-issue the write.\n\n"
        f"Only entries this write ADDS or EDITS are gated. Entries already in the file "
        f"that you are not touching are never flagged, so you never have to retro-tag "
        f"the rest of the note to get this write through."
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    # Fail open: a PreToolUse hook must never crash or block a tool call. Any unexpected
    # error degrades to a silent no-op, never a spurious deny.
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
