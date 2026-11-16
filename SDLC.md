# SDLC

`gh-toolkit:sdlc-workflow` holds the cross-repo workflow. This file holds the rules specific to this repo.

## Merging

A Varde Labs agent may merge its own PR here when all of these hold:

- The review cycle is complete.
- North approved the head.
- CI is green at the head, including the `scrub-guard` and `secret-scan` checks.

Merge with `gh pr merge --match-head-commit <head-sha>`, where the SHA is the head North approved.
