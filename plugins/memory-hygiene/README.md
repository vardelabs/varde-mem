# memory-hygiene

Memory-hygiene tooling for a long-running agent's Claude Code auto-memory dir
(`~/.claude/projects/<slug>/memory/`). The auto-memory dir is cwd-scratch, and its
index `MEMORY.md` loads into every session, so it bloats when facts land in it that
belong in the agent's vault or in a plugin. This plugin keeps it lean from both
ends: a write-time reflex and a periodic cleanup.

## Install

```
/plugin marketplace add vardelabs/varde-mem
/plugin install memory-hygiene@varde-mem
```

## What ships

- **`skills/consolidate-memory/`**: the periodic cleanup. Merges related
  single-fact files into surface-clusters, promotes mis-routed facts to the
  agent's vault, proposes plugin-scoped facts for a human to land (report only,
  never writes to a plugin), deletes facts already covered in canon, trims every
  surviving file to a size budget (15 files, 40 KB in total, 6 KB a file), and
  rebuilds `MEMORY.md`. A deterministic scanner (`scan.py`) skip-gates the run so a healthy
  dir costs no model tokens; `snapshot.sh` takes a pre-pass snapshot so every
  change to the memory dir is revertable. Run it on a scheduled cadence or on
  demand ("consolidate my memory", "memory hygiene pass").
- **`hooks/enforce-memory-routing.py`** (PostToolUse on `Edit|Write|MultiEdit`):
  the write-time reflex. When a memory is written whose frontmatter `type:` is
  vault-bound (per the config's `trip_types`), it injects a non-blocking advisory
  so the agent re-routes it in the same turn. Fail-open, and a no-op unless a
  config opts in. The same script runs on PreToolUse `Write` as the block (see
  "Blocking" below).
- **`skills/consolidate-memory/migrate.py`**: a one-shot migration from the
  auto-memory dir into the homes a tree declares (see "Migrating" below).

## Configuration

Both components share one opt-in per-tenant config. With no config the hook
no-ops, so the pair is inert until a tenant turns it on.

- **Path:** `~/.config/memory-hygiene/memory-routing.json`.
- **Example:** [`hooks/memory-routing.config.example.json`](hooks/memory-routing.config.example.json).

```json
{
  "enabled": true,
  "trip_types": {
    "project": "a project or company fact. Fold it into your vault, then delete the scratch copy.",
    "user": "a person fact. Fold it into that person's contact file, then delete the scratch copy."
  }
}
```

`trip_types` maps a memory frontmatter `type:` to the routing advice to inject.
The scanner reads the same `trip_types` to decide which frontmatter types count as
mis-routed, so scanner and hook agree on what is vault-bound. `memory_path_regex`
optionally overrides the default auto-memory-path matcher. `$AGENT_VAULT` names the
vault the advisory points at.

The config is the only gate. With no usable config the hook no-ops; there is no
separate env off-switch. To stop routing, set `enabled: false` in the config or
delete it.

### Blocking

Set `"block": true` to refuse a new auto-memory file once the tree declares
`homes` for `vardemem rule`. The hook then denies a Write that would create a
memory file, and the reason names `vardemem route <kind>`. The kind comes from
`route_kinds`, which maps a frontmatter `type:` to a rule kind. With an unmapped
type, the reason lists the kinds `route_kinds` names. An Edit, or a Write over a
memory file that exists, still passes, so consolidation can merge and trim in
place.

The hook runs `vardemem` from the session's directory: `"vardemem"` in the config
when set, else the first one on `PATH`. When it cannot confirm a declared home
(no `vardemem`, no `homes`, an error, or a 3 second timeout), the write passes and
the advisory runs as before.

```json
{
  "enabled": true,
  "block": true,
  "trip_types": {"project": "a project or company fact. Fold it into your vault."},
  "route_kinds": {"feedback": "agent-rule", "project": "fact"}
}
```

### Migrating

```bash
python3 skills/consolidate-memory/migrate.py [--apply] [<memory-dir>]
```

Without `--apply` it prints its plan. With it, each memory whose `type:` maps
through `route_kinds`, and whose body is one paragraph that fits one rule line
(280 characters), is recorded with `vardemem rule`. Every other file is listed
as `unplaced` with the reason, for the operator to place by hand: a longer body,
an unmapped type, or a home in a repo. The migration deletes and edits no memory
file, and a rerun records nothing new.

## Tests

```bash
python3 tests/smoke-enforce-memory-routing.py
python3 tests/smoke-migrate.py
python3 tests/smoke-scan.py
python3 tests/smoke-snapshot.py
```

The hook suite runs `enforce-memory-routing.py` against a throwaway `$HOME`: the
opt-in gate, trip-type matching (bare, quoted, hyphenated, and inline-commented
YAML values), the silent cases, the `memory_path_regex` override, the null and
undecodable config handling, filename sanitization in the advisory, Windows-path
parity, and the hot-path no-disk-write guarantee. Its blocking cases run the
real `vardemem` against a synthetic tree: a new memory is denied with the route,
an existing file, an Edit and the index pass, and a missing `vardemem` or an
empty `homes` falls back to advice.

The migration suite builds a synthetic memory dir and tree. It checks the dry
run, what `--apply` records and reports, that no memory file changes, and that a
rerun is a no-op.

The scanner suite runs `scan.py`: the bloat/healthy/missing-dir verdicts, that
misroute detection is opt-in (no routing config, or `enabled: false`, means only
bloat trips), and that an unreadable config turns misroute detection off.

The snapshot suite runs `snapshot.sh`: a normal snapshot mirrors the memory dir,
two same-second passes get distinct non-nested dirs (no reliance on `date +%N`),
pruning keeps the last 10, and the prune never follows a symlinked entry.
