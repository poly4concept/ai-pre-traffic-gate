# Phase 2 explained — teaching the gate what it is judging

> Written to be taught from. Phase 2 still contains no AI. It answers one
> question: **how does the gate find out what is being deployed?**

At the end of Phase 1 the gate could halt a pipeline, but it knew nothing. It
got woken up and asked "job 12345, yes or no?" — with no idea what commit was
being deployed, how large the change was, or what state the target was in.

Phase 2 is entirely about supplying those facts, and about being honest when it
cannot.

---

## The one idea to keep (Phase 2.1)

> **An absent signal is not a negative signal.**

"Amazon Inspector reported no vulnerabilities" and "Amazon Inspector was
unreachable" must never produce the same value.

If they do, a broken collector looks *exactly* like a clean bill of health. The
gate then fails **open** — not through a permissive decision, but through a
plausible-looking zero. Nothing errors. Nothing warns. The verdict reads as
confident.

That is the subtlest way a fail-closed system gets defeated, and it is why every
collector returns a status alongside its data:

| Status | Meaning |
| --- | --- |
| `OK` | Collected. Data present. |
| `DEGRADED` | Partial — a truncated page, a gap in a metric window. |
| `UNAVAILABLE` | Tried and failed. **Data is `None`.** |
| `SKIPPED` | Deliberately switched off. Not a fault, not reassurance either. |

Three mechanisms enforce it:

1. `data` is `None` unless status is `OK`. There is no default-valued
   `SecurityFindings()` object lying around to be mistaken for evidence.
2. The invariant is checked at runtime — `OK` with no data, or `UNAVAILABLE`
   *with* data, raises immediately rather than surfacing later as a confidently
   wrong verdict.
3. **Collectors cannot forget to fail closed, because they do not implement the
   failure path.** `collect()` is concrete on the base class; a subclass writes
   only the happy path and may raise freely. A contributor who never reads this
   document still gets it right.

That third one is the design choice worth stealing: make the safe behaviour
structural, not something each author has to remember.

---

## Getting real change context (Phase 2.2)

### Problem 1 — the gate cannot see the code

The gate Lambda has no copy of the repository. No git, no files. Three options:

| Approach | Cost |
| --- | --- |
| Give the gate GitHub API access | A stored secret to manage and rotate |
| Download the build artifact from S3 | S3 permissions plus unzipping logic |
| **Let whoever already has the code count it** | ← chosen |

CodeBuild already has the code — it just built it. So CodeBuild counts, and
passes a note forward.

The note travels as a **CodePipeline output variable**: a stage publishes
values, later stages read them. CodeBuild publishes `CHANGE_CONTEXT_B64`; the
Gate action reads `#{BuildVariables.CHANGE_CONTEXT_B64}`.

```text
  CodeBuild                                      Gate Lambda
  counts the diff  ────►  pipeline variable  ────►  reads it
```

Two easily-missed details:

- An action's output variables cannot be referenced **at all** unless the action
  declares a `namespace`. The console assigns these implicitly, so
  `#{SourceVariables.CommitId}` silently never resolves when you write the
  pipeline as code.
- The whole note must fit in **1,000 characters**.

### Problem 2 — CodeBuild could not see the history either

The genuine surprise of the increment. By default CodePipeline hands CodeBuild a
**zip of the files**, not a git repository. A zip has no history, so `git diff`
has nothing to compare against.

`OutputArtifactFormat = "CODEBUILD_CLONE_REF"` makes CodeBuild perform a real
clone instead. That is also why the build role gained connection permissions —
cloning requires minting a short-lived token from the GitHub connection.

### Problem 3 — the note format breaks on ordinary commit messages

The note is JSON, and CodePipeline fills it in by **literal text substitution**.
So the obvious implementation:

```json
{"commit_message": "#{SourceVariables.CommitMessage}"}
```

Given the commit message `fix the "off by one"`, produces:

```json
{"commit_message": "fix the "off by one""}
```

Broken JSON. The gate can no longer parse its own configuration. Nobody
attacked anything — that is a normal commit message.

