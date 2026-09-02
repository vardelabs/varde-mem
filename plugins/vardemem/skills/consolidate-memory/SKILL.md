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
SKILL.md in the plugin. Resolve the skill dir once:

```
SKILL_DIR="${CLAUDE_SKILL_DIR}"
[ -f "$SKILL_DIR/scan.py" ] || { echo "vardemem: scan.py not found (SKILL_DIR=$SKILL_DIR) -- is the plugin installed?" >&2; exit 1; }
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
    class only PROPOSES a migration, and a human lands it. So (d) applies when a
    human has already landed the catalog entry in an earlier cycle, never to work
    this run did. An unmerged branch preserves nothing, because it can still be
    rejected or abandoned.
  - **(d) carries two HARD limits, and they live in step 3's CANON-DUP bullet, not
    here.** Read them there before deleting anything against a plugin catalog: a
    SPLIT memory is never canon-dup, and the pointer is required rather than
    preferred. Stated once, in one place: a read-first section that restated the
    rule in a laxer form would be the copy a reader hits first, and the two would
    disagree.

## Autonomy boundary

- **Auto-apply** (no approval needed, the snapshot makes it revertable): merging
  topic-clusters, deleting canon-duplicates, and rebuilding `MEMORY.md`.
- **Vault-moves:** auto-apply when the fact has a clear, unambiguous vault home
  (commit the vault directly, no PR). If the right home is ambiguous, do NOT
  guess: list it for the operator and leave the scratch copy in place.
- **Plugin-scoped memories are PROPOSED, never routed.** This skill does not
  write to a plugin, push a branch, or open a PR. THIS CLASS reaches no other
  repo. (The sibling MIS-ROUTED path does commit to the vault repo, so the claim
  is scoped to this class, not to the skill.) For what happens to the MEMORY
  itself, step 3 is the single statement: it is left in place while the migration
  is UNLANDED, and reduced to a pointer once MERGED.
  Automating the routing is out of scope here: it needs a stateful,
  crash-recoverable, cross-repo workflow that prose instructions cannot express
  safely, so this class only proposes.
- **Never** post to a public channel, open a PR (for the memory dir or for a
  plugin), push to a PLUGIN repo, or touch another tenant's memory. Scoped to
  plugin repos on purpose: the MIS-ROUTED path above commits to the VAULT, and a
  blanket never-push would leave that fact as an unpushed local commit on one
  box while step 6 tells you the undo is reverting a published one.

## Procedure

### 1. Scan and gate

Pass the memory dir explicitly so a wrong cwd cannot silently point the scan at a
non-existent dir. A scheduled job should pass the tenant's absolute memory dir
directly rather than derive it:

```
MEMDIR="$HOME/.claude/projects/$(pwd | sed 's#/#-#g')/memory"
python3 "$SKILL_DIR/scan.py" --dir "$MEMDIR"
```

- **`VERDICT: SKIP`** -> the dir is healthy. **Read the override below BEFORE
  acting on this line.** On a SCHEDULED run: output one line
  (`PASS: auto-memory healthy (N files)`) and STOP, PASS-silent (post nothing).
  On an OPERATOR-REQUESTED run the override applies and the run continues.
  - **Override, operator-requested runs only.** When a human asked for this pass
    directly ("consolidate my memory", "memory hygiene pass"), a SKIP does not end
    the run: read the memories, and report any PLUGIN-SCOPED proposals (step 7),
    then stop. Skip steps 2 and 4 through 6.
    - **On this path step 3 is READ-ONLY, and that is an instruction, not a
      description.** You must ENTER step 3, because PLUGIN-SCOPED is defined
      there and there is no other way to find a proposal. But step 3's other
      branches MUTATE: CANON-DUP deletes a file and MIS-ROUTED commits to the
      vault and then deletes the scratch copy. Step 2 was skipped, so there is
      no snapshot to restore from, which would break the safety model's
      before-any-mutation invariant. So on this path:
      classify for PLUGIN-SCOPED only, and REPORT every other class as a
      deferred item instead of applying it. If something genuinely needs
      applying, say so and stop; the operator re-runs the full pass, which takes
      the snapshot first.
    - **This includes PLUGIN-SCOPED's OWN mutating branch.** Its precedence rule
      says CANON-DUP wins once a migration is MERGED, which means reducing the
      memory to a pointer, and that is a mutation with no snapshot behind it on
      this path. On the override, a LANDED memory is REPORTED as ready for
      pointer reduction and is NOT reduced. Called out explicitly because
      "classify for PLUGIN-SCOPED only" reads as permission to act on that class.
      The MERGED determination can also be wrong (see the remote-resolution
      rule), so this is exactly where a missing snapshot costs the memory's body.
    The scanner has no signal for a plugin-scoped memory (it is an ordinary
    `reference` write, which is why the write-time hook cannot catch it either), so
    without this override the class is unreachable whenever the dir is otherwise
    HEALTHY. (It stays reachable on `NEEDS_RUN`, when the dir trips the scanner
    for an unrelated reason such as index bloat or a vault-typed write, since
    that path enters step 3 and classifies every file.) A SCHEDULED run still stops
    here, unchanged. If you cannot tell
    whether a human asked, treat it as SCHEDULED and stop: the cost is a missed
    proposal, not a wrong action.
- **`VERDICT: NEEDS_RUN`** -> continue. The printed merge hints and `mis-routed`
  list (also in `~/.local/state/memory-consolidation-scan.json`) are your
  work-list, not a substitute for judgment. Note the merge hints are informational
  (the filename-prefix clustering over-groups); confirm each by reading the files.
- **`VERDICT: ERROR`** -> the memory dir was not found. Do NOT treat this as a
  healthy SKIP: surface the warning (the dir path and cwd it printed) to the
  operator and stop. A wrong `--dir` or cwd must never silently mask a bloated dir.

### 2. Snapshot (the undo)

```
bash "$SKILL_DIR/snapshot.sh" "$MEMDIR"
```

Copies the dir to `~/.local/state/memory-snapshots/<timestamp>/` and prunes to
the last 10. Restore a botched pass with
`rsync -a --delete <snapshot_dir>/ ~/.claude/projects/<slug>/memory/`.

### 3. Classify each memory (read them, don't trust filenames alone)

For every `*.md` except `MEMORY.md`, decide one of:

- **CANON-DUP** -> the fact is already **fully** stated somewhere durable.
  - Stated in `AGENTS.md`, `$AGENT_VAULT/CLAUDE.md`, or the per-host
    `CLAUDE.md`: **delete it.**
    - **PRECEDENCE FIRST: if it is also plugin-surface knowledge, it is
      PLUGIN-SCOPED, not CANON-DUP.** This branch deletes outright with no
      pointer, it is listed FIRST under "decide one of", and the two hard limits
      below are scoped to the plugin-catalog branch so neither reaches here.
      Being in your own canon is not preservation for this class's purpose: the
      whole point is that another org installing the plugin gets the fact, and
      `AGENTS.md` is internal to your org. Environment and behaviour facts are
      the shape most likely to be mirrored into a per-host `CLAUDE.md` and so
      most likely to hit this trap. Bites on the `NEEDS_RUN` path only, since the
      step-1 override is read-only.
  - Stated in a **MERGED** plugin catalog (a plugin's `known-gotchas`):
    **POINTER ONLY, never delete.** Split out of the sentence above rather than
    qualified after it: a lead that ended with an unconditional "Delete it." and
    put the prohibition lines lower is the same read-order trap the Safety model
    removed, reappearing inside the bullet readers were redirected TO.
  Two hard limits on the plugin-catalog case, both from real near-misses:
  - **A SPLIT memory is NEVER canon-dup.** Most plugin-scoped memories are MIXED,
    and the catalog excludes the per-deployment half BY CONSTRUCTION, so for
    exactly the memories this class touches the catalog can never satisfy "fully
    stated". The remainder (IDs, trust bindings, an operator's preferences) lives
    nowhere else, and deleting the source destroys the only copy.
  - **For a plugin catalog the pointer is REQUIRED, not preferred**, and outright
    deletion is forbidden. Reduce the memory to a one-line pointer at the owning
    plugin, keeping anything the catalog does not carry.
  - **Establish MERGED before relying on it.** There is no ledger, and a local
    checkout of an open feature branch reads identically to a merged one, so
    check the plugin's DEFAULT branch on the remote, not a working tree. If you
    cannot establish it, treat the memory as not preserved and leave it alone.
  - **Resolve the plugin's remote by INSTALL PROVENANCE, never by search.** The
    remote is the marketplace this plugin was installed from, and nothing else.
    Do not go looking for a repo that happens to contain a
    `skills/<plugin>/known-gotchas/references/gotchas.md`: another org's fork of
    this marketplace matches that shape, and so does any repo that copied the
    convention. **If you cannot identify the remote unambiguously, stop and treat
    it exactly like an unreachable remote** (above): not preserved, leave the
    memory alone, and say the check could not be made.

    This is the sharp edge of the whole class. Resolving to
    the wrong repo finds a catalog, declares MERGED, and CANON-DUP then applies
    "the pointer is REQUIRED" and reduces the memory to a pointer at a catalog
    that never received it. The body is gone from the only durable copy. That is
    the same lost-fact failure automating this class was meant to avoid,
    reappearing through an UNSPECIFIED step, so it gets an explicit rule rather
    than being left to judgment.
- **MIS-ROUTED** -> it maps to a vault category (person, project, company, agent,
  external/internal framework, environment, or a behavior rule). Promote it into
  the agent's vault under `$AGENT_VAULT`, folding it into the vault's existing
  canonical file for that category:
  - person -> that person's contact file
  - project / company -> that project's or company's canonical file
  - environment -> the vault's environment notes
  - behavior rule -> the vault's rules area
  Fold the fact into the existing canonical file (don't spawn 1 new file per
  fact); commit the vault directly; then delete the scratch copy. If no clear
  home exists, defer it to the report.
  - **PRECEDENCE: if it is ALSO plugin-surface knowledge, it is PLUGIN-SCOPED,
    not MIS-ROUTED.** Classify it there and leave it in place. This bullet is
    listed first and matches broad categories (a "behavior rule", an
    "environment"), so without this it silently pre-empts the class this skill
    exists to add, and it is the DESTRUCTIVE one: it commits to the vault and
    then deletes the scratch copy. Neither CANON-DUP limit reaches this path,
    since both are scoped to CANON-DUP, so a MIXED memory would lose its
    portable half into a vault rules file and have its per-deployment half
    deleted from the only place a later run could find it.
- **PLUGIN-SCOPED** -> it is operating knowledge about a SURFACE A PLUGIN WRAPS
  (its tools, its API, its runtime, its failure modes), true for anyone running
  that plugin rather than only for this agent on this box. Its home is the
  plugin, not the vault and not auto-memory. A memory is per-agent and
  per-machine, so knowledge parked there is invisible to every other agent and
  tenant and gets rediscovered the hard way; packaging it makes it versioned,
  reviewable, and shared.

  **PROPOSE ONLY. This class never writes to a plugin, never pushes a branch, and
  never opens a PR.** It identifies the memory, names the destination, and hands
  it to a human. Automating it would mean specifying a stateful, crash-recoverable,
  cross-repo workflow in prose for an LLM to execute, and prose cannot close a
  state machine: each added state has transitions the prose does not cover, and
  one such gap silently LOSES a memory's fact. That work wants real code with real
  state, not instructions.

  What to do:

  - **Report it** in step 7's digest as a proposed migration: the memory's file
    name, the destination plugin, the one-line reason it is plugin-scoped rather
    than vault-scoped, and any part that must be SPLIT OFF and stay behind (see
    the portability test below).
  - **While the migration is UNLANDED, leave the memory exactly where it is.** Do
    not delete it, do not reduce it to a pointer, do not edit it. It is the only
    durable copy until a human lands the plugin change, and this class produces no
    plugin change.
  - **Precedence, once MERGED is established.** A FULLY PORTABLE memory whose
    content is now in a merged catalog matches this class AND CANON-DUP, and the
    two prescribe opposite things: CANON-DUP requires the pointer, this class
    forbids it. **CANON-DUP WINS in that case**: reduce it to a pointer per
    CANON-DUP's plugin-catalog branch. The leave-it-alone rule above is scoped to
    UNLANDED, which is what its own justification says ("until a human lands the
    plugin change"). The overlap is not a corner case, because "a SPLIT memory is
    never canon-dup" narrows CANON-DUP's plugin branch to exactly the fully
    portable set, which is the same set LANDED lands on. A
    memory that was SPLIT stays here and stays put: its remainder lives nowhere
    else.
  - **Apply the portability test before proposing, and say what would have to
    split.** A marketplace plugin may be installed by another org, so only facts
    that survive "would this still be true for a different org, on a different
    server, with different people?" belong in one. IDs, server layout, trust
    policy, credentials, and a specific operator's preferences fail that test.
    Most such memories are MIXED and want splitting, not moving whole, so the
    proposal must name which half travels and which half stays.
  - **Say the claims are unverified.** A memory is a point-in-time observation and
    packaging it promotes it to shared fact, so whoever lands the change re-checks
    every claim against current source or a live tool schema first. Cited line
    numbers drift especially fast. Do not pre-verify them here and do not imply
    they were verified.
  - **If the plugin has no `known-gotchas` skill**, say so in the proposal and let
    the human decide whether the plugin earns one. Do not invent a new shape.
  - **CHECK THE DESTINATION FIRST, and report a landed migration as LANDED.**
    Before proposing, look at the destination catalog on the plugin's DEFAULT
    branch on the remote (the same check CANON-DUP prescribes above; a working
    tree on an open feature branch reads identically to a merged one). If the
    portable half is already there, this migration has SHIPPED: report it as LANDED
    for this run and do NOT carry it as an open proposal.

    **If the remote cannot be reached, report UNLANDED and say the check could
    not be made.** A scheduled headless run with no network, or a cron session
    with no credential for that remote, is the normal case rather than the exotic
    one, and both silent defaults are wrong in a stated way: guessing LANDED drops
    a real proposal, and skipping the check restores the permanent false alarm
    this bullet exists to prevent. Mirrors CANON-DUP's disposition for the same
    unreachable remote.

    Phrased as a per-run determination, not a one-shot: this class keeps no
    ledger, so the LANDED verdict is re-derived from the remote on every run and
    will be re-stated on every run. That is fine, and it is the same shape the
    UNLANDED bullet uses.

    Without that exit condition the class has none. CANON-DUP is the only class
    that retires a memory, and it is BARRED for a SPLIT memory, which most
    plugin-scoped memories are. So a migrated memory keeps matching PLUGIN-SCOPED
    forever (it is still plugin-surface knowledge, and it must STAY, because it
    holds the half that deliberately did not travel), and every later pass
    re-proposes a migration that already shipped.
  - **Expect to re-report a genuinely UNLANDED proposal every run, and say so in
    the digest.** This class edits nothing and keeps no ledger, so it cannot tell
    an already-reported memory from a new one. Phrase
    each proposal as a standing item rather than a new discovery. That repetition
    is the only pressure this class applies, which is exactly why the LANDED check
    above comes first: a repeat that can never be cleared is not pressure, it is a
    standing false alarm.

  For the shape of a destination catalog, the common pattern is a `known-gotchas`
  skill holding a five-field entry per gotcha; follow the destination plugin's own
  convention if it already has one.

- **MERGE** -> a genuine box/build cwd-scratch gotcha that shares a surface with
  other files (e.g. a tool's quirks, an environment's quirks). Group it.
  - **PRECEDENCE, same as MIS-ROUTED above: if it is plugin-surface knowledge, it
    is PLUGIN-SCOPED, not MERGE.** MERGE's definition ("a tool's quirks") is
    nearly word-for-word PLUGIN-SCOPED's ("its tools, its API, its runtime, its
    failure modes"), and step 4 DELETES the source files, so without this the
    same class gets destroyed a different way. Being listed after PLUGIN-SCOPED
    is not enough: "decide one of" is not stated as first-match, and step 1 hands
    the scanner's merge HINTS to the pass as a work-list before step 3 is ever
    read. Concretely, `topic_key()` strips the type prefix and clusters on the
    next token, so two memories about the same tool key together and print as one
    hint; a NEEDS_RUN pass would merge them and delete both, which breaks this
    class's own leave-it-in-place rule, destroys the filename step 7 must report,
    and welds the portable half onto per-deployment content the scope boundary
    says must stay behind. The guard is ENUMERATED rather than summarised: three
    branches carry a PRECEDENCE bullet naming PLUGIN-SCOPED, and they are MERGE
    (this one), MIS-ROUTED, and CANON-DUP's canon branch. CANON-DUP's
    plugin-catalog branch is governed from the other side instead, by
    PLUGIN-SCOPED's own precedence rule (CANON-DUP wins once MERGED) plus its two
    hard limits. Those are the four destructive paths.
- **KEEP** -> a standalone cwd-scratch fact with no cluster and no canon/vault
  home. Leave it.

### 4. Merge clusters

For each surface-cluster, write ONE file `reference_<surface>_gotchas.md` (or the
established name if it already exists) with standard frontmatter
(`name` / `description` / `metadata.type: reference`) and each source fact under
its own `## ` subsection, preserving substance. Then delete the source files. One
file per surface, clear subsections, no facts dropped.

