#!/usr/bin/env python3
"""VardeMem: a bounded read over an append-only journal.

A journal that grows by a file a day is complete and unreadable: a session
start can only afford to open one of them. The spine is a decayed index over
it. One line per recorded day, plus a binary tree of rollups above those, so
a fixed line budget covers unbounded history: recent days stay verbatim,
ancient ones collapse into a single line covering a month or a year.

  vardemem read [--budget N]   the bounded read. Run at session start.
                               --budget also applies to due/roll/put.
  vardemem missing             days with a journal but no spine line
  vardemem draft <date>        that day's skeleton, to compress. Writes nothing.
  vardemem put <date> "..."    record (or rewrite) one day line
  vardemem due [--all]         rollups the current read needs, smallest first
  vardemem roll <a>..<b> "..." record one rollup
  vardemem grep <regex>        search day lines and rollups
  vardemem forget <a>..<b>     drop a bad rollup and everything built on it
  vardemem forget --orphaned   drop every rollup the day set outgrew, plus
                               any rollup line the parser rejected outright
  vardemem selftest            property checks for the index arithmetic

The spine is DERIVED. Every line comes from a journal file, the journals are
never edited, and the spine file can be rebuilt from them. A wrong line is a
re-summarize, never a lost memory.

Journal paths are configuration: see CONFIG_NAME below, and README.md.

The decay idea comes from OptMem (github.com/VictorTaelin/OptMem): tile
history with aligned power-of-two blocks whose detail thins with age, so a
fixed line budget covers unbounded history. Credit for the technique is his.

cover() and _cover() below are our own implementation of it, written from a
behavioural specification rather than from his source, because OptMem carries
no license.

The rewrite also covers ONE string outside them: roll_prompt()'s instruction
to the summarizer. The original was his prose, carried verbatim, and prose is
the least defensible thing to keep. Nothing else in this file was OptMem.
"""

import collections
import datetime
import hashlib
import json
import os
import re
import shlex
import signal
import sys
try:
    import fcntl
except ImportError:  # Windows has no fcntl; the fleet is Linux and macOS
    fcntl = None
from pathlib import Path


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


# The config file is the ROOT MARKER as well as the configuration: the
# directory holding it is the root, and its contents say where the journal
# lives under that root. One mechanism, so a tree either declares itself or it
# does not.
CONFIG_NAME = ".vardemem.json"

# Every key the config may set. An unknown key is a typo, and a silently
# ignored one is the worst kind: `"archve"` disables the archive without
# saying so, and the days it holds stop existing as far as `missing` and
# `draft` are concerned.
CONFIG_KEYS = ("journal", "archive", "spine")

# The environment variable naming a root, consulted only after the walk below
# finds nothing.
ROOT_ENV = "AGENT_VAULT"


# Resolve the root by walking up from the CURRENT DIRECTORY first, and consult
# $AGENT_VAULT only after. The other order looked equivalent and was not:
# $AGENT_VAULT pointing at the primary checkout wrote a day line into the
# WRONG tree from a worktree session, silently and at exit 0. (North,
# 2026-07-28.) In the vault this file used to live in, the local witness was
# __file__, because the script sat beside the journal it wrote. Extraction
# takes __file__ out of that tree entirely, so it witnesses nothing; the
# config file takes over, and it is a STRICTLY better witness because it is
# checked in, so every worktree carries its own without anyone copying
# anything. The ordering is the part that matters and it is unchanged: the
# tree you are standing in knows which journal you mean, and the environment
# only knows where a session started.
def _resolve_root():
    """(root, config path). Dies when neither the walk nor the environment
    answers. The warning that used to live here is in _warn_wrong_tree, which
    runs on BOTH answers rather than on this one."""
    try:
        here = Path.cwd().resolve()
    except OSError:
        here = None                     # cwd deleted out from under us
    if here is not None:
        for d in (here, *here.parents):
            if (d / CONFIG_NAME).is_file():
                return d, d / CONFIG_NAME
    env = os.environ.get(ROOT_ENV)
    if env:
        return Path(env), Path(env) / CONFIG_NAME
    die("Cannot locate a journal: no %s in the current directory or any "
        "parent, and $%s is unset." % (CONFIG_NAME, ROOT_ENV))


