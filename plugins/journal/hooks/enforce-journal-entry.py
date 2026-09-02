#!/usr/bin/env python3
"""Stop gate for silent sessions: real work done, nothing written to the Journal.

`enforce-journal-tags` guards the QUALITY of a Journal entry (a new entry must
carry a tag). Nothing guards its EXISTENCE. A session that edits files, commits,
and closes issues, then ends with no line in the Journal, is never caught, and
in this architecture the Journal is the append-only store VardeMem indexes: no
Journal write means the session fed the memory system nothing.

This hook closes that gap on the `Stop` event, the only per-turn event that can
hold a session open. It reads the transcript once, keeps a block-once ledger
keyed by session, and blocks at most `max_blocks_per_session` times.

WHY ONE FILE, NOT A SUBSTRATE. An earlier cut split the transcript reader and
the block-once ledger into a shared `stop_gate.py` so a second Stop hook (a
promise / broken-commitment detector) could compose with this one on a single
Stop. That second hook does not exist yet, and when it is built it will live in
another plugin and repo, which a Claude Code hook cannot import across at
runtime (each hook runs under its own CLAUDE_PLUGIN_ROOT). So the shared
machinery only ever coordinated one real hook. It is inlined here. What the
inline dropped is the multi-DETECTOR framework (a `Detector` registry and its
dispatch); what it KEEPS is the per-Stop `claim_stop` and the numbered budget
slots, because those are not multi-detector plumbing: even one hook needs them
to hold "block a given Stop at most once" when `max_blocks_per_session > 1`
and the hook runs in overlapping processes. If the promise detector is ever
built and a single-block-per-Stop guarantee ACROSS two gates is wanted,
extract the reader and ledger THEN, as an installed, securely-resolved module
both hooks import from a fixed trusted path (the `vardemem`-binary resolution
pattern), and let that hook's real needs shape the interface.

WHAT COUNTS AS SUBSTANTIVE WORK (bias: do not fire).
----------------------------------------------------
A false block on a Q&A session is worse than a missed entry on a nothing
session, so only unambiguous, SUCCESSFUL, durable actions count:

  - an `Edit` / `Write` / `MultiEdit` whose result was not an error, to a file
    that is not in the Journal, not under a tmp root or the scratchpad, and
    not in the Claude Code auto-memory dir (a memory-dir write is neither
    real work nor a Journal write)
  - a `Bash` call whose result was not an error and where one of the
    configured work commands (`git commit`, `git push`, the `gh pr` /
    `gh issue` mutators, `varde-issue --title`) sits at COMMAND POSITION of a
    shell segment, and that segment carries no `--dry-run` or `--help`

Reads, searches, `Agent` dispatches, and a tool call with no result (the turn
was interrupted) never count. A verb that is merely mentioned (`grep 'git
commit'`, `echo 'git push'`, `git log --grep='git commit'`) never counts:
every one of those blocked a read-only session in review. Whether a `Bash`
command wrote a file through a redirection is not inferred. An edit made by
a subagent inside this transcript counts as this session's work. The
threshold is `min_work_events` (default 1).

WHAT SATISFIES THE GATE (any one).
----------------------------------
  - a write tool call, not denied, whose path is under the resolved journal
    dir, or contains the configured relative journal path as consecutive
    path segments (so a worktree or a second clone of the vault matches)
  - a `Bash` call, not denied, that RUNS `vardemem put <date> ...` at command
    position of a segment, env assignments and `env`/`sudo`/`nice` leaders
    stripped (the day was recorded on the spine, which only happens after
    the Journal is written)
  - a `Bash` call, not denied, that REDIRECTS into the journal path (`>>`,
    `>`, or `tee`), or whose `cp`/`mv`/`install`/`rsync` segment names it

Only a write that SUCCEEDED satisfies: a write the tag gate denied carries
`is_error: true`, and a tool call with no result never ran. A `Bash` that
merely MENTIONS the journal path or the words `vardemem put` (a grep, an
issue body in a heredoc, a doc edit) does not satisfy either: heredoc bodies
are stripped before any command is judged, and the journal path must end a
path segment (`.../Journal_evil/x` is not the journal). Measured on a real
session, the mention cases were the first false satisfiers.

THREAT MODEL, and the bypasses it licenses. The adversary is the agent's own
forgetfulness, never an agent evading the gate: a missed reminder costs one
unjournaled session, a false block costs a real turn, so every ambiguity
resolves toward silence. Named, accepted bypasses that follow: a `Bash`
satisfier proves the COMMAND ran, not that it succeeded inside a compound
(`vardemem put <date> x || true` satisfies).

RECORDED RESIDUALS in the false-fire direction, each too narrow to buy with
more shell modelling, and each covered by the block reason's "if you already
wrote the day file in a way this gate cannot see, end the turn again": a
cp/mv whose journal path is QUOTED does not satisfy (quoted spans are
dropped before the copy is judged; a quoted REDIRECT target is unwrapped and
does satisfy); a heredoc whose terminator never appears hides everything
after it, including a later journal write; the quoted-span probe is per
LINE, so a quoted string that spans lines and carries `<<WORD` on a later
line still opens a phantom heredoc that hides what follows. And one in the
silent direction, new with the redirect unwrapping: the unwrapped target's
own text reaches the segmenter, so `echo x > "foo; vardemem put <date> hi"`
satisfies. Do not "tighten" these into shell parsing: that is where the false
fires came from.

THE SHELL MODEL, stated whole so it stops growing: heredoc bodies (every
delimiter on a line, in order) and quoted spans are data; what remains is
split on `;`, `&`, `|`, newlines and `(`; each piece loses env assignments
and `env`/`sudo`/`nice`/`time`/`command` leaders; a verb counts only as the
first token of a piece, and a cp/mv counts by its last token. Nothing else
about the shell is modelled: no variable expansion, no command substitution,
no exit-status logic, no quoting inside quoting. A case outside this model
that fails SILENT is accepted by the threat model above; one that
FALSE-FIRES is a bug.

JOURNAL RESOLUTION is never hardcoded. It mirrors `vardemem._resolve_root`:
walk up from the session's cwd for `.vardemem.json`, accepting only a root on
the trusted-roots list (`~/.config/vardemem/trust.json`), then fall back to
`$AGENT_VAULT`. The `journal` key is read from whichever config answers. No
resolvable journal means this session has no journal to write, so no-op.

BLOCK ONCE, NOT UNTIL COMPLIANCE. `Stop` has no final-turn signal, so a gate
that blocks until the Journal is written is a per-turn nag by construction,
and it traps a session whose Journal cannot be written. The threat model is
the tag gate's: the adversary is forgetfulness, not intent, and one reminder
with the instruction in context cures forgetfulness. `max_blocks_per_session`
(default 1) is the knob; 0 is observe mode (ledger and log only, never blocks).
The budget is a set of `O_EXCL` slot files, so two overlapping Stop processes
for one session cannot both block.

OPT-IN PER TENANT at `~/.config/journal/journal-entry.json`; see
`journal-entry.config.example.json`. Disable by naming `enforce-journal-entry`
in `$JOURNAL_DISABLED` (the legacy `$HARNESS_MANAGEMENT_DISABLED` is honored).

FAIL OPEN, ALWAYS: any uncertainty lets the session end. A malformed payload,
a missing/oversized/slow transcript, an invalid session_id, an unwritable
state dir, or any unexpected exception degrades to silence, never a block.

Hook input: JSON on stdin (Stop payload: session_id, transcript_path, cwd,
stop_hook_active). Hook output: `{"decision":"block","reason":...}` on stdout
at most once per session; otherwise silence. Always exit 0.
"""

