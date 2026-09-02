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
# Second-resolution timestamp plus PID and $RANDOM, NOT `date +%N`: BSD/macOS date
# has no %N and would emit a literal "N", so two same-second passes would collide
# and the second `cp -r` would nest inside the first snapshot and corrupt it. The
# guard re-rolls on the (astronomically unlikely) collision so the destination is
# always fresh and `cp -r` mirrors the memory dir rather than nesting into it.
DEST="$SNAPROOT/$(date +%Y%m%d-%H%M%S)-$$-$RANDOM"
while [ -e "$DEST" ]; do
  DEST="$SNAPROOT/$(date +%Y%m%d-%H%M%S)-$$-$RANDOM"
done
cp -r "$MEMDIR" "$DEST"
echo "snapshot: $DEST"

# Prune to the last $KEEP snapshots (newest first). Match ONLY tool-created
# snapshots by their name shape -- eight date digits (YYYYMMDD) then a dash -- so
# the glob cannot select an unrelated directory kept in the snapshot root, not even
# one whose name merely starts with a digit (e.g. 2024-backup). Strip the glob's
# trailing slash before rm: `rm -rf "symlink/"` would FOLLOW a symlinked entry and
# delete its target's contents, whereas `rm -rf "symlink"` removes only the link.
ls -1dt "$SNAPROOT"/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-*/ 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -rf "${old%/}"
done
