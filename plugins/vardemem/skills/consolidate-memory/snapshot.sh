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
# Restore: rsync -a --delete --exclude=.vardemem-snapshot <snapshot_dir>/ <memory_dir>/
set -euo pipefail

MARKER=.vardemem-snapshot   # ownership tag; only dirs carrying it are pruned

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
# Tag the snapshot as tool-created. The prune deletes ONLY tagged dirs, so a
# directory a user or another tool keeps in the snapshot root is never removed no
# matter what it is named. The restore command excludes the tag so it does not
# land back in the memory dir.
touch "$DEST/$MARKER"
echo "snapshot: $DEST"

# Prune to the last $KEEP tool-created snapshots (newest first). Ownership is the
# MARKER file, not the name: an untagged directory is never counted or deleted.
# Strip each entry's trailing slash before rm so a symlinked entry is removed as a
# link, never followed into its target.
ls -1dt "$SNAPROOT"/*/ 2>/dev/null | while read -r d; do
  if [ -e "${d%/}/$MARKER" ]; then
    printf '%s\n' "${d%/}"
  fi
done | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -rf "$old"
done