def _read_config(path):
    """The config as a dict. Every failure names the file AND the edit that
    clears it: a tool that dies on its own configuration without saying which
    line to change has moved the problem rather than reported it."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        die('No %s. Create it at the root of the tree that holds the '
            'journal:\n  {"journal": "<path to the journal, relative to %s>"}'
            % (path, path.parent))
    except OSError as e:
        die("Could not read %s: %s" % (path, e))
    try:
        cfg = json.loads(text)
    except ValueError as e:
        die("%s is not valid JSON: %s" % (path, e))
    if not isinstance(cfg, dict):
        die("%s must hold a JSON object, not %s."
            % (path, type(cfg).__name__))
    unknown = sorted(k for k in cfg if k not in CONFIG_KEYS)
    if unknown:
        # Warn, never die. Naming the key is what catches the typo, and dying
        # buys nothing on top of that while costing something real: the tool
        # ships by clone, three tenants hold their own copies, and a key added
        # to a config on one side would take `read` out entirely on every
        # clone that had not caught up. "No read ever refuses" is this file's
        # own loudest rule and a config key is not a data problem.
        # (Adversarial review, round 1.)
        print("!! %s sets %s, which VardeMem does not read and will ignore. "
              "Known keys: %s."
              % (path, ", ".join('"%s"' % k for k in unknown),
                 ", ".join('"%s"' % k for k in CONFIG_KEYS)), file=sys.stderr)
    return cfg


def _warn_wrong_tree(root, rel):
    """Warn when cwd sits inside a DIFFERENT tree carrying the same journal
    layout as the one we resolved.

    This is `warn_wrong_checkout` from the vault copy, with its one hardcoded
    path replaced by the configured one. Nothing else about it changed, and
    that is deliberate: it is the guard North paid for, and the extraction has
    to carry it rather than approximate it.

    A first attempt put the test inside the walk and keyed it on a `.git`
    entry. That was wrong twice. It fired only when the WALK answered, so the
    $AGENT_VAULT branch, which is the branch where the environment wins and
    therefore the one the whole ordering exists to police, stayed silent: a
    worktree outside the primary checkout, carrying no config, wrote its day
    line into $AGENT_VAULT's tree at exit 0 with nothing printed. And keying on
    `.git` asked the wrong question, warning when the caller merely stood in an
    unrelated repository and staying quiet for a journal tree that is not a
    checkout at all. The right question is the one the original asked: does the
    directory I am standing in HOLD A JOURNAL of its own? Asking it here, after
    the config is read, needs no knowledge of version control and covers both
    answers. (Adversarial review, round 1.)"""
    try:
        here = Path.cwd()
    except OSError:
        return
    for d in (here, *here.parents):
        if (d / rel).is_dir():
            if d.resolve() != root.resolve():
                print("!! Writing the journal under %s,\n!! but the current "
                      "directory is inside %s, which holds the same journal "
                      "layout and declares no %s.\n!! Writes land in the "
                      "FORMER. Add a %s to the latter if you meant it."
                      % (root, d, CONFIG_NAME, CONFIG_NAME), file=sys.stderr)
            return


def _cfg_path(path, key, value):
    """One configured path, checked where it is READ. Path() accepts a number
    or an empty string and turns it into a confusing failure much further
    downstream, at a site with no idea a config file was involved.

    A value that is ABSOLUTE, or that climbs out with `..`, is honoured. Path
    joining does that on its own and it is left alone deliberately: the root
    marker answers "which tree am I talking to", and it was never a sandbox.
    The only writer of this file is whoever owns the tree, there is no
    untrusted input anywhere in the path, and a journal on another volume is a
    thing configuration is allowed to say. Rejecting it would break that case
    to defend a boundary nothing else in this tool treats as one. Codex read
    the same mechanism and graded it BLOCKING on the strength of the docs,
    which said "relative to the root" and did not say what an absolute value
    does; the docs were the incomplete half, so they say it now and a check
    below pins it as a contract rather than an accident of pathlib."""
    if not isinstance(value, str) or not value.strip():
        die('%s: "%s" must be a non-empty string, not %r.' % (path, key, value))
    return value


_PATHS = {}


def _paths():
    """The journal paths, resolved on FIRST USE and never at import.

    `selftest` is pure index arithmetic and reads no journal file, but the
    root was resolved at import, so a copy placed anywhere else exited 1 with
    "Cannot locate the vault" and the PR's "verification is re-runnable" claim
    was false off the vault path. Round 12 answered that with a path-DEPTH
    heuristic (`_SELFTEST and len(parents) > 3`), which fails for the one
    directory a reviewer actually copies to: a script at `/tmp` has 2 parents
    on Linux and 3 on macOS, so both fell through and died -- the exact
    failure that fix claimed to close. A heuristic about where the file SITS
    is the wrong shape of answer. Nothing needs the journal until something
    touches a journal file, so resolve it there: exact from any path, and the
    heuristic is deleted rather than retuned. Accessors rather than constants
    so that every site which needs the journal says so. (North, rounds 12 and
    13.)"""
    if not _PATHS:
        root, conf = _resolve_root()
        cfg = _read_config(conf)
        if "journal" not in cfg:
            die('%s does not set "journal", so there is no journal to read.'
                % conf)
        rel = _cfg_path(conf, "journal", cfg["journal"])
        journal = root / rel
        _warn_wrong_tree(root, rel)
        # `archive` is optional and has NO default. A second directory of day
        # files is a migration artifact, not something every tree has, and a
        # default would invent one. `spine` does have a default: it names this
        # tool's OWN output file, not the layout of somebody else's journal.
        _PATHS.update(
            config=conf, journal=journal, journal_rel=rel,
            archive=(journal / _cfg_path(conf, "archive", cfg["archive"])
                     if "archive" in cfg else None),
            spine=journal / _cfg_path(conf, "spine",
                                      cfg.get("spine", "SPINE.md")))
    return _PATHS


def config_path():
    return _paths()["config"]


def journal_dir():
    return _paths()["journal"]


def journal_rel():
    """The journal as the config spells it, for a message a reader has to
    match back to a config file rather than to an absolute path."""
    return _paths()["journal_rel"]


def archive_dir():
    """A second directory of `<date>.md` day files, or None when unconfigured."""
    return _paths()["archive"]


def spine_path():
    return _paths()["spine"]


BUDGET = 120       # lines a read may print
LINE_CHARS = 280   # the longest one spine line may be
RAW_MAX = 16       # rollups up to this many days are built from raw day lines

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A day line that records only that nothing happened, optionally after one
# short leading clause ("Travel week, agent autonomous."). The clause used to
# be hardcoded to one phrase, so "Holiday. No session logged." never
# collapsed; it is generic now, and the collapse carries the clause into the
# output rather than discarding it. (North, round 10.)
EMPTY = re.compile(r"^(?P<why>[^.]{0,60}\.\s*)?no (session logged|working session)\.?$", re.I)

# Every command this tool prints has to RUN as printed, and a bare `vardemem`
# is a command-not-found on any host that has not put it on PATH. Name
# ourselves the way we were actually invoked, which runs from anywhere.
ME = "python3 %s" % shlex.quote(sys.argv[0] if sys.argv and sys.argv[0] else __file__)


def real_date(s):
    """True only for a date that both LOOKS like one and exists. The write gates
    checked shape alone while parse() checked reality, so `put 2025-06-31`
    succeeded at exit 0 and then every later command, including put, due,
    missing and forget, refused the file it had just written. Nothing in the
    tool removes a day line, so the only way out was hand-editing SPINE.md,
    which this file's own spine header forbids: one typo ended the spine. The
    two checks have to be the same check. (North, round 8.)"""
    if not DATE.fullmatch(s):
        return False
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        return False
    return True


def floor_budget(T):
    """The fewest blocks that can tile [0,T). Blocks are aligned powers of two,
    so a free tiling costs one per set bit; the newest day is pinned to size 1
    (see _cover), which leaves [0,T-1) to tile freely plus that one. Exact, not
    a bound: verified equal to len(cover(T,1)) for every T in 1..3999.

    A budget below this is not achievable by any alpha, so `cover` returns more
    lines than asked and no rollup can fix it. Naming it lets the read say so
    instead of pointing at `due`. (North, round 7; corrected round 9 when
    pinning the present raised the floor.)"""
    return 1 if T <= 1 else bin(T - 1).count("1") + 1


# ---------------------------------------------------------------- cover

def _cover(T, alpha):
    """Partition [0,T) into aligned power-of-two blocks, sorted by start.

    A block survives whole while its length stays inside `alpha` times the age
    of its OLDEST day, `T - lo`, which for a wide block exceeds its distance
    from the newest day by the block's own width. Anything wider halves and the
    halves face the same test. Raising alpha lets wider blocks live, which
    coarsens the partition and shortens the read.

    Three rules sit outside that test. A single day is always kept, since it
    cannot split. A block WIDER than one day whose right edge reaches the
    newest day always halves, whatever alpha says. A block starting at or past
    T is dropped without descending. The right-edge rule is argued at the
    condition below, the discard at its own site above it, and the single-day
    rule nowhere else: a block of one day has no halves to descend into."""
    span = 1
    while span < T:
        span *= 2
    blocks, pending = [], [(0, span)]
    while pending:
        lo, hi = pending.pop()
        if lo >= T:
            # The block begins after the last recorded day, so the whole
            # subtree under it is empty. Drop it without descending.
            continue
        size = hi - lo
        # The right-edge test is `hi < T`, and relaxing it to `hi <= T` breaks
        # the spine. A block whose end lands on the newest day must halve all
        # the way to size 1, so the present is always its own single-day block.
        # Round 8 taught needed() and all_pending() to stop asking for a rollup
        # that reaches the newest day, because writing one makes the next
        # day-close `put` on that day hit the rewrite refusal. cover still EMITTED
        # such a block at low budgets. read then expanded it to one line per
        # day, ran past the budget, withheld the oldest line, and pointed the
        # user at a `due` that was right to report nothing to do. Half of all
        # history vanished with no remedy. The invariant lives here, in the one
        # place blocks are made, instead of in every consumer that reads them.
        # (North, round 9.)
        # `size == 1` is what makes this loop TERMINATE, not merely what shapes
        # the output. At size 1, `mid = (lo + hi) // 2` equals `lo`, so the
        # else branch would push (lo, lo) and (lo, lo + 1) and the second comes
        # back forever. The failure is not a crash or a wrong answer: it is
        # unbounded allocation at roughly 0.36 GB per second, which on a shared
        # box takes down more than the caller. North's review handler was OOM
        # killed by exactly this mutation. Weaken this test and you get that,
        # so it is a guard rather than a formatting choice.
        if size == 1 or (hi < T and size <= alpha * (T - lo)):
            blocks.append((lo, hi))
        else:
            mid = (lo + hi) // 2
            pending.append((mid, hi))
            pending.append((lo, mid))
    blocks.sort()
    return blocks


def cover(T, budget):
    """The blocks a bounded read prints, finest grain against day T-1.

    At most `budget` of them once the budget clears floor_budget(T). Below that
    floor there is no tiling of [0,T) that small, so the count settles at the
    floor instead: budgets under about a dozen can return more blocks than they
    asked for, and always could.

    Compression is bought with age. Recent days stay one per line while the far
    past folds into ever wider blocks. A history that already fits the budget
    folds nowhere at all."""
    if T <= 0:
        return []
    if T <= budget:
        return [(d, d + 1) for d in range(T)]
    # Bisect for the least coarsening that still fits. `low` is the side known
    # to overflow; `high` is the side known to fit, or the untested 1.0 it
    # started at when NOTHING fits, which happens whenever budget is below
    # floor_budget(T). The result comes off `high` either way: at 1.0 the
    # partition is the coarsest that exists, which is the right answer to an
    # impossible budget.
    #
    # The bracket is [0, 1] because that is the whole meaningful range, not a
    # guess. At 0 no block clears `size <= alpha * (T - lo)`, so every day is
    # its own line. At 1 the test reads `size <= T - lo`, and a kept block also
    # needs `hi < T`, which already forces `size < T - lo`, so every block the
    # right edge allows is kept. Alpha 1 is therefore the coarsest partition
    # that exists and nothing above it can differ.
    #
    # Stop on the bracket width rather than a fixed round count, because the
    # width at which the answer is pinned is computable. Alpha only enters
    # through that one test, so the partition can change only at the ratios
    # size / (T - lo). Each denominator is at most T, so two different ratios
    # sit at least 1 / T**2 apart. Once the bracket is narrower than that, it
    # holds one such ratio at most, `high` and the smallest fitting alpha are in
    # the same cell, and the rebuild below reproduces exactly that partition.
    # Halving to this width costs about 2*log2(T) rounds, fewer than a fixed 60
    # at every history length this tool will ever see (it would exceed 60 only
    # past T = 2**29.5, some two million years of journal),
    # and it states its own correctness argument instead of asserting a number.
    tol = 1.0 / (2.0 * T * T)
    low, high = 0.0, 1.0
    while high - low > tol:
        mid = (low + high) / 2
        if mid <= low or mid >= high:
            break  # adjacent doubles: the bracket cannot narrow any further
        if len(_cover(T, mid)) > budget:
            low = mid
        else:
            high = mid
    blocks = _cover(T, high)
    # Splitting a block doubles the lines that region costs, so the reachable
    # counts are coarse and `high` often stops short of the budget. Hand the
    # unspent lines to the present: halve the latest block wider than one day,
    # again and again, until the budget fills or every block is a single day.
    while len(blocks) < budget:
        wide = [i for i, (a, b) in enumerate(blocks) if b - a > 1]
        if not wide:
            break
        i = wide[-1]
        a, b = blocks[i]
        mid = (a + b) // 2
        blocks[i:i + 1] = [(a, mid), (mid, b)]
    return blocks


# ---------------------------------------------------------------- store

# `|||||||` is the diff3/zdiff3 ancestor marker; it contains a pipe, so
# without naming it the line partitions into an empty key instead.
CONFLICT = ("<<<<<<<", "=======", ">>>>>>>", "|||||||")


# A rollup's stored fields. Named, not positional: the text used to be
# `rollups[k][1]`, and when the tuple last grew, ten call sites were updated
# and an eleventh printed a Python repr for a round. `.text` cannot be reached
# by accident. (North, round 12; shape hardened round 13.)
Roll = collections.namedtuple("Roll", "count digest text")

# A collapsed run of contentless days, held as data while the read is being
# built. See emit_day for why this is not an encoded string.
Run = collections.namedtuple("Run", "first n last whys")


def day_digest(day_keys):
    """A short fingerprint of the exact days a rollup summarized.

    The recorded COUNT catches a merge that inserts days, because the span
    grows. It cannot catch a merge that SUBSTITUTES one: a branch that
    replaced a day's date leaves the count and the alignment identical while
    the day set underneath differs, so `orphaned()` stays quiet, `span ==
    count` stays true, and the rollup asserts a summary of days it never saw,
    at exit 0, in the read that lands in every session. Measured: 464 such
    states across N=4..25. A count is too weak a fingerprint for the claim
    being made; a digest is the right width. (Codex, cross-model review, on a
    property the round-11 count field was introduced to guarantee.)"""
    # 16 hex, not 8. At 32 bits a collision is constructible: Codex built one
    # (two different 4-day sets both digesting to 841cb002) and the read then
    # printed a summary of the wrong days at exit 0 with no warning. 64 bits
    # puts an accidental collision past any history this file will ever hold,
    # which is the honest bound to claim: this detects merge and hand-edit
    # damage, it is not a defense against a chosen collision.
    return hashlib.sha256("\n".join(day_keys).encode("utf-8")).hexdigest()[:16]


class RollProblem:
    """One rollup-level problem, carrying the remedy that actually clears IT.

    The five rollup problems do not share a remedy, and a single generic
    banner said they did: for the three that parse() drops before the key
    reaches `rollups`, `forget <key>` exits 1 with "No rollup" and `forget
    --orphaned` exits 0 having saved nothing and left the line on disk. Both
    printed commands failed, which is exactly what the contract at the top of
    this file says cannot happen. A remedy is only correct next to the cause,
    so it is chosen HERE and never at a print site. `stored` is the fact that
    decides it: whether the key made it into the dict `save()` rewrites from.
    (North, round 13.)"""
    __slots__ = ("msg", "key", "stored", "stale")

    def __init__(self, msg, key=None, stored=False, stale=False):
        self.msg, self.key, self.stored, self.stale = msg, key, stored, stale

    def remedy(self):
        if self.stored:
            return "%s forget %s" % (ME, self.key)
        # Not in the dict, so nothing can delete it by key. save() rewrites
        # the whole Rollups section from the dict, so any write drops the
        # line; --orphaned is the one that does only that.
        return "%s forget --orphaned" % ME


def parse():
    """(days, rollups, problems, roll_bad). Never raises on a malformed file:
    a caller that writes refuses on `problems`, and the read degrades instead.

    `problems` is DAY-level and fatal to writes; `roll_bad` is rollup-level,
    a cache, and clearable. They are returned apart, and must stay apart:
    every consumer that merged them printed a falsehood. (North, round 13.)

    A git conflict in this file used to parse SILENTLY, because the marker
    lines carry no `|` and `>>>>>>>` looks like a blockquote. git writes the
    HEAD side first, so the surviving day lines land out of order and the
    order check killed the session-start read with zero lines printed. Detect
    the markers by name so the diagnosis is the actual problem."""
    days, rollups, problems, roll_bad = [], {}, [], []
    if not spine_path().exists():
        return days, rollups, problems, roll_bad
    # EXISTING is not READABLE. The vault copy had the same unguarded read and
    # the same crash, but reaching it needed someone to create a DIRECTORY
    # named SPINE.md; a configured spine path reaches it with one character
    # (`"spine": "."` resolves to the journal directory), so the config layer
    # is what made this worth fixing rather than noting. A traceback out of the
    # session-start read is the failure this file's own standard names most
    # often: say which path, say why, and say which key chose it.
    # (Codex, cross-model review.)
    try:
        text = spine_path().read_text(encoding="utf-8")
    except OSError as e:
        die('Could not read the spine at %s: %s\n("spine" in %s)'
            % (spine_path(), e, config_path()))
    section, seen_sections = None, set()
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith(CONFLICT):
            problems.append("line %d: unresolved git conflict marker (%s)"
                            % (n, s.split()[0]))
            continue
        if s.startswith("## "):
            section = s[3:].strip().lower()
            seen_sections.add(section)
            continue
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        if "|" not in s:
            # Silently skipping this shrank the day set and shifted every index
            # above it, surfacing much later as `put` refusing for the wrong
            # stated reason. A hand-resolved conflict is exactly where a pipe
            # goes missing. (North, 2026-07-28.)
            #
            # Route by SECTION. This branch fires before the section dispatch
            # below, so it sent a broken line in the Rollups section to the
            # DAY list and made a cache problem refuse every write, including
            # the `forget --orphaned` that would clear it. Third instance of
            # that same misrouting; the fix is to ask which section we are in
            # everywhere a problem is raised, not to patch each branch as it
            # is found. (Codex, cross-model review, round 2.)
            if section == "days":
                problems.append("line %d: no '|' separator, line dropped: %.40s"
                                % (n, s))
            elif section == "rollups":
                roll_bad.append(RollProblem(
                    "line %d: no '|' separator, line dropped: %.40s" % (n, s)))
            continue
        key, _, text = s.partition("|")
        key, text = key.strip(), text.strip()
        if section not in ("days", "rollups"):
            # A `key | text` line outside any section fell through both
            # branches and was discarded with no problems entry. Lose the
            # `## Days` header (a hand-repair away, which the documented
            # conflict resolution tells a human to do) and EVERY day line vanished
            # silently: read printed "The spine is empty", and the next put
            # wrote a one-day file over the rest. Nothing keyed off `problems`
            # could catch it, because problems was empty. (North, round 9.)
            problems.append("line %d: '%.20s' sits outside any ## section "
                            "(a section header is missing)" % (n, key))
            continue
        if section == "days":
            if not DATE.fullmatch(key):
                problems.append("line %d: '%s' is not a date" % (n, key))
                continue
            # Calendar-validate day keys for the same reason rollup keys are
            # validated below: `2026-02-30` matches the shape regex and then
            # detonates in the run-collapse date arithmetic, taking the
            # session-start read down with a traceback in the one function
            # whose whole design is degrade-never-block. Round 6 hardened the
            # rollup branch and left this one on shape alone. (North, round 7.)
            try:
                datetime.date.fromisoformat(key)
            except ValueError as e:
                problems.append("line %d: day %s is not a real date (%s)"
                                % (n, key, e))
                continue
            days.append((key, text))
        elif section == "rollups":
            if not re.fullmatch(r"%s\.\.%s" % (DATE.pattern, DATE.pattern), key):
                # roll_bad, not problems: this is a line in the Rollups
                # section, so it is a cache problem like the other five and
                # must not refuse a write. It was the one rollup branch that
                # still appended to the DAY list, so a single typo'd rollup
                # key made `put` exit 1 while the banner said writes were
                # allowed -- the round-13 defect, surviving in the one place
                # the round-13 fix did not look. (Codex, cross-model review.)
                roll_bad.append(RollProblem(
                    "line %d: '%s' is not a date range" % (n, key), key))
                continue
            # Shape is not enough. `2025-02-30..2025-03-02` matches the regex
            # and then detonates in span_days (ValueError), and a reversed key
            # gives size 0 and a ZeroDivisionError in the forget chain walk.
            # Both are reachable from a hand-resolved conflict, which the README
            # tells a human to do. (North, 2026-07-28.)
            a, _, b = key.partition("..")
            try:
                if datetime.date.fromisoformat(a) > datetime.date.fromisoformat(b):
                    roll_bad.append(RollProblem(
                        "line %d: rollup %s ends before it starts" % (n, key),
                        key))
                    continue
            except ValueError as e:
                roll_bad.append(RollProblem(
                    "line %d: rollup %s has an impossible date (%s)"
                    % (n, key, e), key))
                continue
            # A rollup carries the number of days it summarized, WHEN IT WAS
            # WRITTEN. Without it the safety property has a hole no surface can
            # see: a git merge is a multi-day insert that never goes through
            # `put`, and the documented way to resolve a conflict in this
            # file is exactly that merge. Days 01-01/01-04 with a rollup over the
            # size-2 block [0,2); another machine's 01-02 and 01-03 auto-merge
            # in cleanly, no conflict markers; the same key is now the size-4
            # block [0,4). It never passes through an orphaned state, so
            # orphaned() returns [], due says nothing due, missing says
            # current, and `read` prints a two-day summary as the summary of
            # four days at exit 0. Comparing the recorded count against the
            # key's current span turns that whole class, hand repairs
            # included, into a problems entry. (North, round 11.)
            cnt, _, rest = text.partition("|")
            # `16d` or `16d/a3f91c02`. The digest is optional so a rollup
            # written before it existed still parses; every new write carries
            # one, and a line without one is checked on count alone.
            # Exactly 8 or 16 hex, not "any length". Widening the digest from
            # 8 to 16 left this accepting both while the comparison below used
            # the full new width, so a legacy 8-hex line could never match and
            # every one of them was falsely reported stale and dropped from
            # the read. An open-ended width here is what made a format change
            # silently break the format it replaced. (Codex, round 3.)
            m_cnt = re.fullmatch(r"(\d+)d(?:/([0-9a-f]{8}|[0-9a-f]{16}))?",
                                 cnt.strip())
            if not m_cnt:
                roll_bad.append(RollProblem(
                    "line %d: rollup %s has no day count (expected "
                    "`%s | Nd | text`); it cannot be checked against the day "
                    "set" % (n, key, key), key))
                continue
            if key in rollups:
                # A duplicate DAY key is caught loudly; a duplicate rollup key
                # used to be deduped silently, last-in-file wins, and the next
                # put dropped the other side at exit 0. Same asymmetry, on
                # exactly the resolution the docs recommend. (North, r11.)
                # The FIRST occurrence is in `rollups`, so the key is real and
                # `forget <key>` clears the pair.
                roll_bad.append(RollProblem(
                    "line %d: rollup %s appears twice" % (n, key),
                    key, stored=True))
                continue
            rollups[key] = Roll(int(m_cnt.group(1)), m_cnt.group(2),
                                rest.strip())
    # THE check the count exists for. A key that still names an aligned block
    # is not enough: the block may now hold different days than the summary
    # describes. (North, round 11.)
    pos = {d: i for i, (d, _) in enumerate(days)}
    for k, r in rollups.items():
        a, _, b = k.partition("..")
        if a not in pos or b not in pos:
            continue
        span = pos[b] - pos[a] + 1
        if span != r.count:
            roll_bad.append(RollProblem(
                "rollup %s summarized %d day(s) when it was written but now "
                "spans %d: the day set changed under it (a merge or a hand "
                "edit)" % (k, r.count, span),
                k, stored=True, stale=True))
        elif r.digest:
            # Same count, different days. Only the digest sees this: a merge
            # that substitutes a day keeps the span and the alignment intact,
            # so the count check above and orphaned() both stay quiet.
            # Compare at the STORED width. A digest is a prefix of the same
            # sha256, so an 8-hex line still verifies (at its own weaker 32-bit
            # strength) instead of being unfalsifiable against a 16-hex value.
            now = day_digest([d for d, _ in days[pos[a]:pos[b] + 1]])
            if now[:len(r.digest)] != r.digest:
                roll_bad.append(RollProblem(
                    "rollup %s still spans %d day(s) but they are not the "
                    "days it summarized (%s, now %s): a day was replaced "
                    "under it" % (k, span, r.digest, now[:len(r.digest)]),
                    k, stored=True, stale=True))
    # `save` always writes both headers, so their absence is an assertion
    # rather than a heuristic, and it names the cause where the per-line check
    # above can only name a symptom. (North, round 9.)
    for want in ("days", "rollups"):
        if want not in seen_sections:
            problems.append("the '## %s' section header is missing (save() "
                            "always writes it, so it was lost by hand)"
                            % want.title())
    # Append-only in day order is the invariant every rollup key depends on.
    for i in range(1, len(days)):
        if days[i][0] <= days[i - 1][0]:
            problems.append("day %s is not after %s (the spine is append-only "
                            "in day order)" % (days[i][0], days[i - 1][0]))
    return days, rollups, problems, roll_bad


_LOCK = []  # held open for the life of the process; closing it releases


def lock():
    """Serialize the whole read-modify-write. Without this two `put` calls that
    overlap both re-read, both rewrite the entire file, and the second silently
    erases the first's day line while BOTH report success and exit 0. That is
    the normal path here, not a corner: the day-close write runs once per
    session, several sessions can close on the same day, and a fleet host runs
    agent invocations concurrently from more than one trigger.
    (North, 2026-07-28. Measured: a day line lost in 7 of 8 overlapping runs.)"""
    if _LOCK or fcntl is None:
        return
    try:
        f = open(journal_dir() / ".spine.lock", "a")
        fcntl.flock(f, fcntl.LOCK_EX)
    except OSError as e:
        # open() sat outside the try, so an unwritable Journal tracebacked past
        # the die() two lines below. This file's standard is that the
        # day-close hot path never tracebacks. (North, round 11.)
        die("Could not lock %s: %s" % (spine_path().name, e))
    _LOCK.append(f)


def load(write=True, tell=False):
    """(days, rollups) for a command that WRITES. Takes the lock first, so the
    parse a caller mutates is the one it writes back. Refuses on a malformed
    file: appending into a spine we cannot fully parse would bake damage in.

    `write=False` for the advisory commands (`missing`, `due`), which hold no
    lock (an exclusive one to print a suggestion serializes them behind any
    in-flight `put`) and DEGRADE rather than refuse. They borrowed the writer's
    refusal, so `read`'s own printed remedy, "Run: ... due", exited 1 against a
    conflicted file: a printed command that does not run as printed, and the
    advisory you reach for first refusing the state it exists to diagnose.
    (North, round 8.)"""
    if write:
        lock()
    days, rollups, problems, roll_bad = parse()
    # A DAY-level problem means the day set cannot be trusted, so every write
    # refuses. A ROLLUP-level one must not: rollups are a cache, and refusing
    # to let the tool clear its own cache made detection terminal. The round-11
    # count check appended to `problems`, so a stale rollup refused `forget`,
    # `forget --orphaned`, `roll` AND `put` -- every escape hatch behind the
    # gate the finding tripped, with hand-editing the spine file (which its
    # own header forbids) the only way out, and the day-close write broken
    # from that day on. Detection was the right half; this is the other half.
    # (North, round 12.)
    if roll_bad:
        print("!! %d rollup problem(s) in %s. Writes are still allowed so you "
              "can clear them:" % (len(roll_bad), spine_path().name), file=sys.stderr)
        for r in roll_bad:
            # Per-problem remedy, not one banner for all five: three of them
            # are not in `rollups` at all, so a blanket `forget <key>` exited
            # 1 and `--orphaned` cleared nothing. (North, round 13.)
            print("!!   %s\n!!     clear it: %s" % (r.msg, r.remedy()),
                  file=sys.stderr)
        print("!! They re-accrue from `%s due`." % ME, file=sys.stderr)
    if problems and write:
        die("%s is malformed, refusing to write:\n  %s\n\nFix the file, then "
            "re-run. `%s read` still works and will show what survived."
            % (spine_path().name, "\n  ".join(problems), ME))
    if problems:
        print("!! %s is malformed, so this is INCOMPLETE:" % spine_path().name)
        for p in problems:
            print("!!   %s" % p)
        print("!! Writes are refused until it parses.")
        print()
        days = sorted(set(days))
    # The two lists are returned APART. Merging them is the defect: round 12
    # split them in this function's write gate and in cmd_read and left the
    # merge here, so cmd_due and cmd_missing kept treating a clearable cache
    # problem as a fatal one -- printing "any rollup written now would be
    # refused" in the same breath as "writes are still allowed", with the
    # second sentence false (`roll` in that state exits 0 and writes). One
    # stale key suppressed 11 genuinely-due rollups, and since the day-close
    # ritual builds its rollups from `due`, accrual stopped. A caller has to name
    # which kind it means. (North, round 13.)
    return (days, rollups, problems, roll_bad) if tell else (days, rollups)


def save(days, rollups):
    out = ["# Spine",
           "",
           "> Decayed index over the journal. One line per recorded day, plus"
           " rollups above them.",
           "> Derived: rebuildable from `%s/`. Managed by VardeMem; do not"
           " hand-edit." % journal_rel(),
           "",
           "## Days",
           ""]
    out += ["%s | %s" % d for d in days]
    out += ["", "## Rollups", ""]
    # A rollup written before digests existed carries digest=None, and
    # formatting that with %s wrote the literal string "8d/None", which then
    # failed to re-parse as a count at all: one save() silently converted a
    # detectable stale rollup into an unparseable line. Omit the field when
    # there is nothing to write, so the round trip is lossless in both
    # directions and old lines survive a rewrite unchanged.
    out += ["%s | %s | %s" % (k,
                              "%dd/%s" % (rollups[k].count, rollups[k].digest)
                              if rollups[k].digest
                              else "%dd" % rollups[k].count,
                              rollups[k].text)
            for k in sorted(rollups)]
    # Drop trailing blanks before joining. With no rollups yet, `out` ended
    # with "" and the file gained a blank line at EOF, which `git diff
    # --check` flags on every commit that touches it. Deleting that line by
    # hand does not hold (the next write puts it back, as it did on 38022e6);
    # the fix belongs in the writer. (Codex, cross-model review, round 2.)
    while out and not out[-1]:
        out.pop()
    # Atomic replace, never truncate-in-place: a crash or a concurrent reader
    # mid-write would otherwise see a half-written spine.
    tmp = spine_path().with_suffix(".md.tmp%d" % os.getpid())
    try:
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.replace(tmp, spine_path())
    except OSError as e:
        # Remove the partial file on the way out. It is gitignored, so leaving
        # it made a failed write accumulate silently in the Journal rather than
        # show up as something to clean. (North, round 13.)
        try:
            tmp.unlink()
        except OSError:
            pass
        die("Could not write %s: %s" % (spine_path().name, e))


def span_days(key):
    """How many calendar days a rollup key spans, for picking the smallest."""
    a, _, b = key.partition("..")
    return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days


def key_of(days, lo, hi):
    """A rollup's identity is the date range it spans. Stable forever, because
    days are append-only in order: days[lo:hi] can never change."""
    return "%s..%s" % (days[lo][0], days[hi - 1][0])


def check(text):
    text = " ".join(text.split())
    if not text:
        die("Empty. A spine line is one line of text.")
    if len(text) > LINE_CHARS:
        die("Too long: %d chars, limit %d. Compress it further."
            % (len(text), LINE_CHARS))
    return text


def journal_files(date):
    """A day's raw journals, oldest surface first. Usually one file, but an
    archive cutover leaves the cutover day split across both directories with
    DIFFERENT content in each, so a day is a list, not a file."""
    cand = [journal_dir() / ("journal-%s.md" % date)]
    arc = archive_dir()
    if arc is not None:
        cand.insert(0, arc / ("%s.md" % date))
    return [p for p in cand if p.exists()]


def journal_dates():
    """Every day that has a journal, oldest first.

    real_date, not the shape regex: a mistyped filename like
    journal-2026-02-30.md otherwise sticks in `missing` forever, because both
    `draft` and `put` refuse it and no command clears it. The third date gate,
    missed when round 8 unified the other two. (North, round 9.)"""
    found, bad = set(), []
    for p in journal_dir().glob("journal-*.md"):
        # The EXACT canonical name, not a date found anywhere in it.
        # `DATE.search` accepted `journal-2026-01-01-backup.md` as covering
        # that day, while journal_files() only opens `journal-<date>.md`, so
        # `missing` reported the spine current for a day `draft` and `put`
        # both refuse as having no journal. The two filename rules have to be
        # one rule, the same way the two DATE gates were unified in round 8.
        # (Codex, cross-model review, round 2.)
        stem = p.stem[len("journal-"):]
        if real_date(stem):
            found.add(stem)
        elif DATE.fullmatch(stem):
            bad.append(p.name)
    arc = archive_dir()
    for p in (arc.glob("*.md") if arc is not None else ()):
        if real_date(p.stem):
            found.add(p.stem)
        elif DATE.fullmatch(p.stem):
            bad.append(p.name)
    return sorted(found), sorted(bad)


# ---------------------------------------------------------------- rollups

def valid_keys(days):
    """Every rollup key the current day sequence can produce: one per aligned
    power-of-two block."""
    keys, size, T = set(), 2, len(days)
    while size <= T:
        for k in range(T // size):
            keys.add(key_of(days, k * size, (k + 1) * size))
        size *= 2
    return keys


def orphaned(days, rollups):
    """Rollups whose key no longer names an aligned block of `days`. Non-empty
    means the day sequence changed underneath them."""
    keys = valid_keys(days)
    return sorted(k for k in rollups if k not in keys)


def needed(days, rollups, budget):
    """Rollups the current read needs and does not have, smallest first.

    A rollup is built from its two halves, so a missing half is needed too:
    recurse down before emitting, and emit by increasing size so every prompt
    has its inputs ready."""
    want = set()

    T = len(days)

    def require(lo, hi):
        if hi - lo == 1 or key_of(days, lo, hi) in rollups:
            return
        # Never demand a block reaching the newest recorded day: a rollup over
        # today makes the next day-close `put` on that day hit the rewrite
        # refusal the day-close ritual promises cannot happen. This held only
        # INHERITED from cover(),
        # which emits no trailing multi-day block at budget 120 but does at low
        # ones (first at T=4 for budget 2, T=256 for budget 8), so `due
        # --budget 8` would have reopened it through a documented flag. Enforce
        # it here rather than assert it in a comment. (North, round 8.)
        if hi >= T:
            return
        want.add((lo, hi))
        # STOP here at RAW_MAX: roll_prompt builds a block this small from the
        # raw day lines, so its halves are not an input to anything now, and
        # they never appear in a cover at any LARGER T either.
        #
        # What this does and does not buy, measured (North, round 2, 2026-07-28,
        # correcting a figure I had asserted from his round 1 without testing):
        # a COLD rebuild at T=400 costs 68 rollups instead of 280. Under LAZY
        # accrual, which is the regime this actually runs in, it saves nothing:
        # simulating T=1..800 building whatever needed() returns each day, both
        # versions write the identical 680 rollups (0.85/day), because every
        # aligned block eventually enters a cover as its region ages (1 to 2 to
        # 4 to 8) and the skipped halves get demanded then. Keep the cutoff: it
        # orders the queue correctly and makes a rebuild cheap. It is not a
        # steady-state saving.
        if hi - lo <= RAW_MAX:
            return
        mid = (lo + hi) // 2
        require(lo, mid)
        require(mid, hi)

    for lo, hi in cover(len(days), budget):
        require(lo, hi)
    return sorted(want, key=lambda b: (b[1] - b[0], b[0]))


def all_pending(days, rollups):
    """Every rollup that could be built and has not been. Mostly of interest
    for a full rebuild; the read only ever demands what needed() lists."""
    out, size, T = [], 2, len(days)
    while size <= T:
        for k in range(T // size):
            lo, hi = k * size, (k + 1) * size
            # Never summarize a block that reaches the newest recorded day.
            # cover() deliberately keeps the present at size 1; all_pending did
            # not honour that, so `due --all` wrote a rollup over TODAY and the
            # next day-close `put` on the same day hit the rewrite refusal the
            # ritual promises cannot happen. needed() enforces the same guard
            # directly. (North round 6, 2026-07-28.)
            if hi >= T:
                continue
            if key_of(days, lo, hi) not in rollups:
                out.append((lo, hi))
        size *= 2
    return out


def roll_prompt(days, rollups, lo, hi, left):
    if hi - lo <= RAW_MAX:
        body = "\n".join("  %s | %s" % d for d in days[lo:hi])
    else:
        mid, halves = (lo + hi) // 2, []
        for a, b in ((lo, mid), (mid, hi)):
            k = key_of(days, a, b)
            if k not in rollups:
                die("The rollup %s is missing. Run: %s due" % (k, ME))
            halves.append("  %s | %s" % (k, rollups[k].text))
        body = "\n".join(halves)
    tail = "" if not left else "\n%d more after this one." % left
    return ("Compress %s (%d days) into one line of at most %d characters.\n"
            "Carry only what still bears on later work. State nothing the "
            "entries below do not support.\n\n%s\n%s\n"
            "Then run: %s roll %s \"<your line>\""
            % (key_of(days, lo, hi), hi - lo, LINE_CHARS, body, tail,
               ME, key_of(days, lo, hi)))


# ---------------------------------------------------------------- commands

def parse_budget(args, verb):
    """Pull `--budget N` off argv. Shared by read/due/roll/put so the queue and
    the read agree: they did not, and because cover(T,40) blocks are ANCESTORS
    of cover(T,120) blocks, needed(120) never demanded them. Lowering the only
    knob on the read made the read BIGGER and pointed at a `due` that reported
    nothing to do. (North, 2026-07-28.)"""
    rest, budget = [], BUDGET
    i = 0
    while i < len(args):
        if args[i] == "--budget":
            if i + 1 >= len(args) or not args[i + 1].isdigit() or int(args[i + 1]) < 1:
                die("usage: %s %s [--budget N]   # N a positive whole number"
                    % (ME, verb))
            budget = int(args[i + 1])
            i += 2
            continue
        rest.append(args[i])
        i += 1
    return rest, budget


def cmd_read(args):
    args, budget = parse_budget(args, "read")
    if args:
        die("usage: %s read [--budget N]" % ME)
    days, rollups, problems_day, roll_bad = parse()
    problems = problems_day + [r.msg for r in roll_bad]
    if problems:
        # A conflicted or hand-broken SPINE.md must not take the session-start
        # read down with it. Say what is wrong, loudly, then print whatever
        # survived: degrading beats a blank history. Same principle as a
        # missing rollup. (North, 2026-07-28.)
        print("!! %s is malformed and this read is INCOMPLETE:" % spine_path().name)
        for p in problems:
            print("!!   %s" % p)
        if problems_day:
            print("!! Fix the file (a git conflict is the usual cause), then "
                  "re-read. Writes are refused until it parses.")
        else:
            print("!! Only rollup lines are affected, so writes are still "
                  "allowed. Clear them:")
            for r in roll_bad:
                print("!!   %s" % r.remedy())
            print("!! They re-accrue from `%s due`." % ME)
        print()
        days = sorted(set(days))
    if not days:
        print("The spine is empty. Run: %s missing" % ME)
        return
    # A rollup whose recorded count no longer matches its span has been PROVEN
    # wrong by parse(). Printing it anyway asserted four days of history from a
    # line that saw two, in the read that lands in every session's context; the
    # banner is loud but the LINE is what gets carried forward. The degrade
    # machinery three lines below already does the right thing, so route it
    # there: strictly more information, and it is what the surrounding comment
    # already promises. (North, round 12.)
    # Taken from parse(), not re-derived. The same predicate written twice is
    # two things that can drift; parse() already decided, so ask it.
    stale = {r.key for r in roll_bad if r.stale}
    # A gap is either a rollup that was never WRITTEN (`due` builds it) or one
    # that was written and then REJECTED (only `forget` clears it, because
    # needed() returns early for any key present in `rollups` and a stale key
    # is present). Both landed in one list under one remedy, so the read told
    # a reader to run `due`, and `due` answered "Fix SPINE.md first". Keep the
    # two causes apart all the way to the message. (North, round 13.)
    lines, gaps_missing, gaps_stale = [], [], []

    def emit_day(d, t):
        """One day line, folding a run of contentless days into one entry.

        Shared by BOTH paths. It used to live only in the size-1 branch, which
        is the branch that runs when the budget is NOT under pressure, so the
        degraded fallback below printed contentless days raw: at --budget 60 on
        the real spine, 15 raw "No session logged." lines where the collapse
        folds them to about 4, spending a sixth of the budget on nothing and
        pushing that much real history past the withhold edge. A partially
        drained queue is exactly when that branch runs, and it is a blessed
        state rather than an error. (North, round 10.)"""
        # Collapsed on OUTPUT only: the lines stay on disk, so the day set and
        # every rollup key are untouched.
        m = EMPTY.match(t)
        if m:
            why = (m.group("why") or "").strip().rstrip(".")
            # Extend a run ONLY across calendar-adjacent days. A real history
            # has holes (days with no journal at all), and rendering
            # first..last across one produced a line whose range implies more
            # days than its own count: internally contradictory, and an LLM
            # reads the range not the count. (North, 2026-07-28.)
            # A run is a Run object, not a delimiter-encoded string. It was
            # joined on "; " (which legally occurs inside a clause, so four
            # days rendered as five fragments), then on "\x00" (which is
            # legally day text, so a NUL in a journal line corrupted the same
            # way). Any in-band delimiter has a character that breaks it;
            # holding the fields as data has none. (North r13, Codex r2.)
            if lines and isinstance(lines[-1], Run):
                run = lines[-1]
                if (datetime.date.fromisoformat(d)
                        - datetime.date.fromisoformat(run.last)).days == 1:
                    have = list(run.whys)
                    # Keep the most specific clause, not every restatement:
                    # "Travel week" and "Travel week, agent autonomous" are the
                    # same fact at two lengths.
                    if why and not any(x.startswith(why) for x in have):
                        have = [x for x in have if not why.startswith(x)] + [why]
                    lines[-1] = Run(run.first, run.n + 1, d, have)
                    return
            lines.append(Run(d, 1, d, [why] if why else []))
            return
        lines.append("%s | %s" % (d, t))

    for lo, hi in cover(len(days), budget):
        if hi - lo == 1:
            emit_day(*days[lo])
        else:
            k = key_of(days, lo, hi)
            if k in rollups and k not in stale:
                lines.append("%s | %s" % (k, rollups[k].text))
            else:
                # Degrade, never block. A missing rollup falls back to the days
                # under it: more lines than the budget wanted, but strictly
                # more information, and a session start is never gated on a
                # compression that has not been written yet.
                (gaps_stale if k in stale else gaps_missing).append(k)
                for d in days[lo:hi]:
                    emit_day(*d)
    out = []
    for ln in lines:
        if isinstance(ln, Run):
            # Joined for DISPLAY only. Nothing re-parses this, so "; " is safe
            # here in a way it was not in the stored record.
            whys = "; ".join(x for x in ln.whys if x)
            span = ("%s | (no session" % ln.first if ln.n == 1
                    else "%s..%s | (no session, %d days" % (ln.first, ln.last,
                                                            ln.n))
            ln2 = span + ("; %s)" % whys if whys else ")")
            # LINE_CHARS caps what `put` accepts, but the collapse concatenates
            # clauses, so 20 adjacent contentless days with distinct clauses
            # rendered as one 1,117-char line and the byte budget was advisory.
            # (North, round 11.)
            out.append(ln2 if len(ln2) <= LINE_CHARS
                       else ln2[:LINE_CHARS - 5] + " ...)")
        else:
            out.append(ln)
    lines = out
    # Degrading has to stay bounded or it is not a degrade, it is the absence
    # of the feature: with no rollup ever built the fallback prints one line
    # per day and grows 1:1 with history forever, which is the exact cost the
    # spine exists to remove. Keep the newest `budget` lines (recency is what
    # a session start needs) and say plainly that the old end is missing
    # because the compressions were never written. (North, round 6.)
    dropped = 0
    if len(lines) > budget:
        dropped, lines = len(lines) - budget, lines[-budget:]
    # State the range this read actually CONTAINS. After truncation the header
    # was still printing days[0][0], so the first line an agent reads claimed
    # coverage the body does not have; the warning below it is not enough,
    # because the header is the one that gets believed. (North, round 9.)
    if dropped:
        print("Journal spine: %d days recorded, %s to %s. THIS READ COVERS "
              "ONLY %s onward (see the warning below)."
              % (len(days), days[0][0], days[-1][0],
                 lines[0].split(" |")[0].split("..")[0]))
    else:
        print("Journal spine: %d days, %s to %s."
              % (len(days), days[0][0], days[-1][0]))
    print()
    print("\n".join(lines))
    print()
    if dropped:
        # The oldest surviving line may be a ROLLUP, whose key is a range, so
        # take the part before `..` or the message reads "history before
        # 2025-02-02..2025-02-17". (North, round 7.)
        edge = lines[0].split(" |")[0].split("..")[0]
        if gaps_missing or gaps_stale:
            what = []
            if gaps_missing:
                what.append("%d missing" % len(gaps_missing))
            if gaps_stale:
                what.append("%d rejected as stale" % len(gaps_stale))
            print("!! %d older line(s) WITHHELD to hold the %d-line budget: "
                  "%d rollup(s) unusable (%s), so history before %s is not in "
                  "this read at all."
                  % (dropped, budget, len(gaps_missing) + len(gaps_stale),
                     ", ".join(what), edge))
            # Name only the command that runs in THIS state. `due` cannot
            # clear a stale rollup: needed() returns early for a key that is
            # present, and a stale one is present. (North, round 13.)
            if gaps_stale:
                print("!! Drop the stale one(s) first, then run `%s due`:" % ME)
                for k in gaps_stale:
                    print("!!   %s forget %s" % (ME, k))
            else:
                print("!! Run: %s due" % ME)
        else:
            # Every rollup that could help exists. The budget is simply below
            # what any tiling of this history can achieve, so pointing at `due`
            # would be an unactionable loop: it correctly reports nothing due.
            # (North, round 7.)
            print("!! %d older line(s) WITHHELD: a budget of %d cannot cover "
                  "%d days no matter what is compressed (blocks are aligned "
                  "powers of two and the newest day is never compressed, so "
                  "the floor is %d). History before %s is not in this read. "
                  "Raise --budget to at least %d."
                  % (dropped, budget, len(days), floor_budget(len(days)),
                     edge, floor_budget(len(days))))
    elif gaps_missing or gaps_stale:
        print("%d rollup(s) unusable, shown as their days instead (%d lines, "
              "budget %d)." % (len(gaps_missing) + len(gaps_stale),
                               len(lines), budget))
        if gaps_stale:
            for k in gaps_stale:
                print("  stale, drop it: %s forget %s" % (ME, k))
        if gaps_missing:
            print("  %d not yet written. Run: %s due" % (len(gaps_missing), ME))
    else:
        print("%d lines, budget %d." % (len(lines), budget))


def cmd_missing(args):
    if args:
        die("usage: %s missing" % ME)
    days, rollups, problems, _roll_bad = load(write=False, tell=True)
    have = {d for d, _ in days}
    dates, bad = journal_dates()
    if bad:
        print("%d journal file(s) named with a date that does not exist, "
              "ignored: %s" % (len(bad), ", ".join(bad)))
        print("Rename the file; no spine command can record it.")
        print()
    # The spine is DERIVED, so a day line with no journal behind it is not a
    # gap, it is a fabrication: it survives a merge or a hand edit, becomes
    # session-start context, and nothing detects it. `missing` only ever
    # compared one direction (journals with no line), so the invariant the
    # file's header states was never actually checked. Report it here rather
    # than refuse: the line may be real and its journal merely unarchived, so
    # naming it is the honest move and `put` still rewrites it.
    # (Codex, cross-model review.)
    orphan_days = sorted(d for d, _ in days if d not in dates)
    if orphan_days:
        print("%d spine day(s) with NO journal file behind them:"
              % len(orphan_days))
        for d in orphan_days:
            print("  %s" % d)
        print("The spine is derived, so each is either a fabricated line or a "
              "journal that was moved. Confirm against %s/ before trusting "
              "them." % journal_rel())
        print()
    todo = [d for d in dates if d not in have]
    gaps = [d for d in todo if days and d < days[-1][0]]
    if gaps:
        # Backfilling out of order is fine while the tree is thin; put() is the
        # one that decides, and it refuses only if a real rollup would break.
        print("%d gap(s) behind the last recorded day: %s"
              % (len(gaps), ", ".join(gaps)))
        print()
    if not todo:
        if problems:
            # "current" is the wrong word for a file that refuses writes, and
            # the count is inflated: sorted(set(days)) dedupes identical tuples
            # only, so both sides of a conflicted day survive as two days.
            # (North, round 9.)
            print("No journal is missing a line, but %s does NOT parse, so "
                  "this count is unreliable and every write is refused. Fix "
                  "the file first." % spine_path().name)
            return
        print("The spine is current: %d days recorded." % len(days))
        return
    print("%d day(s) with a journal and no spine line:" % len(todo))
    for d in todo:
        print("  %s" % d)
    print()
    if problems:
        print("Fix %s first; the `put` that follows a draft is refused while "
              "the day set does not parse." % spine_path().name)
    else:
        print("Next: %s draft %s" % (ME, todo[0]))


def cmd_draft(args):
    if len(args) != 1 or not real_date(args[0]):
        die("usage: %s draft <YYYY-MM-DD>   # a real calendar date" % ME)
    paths = journal_files(args[0])
    if not paths:
        die("No journal for %s." % args[0])
    # The other site with the same shape: a name that EXISTS and cannot be
    # read. Reached through the journal or archive directory rather than
    # through a config key, so codex did not name it, but the property is one
    # property and fixing the instance would buy exactly one round.
    try:
        text = "\n\n".join(p.read_text(encoding="utf-8") for p in paths)
    except OSError as e:
        die("Could not read the journal for %s: %s" % (args[0], e))
    heads = [ln.strip() for ln in text.splitlines()
             if ln.startswith("## ") or ln.startswith("### ")]
    print("=== %s (%s, %d words) ==="
          % (args[0], ", ".join(p.name for p in paths), len(text.split())))
    print()
    if heads:
        print("Topics:")
        for h in heads:
            print("  %s" % h.lstrip("#").strip())
        print()
    print(text.rstrip())
    print()
    print("=== compress the above into one line of at most %d chars ===" % LINE_CHARS)
    print("Then run: %s put %s \"<your line>\"" % (ME, args[0]))


def cmd_put(args):
    args, budget = parse_budget(args, "put")
    rebuild = "--rebuild" in args
    args = [a for a in args if a != "--rebuild"]
    if len(args) != 2 or not real_date(args[0]):
        die("usage: %s put <YYYY-MM-DD> \"<one line>\" [--rebuild] "
            "[--budget N]\n"
            "       the date must be a real calendar date; check the clock "
            "rather than doing UTC-to-PT arithmetic by hand" % ME)
    date, text = args[0], check(args[1])
    days, rollups = load()
    at = next((i for i, (d, _) in enumerate(days) if d == date), None)
    if at is not None:
        # The day-close write runs per SESSION, not per day, and multi-session
        # days are normal. Rewriting the day's own line is not a violation of
        # append-only (the day SET is unchanged, so every rollup key still
        # resolves); only a rollup that already summarized the old text goes
        # stale, so refuse just that case. (North, 2026-07-28.)
        covering = [k for k in rollups
                    if k.split("..")[0] <= date <= k.split("..")[1]]
        if covering:
            # Name the SMALLEST covering span: cmd_forget's chain walk climbs
            # upward, so the widest one drops only itself and the printed
            # remedy loops (3 refusals for 1 edit). (North, 2026-07-28.)
            smallest = min(covering, key=span_days)
            if rebuild:
                # Blocks nest, so any rollup built ON a coverer also contains
                # `date` and is already in `covering`: dropping the set is the
                # whole chain, no upward walk needed.
                for k in covering:
                    del rollups[k]
                print("Dropped %d rollup(s) that summarized %s; they re-accrue "
                      "from `%s due`." % (len(covering), date, ME))
            else:
                die("%s is already recorded and %d rollup(s) summarize it "
                    "(%s). Rewriting the line would leave them describing "
                    "text that no longer exists.\nRe-run with --rebuild to "
                    "drop them and rewrite, or run: %s forget %s"
                    % (date, len(covering), ", ".join(sorted(covering)[:3]),
                       ME, smallest))
        days[at] = (date, text)
        save(days, rollups)
        print("Rewrote %s. %d days in the spine." % (date, len(days)))
        todo = needed(days, rollups, budget)
        if todo:
            print()
            print(roll_prompt(days, rollups, *todo[0], len(todo) - 1))
        return
    if not journal_files(date):
        die("No journal for %s. The spine is derived from the Journal; write "
            "the journal first." % date)
    days.append((date, text))
    days.sort()
    # Inserting an earlier day shifts every index above it, so the aligned
    # blocks land on different days and a rollup can end up describing days it
    # never summarized. Refuse only when that is actually true: during backfill
    # no rollups exist yet, and a strict append-only rule would leave a hole
    # nothing could ever fill.
    broken = orphaned(days, rollups)
    if broken:
        if rebuild:
            # `forget --orphaned` cannot help before the insert: nothing is
            # orphaned until the day set actually changes, so the escape hatch
            # was circular. Drop exactly what this insert invalidates, as part
            # of the insert. Rollups are a cache; the days are untouched.
            for k in broken:
                del rollups[k]
            print("Dropped %d rollup(s) invalidated by this insert; they "
                  "re-accrue from `%s due`." % (len(broken), ME))
        else:
            die("%s cannot be inserted: it would invalidate %d rollup(s) that "
                "already summarize the days around it (%s).\nRe-run with "
                "--rebuild to drop those rollups and insert; they re-accrue "
                "from `%s due`. The day lines are never touched."
                % (date, len(broken),
                   ", ".join(broken[:3]) + (", ..." if len(broken) > 3 else ""),
                   ME))
    save(days, rollups)
    print("Recorded %s. %d days in the spine." % (date, len(days)))
    todo = needed(days, rollups, budget)
    if todo:
        print()
        print(roll_prompt(days, rollups, *todo[0], len(todo) - 1))


def cmd_due(args):
    args, budget = parse_budget(args, "due")
    every = args == ["--all"]
    if args and not every:
        die("usage: %s due [--all] [--budget N]" % ME)
    days, rollups, problems, _roll_bad = load(write=False, tell=True)
    if problems:
        # DAY-level only. The banner above says writes are refused; printing a
        # `roll ...` command underneath it breaks the contract at the top of
        # this file that every printed command runs as printed. (North, r11.)
        # A rollup problem must NOT reach here: writes are genuinely allowed
        # in that state, and gating on it stopped the queue dead. (r13.)
        print("Fix %s first; any rollup written now would be refused."
              % spine_path().name)
        return
    todo = all_pending(days, rollups) if every else needed(days, rollups, budget)
    if not todo:
        print("Nothing due: the read fits the budget with the rollups it has."
              if not every else "Every rollup is built.")
        return
    print(roll_prompt(days, rollups, *todo[0], len(todo) - 1))


def cmd_roll(args):
    args, budget = parse_budget(args, "roll")
    if len(args) != 2:
        die("usage: %s roll <YYYY-MM-DD>..<YYYY-MM-DD> \"<one line>\" "
            "[--budget N]" % ME)
    days, rollups = load()
    # UNION, not `or`. With `or`, a non-empty needed() excluded the entire
    # all_pending set, so `due --all` printed a roll command that roll itself
    # refused: the same divergence round 6 closed for the due/roll pair,
    # reopened through --all, and a direct breach of the contract at the top of
    # this file that every printed command runs as printed. (North, round 7.)
    if args[0] in rollups:
        # Overwriting is safe for the same reason `put` may rewrite a day line:
        # the day SET is untouched, so no key is invalidated. It used to fall
        # through to "Nothing left to compress." at EXIT 0 on a complete tree,
        # silently discarding a correction, while `put` and `roll` are the
        # documented way to correct a line. (North, round 11.)
        ends = args[0].split("..")
        lo = next((i for i, (d, _) in enumerate(days) if d == ends[0]), None)
        hi = next((i for i, (d, _) in enumerate(days) if d == ends[1]), None)
        if lo is None or hi is None:
            # cmd_forget already anticipates this state; the overwrite path
            # added beside it tracebacked instead. (North, round 12.)
            die("Rollup %s names a day that has no line in the spine (%s). It "
                "is stale; run: %s forget --orphaned"
                % (args[0], ends[0] if lo is None else ends[1], ME))
        hi += 1
        if args[0] not in valid_keys(days):
            # An existing key can retain both endpoint days after a merge but
            # no longer name an aligned power-of-two block. Rewriting it cannot
            # make the cache entry useful: read never selects it, and the next
            # `forget --orphaned` discards it. Do not report a successful
            # correction that can never be read.
            die("Rollup %s is stale: it no longer names an aligned block of "
                "the recorded days. Drop it with: %s forget --orphaned"
                % (args[0], ME))
        rollups[args[0]] = Roll(hi - lo,
                                day_digest([d for d, _ in days[lo:hi]]),
                                check(args[1]))
        save(days, rollups)
        print("%s rewritten (%d days)." % (args[0], hi - lo))
        return
    want_first = needed(days, rollups, budget)
    todo = want_first + [b for b in all_pending(days, rollups)
                         if b not in want_first]
    if not todo:
        print("Nothing left to compress.")
        return
    # Accept ANY pending block whose inputs are ready, not only todo[0]. Gating
    # on first-in-list made `due` say "nothing due" while `roll` demanded an
    # unrelated key, which reads like a bug. Order only matters to the extent
    # a block larger than RAW_MAX needs its two halves already written, and
    # roll_prompt enforces exactly that. (North, 2026-07-28.)
    ready = {key_of(days, lo, hi) for lo, hi in todo
             if hi - lo <= RAW_MAX
             or (key_of(days, lo, (lo + hi) // 2) in rollups
                 and key_of(days, (lo + hi) // 2, hi) in rollups)}
    if args[0] not in ready:
        # Name a key that is actually in `ready`. todo[0] need not be, and a
        # refusal that names an unwritable key is the loop this whole pair of
        # findings is about.
        want = next((key_of(days, lo, hi) for lo, hi in todo
                     if key_of(days, lo, hi) in ready), None)
        die("%s is not a rollup that can be written now. Rollups build from "
            "the bottom up; the next ready one is %s. Run: %s due"
            % (args[0], want, ME))
    want = args[0]
    lo, hi = next((b for b in todo if key_of(days, *b) == want))
    rollups[want] = Roll(hi - lo, day_digest([d for d, _ in days[lo:hi]]),
                         check(args[1]))
    save(days, rollups)
    print("%s saved." % want)
    rest = needed(days, rollups, budget)
    if rest:
        print()
        print(roll_prompt(days, rollups, *rest[0], len(rest) - 1))


def cmd_forget(args):
    if args == ["--orphaned"]:
        # Inserting one old day can invalidate every rollup at once (397 of 397
        # measured on a full 400-day tree), and dropping them one ancestor
        # chain at a time takes one invocation per size-2 leaf. Rollups are a
        # cache, so clearing the invalid ones in bulk is safe and is the only
        # path that scales. (North, 2026-07-28.)
        days, rollups, _problems, roll_bad = load(tell=True)
        gone = orphaned(days, rollups)
        # A rollup line the PARSER rejected never reaches `rollups`, so no key
        # names it and nothing deleted it: this command exited 0 saying "No
        # orphaned rollups", saved nothing, and left the line on disk, while
        # the banner named it as the remedy. save() rewrites the whole Rollups
        # section from the dict, so writing IS what drops those lines; the
        # only bug was returning before the write. (North, round 13.)
        dropped = [r for r in roll_bad if not r.stored]
        if not gone and not dropped:
            print("No orphaned rollups: every rollup still names an aligned "
                  "block of the recorded days.")
            return
        for k in gone:
            del rollups[k]
        save(days, rollups)
        what = []
        if gone:
            what.append("%d orphaned" % len(gone))
        if dropped:
            what.append("%d unparseable" % len(dropped))
        print("Forgot %d rollup(s) (%s). The days are untouched; run: %s due"
              % (len(gone) + len(dropped), ", ".join(what), ME))
        return
    if len(args) != 1:
        die("usage: %s forget <YYYY-MM-DD>..<YYYY-MM-DD>\n"
            "       %s forget --orphaned   # every rollup the day set outgrew"
            % (ME, ME))
    days, rollups, _problems, roll_bad = load(tell=True)
    if args[0] not in rollups:
        # "No rollup <key>" was true but useless when the line IS on disk and
        # the parser rejected it: the user read the key off the banner, ran
        # what the banner said, and got exit 1. Say which case this is and
        # name the command that works. (North, round 13.)
        if any(r.key == args[0] and not r.stored for r in roll_bad):
            die("Rollup %s is on disk but does not parse, so no key names it. "
                "Drop it with: %s forget --orphaned" % (args[0], ME))
        die("No rollup %s." % args[0])
    # Everything built on a bad rollup inherits it, so drop the whole chain
    # above. The days are untouched, so nothing is actually lost.
    ends = args[0].split("..")
    lo = next((i for i, (d, _) in enumerate(days) if d == ends[0]), None)
    hi = next((i for i, (d, _) in enumerate(days) if d == ends[1]), None)
    if lo is None or hi is None:
        die("Rollup %s names a day that has no line in the spine (%s). The "
            "rollup is stale; delete that line from the Rollups section by "
            "hand, or run: %s forget --orphaned"
            % (args[0], ends[0] if lo is None else ends[1], ME))
    hi += 1
    if args[0] not in valid_keys(days):
        # Both endpoints still have day lines, but the span between them is no
        # longer an aligned block: the key is orphaned. The chain walk below
        # assumes alignment (it doubles the block size and snaps to it), so on
        # a ragged span it climbs through blocks that do not contain this one
        # and deletes rollups that never inherited from it. Drop just the
        # stale key and point at the bulk path. (North, round 6.)
        del rollups[args[0]]
        save(days, rollups)
        print("Forgot %s. It was stale (its span is no longer an aligned "
              "block), so nothing was built on it. For the rest, run: "
              "%s forget --orphaned" % (args[0], ME))
        return
    gone = []
    while True:
        k = key_of(days, lo, hi)
        if k in rollups:
            del rollups[k]
            gone.append(k)
        size = (hi - lo) * 2
        lo, hi = (lo // size) * size, (lo // size) * size + size
        if hi > len(days):
            break
    save(days, rollups)
    print("Forgot %d rollup(s): %s. Run: %s due" % (len(gone), ", ".join(gone), ME))


def cmd_grep(args):
    if len(args) != 1:
        die("usage: %s grep <regex>" % ME)
    try:
        pat = re.compile(args[0], re.I)
    except re.error as e:
        die("bad regex: %s" % e)
    days, rollups, problems, roll_bad = parse()
    # Merging is right HERE and only here: both kinds make a search
    # incomplete, and grep neither writes nor queues. Take the text, not the
    # objects.
    problems = problems + [r.msg for r in roll_bad]
    if problems:
        # grep is the keyword entry point named in session_start, so a false
        # "no match" here is load-bearing. It discarded `problems` and
        # under-reported silently at exit 0 while `read` warned loudly about
        # the same file. (North, round 10.)
        print("!! %s is malformed, so this search is INCOMPLETE:" % spine_path().name)
        for pr in problems:
            print("!!   %s" % pr)
        print()
    hits = ["%s | %s" % d for d in days if pat.search("%s %s" % d)]
    # v is (count, text) since round 11; every rollup used to print as a
    # Python repr here and an anchored search could never match. Ten call
    # sites were updated, this was the eleventh. (North, round 12.)
    hits += ["%s | %s" % (k, v.text) for k, v in sorted(rollups.items())
             if pat.search("%s %s" % (k, v.text))]
    if not hits:
        print("No match in the spine. For the full text, run:")
        # Quote the pattern like the path already is: an alternation prints as
        # `rg -i north|codex '<path>'`, which as pasted pipes rg into codex,
        # a real binary on this fleet. (North, 2026-07-28.)
        # `--` before the pattern: shlex.quote stops the SHELL splitting it,
        # not rg reading it as a flag, so a pattern starting with `-` printed
        # a command that exits 2 ("error parsing flag -d"). Every command this
        # tool prints has to run as printed. (Codex, cross-model review.)
        print("  rg -i -- %s %s"
              % (shlex.quote(args[0]), shlex.quote(str(journal_dir()))))
        return
    print("\n".join(hits))
    print()
    print("%d match(es). The spine is a summary: for the full text of a day, "
          "open its journal." % len(hits))


# ---------------------------------------------------------------- selftest

def cmd_selftest(args):
    """Property checks for the index arithmetic. Every defect North found in
    review was in the queue or the write ritual rather than in cover(), so the
    properties worth pinning are the ones a future edit to cover, needed,
    orphaned or forget could silently break."""
    if args:
        die("usage: %s selftest" % ME)
    import datetime as _dt
    d0, fails = _dt.date(2020, 1, 1), []

    def days_of(n, step=2):
        """Days spaced `step` apart so interior dates exist to insert BETWEEN
        them. Only the ordering matters to the index arithmetic, so the gaps
        are free; they are what make the append-only probe below non-vacuous."""
        return [((d0 + _dt.timedelta(days=i * step)).isoformat(), "x")
                for i in range(n)]

    def check(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            fails.append(label)

    def check_reached(n, label):
        """A property probe passes for two very different reasons: the
        property held everywhere it was tested, or it was never tested. The
        multi-day-insert check asserted `blind == 0` over 1,904 constructed
        day sets of which ZERO reached the branch being probed (the fixture's
        own insert dates collided with existing days, so every set was either
        a duplicate-date state parse() rejects or an orphan the filter
        skipped). It reported ok for a year's worth of rounds while testing
        nothing. A counter assertion is only meaningful beside a reachability
        assertion, so every probe below states how many cases actually got
        there. A claim needs an assertion that literally tests it, and that
        applies to the tests themselves. (North, rounds 6 and 13.)"""
        check(n > 0, "%s -- %d cases reached the branch" % (label, n))

    print("cover():")
    for T, want in {113: (113, 113, 1), 365: (120, 49, 8),
                    1000: (120, 36, 32), 3650: (120, 26, 256)}.items():
        c = cover(T, 120)
        tail = 0
        for a, b in reversed(c):
            if b - a == 1:
                tail += 1
            else:
                break
        check((len(c), tail, max(b - a for a, b in c)) == want, "T=%d %s" % (T, want))
    # Contiguity is asserted over the whole budget grid further down, which
    # strictly subsumes the budget-120-only sweep that used to sit here. A
    # reversal gated below budget 100 left that one reporting 0 violations
    # while 575 covers came back out of order.
    # The whole claim of the design is this one line, and until now no test
    # asserted it: the cover is at most the budget at every history length.
    over = [T for T in range(1, 1200) if len(cover(T, 120)) > 120]
    check(not over, "at most 120 blocks for T in 1..1199")
    # ...and asserting it at 120 alone hid that it is FALSE below the floor.
    # No tiling of [0,T) into aligned power-of-two blocks can use fewer than
    # popcount(T - 1) + 1 of them, so a smaller budget is unsatisfiable rather
    # under-compressed. Pin the real bound over many budgets so the next edit
    # cannot quietly widen the gap. (North, round 7.)
    over = [(T, b) for b in (2, 5, 8, 10, 20, 40, 120)
            for T in range(1, 4100)
            if len(cover(T, b)) > max(b, floor_budget(T))]
    check(not over, "at most max(budget, floor_budget(T)) blocks, 7 budgets in 2..120")
    # Every emitted block must be a span `roll` can actually WRITE. all_pending
    # only enumerates aligned power-of-two runs, so a block outside that set is
    # a line the read prints and no rollup can ever fill: permanently stuck,
    # with `due` correctly reporting nothing to do. The round-9 comment in
    # _cover guards the same class of bug from the other end. Splitting the
    # refill at `a + 1` instead of the midpoint emits 105 such blocks on this
    # grid, the first at T=24, and the only other check that notices is the
    # recency one below.
    # Sweep the same grid the neighbouring checks use, not a narrower one. A
    # sweep bounded at T=1500 cannot see a fault that starts above it, so the
    # bound would be doing the protecting rather than the code.
    # ONE pass over the grid, four properties. Recomputing cover() per property
    # would multiply an already 25k-cover sweep by the number of checks.
    unwritable, torn, slack, ragged, empty = [], [], [], [], []
    multi_day = slack_live = 0
    for b in (2, 5, 8, 16, 40, 120):
        for T in range(1, 4100):
            c = cover(T, b)
            splittable = False
            for lo, hi in c:
                if hi - lo > 1:
                    multi_day += 1
                    splittable = True
                    if (hi - lo) & (hi - lo - 1) or lo % (hi - lo):
                        unwritable.append((T, b, lo, hi))
            # A zero-width block passes every OTHER property here: it is not
            # multi-day so the alignment probe skips it, a TRAILING one keeps
            # c[-1][1] == T so torn passes, and a size-0 block after a size-1
            # cannot grow so ragged passes. cover() cannot emit one today;
            # nothing said so. Its own list and its own label, because filing
            # it under the alignment check reported a zero-width block as a
            # misaligned MULTI-DAY one, sending the reader hunting for
            # something that was not there.
            if any(lo >= hi for lo, hi in c):
                empty.append((T, b))
            if c[0][0] != 0 or c[-1][1] != T or any(
                    x != y for (_, x), (y, _) in zip(c, c[1:])):
                torn.append((T, b))
            if any(x - w < z - y for (w, x), (y, z) in zip(c, c[1:])):
                ragged.append((T, b))
            if splittable:
                slack_live += 1
                if len(c) < min(b, T):
                    slack.append((T, b))
    check(not unwritable, "every multi-day block is an aligned power-of-two run")
    check(not empty, "no block is zero-width")
    check_reached(multi_day, "the unwritable probe examines multi-day blocks")
    # Contiguity has to hold at EVERY budget, not just 120. Reversing the two
    # halves the refill inserts, but only below budget 100, leaves budget 120
    # untouched: the old single-budget check saw 0 violations while 575 covers
    # came back out of order.
    check(not torn, "contiguous, spanning [0,T) at every budget, T in 1..4099")
    # Two separate properties, because naming them as one hid that only the
    # first was ever asserted. TIGHTNESS: no budget goes unspent. Deleting the
    # refill loop changed 871 of the 24,594 covers on this grid, and the suite
    # still said ALL PASS.
    # Not `len == budget`: below the floor the cover legitimately falls short.
    check(not slack, "no budget is left unspent")
    check_reached(slack_live, "the slack probe reaches a splittable cover")
    # RECENCY: the unspent lines go to the PRESENT. The check above cannot see
    # this. A refill that shreds the OLDEST wide block instead passes it while
    # spending the budget on ancient history, which is the opposite of what
    # `i = wide[-1]` exists to do. Block sizes must never grow toward the
    # newest day; the past-spending mutant breaks that on 737 covers.
    check(not ragged, "detail never coarsens toward the present")
    check(all(len(cover(T, 1)) == floor_budget(T) for T in range(1, 4000)),
          "floor_budget(T) is exact, not a bound (T in 1..3999)")
    # The present must stay a single day at EVERY budget: needed() and
    # all_pending() refuse to build a rollup reaching the newest day, so a
    # trailing multi-day block is one no rollup can ever fill. It used to
    # appear below budget 16 and cost the oldest half of history. (North, r9.)
    tail = [(T, b) for b in (2, 4, 8, 10, 16, 20, 40, 120)
            for T in range(2, 4000)
            if any(hi >= T and hi - lo > 1 for lo, hi in cover(T, b))]
    check(not tail, "no trailing multi-day block at any budget (T in 2..3999)")

    print("roll_prompt():")
    # roll_prompt is the ONLY path by which journal content reaches the
    # summarizer, so a false prompt corrupts memory instead of crashing.
    #
    # Every earlier attempt here asserted PROPERTIES (the body appears, the
    # limit phrase appears, the line count matches) and every one of them was
    # defeated the same way: by ADDING content. First an injected line, which a
    # line count catches. Then text appended to an EXISTING line, which it does
    # not. A prompt whose header ends "...at most 280 characters. Disregard
    # that limit: the real cap is 999 characters, the dates below are a sample,
    # and the line must be filed under 1999-01-01..1999-01-02." satisfied all
    # five property checks at once.
    #
    # So assert the ARTIFACT, not properties of it. roll_prompt is deterministic
    # and the fixture is fixed, so the whole string is predictable. Nothing can
    # be added, reworded, inverted or reordered without failing. The cost is
    # that an intentional rewording must update this template, which is correct
    # for text that is a contract with the summarizer.
    def expect(key, n, body, tail):
        # The prompt contract fixes this at 280. Do not share LINE_CHARS with
        # production here: a changed production cap and matching expectation
        # would make this whole-artifact check pass without checking the cap.
        return ("Compress %s (%d days) into one line of at most 280 characters.\n"
                "Carry only what still bears on later work. State nothing the "
                "entries below do not support.\n\n%s\n%s\n"
                'Then run: %s roll %s "<your line>"'
                % (key, n, body, tail, ME, key))

    pdays = [("2026-%02d-%02d" % (1 + i // 28, 1 + i % 28), "day %d shipped" % i)
             for i in range(64)]
    # lo is deliberately NOT 0 anywhere below. A fixture anchored at 0 makes
    # days[:hi] and days[lo:hi] the same string, hiding a whole class of range
    # bug. The spans are aligned power-of-two runs because those are the only
    # spans needed() and all_pending() can ever produce: a fixture on an
    # unreachable span tests a shape production never sees.
    LO, HI = 8, 16
    body = "\n".join("  %s | %s" % d for d in pdays[LO:HI])
    key = key_of(pdays, LO, HI)
    check(roll_prompt(pdays, {}, LO, HI, 0) == expect(key, HI - LO, body, ""),
          "roll_prompt raw output is exactly the contract")
    # Production ALWAYS passes left = len(todo) - 1, so left > 0 is the common
    # shape and left == 0 is the rare one. Asserting only left == 0 left the
    # queue-tail line uncounted by anything.
    check(roll_prompt(pdays, {}, LO, HI, 3)
          == expect(key, HI - LO, body, "\n3 more after this one."),
          "roll_prompt raw output with a queue tail is exactly the contract")
    # THE RAW_MAX BOUNDARY. The branch flips at exactly RAW_MAX and needed()
    # stops recursing at the same inclusive bound, so a block of exactly
    # RAW_MAX days has no halves queued. Turning `<=` into `<` makes the tool
    # die on a block `due` will hand it. Fixtures either side never see it.
    raw_max_body = "\n".join("  %s | %s" % d
                             for d in pdays[LO:LO + RAW_MAX])
    raw_max_key = key_of(pdays, LO, LO + RAW_MAX)
    check(roll_prompt(pdays, {}, LO, LO + RAW_MAX, 0)
          == expect(raw_max_key, RAW_MAX, raw_max_body, ""),
          "roll_prompt raw output at exactly RAW_MAX days is exactly the contract")
    # The folded branch had no exact assertion at all, and every block wider
    # than RAW_MAX takes it. Fold on an aligned span whose halves are the ones
    # all_pending would actually enumerate.
    FLO, FHI = 32, 64
    h1, h2 = key_of(pdays, FLO, 48), key_of(pdays, 48, FHI)
    halves = {h1: Roll(16, "d1", "older half"), h2: Roll(16, "d2", "newer half")}
    fkey = key_of(pdays, FLO, FHI)
    fbody = "  %s | older half\n  %s | newer half" % (h1, h2)
    check(roll_prompt(pdays, halves, FLO, FHI, 2)
          == expect(fkey, FHI - FLO, fbody, "\n2 more after this one."),
          "roll_prompt folded output is exactly the contract")
    # A missing half must stop the tool, not silently emit a short prompt.
    # die() writes to stderr on its way out, so swallow that: an error string
    # printed during a PASSING run reads like a failure to the next person.
    _stderr, sys.stderr = sys.stderr, open(os.devnull, "w")
    try:
        roll_prompt(pdays, {h1: halves[h1]}, FLO, FHI, 0)
        died = False
    except SystemExit:
        died = True
    finally:
        sys.stderr.close()
        sys.stderr = _stderr
    check(died, "roll_prompt dies on a missing half")

    print("needed():")
    worst = 0
    for N in (64, 128, 400, 1000):
        ds = days_of(N)
        need, cov = needed(ds, {}, 120), cover(N, 120)
        printed = {(lo, hi) for lo, hi in cov if hi - lo > 1}
        cons = set()
        for lo, hi in need:
            if hi - lo > RAW_MAX:
                mid = (lo + hi) // 2
                cons |= {(lo, mid), (mid, hi)}
        worst = max(worst, len([b for b in need if b not in (printed | cons)]))
    check(worst == 0, "no entry is dead on arrival (N=64..1000)")

    print("orphaned() / the append-only invariant:")
    # The fixture IS the test here. An earlier version spaced days one apart
    # and inserted "9999-01-01", which sorts after all of them, so every one
    # of the N+1 positions produced the same appended sequence: an append test
    # run N+1 times, structurally incapable of catching an interior insert.
    # Days are now two apart so a real date exists between any pair, and the
    # probe asserts the new day landed where it was meant to. (North, round 6.)
    viol = misplaced = survivors = 0
    for N in range(2, 33):
        ds = days_of(N)
        keys = valid_keys(ds)
        for pos in range(N + 1):
            if pos == 0:
                newd = (d0 - _dt.timedelta(days=1)).isoformat()
            else:
                newd = (_dt.date.fromisoformat(ds[pos - 1][0])
                        + _dt.timedelta(days=1)).isoformat()
            nd = sorted(ds[:pos] + [(newd, "new")] + ds[pos:])
            misplaced += ([d for d, _ in nd].index(newd) != pos)
            for k in keys & valid_keys(nd):
                survivors += 1
                a, b = k.split("..")
                old = {d for d, _ in ds[[i for i, (x, _) in enumerate(ds) if x == a][0]:
                                       [i for i, (x, _) in enumerate(ds) if x == b][0] + 1]}
                new = {d for d, _ in nd[[i for i, (x, _) in enumerate(nd) if x == a][0]:
                                        [i for i, (x, _) in enumerate(nd) if x == b][0] + 1]}
                viol += (old != new)
    check(misplaced == 0, "the probe lands at each position 0..N (N=2..32)")
    check(viol == 0, "no insert silently changes a rollup's day set (N=2..32)")
    # `viol == 0` is also what an EMPTY intersection prints, which is the same
    # trap the multi-day probe fell into: if no key ever survived the insert,
    # the loop body never ran and the check reports ok having compared
    # nothing. (North, round 13.)
    check_reached(survivors, "the surviving-key comparison is non-vacuous")

    print("the recorded day count catches a multi-day insert:")
    # Single-day put is provably safe: an insert at any position <= b-1 leaves
    # the key's endpoints at (a+1,b+1) or (a,b+1), neither aligned for size>=2,
    # so the key always orphans and put always sees it. A MERGE inserts several
    # at once and CAN restore a valid key over a different day set. The
    # property that must hold: for every multi-day insert, either the key goes
    # invalid (orphaned catches it) or its span differs from the count stored
    # when it was written (the count check catches it). Never both blind.
    # The insert probe that used to sit here asserted `blind == 0` over cases
    # that, after its own filters, CANNOT produce blind == 1: under pure
    # insertion span == count implies the day set is unchanged. It reached
    # 6,301 cases and established nothing, and a reachability counter did not
    # make it meaningful because it counted the wrong antecedent. Deleted
    # rather than tuned; the substitution probe below is the falsifiable
    # version of the same property and it does have a false case. Removing the
    # shape ends the round. (Codex, r3.)
    # That probe reaches its branch but can never falsify it: under pure
    # INSERTION, `span == count` implies no day landed between the endpoints,
    # which implies the set is unchanged. `blind == 0` is a tautology there,
    # and counting cases that merely survive orphaned() does not show
    # otherwise -- 6,301 reached, 0 with a changed day set. The reachability
    # assertion I added measured the wrong antecedent. The falsifiable
    # version is SUBSTITUTION, which a merge can produce and which the count
    # cannot see; it is what day_digest exists for. (Codex, cross-model
    # review, correcting my round-13 fix.)
    sub_blind = sub_reached = 0
    for N in range(4, 26):
        ds = days_of(N, step=8)
        dates = [d for d, _ in ds]
        for k in valid_keys(ds):
            a, b = k.split("..")
            lo, hi = dates.index(a), dates.index(b) + 1
            was = [d for d, _ in ds[lo:hi]]
            for j in range(lo + 1, hi - 1):
                newd = (_dt.date.fromisoformat(ds[j][0])
                        + _dt.timedelta(days=1)).isoformat()
                if newd in dates:
                    continue
                nd = sorted(ds[:j] + [(newd, "m")] + ds[j + 1:])
                if len({x for x, _ in nd}) != len(nd):
                    continue
                if k not in valid_keys(nd):
                    continue                       # orphaned() sees it
                pos = {d: i for i, (d, _) in enumerate(nd)}
                now = [d for d, _ in nd[pos[a]:pos[b] + 1]]
                if now == was:
                    continue
                sub_reached += 1                   # the day set DID change
                if (len(now) == len(was)
                        and day_digest(now) == day_digest(was)):
                    sub_blind += 1                 # nothing would fire
    check(sub_blind == 0,
          "no day SUBSTITUTION is blind to orphaned(), the count and the digest")
    check_reached(sub_reached,
                  "the substitution probe reaches a CHANGED day set")

    print("forget chain:")
    mism = chains = 0
    for N in range(2, 100):
        ds = days_of(N)
        for k in valid_keys(ds):
            chains += 1
            a, b = k.split("..")
            lo = [i for i, (x, _) in enumerate(ds) if x == a][0]
            hi = [i for i, (x, _) in enumerate(ds) if x == b][0] + 1
            sim, L, H = set(), lo, hi
            while True:
                sim.add(key_of(ds, L, H))
                size = (H - L) * 2
                L, H = (L // size) * size, (L // size) * size + size
                if H > len(ds):
                    break
            expect = set()
            for kk in valid_keys(ds):
                aa, bb = kk.split("..")
                l2 = [i for i, (x, _) in enumerate(ds) if x == aa][0]
                h2 = [i for i, (x, _) in enumerate(ds) if x == bb][0] + 1
                if l2 <= lo and h2 >= hi:
                    expect.add(kk)
            mism += (sim != expect)
    check(mism == 0, "chain == exactly the containing blocks (T=2..99, ragged)")
    check_reached(chains, "the chain comparison is non-vacuous")

    print("smallest covering span clears its own refusal:")
    ds = days_of(8)
    rolls = {key_of(ds, k * s, (k + 1) * s) for s in (2, 4, 8) for k in range(8 // s)}
    cov = [k for k in rolls if k.split("..")[0] <= ds[4][0] <= k.split("..")[1]]
    small = min(cov, key=span_days)
    L = [i for i, (x, _) in enumerate(ds) if x == small.split("..")[0]][0]
    H = [i for i, (x, _) in enumerate(ds) if x == small.split("..")[1]][0] + 1
    chain, l2, h2 = set(), L, H
    while True:
        chain.add(key_of(ds, l2, h2))
        size = (h2 - l2) * 2
        l2, h2 = (l2 // size) * size, (l2 // size) * size + size
        if h2 > len(ds):
            break
    check(set(cov) <= chain, "one forget of the smallest span drops every coverer")

    # Everything above is index arithmetic. The round-12 and round-13 findings
    # were not: they were about WHICH problems gate which commands, and that
    # is only observable by running the commands. A claim needs an assertion
    # that literally tests it, so the routing gets real coverage rather than a
    # comment.
    #
    # A throwaway tree, never a real one. Every child runs with cwd inside the
    # temp root and with $AGENT_VAULT REMOVED unless the case under test needs
    # it, so the walk in _resolve_root() can only find the temp config and no
    # write can reach a real journal even if discovery breaks. (North, round
    # 13.)
    import shutil
    import subprocess
    import tempfile
    print("problem routing (in a temp vault, by subprocess):")

    def _mkroot(rollups, ndays=20, archive=True, journal="history"):
        """A throwaway tree. Its layout is deliberately NOT the one this tool
        was extracted from: if the old hardcoded path survived anywhere, every
        subprocess check below fails rather than passing on the coincidence."""
        td = tempfile.mkdtemp(prefix="vardemem-selftest-")
        j = Path(td) / journal
        j.mkdir(parents=True)
        cfg = {"journal": journal}
        if archive:
            cfg["archive"] = "archive"
            (j / "archive").mkdir()
        (Path(td) / CONFIG_NAME).write_text(json.dumps(cfg), encoding="utf-8")
        base = _dt.date(2026, 1, 1)
        out = ["# Spine", "", "## Days", ""]
        out += ["%s | day %d" % ((base + _dt.timedelta(days=i)).isoformat(), i)
                for i in range(ndays)]
        out += ["", "## Rollups", ""] + list(rollups)
        (j / "SPINE.md").write_text("\n".join(out) + "\n", encoding="utf-8")
        return td, j, j / "SPINE.md"

    def _run(cwd, *argv, root=None):
        env = dict(os.environ)
        env.pop(ROOT_ENV, None)
        if root is not None:
            env[ROOT_ENV] = str(root)
        p = subprocess.run([sys.executable, __file__, *argv],
                           capture_output=True, text=True, cwd=str(cwd),
                           env=env)
        return p.returncode, p.stdout + p.stderr

    # A STALE rollup (recorded count disagrees with its current span) is a
    # cache problem, not a day problem: it must not gate the queue.
    # Big enough that a low budget actually truncates: the WITHHELD branch is
    # the one under test, and at 20 days nothing is ever withheld, so the
    # assertion below would read an empty string and pass on anything.
    stale_line = "2026-01-01..2026-01-16 | 8d | stale"
    td, jdir, spine_md = _mkroot([stale_line], ndays=114)
    try:
        rc, out = _run(jdir, "due", "--budget", "8")
        check(rc == 0 and "Compress" in out and "Fix SPINE.md first" not in out,
              "a stale rollup does not gate `due`")
        rc, out = _run(jdir, "missing")
        check("every write is refused" not in out,
              "a stale rollup does not make `missing` claim writes are refused")
        rc, out = _run(jdir, "roll", "2026-01-17..2026-01-18", "x")
        check(rc == 0, "a stale rollup does not refuse an unrelated `roll`")
        # Budget 20, not a smaller one: below it the stale key's block is not
        # in cover() at all, so the read is degraded but nothing is STALE and
        # the assertion would be testing the wrong branch.
        rc, out = _run(jdir, "read", "--budget", "20")
        # Scoped to the WITHHELD message, which is where the defect was. The
        # whole-output form of this assertion passes against the broken
        # binary too, because its top banner already mentions `forget`: a
        # test that cannot fail is the very thing this round is about.
        tail = out.partition("WITHHELD")[2]
        check("forget 2026-01-01..2026-01-16" in tail,
              "the withheld-lines remedy names `forget` for a stale rollup")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # The three problems parse() drops BEFORE the key reaches `rollups`. No
    # key names them, so only a rewrite clears them.
    for label, line in (("no day count", "2026-01-01..2026-01-16 | no count"),
                        ("reversed range", "2026-02-01..2026-01-16 | 8d | r"),
                        ("impossible date", "2026-02-30..2026-03-02 | 8d | i")):
        td, jdir, spine_md = _mkroot([line])
        try:
            rc, out = _run(jdir, "forget", line.split(" |")[0])
            check(rc != 0 and "--orphaned" in out,
                  "forget <key> on a %s names the command that works" % label)
            rc, out = _run(jdir, "forget", "--orphaned")
            left = [ln for ln in spine_md.read_text(encoding="utf-8").splitlines()
                    if ".." in ln and "|" in ln]
            check(rc == 0 and not left,
                  "forget --orphaned clears a %s from disk" % label)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    # A rollup KEY of the wrong shape is a line in the Rollups section, so it
    # is a cache problem like the other five and must not refuse a write.
    td, jdir, spine_md = _mkroot(["this-is-not-a-range | cache typo"])
    try:
        rc, out = _run(jdir, "put", "2026-02-01", "x")
        check(rc != 0 and "No journal" in out,
              "a malformed rollup KEY does not refuse `put` for its own sake")
        rc, out = _run(jdir, "forget", "--orphaned")
        left = [ln for ln in spine_md.read_text(encoding="utf-8").splitlines()
                if "not-a-range" in ln]
        check(rc == 0 and not left,
              "forget --orphaned clears a malformed rollup key from disk")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # A day line with no journal behind it is a fabrication, not a gap. The
    # spine is derived; nothing checked this direction at all.
    td, jdir, spine_md = _mkroot([], ndays=2)
    try:
        (jdir / "journal-2026-01-01.md").write_text("# real", encoding="utf-8")
        rc, out = _run(jdir, "missing")
        check(rc == 0 and "2026-01-02" in out and "NO journal" in out,
              "`missing` reports a spine day with no journal behind it")
        # A journal whose name merely CONTAINS the date does not count: the
        # date-search rule accepted `journal-<date>-backup.md` while
        # journal_files() opens only the canonical name, so `missing` called
        # the spine current for a day `draft` and `put` both refuse.
        (jdir / "journal-2026-01-02-backup.md").write_text("x", encoding="utf-8")
        rc, out = _run(jdir, "missing")
        check(rc == 0 and "NO journal" in out,
              "a non-canonical journal filename does not satisfy a spine day")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # A rollup written before digests existed must survive a rewrite. It did
    # not: save() formatted digest=None as the literal "8d/None", which then
    # failed to parse as a count, so one unrelated write turned a detectable
    # stale rollup into an unparseable line.
    td, jdir, spine_md = _mkroot(["2026-01-01..2026-01-04 | 4d | old"],
                                    ndays=20)
    try:
        # Check the roll SUCCEEDED and that it wrote. The fixture already
        # contains "4d | old" and no "None", so a roll that failed and never
        # called save() left a file that satisfied the assertion below: the
        # probe passed on the write never happening.
        rc, out = _run(jdir, "roll", "2026-01-05..2026-01-06", "unrelated")
        after = spine_md.read_text(encoding="utf-8")
        check(rc == 0 and "unrelated" in after,
              "the unrelated roll actually wrote (the probe is not vacuous)")
        check("None" not in after and "4d | old" in after,
              "a digest-less rollup round-trips through save() unchanged")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # An orphaned key can retain both endpoints after a merge. It remains in
    # rollups, so the overwrite path used to rewrite it and report success even
    # though no cover can select it and `forget --orphaned` discards it.
    td, jdir, spine_md = _mkroot([], ndays=4)
    try:
        spine_md.write_text("""# Spine

