# CLAUDE.md: varde-mem

This repo is private now and will be public. Read `README.md` for what VardeMem does and `CONTRIBUTING.md` for the pull request checklist before changing anything. `CONTRIBUTING.md` owns the checklist; the rules below are its safety subset, for agents. Every reachable commit must be safe to publish: a mistake stays in history even after a later commit removes it.

## Rules

- **No journal content.** No real journal text enters this repo, test fixtures included. `selftest` builds the synthetic trees it needs at run time.
- **Nothing private in the repo.** Do not add secrets, private hostnames, tenant paths, customer material, or operational logs. Deployment values live in the managed tree's `.vardemem.json`, outside this repo, never in a committed default.
- **No customer names or data.** Do not name a customer firm, its staff, or its data anywhere: code, comments, docs, tests, fixtures, branch names, commit messages, or issue and PR text. Write "a firm" or "the example firm", never an invented firm name, which can collide with a real one.
- **No internal names in content.** Do not name internal people, agents, hosts, or private repos in code, comments, docs, tests, or fixtures. Use a generic role ("the agent", "the operator", "the reviewer"). An example config or test that needs an agent name uses an `example-` name, such as `example-agent`. The `example-` prefix marks the name as fake.
- **No reviewer credits in comments.** A comment states the constraint or the reason. It does not credit a reviewer, a review round, or a date.
- **Standard library only.** `vardemem.py` is one file of Python 3 with no dependencies.
- **Keep the discovery boundary.** Auto-discovery reads only a tree on the trusted-roots list (`_trusted_journal_roots()` in `vardemem.py`) or one that `$AGENT_VAULT` names. Do not widen it.
- **If private material lands, stop and tell the operator.** This covers a secret, journal text, a customer identifier, or an internal name that reaches a commit, a branch, or a PR. Rotate any exposed secret, because deleting it does not make it private again. A follow-up commit that removes the text does not fix it, because history keeps it. Rewriting history is the operator's decision.
- **Run the checks before a PR:** `python3 vardemem.py selftest` from the repo root, and a test for any index-arithmetic or configuration defect you fix.