**Fix:** base64-encode the entire payload. Base64 emits only letters, digits,
`+`, `/` and `=`, none of which can terminate a JSON string.

This deserves a slide of its own. Everyone worries about prompt injection.
This broke one layer *earlier* — in the config format, before the model was
involved at all — and it would have presented as a random pipeline bug.

### Problem 4 — can the note be trusted?

CodeBuild computes those numbers by running `buildspec.yml`, **a file inside the
repository being judged**. A change can therefore rewrite the code that measures
it: *"this 5,000-line payments rewrite? three lines."*

That is a different attack from prompt injection and arguably worse. Injected
prose is at least visible in the audit record; a forged integer looks exactly
like a real one, and it would corrupt Phase 4's measurements too.

Two responses, neither a complete fix:

**Label every fact with its provenance.**

| Fact | Source | Trust |
| --- | --- | --- |
| commit SHA, branch, author, timestamp | CodePipeline, read from the connection | trusted |
| lines added/removed, files touched | the build's own script | self-reported |
| deploy cadence | not collected yet | absent |

Phase 3 puts "diff statistics are self-reported" into the prompt, so the caveat
lands in the audit record rather than only in a design document.

**Cross-check the commit.** The SHA arrives **twice** — once from CodePipeline,
once inside the build's payload. Disagreement means the build described a
different commit than the one being deployed, and the gate refuses to proceed.

Its limit, stated plainly: this catches "described the wrong commit". It does
**not** catch "correct commit, dishonest line count". Provenance labelling
handles that by making the claim visible, not by verifying it.

### Problem 5 — when the build genuinely cannot count

Shallow clone, root commit, git absent. The script reports
`diff_stats_ok: false` — **not zeros.**

`0 files changed` is a real and meaningful state: an empty commit. It must never
double as "we could not tell". If it did, a broken collector would look
identical to a tiny safe change, and the gate would wave through a large one.

### A real run

Against this repository's own history:

```json
{
  "commit_sha": "dcc3c4bc37e199ff636f0b7df0e610691ca3860c",
  "commit_message": "Phase 1 increment 3: pipeline, executor, buildspec",
  "author": "Poly4",
  "committed_at": "2026-08-12T12:57:39+01:00",
  "diff_stats_ok": true,
  "files_changed": 26,
  "lines_added": 3739,
  "lines_removed": 41,
  "paths": ["CLAUDE.md", "DECISIONS.md", "..."],
  "paths_omitted": 14
}
```

820 base64 characters. Note `paths_omitted: 14` — the payload hit its size
budget, trimmed the file list, **and said so**. It shows 12 paths while still
reporting `files_changed: 26`. A silently truncated list would have looked like
a 12-file change, which is the same failure as a fabricated zero wearing
different clothes.

---

## Which signals are mandatory

Only **change context**. Lose it and there is no subject for a verdict — Phase 3
routes straight to human review without calling the model at all, because asking
a model to assess a change it was never told about produces a fluent, confident,
baseless answer. That is the worst output this system could generate, because it
is indistinguishable from a real one.

Security findings and target health are **degrading, not blocking**. A gate
brittle enough that people route around it is a worse security outcome than a
two-of-three verdict that is honestly labelled as such.

---

## Things computed, not inferred

`is_off_hours`, `is_low_traffic`, `is_fixable` are plain deterministic
functions, not questions for the model.

Anything a normal function can decide should not be delegated to a
probabilistic one. The model cannot get it wrong if it is never asked, and the
eval harness can assert on it.

`is_low_traffic` is the interesting one. A 0% error rate over 12 requests is not
evidence of health — the same small-denominator arithmetic that made the Phase 1
canary look broken. Every rate the gate is shown needs a denominator attached.

---

## Real security findings (Phase 2.3)

### The empty list that means nothing

This is the increment where the Phase 2.1 rule stopped being theory.

```text
$ aws inspector2 list-findings
{ "findings": [] }
```

No error. No warning. An empty list — byte-for-byte what a thoroughly scanned,
genuinely clean service returns.

