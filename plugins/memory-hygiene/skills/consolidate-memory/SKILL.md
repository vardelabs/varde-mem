---
name: consolidate-memory
description: >-
  Consolidate the Claude Code auto-memory dir: merge related single-fact files
  into surface-clusters, promote mis-routed facts to the agent's vault, PROPOSE
  plugin-scoped surface knowledge for migration into the plugin that owns it
  (report only, never writes to a plugin), delete facts already covered in canon
  (AGENTS.md / CLAUDE.md), and rebuild MEMORY.md, with a pre-pass snapshot that
  makes every change to the memory dir itself revertable (a vault commit is not).
  Run on a scheduled cadence
  (per-tenant job) or on demand ("consolidate my memory", "clean up auto-memory",
  "memory hygiene pass"). PASS-silent when the deterministic scanner returns SKIP.
---

# Consolidate Memory

Keep the Claude Code auto-memory dir (`~/.claude/projects/<slug>/memory/`) lean.
The index `MEMORY.md` loads into every session, so a bloated dir is pure context
cost. This skill is the recurring cleanup; the `enforce-memory-routing` hook in
this same plugin is the write-time prevention. Tenant-portable: it operates on
the current working dir's memory dir and the agent's vault (`$AGENT_VAULT`).

The scanner (`scan.py`) and snapshot helper (`snapshot.sh`) live alongside this
SKILL.md. Resolve the skill dir once:

```
SKILL_DIR="${CLAUDE_SKILL_DIR}"
[ -f "$SKILL_DIR/scan.py" ] || { echo "memory-hygiene: scan.py not found (SKILL_DIR=$SKILL_DIR) -- is the plugin installed?" >&2; exit 1; }
```

## Safety model (read first)

The auto-memory dir is harness-managed cwd-scratch and is deliberately NOT
version-controlled. The undo for a pass is a **pre-pass snapshot**: before any
mutation, copy the whole dir to a timestamped backup. A botched pass is restored
from the latest snapshot.

- **Before mutating:** run the snapshot helper (step 2). It copies the dir to
  `~/.local/state/memory-snapshots/<timestamp>/` and keeps the last 10.
- **Never delete a fact that isn't preserved elsewhere.** A memory may be deleted
  ONLY when its content now lives in (a) a merged file in this dir, (b) the vault,
  (c) canon (`AGENTS.md`, `<vault>/CLAUDE.md`, or the per-host `CLAUDE.md`), or
  (d) a **MERGED** plugin catalog (POINTER ONLY, never deletion). When in doubt,
  keep it.
- (d) is MERGED-only, and this skill never creates that state: the PLUGIN-SCOPED
  class only PROPOSES a migration, and a human lands it. An unmerged branch
  preserves nothing. (d) also carries two hard limits (a SPLIT memory is never
  canon-dup; the pointer is required, not optional); they live in step 3's
  CANON-DUP bullet. Read them there before deleting anything against a catalog.

## Autonomy boundary

- **Auto-apply** (the snapshot makes it revertable): merging topic-clusters,
  deleting canon-duplicates, and rebuilding `MEMORY.md`.
- **Vault-moves:** auto-apply when the fact has a clear, unambiguous vault home
  (commit the vault directly, no PR). If the home is ambiguous, do NOT guess: list
  it for the operator and leave the scratch copy in place.
- **Plugin-scoped memories are PROPOSED, never routed.** This skill does not write
  to a plugin, push a branch, or open a PR. Automating that routing is out of
  scope: it needs a stateful, crash-recoverable, cross-repo workflow that prose
  cannot express safely, so this class only proposes.
- **Never** open a PR (for the memory dir or a plugin), push to a PLUGIN repo,
  post to a public channel, or touch another tenant's memory. The MIS-ROUTED path
  does commit to the VAULT repo, so a blanket never-push would strand that fact as
  an unpushed local commit while step 6 says the undo is reverting a published one.

## Procedure

