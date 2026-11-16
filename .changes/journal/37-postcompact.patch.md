preload-spine registers on SessionStart only and always replies as SessionStart, because the harness rejects hookSpecificOutput from PostCompact and SessionStart already fires after a compaction.