The account's actual state:

```text
$ aws inspector2 batch-get-account-status
594380318102   DISABLED   lambda: DISABLED   ecr: DISABLED   ec2: DISABLED
```

Amazon Inspector had **never been switched on**. And the API for "show me the
vulnerabilities" said "there are none".

The naive collector is three lines. It is obviously correct, it passes review,
and it tells the verdict layer that a service nobody has ever scanned has zero
known vulnerabilities.

**That is worse than having no security signal at all.** A missing collector
prompts a question. A clean scan closes it.

### The fix: earn the right to interpret the answer

```text
  1. BatchGetAccountStatus   is Inspector on, and is Lambda scanning on?
  2. ListCoverage            is THIS function covered and actively scanned?
  3. ListFindings            only now does [] mean "clean"
```

Any of the first two failing gives `UNAVAILABLE` with a reason — never zero.

Two details worth keeping:

- **Two switches, not one.** Inspector can be `ENABLED` account-wide while Lambda
  scanning specifically is `DISABLED`. Checking only account status looks
  thorough and catches nothing.
- **A test asserts `ListFindings` is not even called when the guard fails.**
  Asserting only on the returned status would also pass for a collector that asks
  the question first and throws the answer away — which is one careless refactor
  away from using it.

### Inspector answers a different question than the gate asks

Amazon Inspector scans **deployed** resources. The gate runs **before** the
deploy. So findings collected at gate time describe the version *currently live*,
not the candidate about to replace it.

| The gate asks | Inspector answers |
| --- | --- |
| Is this change safe? | Is the thing this change would replace currently known to be vulnerable? |

Both are useful. They are not the same question, and nothing in the API surface
hints at the difference. Recorded as `is_candidate_artifact: False` on every
result, so the verdict layer and the audit record can both see which question got
answered.

Answering the real one needs a different mechanism entirely: an SBOM generated
during the build and scanned before deploy. Deferred, not rejected.

**The transferable lesson:** the obvious signal source for a question often
answers a subtly different one. Recording which question was actually answered
costs one field. Finding out after building a verdict layer on top costs a great
deal more.

### Three smaller judgements, same rule each time

- **`UNTRIAGED` severity becomes `UNKNOWN`, not `LOW`.** Folding unscored findings
  downward makes an unscored critical vanish into the noise floor; folding them
  upward makes the gate cry wolf. "3 critical, 1 unknown" is true. "3 critical,
  1 low" is not.
- **`fixedInVersion: "NotAvailable"` becomes `None`.** Kept as a literal string it
  reads as a version number, and `is_fixable` would report `True` for something
  with no patch anywhere.
- **Only `ACTIVE` findings are requested.** Reporting suppressed findings somebody
  deliberately accepted, or closed ones already fixed, is how a gate loses
  credibility and gets switched off.

### Truncation is DEGRADED, not silent

A function with 400 findings would blow any prompt budget. The collector carries
the 50 most severe and raises `PartialSignal`, which the base class turns into a
`DEGRADED` result carrying both the data *and* the admission:

```text
400 findings found, carrying the 50 most severe;
counts below are therefore a floor, not a total
```

Same reasoning as `paths_omitted` in the change-context payload. Until 2.3,
`DEGRADED` was a status with nothing able to produce it.

### The cost, and why it is optional

| Scan type | Per function per hour | ~Monthly |
| --- | --- | --- |
| Lambda standard (dependencies) | $0.00042 | $0.31 |
| Lambda code (application logic) | $0.00084 | $0.61 |

Verified from the AWS Price List API, not from memory. At three Lambdas: about
**$0.92/month** for standard scanning. Standard is enough — Phase 2.5 synthesises
vulnerable *dependencies*, and CLAUDE.md forbids deliberately exploitable
application logic, so code scanning would cost double to find nothing by design.

Declining to enable Inspector is a supported state. `security_scanning = false`
yields `SKIPPED` ("we chose not to look"); `true` runs the real collector, which
reports `UNAVAILABLE` with a reason while Inspector is off. Neither is ever
allowed to read as "nothing wrong".

