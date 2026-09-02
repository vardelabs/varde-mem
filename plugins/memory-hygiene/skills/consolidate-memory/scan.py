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
  - mis-routed files (frontmatter type in the config's trip set) >= MISROUTE_TRIP

Misroute detection is opt-in, like the hook: with no usable routing config the
scanner has no trip set, so only bloat can trip a run.

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
CONFIG_FILE = Path.home() / ".config/memory-hygiene/memory-routing.json"
LEGACY_CONFIG_FILE = Path.home() / ".config/harness-management/memory-routing.json"

FILE_COUNT_TRIP = 22   # index bloat threshold (lean target is ~15-17)
CLUSTER_MIN = 3        # same-topic files reported as a notable merge hint
MISROUTE_TRIP = 2      # mis-routed files that warrant a routing pass

TYPE_PREFIXES = ("reference_", "feedback_", "project_", "user_")


def default_memory_dir() -> Path:
    """Auto-memory dir for the current working dir's project slug."""
    slug = str(Path.cwd()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / "memory"


def _trip_types_from(text: str):
    """trip_types keys from config text; None if unusable (bad JSON, non-dict,
    `enabled` not true, or no trip_types). Honors `enabled` the same way the hook
    does, so a config with `enabled: false` yields no trip set and a scheduled pass
    does not route. The config is the only gate for both halves: there is no env
    off-switch, so to stop routing set `enabled: false` in the canonical config
    (authoritative even over a legacy fallback), not merely delete one config file."""
    try:
        cfg = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None
    tt = cfg.get("trip_types")
    if isinstance(tt, dict) and tt:
        return {str(k).strip().lower() for k in tt.keys()}
    return None


def load_vault_types() -> set:
    """Frontmatter `type:` values that count as mis-routed: the config's trip_types
    keys, so scanner and hook agree on what is vault-bound. Same read-order as the
    hook: only a MISSING canonical (FileNotFoundError) falls back to legacy; a
    present-but-unreadable or undecodable canonical is authoritative and does not
    fall back. Misroute detection is opt-in like the hook: with no usable config
    the trip set is empty, so a routing pass is never triggered for a tenant who
    has not configured routing (bloat still trips)."""
    types = None
    ctext = None
    canonical_missing = False
    try:
        ctext = CONFIG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        canonical_missing = True
    except (OSError, UnicodeDecodeError):
        pass  # present but unreadable: authoritative-unusable, no legacy fallback
    if not canonical_missing:
        types = _trip_types_from(ctext) if ctext is not None else None
    else:
        try:
            ltext = LEGACY_CONFIG_FILE.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            ltext = None
        if ltext is not None:
            types = _trip_types_from(ltext)
            if types is not None:
                print(f"note: config fallback served from legacy path {LEGACY_CONFIG_FILE} "
                      f"(canonical {CONFIG_FILE} missing)")
    return types if types is not None else set()


def parse_frontmatter_type(text: str) -> str:
    # Byte-identical matcher to the hook's frontmatter_type (see its docstring for
    # the deliberate line-oriented, dependency-free simplification and its bounded cost).
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    # Optional surrounding quotes and a trailing YAML inline comment (which needs
    # whitespace before the #, so a # inside the value is not mistaken for one); the
    # value may contain digits and hyphens (`type: project-fact`, `type: "project"`,
    # and `type: project # note` all read as the bare form).
    m = re.search(r"""^\s*type:\s*["']?([A-Za-z0-9_-]+)["']?(?:\s+#.*)?\s*$""", text[3:end], re.MULTILINE)
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
        except (OSError, UnicodeDecodeError):
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