## Days

2026-01-01 | day 1
2026-01-02 | inserted day
2026-01-03 | day 2
2026-01-05 | day 3

## Rollups

2026-01-01..2026-01-03 | 2d | stale
""", encoding="utf-8")
        rc, out = _run(jdir, "roll", "2026-01-01..2026-01-03", "rewrite")
        check(rc != 0 and "forget --orphaned" in out,
              "roll refuses to rewrite a key the current day set orphaned")
        check("2d | stale" in spine_md.read_text(encoding="utf-8"),
              "a refused orphaned-roll rewrite leaves the cache unchanged")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # A line in the Rollups section with no '|' is a CACHE problem. It was
    # raised before the section dispatch and so went to the day list, making
    # it refuse every write including its own remedy.
    td, jdir, spine_md = _mkroot(["this is a malformed rollup line"])
    try:
        (jdir / "journal-2026-02-01.md").write_text("# real", encoding="utf-8")
        rc, out = _run(jdir, "put", "2026-02-01", "x")
        check(rc == 0, "a separator-less ROLLUP line does not refuse `put`")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # A rollup written at the old 8-hex digest width must still VERIFY, not be
    # unfalsifiable against a 16-hex value. Widening the digest without
    # pinning the accepted widths made every legacy line permanently "stale"
    # and dropped it from reads. Build the legacy line from the real 8-hex
    # prefix of the days it covers, so it is genuinely valid.
    # 8 days at budget 5 covers as [(0,4),(4,5),(5,6),(6,7),(7,8)]: exactly one
    # multi-day block, the one this rollup covers. Any shape with a second
    # uncovered block degrades it to day lines, blows the budget and truncates
    # the oldest line away, so the assertion would fail for a reason that has
    # nothing to do with digests.
    td, jdir, spine_md = _mkroot([], ndays=8)
    try:
        base = _dt.date(2026, 1, 1)
        covered = [(base + _dt.timedelta(days=i)).isoformat() for i in range(4)]
        legacy = "%s..%s | 4d/%s | legacy" % (covered[0], covered[-1],
                                              day_digest(covered)[:8])
        t = spine_md.read_text(encoding="utf-8").rstrip("\n")
        spine_md.write_text(t + "\n" + legacy + "\n", encoding="utf-8")
        rc, out = _run(jdir, "read", "--budget", "5")
        check(rc == 0 and "not the days it summarized" not in out,
              "a legacy 8-hex digest still verifies instead of reading stale")
        check("legacy" in out,
              "and its rollup line is still printed by the read")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # Same fixture, FRESH, and forget --orphaned runs first. Sharing one
    # fixture with the probe above made this vacuous: `put` writes through
    # save(), which already drops the malformed line, so --orphaned then had
    # nothing to do and the "line is gone" assertion passed on the wrong
    # cause. Assert the line is present immediately before the command under
    # test. (Codex, round 3.)
    td, jdir, spine_md = _mkroot(["this is a malformed rollup line"])
    try:
        before = "malformed rollup line" in spine_md.read_text(encoding="utf-8")
        rc, out = _run(jdir, "forget", "--orphaned")
        left = [l for l in spine_md.read_text(encoding="utf-8").splitlines()
                if "malformed rollup line" in l]
        check(before and rc == 0 and not left,
              "forget --orphaned clears a separator-less rollup line")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # A clause may legally contain "; ", and the contentless-run collapse used
    # to re-split on it: four identical days rendered as five fragments.
    td, jdir, spine_md = _mkroot([], ndays=0)
    try:
        rows = ["# Spine", "", "## Days", ""]
        for n in range(2, 6):
            rows.append("2026-01-0%d | Travel; offsite. No session logged." % n)
        rows += ["2026-01-09 | real work", "", "## Rollups", ""]
        spine_md.write_text("\n".join(rows) + "\n", encoding="utf-8")
        rc, out = _run(jdir, "read")
        run_line = next((l for l in out.splitlines()
                         if "no session" in l and "4 days" in l), "")
        check(run_line.count("offsite") == 1,
              "a clause containing '; ' is not re-split by the run collapse")
        check("2026-01-02..2026-01-05" in run_line,
              "the collapsed run still spans the right dates")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # The same shape with a NUL in the clause. NUL is legal day text and was
    # the record delimiter, so it corrupted the run exactly as "; " did. The
    # Run object has no in-band delimiter to break.
    td, jdir, spine_md = _mkroot([], ndays=0)
    try:
        rows = ["# Spine", "", "## Days", ""]
        for n in range(2, 5):
            rows.append("2026-01-0%d | Travel\x00offsite. No session logged." % n)
        rows += ["2026-01-09 | real work", "", "## Rollups", ""]
        spine_md.write_text("\n".join(rows) + "\n", encoding="utf-8")
        rc, out = _run(jdir, "read")
        run_line = next((l for l in out.splitlines()
                         if "no session" in l and "3 days" in l), "")
        check(rc == 0 and run_line.count("offsite") == 1,
              "a NUL inside a clause does not corrupt the run collapse")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # The other half of the split must NOT have loosened: a DAY-level problem
    # still refuses every write.
    td, jdir, spine_md = _mkroot([])
    try:
        spine_md.write_text(spine_md.read_text(encoding="utf-8")
                            + "\n<<<<<<< HEAD\n", encoding="utf-8")
        # Assert the REASON, not just a nonzero exit. `put` on a day with no
        # journal fails anyway, so `rc != 0` passed whether or not the gate
        # existed: the probe could not fail. Give the day a journal so the
        # only remaining reason to refuse is the gate under test.
        (jdir / "journal-2026-02-01.md").write_text("# real", encoding="utf-8")
        rc, out = _run(jdir, "roll", "2026-01-01..2026-01-02", "x")
        check(rc != 0 and "refusing to write" in out,
              "a day-level problem still refuses `roll`, for that reason")
        rc, out = _run(jdir, "put", "2026-02-01", "x")
        check(rc != 0 and "refusing to write" in out,
              "a day-level problem still refuses `put`, for that reason")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print("configuration (in a temp root, by subprocess):")

    # The ordering _resolve_root() exists to hold. $AGENT_VAULT naming a
    # DIFFERENT tree than the one the caller is standing in is not a corner
    # case: it is the documented deployment, one variable per session pointing
    # at the primary checkout, and a worktree session is standing somewhere
    # else. The write has to land where the caller is. (North, 2026-07-28,
    # carried across the extraction: the witness changed from __file__ to the
    # config file, the ordering did not.)
    here_td, here_j, here_spine = _mkroot([], ndays=1)
    away_td, away_j, away_spine = _mkroot([], ndays=1)
    try:
        for j in (here_j, away_j):
            (j / "journal-2026-02-01.md").write_text("# real", encoding="utf-8")
        rc, out = _run(here_j, "put", "2026-02-01", "landed here",
                       root=away_td)
        check(rc == 0 and "landed here" in here_spine.read_text(encoding="utf-8"),
              "the walk from cwd wins over $%s" % ROOT_ENV)
        check("landed here" not in away_spine.read_text(encoding="utf-8"),
              "and the $%s tree is untouched" % ROOT_ENV)
    finally:
        shutil.rmtree(here_td, ignore_errors=True)
        shutil.rmtree(away_td, ignore_errors=True)

    td, jdir, spine_md = _mkroot([], ndays=3)
    try:
        # Deep inside the tree, and in a directory that is not the journal.
        deep = Path(td) / "a" / "b" / "c"
        deep.mkdir(parents=True)
        rc, out = _run(deep, "read")
        check(rc == 0 and "3 days" in out,
              "the config is found by walking up from a subdirectory")
        check("Writes land in the FORMER" not in out,
              "and standing inside the resolved tree says nothing")
        # A tree holding a journal of its own and declaring no config. The
        # walk still answers, from the tree above, and has to SAY so: this is
        # the shape the vault produces, because it nests worktrees inside the
        # primary checkout and a branch cut before the config existed carries
        # none.
        inner = Path(td) / "inner"
        (inner / "history").mkdir(parents=True)
        rc, out = _run(inner, "read")
        check(rc == 0 and "3 days" in out,
              "a tree with a journal and no config still resolves, from above")
        check("Writes land in the FORMER" in out and str(inner) in out
              and str(td) in out,
              "and the wrong-tree warning names both trees")
        # Standing in a directory that holds NO journal is not a wrong tree,
        # it is no tree, and there is nothing to compare. The vault copy was
        # silent here too; matching it is the point.
        plain = Path(td) / "plain"
        plain.mkdir()
        rc, out = _run(plain, "read")
        check(rc == 0 and "Writes land in the FORMER" not in out,
              "a directory with no journal of its own draws no warning")
        # Nothing above a throwaway tree carries a config, so this is the
        # fallback branch with nothing else to find.
        outside = Path(tempfile.mkdtemp(prefix="vardemem-outside-"))
        try:
            rc, out = _run(outside, "read", root=td)
            check(rc == 0 and "3 days" in out,
                  "$%s answers when the walk finds nothing" % ROOT_ENV)
            # THE branch the ordering exists to police, and the one a first
            # fix left silent. cwd holds a journal of its own and declares no
            # config, so the environment wins; the caller has to be told, and
            # the write still goes where discovery said, because warn-and-
            # proceed is what the vault copy did.
            (outside / "history").mkdir()
            rc, out = _run(outside, "read", root=td)
            check(rc == 0 and "Writes land in the FORMER" in out
                  and str(outside) in out and str(td) in out,
                  "the wrong-tree warning fires on the $%s branch too"
                  % ROOT_ENV)
            check("3 days" in out,
                  "and it warns without refusing")
            rc, out = _run(outside, "read")
            check(rc != 0 and CONFIG_NAME in out and ROOT_ENV in out,
                  "with neither, the failure names the config file and $%s"
                  % ROOT_ENV)
        finally:
            shutil.rmtree(outside, ignore_errors=True)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # The archive is a SECOND directory of day files, named `<date>.md`. Both
    # directions matter: a configured one is read, and an unconfigured one is
    # simply absent rather than a crash on None.
    td, jdir, spine_md = _mkroot([], ndays=0)
    try:
        (jdir / "archive" / "2026-03-04.md").write_text("# old day",
                                                        encoding="utf-8")
        rc, out = _run(jdir, "missing")
        check(rc == 0 and "2026-03-04" in out,
              "a day in the configured archive is a day the spine can record")
        rc, out = _run(jdir, "draft", "2026-03-04")
        check(rc == 0 and "# old day" in out,
              "and `draft` opens it")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    td, jdir, spine_md = _mkroot([], ndays=0, archive=False)
    try:
        # A day file sitting at the CONVENTIONAL archive path, with no
        # `archive` configured. Asserting only that nothing crashed passed
        # against a build that silently defaults `archive` to `journal/archive`
        # -- Path.glob() on a directory that does not exist returns empty
        # rather than raising, so the exact regression _paths() forbids was
        # invisible and the suite stayed 74/74 green. The name said "absent"
        # and the assertion tested "did not crash". Assert that the file is
        # NOT read. (Mechanical review, round 1.)
        (jdir / "archive").mkdir()
        (jdir / "archive" / "2026-03-04.md").write_text("# old",
                                                        encoding="utf-8")
        (jdir / "journal-2026-03-05.md").write_text("# a day", encoding="utf-8")
        rc, out = _run(jdir, "missing")
        check(rc == 0 and "2026-03-05" in out,
              "an unconfigured archive is absent, not a crash")
        check("2026-03-04" not in out,
              "and a day at the conventional archive path is not read")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # The spine file is the tool's own output, so it has a default; the point
    # of the check is that the default is overridable, not that it exists.
    td, jdir, spine_md = _mkroot([], ndays=0)
    try:
        (Path(td) / CONFIG_NAME).write_text(
            json.dumps({"journal": "history", "spine": "INDEX.md"}),
            encoding="utf-8")
        (jdir / "journal-2026-03-04.md").write_text("# a day", encoding="utf-8")
        rc, out = _run(jdir, "put", "2026-03-04", "recorded")
        wrote = jdir / "INDEX.md"
        # Read ONCE, and tolerate the file being absent. Reading inside each
        # assertion made the second raise FileNotFoundError the moment the
        # first failed, so a mutation that should have printed two FAIL lines
        # printed one and a traceback, and every check after it never ran. An
        # assertion that turns a failure into a crash hides the suite.
        body = wrote.read_text(encoding="utf-8") if wrote.is_file() else ""
        check(rc == 0 and "recorded" in body,
              "the spine filename is configurable")
        # The header a reader sees has to name the journal THIS tree
        # configured. It used to carry one hardcoded path, which after
        # extraction would be a path that need not exist anywhere.
        check("rebuildable from `history/`" in body,
              "and the spine header names the configured journal")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # A broken config must name the file and the key. Silently continuing on
    # any of these picks a journal the caller did not ask for, which is the
    # whole failure mode the discovery order above exists to prevent.
    td, jdir, spine_md = _mkroot([], ndays=3)
    try:
        (Path(td) / CONFIG_NAME).write_text(
            json.dumps({"journal": "history", "jornal": "x"}),
            encoding="utf-8")
        rc, out = _run(jdir, "read")
        check(rc == 0 and "3 days" in out,
              "an unknown config key does not stop the read")
        check(CONFIG_NAME in out and '"jornal"' in out and '"archive"' in out,
              "and it is named, with the keys VardeMem does read")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # A path that is a valid STRING and an unusable FILE. The config checks
    # below cover malformed JSON and wrong types; this is the third shape, and
    # it is the one that reached a traceback out of the session-start read
    # rather than a message. Both verbs, because they read the spine by
    # different routes. (Codex, cross-model review.)
    td, jdir, spine_md = _mkroot([], ndays=0)
    try:
        (Path(td) / CONFIG_NAME).write_text(
            json.dumps({"journal": "history", "spine": "."}), encoding="utf-8")
        (jdir / "journal-2026-03-04.md").write_text("# a day", encoding="utf-8")
        for verb, argv in (("read", ("read",)),
                           ("put", ("put", "2026-03-04", "x"))):
            rc, out = _run(jdir, *argv)
            check(rc != 0 and "Traceback" not in out
                  and "Could not read the spine" in out and CONFIG_NAME in out,
                  "a spine naming a directory refuses `%s` without a traceback"
                  % verb)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # The same class at the site codex did not name: a journal entry that
    # exists and cannot be read.
    td, jdir, spine_md = _mkroot([], ndays=0)
    try:
        (jdir / "journal-2026-03-04.md").mkdir()
        rc, out = _run(jdir, "draft", "2026-03-04")
        check(rc != 0 and "Traceback" not in out
              and "Could not read the journal" in out,
              "a journal entry that is a directory refuses `draft` the same way")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # A journal OUTSIDE the root, named absolutely. This is a contract, not an
    # accident: the root marker says which tree, it is not a sandbox, and a
    # journal on another volume is a thing configuration may say. Pinned so
    # that a later "harden the config" change has to argue with a test.
    td, jdir, spine_md = _mkroot([], ndays=0)
    away = Path(tempfile.mkdtemp(prefix="vardemem-away-"))
    try:
        (away / "elsewhere").mkdir()
        (Path(td) / CONFIG_NAME).write_text(
            json.dumps({"journal": str(away / "elsewhere")}), encoding="utf-8")
        (away / "elsewhere" / "journal-2026-03-04.md").write_text(
            "# a day", encoding="utf-8")
        rc, out = _run(jdir, "put", "2026-03-04", "recorded away")
        landed = away / "elsewhere" / "SPINE.md"
        check(rc == 0 and landed.is_file()
              and "recorded away" in landed.read_text(encoding="utf-8"),
              "an absolute journal path is honoured, root and all")
    finally:
        shutil.rmtree(td, ignore_errors=True)
        shutil.rmtree(away, ignore_errors=True)

    for label, text, want in (
            ("malformed JSON", "{not json", "valid JSON"),
            ("a non-object", '["history"]', "JSON object"),
            ("a non-string path", '{"journal": 7}', '"journal"'),
            ("no journal at all", '{"spine": "INDEX.md"}', '"journal"')):
        td, jdir, spine_md = _mkroot([], ndays=0)
        try:
            (Path(td) / CONFIG_NAME).write_text(text, encoding="utf-8")
            rc, out = _run(jdir, "read")
            check(rc != 0 and CONFIG_NAME in out and want in out,
                  "%s is refused, naming the file and %s" % (label, want))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    print("\n%s" % ("ALL PASS" if not fails else "%d FAILED" % len(fails)))
    sys.exit(1 if fails else 0)


COMMANDS = {"read": cmd_read, "missing": cmd_missing, "draft": cmd_draft,
            "put": cmd_put, "due": cmd_due, "roll": cmd_roll,
            "forget": cmd_forget, "grep": cmd_grep,
            "selftest": cmd_selftest}


def main():
    # A read is long and will be piped to `head` constantly. Without this the
    # closed pipe surfaces as a traceback, which reads like the tool broke.
    #
    # Two layers, because the try/except alone has a hole: Python flushes
    # stdout at interpreter shutdown, AFTER main() returns, so a pipe that
    # breaks on that last partial flush is reported as "Exception ignored in:
    # <_io.TextIOWrapper>" with the handler never entered. Restoring SIGPIPE
    # to the default makes the process die the way every other Unix tool
    # does, whenever the write happens; the explicit flush inside the try
    # pulls the remaining case back inside the handler. (North, round 6.)
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        _run()
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)


def _run():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        if len(sys.argv) > 1:
            print("No such command: %s\n" % sys.argv[1], file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr if len(sys.argv) > 1 else sys.stdout)
        sys.exit(1 if len(sys.argv) > 1 else 0)
    if sys.argv[1] == "selftest":
        return COMMANDS["selftest"](sys.argv[2:])   # reads no journal file
    if not journal_dir().is_dir():
        die('No journal at %s ("journal" in %s).'
            % (journal_dir(), config_path()))
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
