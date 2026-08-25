# varde-mem

> A bounded read over an append-only journal, so a long-running agent can hold unbounded history at a fixed cost.

VardeMem keeps a decayed index over a directory of per-day journal files. The index holds one line per recorded day, plus a binary tree of rollups above those days. A read prints recent days verbatim and folds older ones into a single line covering a week, a month, a quarter, so a fixed line budget covers history of any length.

The index is **derived**. Every line comes from a journal file, the journals are never edited, and the index can be rebuilt from the journal directory alone. A wrong line is a re-summarize, never a lost memory.

One file, `vardemem.py`, standard library only. No install step, no build, no dependencies.

## Scope

- **The index.** One line per recorded day, rollups above them, and the arithmetic that decides which blocks a bounded read prints.
- **The write ritual.** `put` records a day, `due` names the next rollup the read needs, `roll` writes it. The read stays bounded only while those rollups get written.
- **Damage tolerance.** A git conflict, a hand edit, or a merge that changes the day set under a rollup is detected and named, and the read degrades to printing more lines rather than failing.
- **Configuration.** Journal paths and root discovery are configuration, so more than one tree can use the same tool.

## Non-goals

- **Summarizing.** The tool prints a prompt and takes a line back. Compression is the caller's job.
- **Storage.** Journals are plain markdown files that something else writes.
- **Search over full text.** `grep` searches the index; the index points at the file that holds the detail.

## Repository layout

| Path | Purpose |
| --- | --- |
| `vardemem.py` | The tool. Implementation plus `selftest`. |
| `.github/workflows/selftest.yml` | CI: parses the file, then runs `selftest` on Linux and macOS, from inside and outside the checkout. |

## Configuration

A tree that VardeMem manages carries a `.vardemem.json` at its root. That file is both the configuration and the marker: the directory holding it is the root.

```json
{
  "journal": "history",
  "archive": "archive",
  "spine": "SPINE.md"
}
```

| Key | Required | Resolved against | Meaning |
| --- | --- | --- | --- |
| `journal` | yes | the root | The directory of `journal-<YYYY-MM-DD>.md` day files. |
| `archive` | no | the journal | A second directory of `<YYYY-MM-DD>.md` day files, from before a cutover. Absent means there is none. |
| `spine` | no | the journal | The index file. Defaults to `SPINE.md`. |

There is no built-in default for `journal`: a journal layout is deployment knowledge, and this repository carries none of it.

Every value must name a path under its base. An absolute path, one with a `..` component, and one naming the base itself are all refused. For `journal` that is load-bearing: the wrong-tree warning below compares trees, and a journal that is not under its root makes that comparison meaningless. For `archive` and `spine` it is consistency. The check is lexical, so a symlink still escapes, and that is the supported way to put a journal outside its root: symlink it in. Two trees that symlink one shared journal are one journal, and the wrong-tree warning below knows it.

Symlink the journal **directory**, never the `spine` file. The index is written by atomic replace, which swaps the link rather than writing through it, so a symlinked `spine` silently stops receiving days. Give a shared journal the same relative name in both trees as well: the index's own header line records the journal it was rebuilt from, so two names rewrite it at each other on alternating writes.

### How the root is found

1. Walk up from the **current directory** for `.vardemem.json`. The first one found wins, and the directory holding it is the root. An ancestor along that walk that cannot even be checked (permissions, a broken symlink) stops the walk and refuses right there instead of falling through to step 2: it might be hiding the real config, and guessing past it is worse than refusing. A current directory that no longer exists (a worktree removed out from under a live shell) skips this step entirely rather than refusing here: there is nothing to walk from, which is not the same as walking and finding nothing.
2. If step 1 found nothing checkable, or never ran, read **`$AGENT_VAULT`** and look for the config at its root. The same rule applies here too: a config that exists but cannot be checked refuses rather than reporting no journal.
3. Otherwise refuse. A deleted current directory says so by name; every other case names both places checked.

**The order is load-bearing.** `$AGENT_VAULT` names where a session started, which is not where the caller is working: one variable set to a primary checkout wrote a day line into the wrong tree from a worktree session, silently and at exit 0. The tree you are standing in knows which journal you mean. Because the config file is checked in, every worktree carries its own copy without anyone copying anything.