### 5. Rebuild the index

Rewrite `MEMORY.md` to exactly one line per surviving file: a markdown link to
the file plus a short one-line hook. No dangling references to deleted files.

### 6. Finalize

The memory dir is not version-controlled, so there is nothing to commit here: the
writes are the durable result and the step-2 snapshot is the undo. (Any vault
files changed in step 3 ARE committed, in their own repos.) Verify the dir is
coherent: every `MEMORY.md` line points to an existing file, and no fact was
dropped.

**The snapshot does NOT cover everything a run can do.** It restores the memory
dir and nothing else. A MIS-ROUTED promotion commits to a vault repo, which is
undone by reverting that commit, not by restoring the snapshot. Say so in the
report rather than leaving "the snapshot is the undo" to cover an action it cannot
reach. (PLUGIN-SCOPED needs no entry here: it only proposes, so it changes nothing
outside this dir.)

### 7. Report

If the operator's job posts a digest, include: before -> after file count; what
merged; what moved to the vault (with destination file); what was deleted as a
canon-dup; **every PLUGIN-SCOPED PROPOSAL** (see below); any deferred items
needing the operator's call; and the snapshot path (the undo point), stated as
covering the memory dir only. OMIT the snapshot line on the step-1 override path:
it skips step 2, nothing was mutated, and naming a snapshot that was never taken
is worse than naming none. On a scheduled cadence, a SKIP run reports nothing
(PASS-silent). An operator-requested run is by definition not the scheduled
cadence, so it is not an exception to that rule: on the step-1 override path the
proposals ARE the output and are always reported.

