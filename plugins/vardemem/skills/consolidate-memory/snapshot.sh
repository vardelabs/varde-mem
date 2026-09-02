#!/usr/bin/env bash
# Pre-consolidation snapshot of the Claude Code auto-memory dir.
#
# The memory dir is harness-managed cwd-scratch and is deliberately NOT
# version-controlled. This timestamped copy is the undo for a consolidation
# pass: the skill runs it before any mutation, and a botched pass is restored
# from the latest snapshot. Keeps the last 10 snapshots and prunes the rest.
#
# Tenant-portable: the memory dir defaults to the one for the current working
# dir (~/.claude/projects/<cwd-as-slug>/memory); override with arg 1.
#
# Usage:   snapshot.sh [memory_dir]
# Restore: rsync -a --delete <snapshot_dir>/ <memory_dir>/
set -euo pipefail

if [ "${1:-}" != "" ]; then
  MEMDIR="$1"
else
  SLUG="$(pwd | sed 's#/#-#g')"
  MEMDIR="$HOME/.claude/projects/$SLUG/memory"
fi
SNAPROOT="$HOME/.local/state/memory-snapshots"
KEEP=10

if [ ! -d "$MEMDIR" ]; then
  echo "snapshot: memory dir not found: $MEMDIR" >&2
  exit 1
fi

mkdir -p "$SNAPROOT"
# Nanosecond suffix so two runs in the same second get distinct dirs (otherwise
# the second cp -r nests inside the first snapshot and corrupts its shape).
DEST="$SNAPROOT/$(date +%Y%m%d-%H%M%S-%N)"
cp -r "$MEMDIR" "$DEST"
echo "snapshot: $DEST"

# Prune to the last $KEEP snapshots (newest first). Strip the glob's trailing
# slash before rm: `rm -rf "symlink/"` would FOLLOW a symlinked entry and delete
# its target's contents, whereas `rm -rf "symlink"` removes only the link.
ls -1dt "$SNAPROOT"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -rf "${old%/}"
done
