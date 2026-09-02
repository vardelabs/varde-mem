#!/usr/bin/env python3
"""Deterministic pre-scan + skip-gate for the consolidate-memory skill.

Walks the Claude Code auto-memory dir, computes the signals that decide whether
a consolidation pass is worth an LLM run, and writes a state JSON. Prints a
human/LLM-readable summary ending in a verdict line:

    VERDICT: SKIP      -> dir is healthy; the skill should PASS-silent
    VERDICT: NEEDS_RUN -> the skill should do a consolidation pass
    VERDICT: ERROR     -> the dir could not be found; do NOT PASS-silent (a
                          wrong-cwd run must not masquerade as a healthy dir)

Cheap front half of the loop (no model tokens). The skill runs this first and
early-exits on SKIP so quiet fortnights cost nothing.

Tenant-portable: the auto-memory dir defaults to the one for the current working
dir (`~/.claude/projects/<cwd-as-slug>/memory`). A scheduled job should pass
`--dir <abs path>` explicitly rather than rely on cwd (see the ERROR verdict).

Trip conditions for NEEDS_RUN (any one):
  - total memory files >= FILE_COUNT_TRIP        (index bloat)
  - mis-routed files (frontmatter type in the trip set) >= MISROUTE_TRIP

Topic-clusters are REPORTED as merge hints for the pass to weigh, but do not
trip a run on their own: filename-prefix clustering over-groups distinct facts
that share a first word, so a cluster alone is too weak a signal to spend a pass.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

STATE_FILE = Path.home() / ".local/state/memory-consolidation-scan.json"
# Canonical config path; the pre-move path is kept as a fallback. Shared with the
# enforce-memory-routing hook so scanner and hook agree on trip_types.
CONFIG_FILE = Path.home() / ".config/vardemem/memory-routing.json"
LEGACY_CONFIG_FILE = Path.home() / ".config/harness-management/memory-routing.json"

FILE_COUNT_TRIP = 22   # index bloat threshold (lean target is ~15-17)
CLUSTER_MIN = 3        # same-topic files reported as a notable merge hint
MISROUTE_TRIP = 2      # mis-routed files that warrant a routing pass

TYPE_PREFIXES = ("reference_", "feedback_", "project_", "user_")
DEFAULT_VAULT_TYPES = {"project", "user"}  # types that usually belong in the vault


def default_memory_dir() -> Path:
    """Auto-memory dir for the current working dir's project slug."""
    slug = str(Path.cwd()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / "memory"


def _trip_types_from(text: str):
    """Parse trip_types keys from config text; None if unusable (malformed JSON,
    non-dict, or no non-empty trip_types map).

    NOTE: `trip_types` is read regardless of `enabled`. `enabled` gates the
    write-time HOOK's advisory; the periodic scanner is a separate detector and
    still uses a tenant's custom vault-bound types to find misroutes even when the
    reflexive hook is switched off. Scanner and hook therefore agree on WHICH types
    are vault-bound (this shared trip_types set); `enabled` is an action gate on
    the hook, not part of the vault-bound definition."""
    try:
        cfg = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    tt = cfg.get("trip_types")
    if isinstance(tt, dict) and tt:
        return {str(k).strip().lower() for k in tt.keys()}
    return None


def load_vault_types() -> set:
    """Frontmatter `type:` values that count as mis-routed. Read from the same
    per-tenant config the enforce-memory-routing hook uses (`trip_types` keys),
    so scanner and hook agree on which frontmatter types are vault-bound. Mirrors
    the hook's read-order EXACTLY: only an ABSENT (unreadable) canonical
    `~/.config/vardemem/memory-routing.json` falls back to the legacy
    `~/.config/harness-management/memory-routing.json` (printing a one-line note
    when legacy serves). A PRESENT canonical is authoritative even when its content
    is null / malformed / empty: it does NOT trigger legacy fallback, matching the
    hook (which treats a present-but-null canonical as a present-authoritative
    degenerate config, not a fallback trigger). Conflating those two would be a
    scanner/hook divergence (a `null` canonical making the scanner serve legacy
    while the hook did not).

    Divergence-by-design in the terminal fallback: where NEITHER a present
    authoritative canonical NOR (on absence) legacy yields a usable `trip_types`
    map, the scanner returns the built-in DEFAULT_VAULT_TYPES so a periodic run
    still catches obvious misroutes, whereas the opt-in hook no-ops (`{}`) on the
    same input. The two agree on the vault-bound set whenever a usable config is
    present; they differ only here (detector default vs opt-in silence)."""
    # Only an ABSENT canonical serves legacy. A PRESENT canonical is authoritative
    # even if null / malformed, so a present-but-null canonical does not strand the
    # scanner on legacy while the hook stays on canonical.
    types = None
    ctext = None
    canonical_absent = False
    try:
        ctext = CONFIG_FILE.read_text(encoding="utf-8")
    except OSError:
        canonical_absent = True
    if not canonical_absent and ctext is not None:
        types = _trip_types_from(ctext)  # present: authoritative (may be None)
    else:
        try:
            ltext = LEGACY_CONFIG_FILE.read_text(encoding="utf-8")
        except OSError:
            ltext = None
        if ltext is not None:
            types = _trip_types_from(ltext)
            if types is not None:
                print(f"note: config fallback served from legacy path {LEGACY_CONFIG_FILE} "
                      f"(canonical {CONFIG_FILE} absent)")
    return types if types is not None else set(DEFAULT_VAULT_TYPES)


def parse_frontmatter_type(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    m = re.search(r"^\s*type:\s*([A-Za-z_]+)\s*$", text[3:end], re.MULTILINE)
    return m.group(1).strip().lower() if m else ""


def topic_key(stem: str) -> str:
    s = stem
    for p in TYPE_PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
            break
    parts = s.split("_")
    return parts[0] if parts else s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None, help="auto-memory dir to scan (default: derived from cwd)")
    ap.add_argument("--json", action="store_true", help="print the state JSON to stdout too")
    args = ap.parse_args()

    mem_dir = Path(args.dir).expanduser() if args.dir else default_memory_dir()
    if not mem_dir.is_dir():
        # Loud, NOT a clean SKIP: a scheduled job launched from the wrong cwd
        # would otherwise silently mask a bloated dir forever.
        print(f"WARNING: memory dir not found: {mem_dir}")
        print(f"  (cwd={Path.cwd()}; a scheduled job should pass --dir <abs path> "
              "rather than rely on cwd, or a real dir is silently skipped)")
        print("VERDICT: ERROR")
        return 0

    vault_types = load_vault_types()
    files = sorted(p for p in mem_dir.glob("*.md") if p.name != "MEMORY.md")

    clusters: dict[str, list[str]] = {}
    misrouted: list[dict] = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            text = ""
        ftype = parse_frontmatter_type(text)
        clusters.setdefault(topic_key(p.stem), []).append(p.name)
        if ftype in vault_types:
            misrouted.append({"file": p.name, "type": ftype})

    merge_candidates = {k: v for k, v in clusters.items() if len(v) >= 2}
    notable_clusters = {k: v for k, v in merge_candidates.items() if len(v) >= CLUSTER_MIN}

    # NEEDS_RUN triggers on index bloat or mis-routes only. Clusters are reported
    # below as merge hints but do not trip a run (the prefix heuristic over-groups).
    reasons = []
    if len(files) >= FILE_COUNT_TRIP:
        reasons.append(f"file count {len(files)} >= {FILE_COUNT_TRIP}")
    if len(misrouted) >= MISROUTE_TRIP:
        reasons.append(f"{len(misrouted)} mis-routed file(s) (type in {sorted(vault_types)})")

    verdict = "NEEDS_RUN" if reasons else "SKIP"

    state = {
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dir": str(mem_dir),
        "file_count": len(files),
        "verdict": verdict,
        "reasons": reasons,
        "vault_types": sorted(vault_types),
        "merge_candidates": merge_candidates,
        "notable_clusters": sorted(notable_clusters),
        "misrouted": misrouted,
        "thresholds": {
            "file_count_trip": FILE_COUNT_TRIP,
            "cluster_min": CLUSTER_MIN,
            "misroute_trip": MISROUTE_TRIP,
        },
    }
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"scan: warning, could not write state file: {e}")

    print(f"memory dir: {mem_dir}")
    print(f"files: {len(files)} (lean target ~15-17, trip at {FILE_COUNT_TRIP})")
    if merge_candidates:
        print("merge hints (same first-token, >=2 files; informational, not a trigger):")
        for k in sorted(merge_candidates):
            mark = "  <-- notable" if len(merge_candidates[k]) >= CLUSTER_MIN else ""
            print(f"  {k}: {', '.join(merge_candidates[k])}{mark}")
    else:
        print("merge hints: none")
    if misrouted:
        print(f"mis-routed (type in {sorted(vault_types)}, belong in vault): "
              + ", ".join(m['file'] for m in misrouted))
    else:
        print("mis-routed: none")
    if args.json:
        print(json.dumps(state, indent=2))
    print(f"VERDICT: {verdict}" + (f"  ({'; '.join(reasons)})" if reasons else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
