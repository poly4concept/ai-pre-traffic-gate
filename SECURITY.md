# Security

## What this is

A reference implementation, built and run on a single personal AWS account to
support a conference talk. It is not production-hardened and has had no external
security review. Phase 7 — multi-account separation, least-privilege review,
secrets handling — is explicitly **not started**.

Read it, copy the ideas, run it in an account you do not mind breaking. Do not
put it in front of anything that matters without doing that work yourself.

## The deliberately dangerous parts

The demo app exists only to be deployed, canaried and broken on demand, so it
ships with **controllable fault injection**: a configurable error rate, an
artificial latency mode and a memory-growth mode, driven by a DynamoDB config
record. This is how the gate gets a genuinely unhealthy target to observe.

If you deploy it:

- **Never make the demo app or its fault controls publicly reachable.** The
  function URL is IAM-authenticated in this repository and must stay that way.
  An unauthenticated fault endpoint is a denial-of-service switch with a URL.
- **Keep it isolated.** Separate account, or at minimum nothing else of value in
  the same one. Everything is tagged for teardown.
- **No real data, no real secrets, ever.** Nothing here is designed to hold them.
- **Turn the faults off when you are done.** `python scripts/inject_fault.py
  clear`, and the runbook in `docs/runbooks/synthesized-signals.md` has a
  teardown for every synthesised condition.

Phase 2.5c of the plan calls for pinning dependencies with published CVEs so
Amazon Inspector produces real findings. The rule there is **vulnerable
dependencies only, never deliberately exploitable application logic** — the goal
is a real finding in a real report, not a working exploit.

## Design properties worth knowing

Two things are load-bearing and should survive any fork:

- **The decision service holds no deploy permission.** It reads signals and
  writes a verdict. A malicious commit message or prompt injection cannot cause a
  deploy, because the component that reads untrusted text has nothing to grant.
- **The deterministic floor reads only security findings** — never the commit
  message, never diff statistics. Those are written by whoever wants the deploy.
  A floor built on self-reported facts is a floor the change can lower.

If you change either, you have removed the part that makes the rest safe.

## Reporting

Open a GitHub issue for anything non-sensitive. For something you would rather
not post publicly, use GitHub's **Report a vulnerability** button on the
Security tab, which opens a private advisory.

This is a personal project maintained in spare time around other work. I will
read what you send, but I cannot promise a response time, and there is no
supported version or patch pipeline. Treat it as source to learn from rather
than a dependency to take.