Whichever of the two answers, VardeMem then warns on stderr if the current directory sits inside a **different** tree that holds the same journal layout and declares no config of its own. That is the case above: a checkout that says nothing is not a checkout that agrees with its neighbour. It warns and proceeds rather than refusing, because the read runs at session start and a refusal there turns "wrong tree, and you were told" into "no history at all".

## Install

There is nothing to build. Clone the repository and run the file:

```bash
python3 /path/to/varde-mem/vardemem.py read
```

To get a short command, link it onto `PATH`:

```bash
ln -s /path/to/varde-mem/vardemem.py ~/.local/bin/vardemem
```

## Commands

| Command | Does |
| --- | --- |
| `read [--budget N]` | The bounded read. Run it at session start. |
| `missing` | Days with a journal and no index line. |
| `draft <date>` | That day's topic headers and body, to compress from. Writes nothing. |
| `put <date> "..."` | Record, or rewrite, one day line. |
| `put <date> "..." --rebuild` | Insert a day the tree has outgrown, dropping the rollups it invalidates. |
| `due [--all]` | Rollups the current read needs, smallest first. |
| `roll <a>..<b> "..."` | Record one rollup. |
| `grep <regex>` | Search day lines and rollups. |
| `forget <a>..<b>` | Drop a bad rollup and everything built on it. |
| `forget --orphaned` | Drop every rollup the day set outgrew, plus any the parser rejected. |
| `selftest` | Property checks for the index arithmetic and the configuration layer. Needs no journal. |

`selftest` is the safety net. It runs from any directory, touches no real journal, and builds its own throwaway trees.

### Exit codes

| Code | Means |
| --- | --- |
| 0 | The command did what it says. |
| 1 | A failure, named on stderr. Usage errors are exit 1 too; there is no separate usage code. |
| 3 | There is no journal to read: the walk from the current directory completed with no config found and `$AGENT_VAULT` unset, `$AGENT_VAULT` names a tree with no config, the config does not set `journal`, or it does and the named path is absent or is not a directory. |
| 130 | Interrupted (Ctrl-C). |

`3` exists so a caller that runs this at every session start can stay quiet in a
tree that never had a journal, and stay loud about everything else. It means a
real check ran and the journal genuinely is not there; anything that could not
be checked at all -- a journal that IS configured but cannot be reached
(permissions, a broken symlink, a non-directory ancestor blocking the path), or
a current directory that no longer exists, so the walk itself never ran -- is
exit 1, same as any other failure. A caller that treats any non-zero code as
"nothing to show" hides those; key the silence on `3` alone.

The stderr promise on `1` is for the tool's own failures. `selftest` is a report
a human or CI reads, not a single error, so its per-check lines, including any
`FAIL` line, print to stdout along with everything else it prints; only its
final exit code is the pass/fail signal, the same as any other command.

## Provenance

The decay idea comes from OptMem (`github.com/VictorTaelin/OptMem`): tile history with aligned power-of-two blocks whose detail thins with age, so a fixed line budget covers unbounded history. Credit for the technique is his.

`cover()` and `_cover()` are our own implementation of it, written from a behavioural specification by a party who had not read his source, because OptMem states no license and we do not rely on permission to copy its code. The same applies to one string outside them, `roll_prompt()`'s instruction to the summarizer, whose original wording was his prose.

Matching prior behaviour is a correctness check. It is not evidence of provenance: an independent rewrite and a renamed copy can behave the same.

This repository was seeded from the rewritten implementation, never from the history that preceded it.

## Public-ready rule

Every reachable commit must be safe to publish.

Do not add credentials, private hostnames, tenant paths, real journal content, or operational logs. **Journals are real session history**, so no journal content enters this repository, test fixtures included. Fixtures are synthetic by construction. Keep deployment-specific values in the `.vardemem.json` of the tree being managed, which lives outside this repository.

## Status

Early development, private. The tool is in use; the repository scaffold is not complete.

## License

No license has been selected yet. Until one is added, all rights are reserved.