import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_home import trusted_home  # noqa: E402  (sibling module)

HOOK_NAME = "enforce-journal-entry"
CONFIG_FILE = "journal-entry.json"

CONFIG_DIR = Path.home() / ".config/journal"
STATE_DIR = Path.home() / ".local/state/journal"
LOG_DIR = Path.home() / ".local/log"

VARDEMEM_CONFIG = ".vardemem.json"
TRUST_FILE_ENV = "VARDEMEM_TRUST_FILE"
ROOT_ENV = "AGENT_VAULT"

# A session id is used as a filename. Anything outside this set is refused,
# so a malformed payload cannot steer the ledger outside STATE_DIR.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
STOP_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Past this, the transcript is not read at all. Parsing is linear, but the hook
# has a 5s budget and a spurious block is the worse failure.
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
TRANSCRIPT_DEADLINE_S = 3.0

PRUNE_AFTER_S = 14 * 86400
PRUNE_MAX_UNLINKS = 50

WRITE_TOOLS = ("Edit", "Write", "MultiEdit")

DEFAULTS = {
    "max_blocks_per_session": 1,
    "min_work_events": 1,
    "work_commands": [
        "git commit", "git push",
        "gh pr create", "gh pr merge", "gh pr edit", "gh pr close", "gh pr comment",
        "gh issue create", "gh issue close", "gh issue edit", "gh issue comment",
        "varde-issue --title",
    ],
    "ignore_path_segments": ["scratchpad"],
}


