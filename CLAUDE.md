# CLAUDE.md: varde-mem

This repo is private now and will be public. Read `README.md` for what VardeMem does and `CONTRIBUTING.md` for the pull request checklist before changing anything. Every reachable commit must be safe to publish, so a mistake here cannot be fixed by a later commit.

## Rules

- **No journal content.** No real journal text enters this repo, test fixtures included. `selftest` builds the synthetic trees it needs at run time.
- **Nothing private in the repo.** Do not add secrets, private hostnames, tenant paths, customer material, or operational logs. Deployment values live in the managed tree's `.vardemem.json`, outside this repo, never in a committed default.
- **No customer names or data.** Do not name a customer firm, its staff, or its data anywhere: code, comments, docs, tests, fixtures, branch names, commit messages, or issue and PR text. Write "a firm" or "the example firm", never an invented firm name, which can collide with a real one.
- **No internal names in content.** Do not name internal people, agents, hosts, or private repos in code, comments, docs, tests, or fixtures. Use a generic role ("the agent", "the operator", "the reviewer"). An example config that needs an agent name uses an invented one.
- **No reviewer credits in comments.** A comment states the constraint or the reason. It does not credit a reviewer, a review round, or a date.
- **Standard library only.** `vardemem.py` is one file of Python 3 with no dependencies.
- **Keep the discovery boundary.** Auto-discovery reads only a tree on the trusted-roots list or one that `$AGENT_VAULT` names. Do not widen it.
- **Run the checks before a PR:** `python3 vardemem.py selftest` from the repo root, and a test for any index-arithmetic or configuration defect you fix.
