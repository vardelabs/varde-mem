# Contributing

This repository is private during early development.
Every reachable commit must be safe to publish.

## Before opening a pull request

1. From the repository root, run `python3 vardemem.py selftest`. It needs no journal and builds its own throwaway trees, so it touches nothing real.
2. Confirm the diff contains no secrets, private hostnames, tenant paths, real journal content, customer material, or operational logs.
3. Keep deployment-specific values in the managed tree's `.vardemem.json`, which lives outside this repository, never in a committed default.
4. Add a `selftest` property check for any index-arithmetic or configuration defect you fix.

## Boundaries

No journal content enters this repository, test fixtures included. Journals are real session history; fixtures are synthetic by construction, and `selftest` generates the trees it needs at runtime.

The tool auto-discovers a journal only from a tree on the trusted-roots list, or from a tree `$AGENT_VAULT` explicitly names; a repo you merely have open never auto-reads. It then reads and writes the journal the config points at, which may be a symlink outside the tree, an explicit operator choice. Keep the discovery boundary: do not widen auto-discovery past the trust list, and do not let a read reach a tree the operator did not name.
