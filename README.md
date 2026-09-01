# varde-mem

> Persistent memory for a long-running agent, at a fixed reading cost. One file of Python 3, standard library only, no dependencies.

An agent that runs for months piles up more history than it can afford to re-read at the start of every session. VardeMem keeps a **decayed index** over a directory of per-day journal files: recent days stay verbatim, and the older a stretch gets, the fewer lines it keeps, folding a week, then a month, then a quarter into a single line. One read covers all of history within a fixed line budget, however long the history grows.

The index is **derived**. Every line comes from a journal file, the journals are never edited, and the index rebuilds from the journal directory alone. A wrong line is a re-summarize, never a lost memory.

## What a read looks like

```
2026-08-31 | Shipped the release; two follow-ups filed.
2026-08-30 | Reviewed the parser change, merged after one round.
2026-08-29 | Flaky test traced to a shared fixture; isolated it.
2026-08-27..2026-08-28 | Cut the legacy adapter, stabilised CI.
2026-08-01..2026-08-24 | 3 weeks: migrated the store, retired v1's ingest path.
2026-05-01..2026-07-31 | 1 quarter: built the pipeline, shipped v1.
```

The most recent days print in full; older stretches collapse into ever-wider single lines. Everything above fits a fixed budget (default 120 lines); raise it with `read --budget N`.

## Install

Nothing to build. Clone the repo and link the one file onto your `PATH`:

```bash
ln -s /path/to/varde-mem/vardemem.py ~/.local/bin/vardemem
```

Now run `vardemem read`, or invoke the file directly with `python3 vardemem.py read`.

## The loop

Read at the start of a session, write at the end.

```bash
vardemem read                                    # the bounded view above
vardemem put 2026-08-31 "Shipped the release."   # record one day
vardemem due                                     # the next rollup the read needs, if any
vardemem roll 2026-08-01..2026-08-24 "3 weeks: migrated the store, ..."
```

VardeMem prints the text to compress and takes one line back; it does not summarize for you. The read stays cheap only while those rollups get written: skip the write and the index slowly decays toward one line per day.

## Commands

| Command | Does |
| --- | --- |
| `read [--budget N]` | The bounded read. Run it at session start. |
| `missing` | Days with a journal file and no index line. |
| `draft <date>` | That day's headers and body, to compress from. Writes nothing. |
| `put <date> "..."` | Record, or rewrite, one day line. |
| `put <date> "..." --rebuild` | Insert a day the tree has outgrown, dropping the rollups it invalidates. |
| `due [--all]` | Rollups the current read needs, smallest first. |
| `roll <a>..<b> "..."` | Record one rollup. |
| `grep <regex>` | Search day lines and rollups. |
| `forget <a>..<b>` | Drop a bad rollup and everything built on it. |
| `forget --orphaned` | Drop every rollup the day set outgrew. |
| `selftest` | Property checks for the arithmetic and the config layer. Needs no journal. |

## Configuration

A managed tree carries a `.vardemem.json` at its root. That file is both the config and the marker: the directory holding it is the root.

```json
{ "journal": "history" }
```

| Key | Required | Meaning |
| --- | --- | --- |
| `journal` | yes | Directory of `journal-<YYYY-MM-DD>.md` day files, resolved against the root. |
| `archive` | no | A second day-file directory (`<YYYY-MM-DD>.md`) from before a cutover, resolved against the journal directory. |
| `spine` | no | The index file, resolved against the journal directory. Defaults to `SPINE.md`, so the index sits beside the day files. |

Each path stays under its own base: no absolute paths, no `..`. To keep a journal elsewhere, symlink the directory in.

**Finding the root.** VardeMem walks up from the current directory for a `.vardemem.json`, but only trusts directories on a list you control, so opening an unrelated repo can never auto-read whatever config it happens to carry:

```
~/.config/vardemem/trust.json
```
```json
["/path/to/your/first-tree", "/path/to/your/second-tree"]
```

If the walk finds nothing trusted, VardeMem falls back to `$AGENT_VAULT` when it is set (naming that variable is itself an explicit choice, so the trust list does not gate it); otherwise it reports that there is no journal. The trust file is read from your passwd-entry home, not `$HOME`, so a repo you merely have open cannot redirect the tool at itself.

## Exit codes

| Code | Means |
| --- | --- |
| `0` | The command did what it says. |
| `1` | A failure, named on stderr. |
| `3` | No journal to read: nothing trusted was found and `$AGENT_VAULT` is unset or names a journal-less tree. Distinct from `1` so a session-start caller can stay quiet where there was never a journal, and loud everywhere else. |
| `130` | Interrupted (Ctrl-C). |

## How it stays bounded

VardeMem tiles history with aligned power-of-two blocks whose detail thins with age: the newest days are single lines, and each older region folds into a block twice as wide. A fixed line budget therefore covers unbounded history. When a block's summary has not been written yet, the read shows that stretch in finer lines and, past the budget, withholds the oldest and names the range it dropped, so it degrades rather than fails.

**Keep the write bounded.** Normal accrual owes little, usually nothing and at most a handful as the history grows, so a few rollups per session keep the read current. The one event that owes a lot at once is backfilling a day older than the whole history: it shifts every block boundary and invalidates the tree (73 rollups on a 400-day history, measured). Clear a few and let the rest re-accrue; the read stays usable meanwhile, just less compressed.

**Concurrency.** Two writers on one host serialize under a file lock. A real conflict needs two diverged checkouts recording the same day, and date-keying localizes it to one line or one file: a clashing index line is derived, so keep either side or re-summarize; a journal day file is real append-only history, so keep both.

## Files

| Path | Purpose |
| --- | --- |
| `vardemem.py` | The tool. Implementation plus `selftest`. |
| `CONTRIBUTING.md` | Pre-PR checklist and the boundaries a change keeps. |
| `.github/workflows/` | CI: `selftest.yml` runs the property checks; `no-journal-content.yml` keeps real history out of the repo. |

## Contributing

This repository ships no real journal content, test fixtures included; fixtures are synthetic by construction. See [CONTRIBUTING.md](CONTRIBUTING.md) for the pre-PR checklist.

## Credit

The decay idea is [OptMem](https://github.com/VictorTaelin/OptMem)'s: tile history with aligned power-of-two blocks whose detail thins with age, so a fixed line budget covers unbounded history. The implementation here is an independent reimplementation from a behavioural specification; OptMem states no license, so no code was copied.

## License

No license has been selected yet. Until one is added, all rights are reserved.
