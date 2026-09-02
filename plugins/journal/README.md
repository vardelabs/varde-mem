# journal

Journal-discipline hooks for a VardeMem-backed agent. The Journal is the
append-only store VardeMem indexes; these hooks keep it **non-empty** and
**well-formed**. Both are opt-in per tenant, fail open, and resolve the journal
path the way `vardemem` does (a trusted walk from the session cwd for
`.vardemem.json`, then `$AGENT_VAULT`) rather than hardcoding it.

**Version:** see the `journal` entry in the consuming marketplace; it is not
carried in `plugin.json`.

## `enforce-journal-entry.py` (Stop hook)

Blocks the Stop **once per session** when the session did substantive work and
no tool call in it wrote to the Journal. A session that edits files, commits,
and closes issues, then ends with no Journal line, fed the memory system
nothing; this catches that.

- **Substantive work** (bias: do not fire): a successful `Edit`/`Write`/
  `MultiEdit` outside the Journal, tmp roots, the scratchpad, and the
  auto-memory dir; or a successful `Bash` with a work command (`git commit`,
  `git push`, the `gh pr`/`gh issue` mutators, `varde-issue --title`) at command
  position. Reads, `Agent` dispatches, failed and interrupted calls never count.
- **Satisfied by** a successful write under the resolved journal dir (or any
  path carrying the relative journal segments, so a worktree matches), a `Bash`
  running `vardemem put <date>`, or a redirect/copy into the journal path.
- **Block once, not until compliance.** `Stop` has no final-turn signal, so
  "block until written" is a per-turn nag and traps a session that cannot write
  the Journal. One reminder cures forgetfulness. `max_blocks_per_session`
  (default 1) is the knob; `0` is observe mode (ledger and
  `~/.local/log/enforce-journal-entry-hook.log` only, never blocks). Run a
  tenant in observe mode first, read the log, then let it block.

Opt in with a config under the trusted (passwd/token) home at
`<home>/.config/journal/journal-entry.json` (see
`hooks/journal-entry.config.example.json`); the location is not derived from
`$HOME` (#2310), and `$JOURNAL_CONFIG_DIR` (absolute) overrides it. The block budget is a set of
`O_EXCL` slot files under `~/.local/state/journal/`, so two overlapping Stop
processes for one session cannot both block. Tests:
`tests/smoke-enforce-journal-entry.py`.

The transcript reader and block-once ledger live in this one file. An earlier
cut split them into a shared `stop_gate.py` substrate so a second Stop hook (a
promise / broken-commitment detector) could compose on a single Stop; that hook
does not exist yet and would live in another plugin a Claude Code hook cannot
import across at runtime, so the machinery only ever served one hook and is
inlined. If that detector is built and a single-block-per-Stop guarantee across
two gates is genuinely wanted, extract the reader and ledger then, as an
installed, securely-resolved module both hooks import from a fixed trusted path.

## `enforce-journal-tags.py` (PreToolUse (Edit|Write|MultiEdit))

Denies a write that **adds** an untagged entry to a Daily Note (a bold
lead-in at column 0 followed by prose, e.g. `[decision] **Retired the charter
model.** Kai's call.`). It diffs the before- and after-documents rather than
the raw tool input, so overwriting or appending to a note full of legacy
untagged entries is never blocked; only an entry the write genuinely adds or
edits is gated. Moved and renamed from the monolith's
`enforce-daily-note-tags`, parser and threat model unchanged.

Opt in at `~/.config/journal/journal-tags.json` (see
`hooks/journal-tags.config.example.json`). Tests:
`tests/smoke-enforce-journal-tags.py`.

## Arriving in a stacked follow-up

One more journal hook joins this plugin in a stacked PR: `preload-spine` (the
SessionStart hook that injects VardeMem's episodic spine, moved from the
monolith).

## Disabling

Name a hook in `$JOURNAL_DISABLED` (comma-separated) to turn it off for a
tenant; the legacy `$HARNESS_MANAGEMENT_DISABLED`, under which these shipped in
the monolith, is still honored.

## Testing

```bash
for t in plugins/journal/tests/smoke-*.py; do python3 "$t"; done
```

Each hook has a standalone smoke suite that runs it as a subprocess against a
throwaway `$HOME` and `$AGENT_VAULT`, so nothing touches a real journal and no
fixture is committed (the repo's `no-journal-content` guard forbids it).