**Each plugin-scoped proposal reports:** the memory file, the destination plugin,
the one-line reason it is plugin-scoped rather than vault-scoped, which parts must
SPLIT OFF and stay behind, and an explicit note that its claims are UNVERIFIED and
must be re-checked against current source before anything is packaged.

**And say this on every proposal, because the human acting on it is the one who
can leak:** packaging a memory into a marketplace plugin is a PUBLISH. If a
per-deployment fact slips past the portability test, the PUSH is the event, not
the merge, and review happens after it. Deleting the branch does not unpublish
anything: GitHub retains a PR's commits at `refs/pull/<N>/head` for the life of
the repo. So a leaked credential must be ROTATED, and a leaked identifier or
person's name is public and cannot be recalled.

## Notes

- Invoked by each tenant's own scheduled job (scheduling is per-tenant) and on
  demand. The job points here by skill name; this skill self-locates its scripts.
- The deterministic scanner is the only cheap gate; everything in steps 3-5 is
  judgment and must be done by reading the actual files.
- Routing targets follow the tenant's own vault layout (person files, project /
  company files, environment notes, a rules area). The companion
  `enforce-memory-routing` hook reads `~/.config/vardemem/memory-routing.json`
  (canonical) for its write-time advice, falling back to the pre-move
  `~/.config/harness-management/memory-routing.json` when the canonical path is
  absent. This scanner reads the same config for its `trip_types`, so scanner and
  hook agree on what is vault-bound.
