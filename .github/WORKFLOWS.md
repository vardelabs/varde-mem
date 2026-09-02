# Continuous integration

Repository workflows live in `.github/workflows/` and are part of the public-ready boundary.

## Rules

- Keep workflows read-only by default and pin third-party actions to reviewed commit SHAs.
- A workflow must run the validation it claims to run.
- Do not use `pull_request_target` for untrusted pull-request code.

## Current workflows

- `selftest.yml`: the `selftest` job parses `vardemem.py`, then runs `selftest` on the runner from inside and outside the checkout, so a check that quietly depended on sitting next to the file fails here rather than later. The `plugin-tests` job runs the `memory-hygiene` plugin's stdlib-only smoke suites (hook, scanner, snapshot helper), so those tests are validated in CI rather than only by hand.
- `no-journal-content.yml`: fails CI on any commit the event introduces that adds a journal day file or the derived index, the structured history artifacts, so they cannot enter the repository in any reachable commit. The workflow's own header comment states the full scope, including the by-name and by-header matching, the LFS and submodule rejection, the arbitrary-prose limit, and the fail-closed and NUL-safety guarantees.