### A bug the increment caused

Wiring the real collector in made the test suite start calling AWS for real —
runtime went from 2 seconds to 88. The collector builds its boto3 client lazily,
so every test touching the gate silently made three live API calls.

The slowness was the least of it. Such tests depend on credentials, on network,
and on live account state: they pass on one laptop and fail in CI, and enabling
Inspector later would have changed test outcomes with no code change.

Fixed structurally — an autouse fixture makes `boto3.client` raise unless a test
is explicitly marked `@pytest.mark.aws`, and a test verifies the guard itself
works. A guardrail nobody checks is not a guardrail.

---

## Live target health (Phase 2.4)

This is the signal a test suite **structurally cannot provide**, and the reason
the project exists. Tests tell you the code is correct. They cannot tell you the
service you are about to deploy into is currently on fire.

### The third empty answer, and the most flattering

```text
$ aws cloudwatch get-metric-data ...
[["inv", "Complete", []], ["err", "Complete", []], ["dur", "Complete", []]]
```

`StatusCode: "Complete"` — the query succeeded. `Values: []` — nothing in it. The
demo app had not been invoked in the window.

What the obvious implementation reports:

| Field | Naive result | Reality |
| --- | --- | --- |
| invocations | 0 | 0 — correct |
| error rate | **0%** | undefined |
| p99 latency | **0ms** | undefined |

That is not a missing signal. **A 0% error rate with a 0ms p99 is a better health
report than any real service could produce** — and `StatusCode: Complete` invites
you to trust it. Inspector's empty list merely failed to raise a concern; these
numbers actively assert excellence.

**A rate is not a number, it is a pair.** `0/0` is undefined, not zero. So
`error_rate_pct` and `p99_latency_ms` are `None` when there is no traffic, and
`has_health_evidence` separates *measured and fine* from *nothing to measure*.

There is an uncomfortable detail worth teaching: `sum(values) or 0` exists to
stop a crash on empty input. It succeeds — and converts a loud failure into a
silent falsehood. **The defensive guard is the bug.** The crash would have been
safer.

### The same ambiguity in the alarm list

`DescribeAlarms` also returned `[]` — the account has no alarms at all. Empty is
ambiguous between:

- *monitored, nothing firing* — positive evidence
- *not monitored* — no evidence

Opposite meanings, identical representation. Recorded as `has_alarm_coverage`, and
its absence makes the signal DEGRADED: the metrics are real, but the alarm
dimension says nothing however healthy they look.

Alarms are matched to a service by the **FunctionName dimension**, never by alarm
name. An alarm called `demo-app-errors` that actually watches a different
function would otherwise be counted as evidence about this one. Names drift;
dimensions are what CloudWatch evaluates.

**Expect every verdict to be DEGRADED until Phase 2.5 creates alarms.** That is
accurate rather than noisy — and it is the clearest possible argument for doing
Phase 2.5.

### Throttles count as errors

A throttled invocation never ran, and from the caller's side that is a failed
request. Excluding them would let a service pinned at its concurrency ceiling
report a healthy error rate while rejecting traffic — exactly the condition a
deployment gate should notice.

### The pattern, now that there are three

| Collector | How "nothing" arrives | What it looks like |
| --- | --- | --- |
| Inspector | `{"findings": []}` | no vulnerabilities |
| CloudWatch metrics | `Complete` + no datapoints | perfect health |
| CloudWatch alarms | `[]` | nothing firing |

**None of them error.** This is not a quirk of one service — it is what "no data"
looks like across AWS. After the third instance it stops being a discovery and
becomes the default assumption: *an API that returns a collection will return an
empty collection for reasons that have nothing to do with your question.*

---

## Still to come in Phase 2

- **Deploy cadence** — a collector reading the CodeDeploy control plane, so
  cadence carries `AWS_API` provenance rather than being self-reported or absent.
- **2.5 — synthesized-real signals.** Deliberately break the demo app so the
  real collectors parse real findings, rather than trusting mocks that agree
  with our assumptions about API shapes.
