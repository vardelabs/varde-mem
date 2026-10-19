# Security Policy

## Reporting a Vulnerability

Report a vulnerability privately through GitHub: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public
issue for a security problem.

If you cannot use GitHub's private reporting, email hello@vardelabs.com with
"Security: varde-mem" in the subject. Send the details only after we reply
with a private channel.

Include the command or hook involved, the commit you tested, the steps to
reproduce, and the impact you expect. Use a synthetic journal. Never send real
journal content.

## What to Expect

We acknowledge a report within 5 business days. We then confirm or reject the
issue, agree a disclosure date with you, and credit you in the fix unless you
ask us not to.

## Scope

Only the latest commit on `main` receives fixes.

These count as vulnerabilities here:

- auto-discovery that reads or writes a tree the operator did not put on the
  trusted-roots list or name in `$AGENT_VAULT`
- a `.vardemem.json` path that escapes its base directory
- a hook in `plugins/` that runs a command or writes a file outside the scope
  its README states
- a journal write that loses or edits an existing day file