# --------------------------------------------------------------------------
# Transcript reading and the block-once ledger (inlined from the substrate).
# --------------------------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    input: dict
    # True: a result was seen and it was not an error. False: the result was an
    # error (a tool failure, or a PreToolUse deny). None: no result was seen.
    ok: "bool | None"


@dataclass
class SessionActivity:
    # Every tool call of the session, in order. The gate judges the WHOLE
    # session, never the last turn: a Journal write four turns back still
    # satisfies, so no turn split is kept.
    calls: list = field(default_factory=list)
    # The Stop key of the audit record, nothing else.
    last_assistant_uuid: "str | None" = None
    truncated: bool = False


def log(msg: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with (LOG_DIR / f"{HOOK_NAME}-hook.log").open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


def disabled() -> bool:
    """True when this hook is named in $JOURNAL_DISABLED, or in the legacy
    $HARNESS_MANAGEMENT_DISABLED it shipped under in the monolith. The legacy
    var also honors the `stop-gate` umbrella token the shared substrate
    accepted, so a tenant who set it before the move stays disabled."""
    def named(var):
        return {p.strip() for p in os.environ.get(var, "").split(",") if p.strip()}
    if HOOK_NAME in named("JOURNAL_DISABLED"):
        return True
    legacy = named("HARNESS_MANAGEMENT_DISABLED")
    return HOOK_NAME in legacy or "stop-gate" in legacy


def read_regular(path: Path):
    """A regular file's text, or None. A FIFO would block `open()` past the
    hook's timeout, and the top-level handler cannot interrupt a syscall."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def load_config() -> "dict | None":
    """The tenant's opt-in config with defaults applied, or None to no-op.
    PURE READ: never writes, mkdirs, or logs."""
    raw = read_regular(CONFIG_DIR / CONFIG_FILE)
    if raw is None:
        return None
    try:
        cfg = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None
    merged = dict(DEFAULTS)
    merged.update(cfg)
    return merged


class _Scan:
    """Mutable state of one transcript pass."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.calls = []
        self.by_id = {}
        self.last_assistant_uuid = None

    def _parse(self, line: str):
        line = line.strip()
        if not line:
            return None
        try:
            rec = json.loads(line)
        except ValueError:
            return None
        if not isinstance(rec, dict) or not isinstance(rec.get("message"), dict):
            return None
        # When the session is known, a record that does not name it is not
        # counted, including one with no sessionId at all: an unattributed
        # stale edit in a resumed transcript must not fire the gate.
        if self.session_id and rec.get("sessionId") != self.session_id:
            return None
        return rec

    def _assistant(self, rec: dict) -> None:
        uuid = rec.get("uuid")
        if isinstance(uuid, str):
            self.last_assistant_uuid = uuid
        content = rec["message"].get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                self._tool_use(block)

    def _tool_use(self, block: dict) -> None:
        inp = block.get("input")
        call = ToolCall(name=str(block.get("name", "")),
                        input=inp if isinstance(inp, dict) else {}, ok=None)
        self.calls.append(call)
        if isinstance(block.get("id"), str):
            self.by_id[block["id"]] = call

    def _user(self, rec: dict) -> None:
        """Map each tool result onto the call it answers. A record with no
        result block (a real prompt) carries nothing this gate reads."""
        content = rec["message"].get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            call = self.by_id.get(block.get("tool_use_id"))
            if call is not None:
                call.ok = not bool(block.get("is_error"))

    def feed(self, line: str) -> None:
        rec = self._parse(line)
        if rec is None:
            return
        if rec.get("type") == "assistant":
            self._assistant(rec)
        elif rec.get("type") == "user":
            self._user(rec)

    def result(self, truncated: bool) -> SessionActivity:
        return SessionActivity(calls=self.calls,
                               last_assistant_uuid=self.last_assistant_uuid,
                               truncated=truncated)


def inspect_transcript(path: str, session_id: "str | None" = None,
                       deadline: "float | None" = None) -> "SessionActivity | None":
    """One linear pass over the transcript. None when it cannot be read.

    `deadline` is an absolute `time.monotonic()` value; past it the pass stops
    and the result is marked `truncated`, which callers treat as fail-open."""
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_TRANSCRIPT_BYTES:
            return None
        fh = open(path, encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    scan = _Scan(session_id)
    truncated = False
    with fh:
        for n, line in enumerate(fh):
            if deadline is not None and n % 200 == 0 and time.monotonic() > deadline:
                truncated = True
                break
            scan.feed(line)
    return scan.result(truncated)


def stop_key_for(activity: SessionActivity, transcript_path: str) -> str:
    key = activity.last_assistant_uuid
    if isinstance(key, str) and STOP_KEY_RE.match(key):
        return key
    try:
        st = os.stat(transcript_path)
        raw = f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        raw = str(time.time())
    return hashlib.sha1(raw.encode()).hexdigest()[:32]


class Ledger:
    """Per-session block-once state under STATE_DIR: one JSON audit file plus
    one `O_EXCL` block-slot file per taken block. Every method fails open."""

    def __init__(self, session_id: str, state_dir: Path = STATE_DIR):
        self.session_id = session_id
        self.state_dir = state_dir
        self.valid = bool(SESSION_ID_RE.match(session_id or ""))
        self.path = state_dir / f"{session_id}.json" if self.valid else None

    def load(self) -> dict:
        if not self.valid:
            return {}
        raw = read_regular(self.path)
        if raw is None:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> bool:
        """True only when the ledger is durably on disk. A block that cannot
        be recorded still stands on its slot; this is the audit trail."""
        if not self.valid:
            return False
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    def _slot(self, n: int) -> Path:
        return self.state_dir / f"{self.session_id}.{HOOK_NAME}.block{n}"

    def blocks(self) -> int:
        """Blocks already taken this session: the slot FILES, not the JSON.
        A slot is created O_EXCL, so the count is exact under any overlap."""
        if not self.valid:
            return 0
        return sum(1 for n in range(64) if self._slot(n).exists())

    def claim_stop(self, stop_key: str) -> bool:
        """Take a one-time claim for THIS Stop, keyed by stop_key. True exactly
        once per (session, stop_key) across processes. Deduping by Stop is why
        `max_blocks_per_session > 1` cannot let a single Stop, invoked in
        overlapping processes, consume more than one budget slot: the numbered
        slots cap the COUNT, this caps it to one-per-distinct-Stop."""
        if not self.valid or not STOP_KEY_RE.match(stop_key or ""):
            return False
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            claim = self.state_dir / f"{self.session_id}.{HOOK_NAME}.{stop_key}.claim"
            fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        except OSError as e:
            log(f"state dir {self.state_dir} unwritable ({e}); no claim, passing")
            return False
        os.close(fd)
        return True

    def claim_block_slot(self, budget: int) -> bool:
        """Atomically take one of `budget` block slots for this session.
        Two overlapping processes with different Stop keys cannot both win
        the last slot, which is what a read-modify-write JSON count allowed."""
        if not self.valid:
            return False
        for n in range(min(budget, 64)):
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                fd = os.open(self._slot(n), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            except OSError as e:
                log(f"state dir {self.state_dir} unwritable ({e}); no slot, passing")
                return False
            os.close(fd)
            return True
        return False

    def _record(self, kind: str, stop_key: str, summary: str) -> bool:
        data = self.load()
        data["session_id"] = self.session_id
        entry = data.get(HOOK_NAME)
        if not isinstance(entry, dict):
            entry = data[HOOK_NAME] = {"blocks": 0, "observed": 0, "events": []}
        entry[kind] = int(entry.get(kind, 0) or 0) + 1
        events = entry.setdefault("events", [])
        if isinstance(events, list):
            events.append({"kind": kind, "stop_key": stop_key, "ts": time.time(),
                           "summary": summary[:200]})
            del events[:-20]
        return self._save(data)

    def record_block(self, stop_key: str, summary: str) -> bool:
        return self._record("blocks", stop_key, summary)

    def record_observation(self, stop_key: str, summary: str) -> bool:
        return self._record("observed", stop_key, summary)

    def prune(self, now: "float | None" = None) -> None:
        now = time.time() if now is None else now
        try:
            names = os.listdir(self.state_dir)
        except OSError:
            return
        removed = 0
        for name in names:
            if removed >= PRUNE_MAX_UNLINKS:
                break
            p = self.state_dir / name
            try:
                if now - os.stat(p).st_mtime > PRUNE_AFTER_S:
                    os.unlink(p)
                    removed += 1
            except OSError:
                continue


def _budget(cfg: dict) -> int:
    n = cfg.get("max_blocks_per_session", 1)
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 1


# --------------------------------------------------------------------------
# Journal resolution (mirrors vardemem) and the shell model.
# --------------------------------------------------------------------------

def _trust_file_path():
    """Mirrors vardemem: an ABSOLUTE `$VARDEMEM_TRUST_FILE` wins outright;
    otherwise `~/.config/vardemem/trust.json` under a home resolved from the
    OS, never `$HOME` / `%USERPROFILE%`, which the walked tree could itself
    have set.

    WINDOWS (#2301). Reading the passwd entry directly returned None whenever
    `pwd` was missing, which on Windows is always, so no trust file was ever
    found and the whole gate was a permanent silent no-op on a real operator
    machine. `agent_home.trusted_home()` answers on both platforms from the
    OS, so the gate resolves a journal on Windows too."""
    override = os.environ.get(TRUST_FILE_ENV, "").strip()
    if override and os.path.isabs(override):
        return Path(override)
    home = trusted_home(log)
    if home is None:
        return None
    return Path(home) / ".config" / "vardemem" / "trust.json"


# Fixed, never user-supplied: a tenant regex is a ReDoS foot-gun on a 5s hook.
SEGMENT_SPLIT_RE = re.compile(r"[;&|\n(]+")
LEADER_RE = re.compile(r"^(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*|env|sudo|nice|time|command)\s+)*")
# Command position is the whole discipline: a mention lives in a quoted span
# or a heredoc body, both stripped before this runs, so the date argument is
# not required (it may be a quoted variable, `"$DAY"`, which stripping drops).
SPINE_PUT_RE = re.compile(r"^(?:\S*/)?vardemem(?:\.py)?\s+put\b")
COPY_LEADERS = ("cp ", "mv ", "install ", "rsync ")
# A work verb followed by one of these TOKENS did nothing durable.
NO_OP_FLAGS = {"--dry-run", "--help", "-h"}
AUTO_MEMORY_RE = re.compile(r"/\.claude/projects/[^/]+/memory/")
MAX_CMD_SCAN = 20_000

HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z0-9_.-]+)\1")
HEREDOC_QUOTED_DELIM_RE = re.compile(r"(<<-?)\s*(['\"])([A-Za-z0-9_.-]+)\2")
# A quoted span is data. Dropped before segmenting, so `printf 'x; git
# commit'` has no segment at command position. The cost is that a QUOTED
# journal path in a cp/mv never satisfies, which is the silent direction.
QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
REDIRECT_TAIL_RE = re.compile(r"(?:>>?|\btee\b(?:\s+-a)?)\s*$")


def strip_quotes(text: str) -> str:
    return QUOTED_RE.sub("", text)


def strip_quotes_keep_targets(text: str) -> str:
    """Quoted spans dropped, EXCEPT a span that is the target of `>`, `>>`
    or `tee`, which is unwrapped instead: `echo "- x" >> "$V/Journal/d.md"`
    is a journal write and must not lose its path to quote stripping."""
    def keep_or_drop(m):
        return m.group(0)[1:-1] if REDIRECT_TAIL_RE.search(text[:m.start()]) else ""
    return QUOTED_RE.sub(keep_or_drop, text)


def strip_heredocs(cmd: str) -> str:
    """`cmd` with every heredoc BODY removed. A body is data, not commands:
    an issue body quoting `vardemem put 2026-09-02 ...` or `git commit -m`
    must neither satisfy the gate nor count as work."""
    out, pending = [], []  # (terminator, strips_tabs) still owed, in order
    for line in cmd.split("\n"):
        if pending:
            term, dash = pending[0]
            # The shell wants the word alone on the line; only `<<-` forgives
            # leading tabs. An indented `  EOF` under `<<EOF` is body text.
            if line == term or (dash and line.lstrip("\t") == term):
                pending.pop(0)
            continue
        out.append(line)
        # Openers are looked for OUTSIDE quoted spans: `echo "1 << 2"` opens
        # nothing, and treating it as one would swallow the rest of the call.
        # A quoted DELIMITER (`<<'EOF'`) is unquoted first so it survives.
        probe = strip_quotes(HEREDOC_QUOTED_DELIM_RE.sub(r"\1\3", line))
        pending.extend((m.group(2), m.group(0).startswith("<<-"))
                       for m in HEREDOC_RE.finditer(probe))
    return "\n".join(out)


def segments(cmd: str):
    """Each shell segment of `cmd` with its leaders stripped, in order."""
    out = []
    for raw in SEGMENT_SPLIT_RE.split(strip_quotes(strip_heredocs(cmd[:MAX_CMD_SCAN]))):
        seg = LEADER_RE.sub("", raw.strip(), count=1)
        if seg:
            out.append(seg)
    return out


def _journal_redirect_re(journal_rel: str, journal_abs: Path):
    """A redirection or `tee` whose target is in the journal. The journal path
    is escaped in, so a tenant's config value is a literal, never a pattern."""
    target = f"(?:{re.escape(journal_rel)}|{re.escape(str(journal_abs))})"
    # The target must END a path segment: `.../Journal_evil/x` is not the journal.
    return re.compile(r"(?:>>?|\btee\b(?:\s+-a)?)\s*[\"']?[^\s\"'|;&<>]*" + target
                      + r"(?=/|\s|[\"']|$)")


def _clean_str_list(value, default):
    if not isinstance(value, list):
        return list(default)
    out = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return out or list(default)


def _trusted_roots():
    path = _trust_file_path()
    raw = read_regular(path) if path is not None else None
    if raw is None:
        return ()
    try:
        entries = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(entries, list):
        return ()
    roots = []
    for e in entries:
        if isinstance(e, str) and e.strip() and os.path.isabs(e):
            try:
                roots.append(Path(e).resolve())
            except (OSError, ValueError):
                continue
    return tuple(roots)


def _journal_rel(config_path: Path):
    raw = read_regular(config_path)
    if raw is None:
        return None
    try:
        cfg = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(cfg, dict):
        return None
    rel = cfg.get("journal")
    if not isinstance(rel, str) or not rel.strip():
        return None
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts or "\0" in rel:
        return None
    return rel


class _Unreachable(Exception):
    """A config that exists but cannot be checked; resolution stops."""


def _trusted_walk(cwd: str):
    """The nearest TRUSTED ancestor of cwd holding a `.vardemem.json`, or None.
    An untrusted config is walked past, so a trusted ancestor still answers."""
    try:
        here = Path(cwd).resolve()
    except (OSError, ValueError):
        return None
    trusted = _trusted_roots()
    for d in (here, *here.parents):
        cfg = d / VARDEMEM_CONFIG
        try:
            is_file = stat.S_ISREG(os.stat(cfg).st_mode)
        except FileNotFoundError:
            if os.path.islink(cfg):
                # A dangling config symlink is a route that broke, not an
                # absent one; vardemem refuses here, so this gate has no
                # journal to name and must not fall through to the env.
                raise _Unreachable()
            continue
        except OSError:
            raise _Unreachable()
        if is_file and any(d == r or r in d.parents for r in trusted):
            return d
    return None


def resolve_journal(cwd: str):
    """(journal_abs: Path, journal_rel: str) or None. Same order as vardemem:
    the trusted walk from cwd first, `$AGENT_VAULT` second."""
    try:
        root = _trusted_walk(cwd)
    except _Unreachable:
        return None
    if root is None:
        env = os.environ.get(ROOT_ENV)  # literal, as vardemem reads it
        if not env:
            return None
        root = Path(env)
    rel = _journal_rel(root / VARDEMEM_CONFIG)
    return (root / rel, rel) if rel else None


def _segments(path: str):
    return tuple(p for p in path.replace("\\", "/").split("/") if p and p != ".")


def _has_consecutive(haystack: tuple, needle: tuple) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def in_journal(file_path: str, journal_abs: Path, journal_rel: str) -> bool:
    try:
        resolved = os.path.realpath(file_path)
        if os.path.commonpath([resolved, os.path.realpath(journal_abs)]) == os.path.realpath(journal_abs):
            return True
    except (OSError, ValueError):
        pass
    # Segment match on the NORMALIZED path, so `.../Journal/../../x` has no
    # journal segments left to match.
    return _has_consecutive(_segments(os.path.normpath(file_path)), _segments(journal_rel))


def _tmp_roots():
    roots = ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders"]
    t = os.environ.get("TMPDIR", "").strip()
    if t:
        roots.append(t.rstrip("/"))
    return roots


def is_ignored(file_path: str, ignore_segments) -> bool:
    if AUTO_MEMORY_RE.search(file_path.replace("\\", "/")):
        return True
    if any(seg in ignore_segments for seg in _segments(file_path)):
        return True
    try:
        resolved = os.path.realpath(file_path)
    except (OSError, ValueError):
        resolved = file_path
    return any(resolved == r or resolved.startswith(r + "/") for r in _tmp_roots())


def _file_path(call) -> str:
    fp = call.input.get("file_path")
    return fp if isinstance(fp, str) and fp else ""


def _command(call) -> str:
    c = call.input.get("command")
    return c if isinstance(c, str) else ""


def _copies_into_journal(seg: str, journal_abs: Path, journal_rel: str) -> bool:
    """Only the DESTINATION (last argument) counts; copying a day file out
    of the journal writes nothing into it."""
    toks = seg.split()
    return seg.startswith(COPY_LEADERS) and len(toks) >= 3 and in_journal(
        toks[-1].strip("\"'"), journal_abs, journal_rel)


def satisfied_by(call, journal_abs: Path, journal_rel: str, redirect_re) -> bool:
    # Only a write that SUCCEEDED counts. A denied write is False; a call
    # with no result never ran, and a Stop after a user interrupt does not
    # fire, so None here is a write that did not happen.
    if call.ok is not True:
        return False
    if call.name in WRITE_TOOLS:
        fp = _file_path(call)
        return bool(fp) and in_journal(fp, journal_abs, journal_rel)
    if call.name == "Bash":
        # Bounded scan: the redirect pattern backtracks per `>` on a long run
        # of non-separator text, and a pasted blob is not where a journal
        # write hides. Heredoc bodies are data and are stripped first.
        cmd = strip_quotes_keep_targets(strip_heredocs(_command(call)[:MAX_CMD_SCAN]))
        if redirect_re.search(cmd):
            return True
        return any(SPINE_PUT_RE.match(seg) or _copies_into_journal(seg, journal_abs, journal_rel)
                   for seg in segments(cmd))
    return False


def _runs_verb(seg: str, verbs) -> bool:
    """True when one of `verbs` is the whole start of `seg`, ending on a token
    boundary. A bare prefix test also matched `git commit-tree` and `git
    commit-graph`, real git commands that are not the configured verb, so a
    plumbing call fired the gate: a false fire, which this model calls a bug."""
    return any(seg.startswith(v) and seg[len(v):len(v) + 1] in ("", " ", "\t")
               for v in verbs)


def is_work(call, cfg: dict) -> bool:
    """A journal write is never asked here: any journal write that was not
    denied already satisfied the gate, and a denied one has `ok` False."""
    if call.ok is not True:
        return False
    if call.name in WRITE_TOOLS:
        fp = _file_path(call)
        return bool(fp) and not is_ignored(fp, cfg["ignore_path_segments"])
    if call.name == "Bash":
        return any(_runs_verb(seg, cfg["work_commands"])
                   and NO_OP_FLAGS.isdisjoint(seg.split())
                   for seg in segments(_command(call)))
    return False


def newest_day_file(journal_abs: Path) -> str:
    try:
        names = sorted(n for n in os.listdir(journal_abs) if n.lower().endswith(".md"))
    except OSError:
        return "none yet"
    return names[-1] if names else "none yet"


def evaluate(activity, cfg, journal_abs, journal_rel):
    """(reason, summary) to block, or None to pass."""
    cfg["work_commands"] = _clean_str_list(cfg.get("work_commands"), DEFAULTS["work_commands"])
    cfg["ignore_path_segments"] = _clean_str_list(
        cfg.get("ignore_path_segments"), DEFAULTS["ignore_path_segments"])

    calls = activity.calls
    redirect_re = _journal_redirect_re(journal_rel, journal_abs)
    if any(satisfied_by(c, journal_abs, journal_rel, redirect_re) for c in calls):
        return None
    work = [c for c in calls if is_work(c, cfg)]
    threshold = cfg.get("min_work_events", 1)
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
        threshold = 1
    if len(work) < threshold:
        return None

    edits = sum(1 for c in work if c.name in WRITE_TOOLS)
    actions = len(work) - edits
    newest = newest_day_file(journal_abs)
    summary = f"{edits} edits, {actions} git/gh actions, no write under {journal_rel}"
    reason = (
        f"This session did real work ({edits} file edits, {actions} commit or GitHub "
        f"actions) and wrote nothing to the Journal at {journal_abs} (newest day file: "
        f"{newest}). The Journal is VardeMem's only durable record, so an unjournaled "
        f"session is invisible to every future session.\n\n"
        f"Before ending: add one tagged, past-tense line for this session to today's "
        f"day file in that directory (name it like the newest one; create it if the day "
        f"has none), commit it, and if the day is closing record it on the spine with "
        f"`vardemem put <YYYY-MM-DD> \"<one line>\"`. Then end the turn.\n\n"
        f"If this session genuinely produced nothing worth recording, or you already "
        f"wrote today's day file in a way this gate cannot see (it reads tool calls, "
        f"not the disk), do NOT add a second entry: end the turn again. This gate "
        f"fires at most once per session."
    )
    return reason, summary


def _payload_fields(payload):
    """(session_id, transcript_path, cwd), or None when the Stop must pass:
    a malformed payload, or a continuation already caused by a Stop block."""
    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return None
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    if not isinstance(session_id, str) or not isinstance(transcript_path, str):
        return None
    cwd = payload.get("cwd")
    return session_id, transcript_path, cwd if isinstance(cwd, str) and cwd else os.getcwd()


def _resolve_context(payload):
    """(session_id, transcript_path, cfg, journal_abs, journal_rel), or None to
    no-op: a bad payload, a disabled or unconfigured tenant, or no journal here.
    Cheap checks first, so a no-journal host never reads the transcript."""
    fields = _payload_fields(payload)
    if fields is None or disabled():
        return None
    session_id, transcript_path, cwd = fields
    cfg = load_config()
    if cfg is None:
        return None
    resolved = resolve_journal(cwd)
    if resolved is None:
        return None
    return session_id, transcript_path, cfg, resolved[0], resolved[1]


def _emit_block(ledger, budget, stop_key, session_id, reason, summary):
    """The reason to block, or None. Observe mode and a spent or lost budget
    are logged and pass; the O_EXCL slot is the durable budget record."""
    if budget == 0:
        ledger.record_observation(stop_key, summary)
        log(f"observe: would block session {session_id}: {summary}")
        return None
    if ledger.blocks() >= budget:
        log(f"budget spent ({budget}) for session {session_id}; passing")
        return None
    if not ledger.claim_stop(stop_key):
        log(f"stop {stop_key} already blocked for session {session_id}; passing")
        return None
    if not ledger.claim_block_slot(budget):
        log(f"budget spent ({budget}) for session {session_id} under overlap; passing")
        return None
    if not ledger.record_block(stop_key, summary):
        log(f"ledger {ledger.path} not writable; slot held, audit entry lost")
    log(f"BLOCK session {session_id} stop {stop_key}: {summary}")
    return reason


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    ctx = _resolve_context(payload)
    if ctx is None:
        return 0
    session_id, transcript_path, cfg, journal_abs, journal_rel = ctx

    activity = inspect_transcript(transcript_path, session_id,
                                  deadline=time.monotonic() + TRANSCRIPT_DEADLINE_S)
    if activity is None or activity.truncated:
        return 0
    ledger = Ledger(session_id)
    if not ledger.valid:
        return 0

    verdict = evaluate(activity, cfg, journal_abs, journal_rel)
    budget = _budget(cfg)
    if verdict is None:
        if budget == 0:
            log(f"observe: pass for session {session_id}")
    else:
        reason, summary = verdict
        stop_key = stop_key_for(activity, transcript_path)
        emit = _emit_block(ledger, budget, stop_key, session_id, reason, summary)
        if emit is not None:
            sys.stdout.write(json.dumps({"decision": "block", "reason": emit}))
            sys.stdout.flush()
    ledger.prune()
    return 0


if __name__ == "__main__":
    # Fail open: any unexpected error degrades to a silent no-op, never a block.
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
