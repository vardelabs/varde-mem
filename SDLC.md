# SDLC

The organization's cross-repo workflow applies. This file holds the rules specific to this repo.

## Merging

A Varde Labs agent may merge its own PR here when all of these hold:

- The "Review cycle converged" comment is on the head.
- The closing reviewer approved the head.
- CI is green at the head, including the `scrub-guard` and `secret-scan` checks.

Merge with `gh pr merge <n> --squash --match-head-commit <head-sha>`, where the SHA is the head the closing reviewer approved.
