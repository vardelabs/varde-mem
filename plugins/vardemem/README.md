# vardemem

Memory-hygiene tooling for a long-running agent's Claude Code auto-memory dir
(`~/.claude/projects/<slug>/memory/`). The auto-memory dir is cwd-scratch, and its
index `MEMORY.md` loads into every session, so it bloats when facts land in it that
belong in the agent's vault or in a plugin. This plugin keeps it lean from both
ends: a write-time reflex and a periodic cleanup.

## What ships

- **`skills/consolidate-memory/`**: the periodic cleanup. Merges related
  single-fact files into surface-clusters, promotes mis-routed facts to the
  agent's vault, proposes plugin-scoped facts for a human to land (report only,
  never writes to a plugin), deletes facts already covered in canon, and rebuilds
  `MEMORY.md`. A deterministic scanner (`scan.py`) skip-gates the run so a healthy
  dir costs no model tokens; `snapshot.sh` takes a pre-pass snapshot so every
  change to the memory dir is revertable. Run it on a scheduled cadence or on
  demand ("consolidate my memory", "memory hygiene pass").
- **`hooks/enforce-memory-routing.py`** (PostToolUse on `Edit|Write|MultiEdit`):
  the write-time reflex. When a memory is written whose frontmatter `type:` is
  vault-bound (per the config's `trip_types`), it injects a non-blocking advisory
  so the agent re-routes it in the same turn. Fail-open, and a no-op unless a
  config opts in.

## Configuration

Both components share one opt-in per-tenant config. With no config the hook
no-ops, so the pair is inert until a tenant turns it on.

- **Canonical path:** `~/.config/vardemem/memory-routing.json`.
- **Legacy fallback:** `~/.config/harness-management/memory-routing.json`, read
  only when the canonical path is absent (this pair lived in a `harness-management`
  plugin before it moved here).
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

Disable per-tenant by naming a component in `VARDEMEM_DISABLED` (the legacy
`HARNESS_MANAGEMENT_DISABLED` is also honored), e.g.
`VARDEMEM_DISABLED=enforce-memory-routing`.

## Tests

```bash
python3 tests/smoke-enforce-memory-routing.py
python3 tests/smoke-scan.py
```

The hook suite runs `enforce-memory-routing.py` against a throwaway `$HOME`: the
opt-in gate, trip-type matching (including quoted YAML values), the silent cases,
the canonical/legacy config-path fallback, the dual-honor disable env, the
`memory_path_regex` override, the null- and undecodable-canonical handling,
Windows-path parity, and the hot-path no-disk-write guarantee.

The scanner suite runs `scan.py`: the bloat/healthy/missing-dir verdicts, that
misroute detection is opt-in (no routing config means only bloat trips), and that
a present-but-unreadable canonical config does not fall back to a stale legacy one.