### 1. Scan and gate

Pass the memory dir explicitly so a wrong cwd cannot point the scan at the wrong
place. A scheduled job should pass the absolute memory dir directly:

```
MEMDIR="$HOME/.claude/projects/$(pwd | sed 's#/#-#g')/memory"
python3 "$SKILL_DIR/scan.py" --dir "$MEMDIR"
```

- **`VERDICT: SKIP`** -> the dir is healthy. On a SCHEDULED run: output one line
  (`PASS: auto-memory healthy (N files)`) and STOP, PASS-silent. On an
  OPERATOR-REQUESTED run, the override below applies.
  - **Override, operator-requested runs only** ("consolidate my memory", "memory
    hygiene pass"): a SKIP does not end the run. Read the memories, report any
    PLUGIN-SCOPED proposals (step 7), then stop. Skip steps 2 and 4-6.
    - **Step 3 is READ-ONLY on this path.** You must enter step 3 (PLUGIN-SCOPED
      is defined there and is the only class this path acts on), but its other
      branches MUTATE and step 2 was skipped, so there is no snapshot to restore
      from. So: classify for PLUGIN-SCOPED only, and REPORT every other class as a
      deferred item rather than applying it. This includes PLUGIN-SCOPED's own
      mutating branch (a LANDED memory's pointer reduction): report it as ready,
      do not reduce it. If something genuinely needs applying, say so and stop; the
      operator re-runs the full pass, which snapshots first.
    The scanner has no signal for a plugin-scoped memory (it is an ordinary
    `reference` write), so without this override the class is unreachable while the
    dir is otherwise healthy. It stays reachable on `NEEDS_RUN`, which enters step 3
    and classifies every file. If you cannot tell whether a human asked, treat it
    as SCHEDULED and stop.
- **`VERDICT: NEEDS_RUN`** -> continue. The printed merge hints and `mis-routed`
  list (also in `~/.local/state/memory-consolidation-scan.json`) are a work-list,
  not a substitute for judgment: the filename-prefix clustering over-groups, so
  confirm each hint by reading the files.
- **`VERDICT: ERROR`** -> the memory dir was not found. Do NOT treat this as a
  healthy SKIP: surface the warning (dir path and cwd) and stop. A wrong `--dir`
  must never silently mask a bloated dir.

### 2. Snapshot (the undo)

```
bash "$SKILL_DIR/snapshot.sh" "$MEMDIR"
```

Copies the dir to `~/.local/state/memory-snapshots/<timestamp>/` and prunes to
the last 10. Restore with
`rsync -a --delete --exclude=.memory-hygiene-snapshot <snapshot_dir>/ ~/.claude/projects/<slug>/memory/`
(the `--exclude` keeps snapshot.sh's ownership marker out of the restored dir).

### 3. Classify each memory (read them, don't trust filenames)

For every `*.md` except `MEMORY.md`, decide one of the following. **PLUGIN-SCOPED
wins over every other class** ("decide one of" is not first-match): CANON-DUP,
MIS-ROUTED, and MERGE are all DESTRUCTIVE, and each carries a precedence bullet
pointing here, because a memory misclassified into one of them loses the
per-deployment half that the scope boundary says must stay behind.

- **CANON-DUP** -> the fact is already **fully** stated somewhere durable.
  - Stated in `AGENTS.md`, `$AGENT_VAULT/CLAUDE.md`, or the per-host `CLAUDE.md`:
    **delete it.** But if it is ALSO plugin-surface knowledge, it is PLUGIN-SCOPED,
    not CANON-DUP: being in your own canon is not preservation for that class,
    whose whole point is that another org installing the plugin gets the fact, and
    `AGENTS.md` is internal to your org.
  - Stated in a **MERGED** plugin catalog (a plugin's `known-gotchas`): **POINTER
    ONLY, never delete.**
  Two hard limits on the plugin-catalog case, both from real near-misses:
  - **A SPLIT memory is NEVER canon-dup.** Most plugin-scoped memories are MIXED,
    and the catalog excludes the per-deployment half by construction, so it can
    never "fully" state them. The remainder (IDs, trust bindings, an operator's
    preferences) lives nowhere else; deleting the source destroys the only copy.
  - **The pointer is REQUIRED, not preferred.** Reduce the memory to a one-line
    pointer at the owning plugin, keeping anything the catalog does not carry.
  - **Establish MERGED before relying on it.** There is no ledger, and a local
    checkout of an open feature branch reads identically to a merged one, so check
    the plugin's DEFAULT branch on the remote, not a working tree. If you cannot,
    treat the memory as not preserved and leave it alone.
  - **Resolve the plugin's remote by INSTALL PROVENANCE, never by search.** The
    remote is the marketplace this plugin was installed from, nothing else. Another
    org's fork, or any repo that copied the `skills/<plugin>/known-gotchas/`
    convention, matches the same shape; resolving to the wrong repo finds a catalog,
    declares MERGED, and reduces the memory to a pointer at a catalog that never
    received it, destroying the body. **If you cannot identify the remote
    unambiguously, treat it as unreachable:** not preserved, leave the memory alone,
    and say the check could not be made.
- **MIS-ROUTED** -> it maps to a vault category (person, project, company, agent,
  framework, environment, or a behavior rule). Promote it into the vault under
  `$AGENT_VAULT`, folding it into the existing canonical file for that category
  (person -> that person's contact file; project/company -> that entity's file;
  environment -> environment notes; behavior rule -> the rules area). Don't spawn
  one new file per fact; commit the vault directly; then delete the scratch copy.
  If no clear home exists, defer it to the report.
  - **If it is ALSO plugin-surface knowledge, it is PLUGIN-SCOPED, not MIS-ROUTED.**
    This bullet matches broad categories and is destructive (commits to the vault,
    then deletes the scratch copy), so without the precedence a MIXED memory loses
    its portable half into a vault rules file and has its per-deployment half deleted.
- **PLUGIN-SCOPED** -> operating knowledge about a SURFACE A PLUGIN WRAPS (its
  tools, API, runtime, failure modes), true for anyone running that plugin, not
  just this agent on this box. Its home is the plugin, not the vault and not
  auto-memory. A memory is per-agent and per-machine, so knowledge parked there is
  invisible to every other agent and gets rediscovered the hard way.

  **PROPOSE ONLY. Never write to a plugin, push a branch, or open a PR.** Identify
  the memory, name the destination, hand it to a human.

  - **Report it** in step 7 as a proposed migration: file name, destination plugin,
    the one-line reason it is plugin-scoped, and any part that must SPLIT OFF and
    stay behind (see the portability test).
  - **While UNLANDED, leave the memory exactly where it is.** It is the only durable
    copy until a human lands the plugin change, and this class produces no change.
  - **Once MERGED, CANON-DUP wins:** a fully portable memory now in a merged catalog
    matches both classes, which prescribe opposite things; reduce it to a pointer
    per CANON-DUP. A memory that was SPLIT stays here and stays put -- its remainder
    lives nowhere else.
  - **Apply the portability test, and say what would split.** A marketplace plugin
    may be installed by another org, so only facts that survive "still true for a
    different org, on a different server, with different people?" belong in one. IDs,
    server layout, trust policy, credentials, and an operator's preferences fail it.
    Most such memories are MIXED, so name which half travels and which stays.
  - **Say the claims are UNVERIFIED.** A memory is a point-in-time observation;
    packaging it promotes it to shared fact, so whoever lands the change re-checks
    every claim against current source first. Cited line numbers drift fastest.
  - **If the plugin has no `known-gotchas` skill**, say so and let the human decide
    whether it earns one. Do not invent a new shape.
  - **Check the destination first, and report a landed migration as LANDED.** Look
    at the destination catalog on the plugin's DEFAULT branch on the remote (same
    check as CANON-DUP). If the portable half is already there, the migration has
    SHIPPED: report LANDED and do not carry it as an open proposal. **If the remote
    cannot be reached, report UNLANDED and say so** -- guessing LANDED drops a real
    proposal. This is a per-run determination (no ledger), re-derived every run.
  - **Expect to re-report a genuinely UNLANDED proposal every run.** This class
    keeps no ledger, so it cannot tell an already-reported memory from a new one;
    phrase each as a standing item. That repetition is its only pressure, which is
    why the LANDED check comes first: a repeat that can never clear is a false alarm.
- **MERGE** -> a genuine cwd-scratch gotcha that shares a surface with other files
  (a tool's quirks, an environment's quirks). Group it. **If it is plugin-surface
  knowledge, it is PLUGIN-SCOPED, not MERGE:** MERGE's definition is nearly
  word-for-word PLUGIN-SCOPED's, and step 4 DELETES the merged source files, so
  without the precedence the same class is destroyed a different way. (Note step 1
  hands the scanner's merge HINTS to the pass before step 3 is read, and
  `topic_key()` clusters two same-tool memories into one hint.)
- **KEEP** -> a standalone cwd-scratch fact with no cluster and no canon/vault home.
  Leave it.

### 4. Merge clusters

For each surface-cluster, write ONE file `reference_<surface>_gotchas.md` (or the
established name) with standard frontmatter (`name` / `description` /
`metadata.type: reference`) and each source fact under its own `## ` subsection,
preserving substance. Then delete the source files. No facts dropped.

### 5. Rebuild the index

Rewrite `MEMORY.md` to exactly one line per surviving file: a markdown link plus a
short one-line hook. No dangling references to deleted files.

### 6. Finalize

The memory dir is not version-controlled, so there is nothing to commit here: the
writes are the durable result and the step-2 snapshot is the undo. Verify every
`MEMORY.md` line points to an existing file and no fact was dropped.

**The snapshot does NOT cover everything.** It restores the memory dir only. A
MIS-ROUTED promotion commits to a vault repo, undone by reverting that commit, not
by restoring the snapshot. Say so in the report. (PLUGIN-SCOPED only proposes, so
it changes nothing outside this dir.)

### 7. Report

If the operator's job posts a digest, include: before -> after file count; what
merged; what moved to the vault (with destination file); what was deleted as a
canon-dup; **every PLUGIN-SCOPED PROPOSAL** (below); any deferred items needing the
operator's call; and the snapshot path, stated as covering the memory dir only.
OMIT the snapshot line on the step-1 override path (nothing was mutated; step 2 was
skipped). A scheduled SKIP run reports nothing; on the override path the proposals
ARE the output and are always reported.

**Each plugin-scoped proposal reports:** the memory file, the destination plugin,
the one-line reason it is plugin-scoped, which parts must SPLIT OFF and stay behind,
and an explicit note that its claims are UNVERIFIED and must be re-checked before
packaging.

**And on every proposal:** packaging a memory into a marketplace plugin is a
PUBLISH. If a per-deployment fact slips past the portability test, the PUSH is the
event and review happens after it. Deleting the branch does not unpublish anything:
GitHub retains a PR's commits at `refs/pull/<N>/head` for the life of the repo. So a
leaked credential must be ROTATED, and a leaked identifier or person's name is public.

## Notes

- Invoked by each tenant's own scheduled job and on demand. The job points here by
  skill name; this skill self-locates its scripts.
- The deterministic scanner is the only cheap gate; steps 3-5 are judgment and must
  be done by reading the actual files.
- Routing targets follow the tenant's own vault layout. The companion
  `enforce-memory-routing` hook reads `~/.config/memory-hygiene/memory-routing.json`
  (canonical), falling back to `~/.config/harness-management/memory-routing.json`.
  This scanner reads the same config for its `trip_types`, so scanner and hook agree
  on what is vault-bound.
