# Phase 1 explained — the two levers

> Written to be taught from. Phase 1 contains no AI at all, and that is the
> point: it builds the two mechanisms an intelligent gate will later choose
> between, and proves both work by hand first.

**The frame for everything below:**

> A **canary** and a **halt** are two levers. Phase 1 builds the levers and
> pulls them manually. The model's only job, from Phase 3 onward, is to decide
> which lever to pull.

If that framing lands, the rest is mechanism.

---

## Lever 1 — the canary

### The problem

A Lambda function is normally all-or-nothing. Update the code and every
subsequent request runs it. There is no "send 10% of users to the new build."

AWS solves this with three pieces that stack on each other.

### Piece 1: versions

Publishing a Lambda freezes a snapshot and numbers it. Versions are immutable
forever. `$LATEST` is the mutable working copy and is never what users hit.

```text
  $LATEST    mutable scratch space
  v1         frozen, immutable
  v2         frozen, immutable
```

Immutability is what makes rollback trivial: `v1` still exists and still works,
so "roll back" just means "point at v1 again."

### Piece 2: the alias

A named pointer to a version. Callers address `demo-app:live`, never
`demo-app:2`.

```text
  alias "live" ────────► v1
```

The indirection is the whole value. You move the pointer; callers never change.

### Piece 3: weighted routing

The actual trick. An alias can point at **two versions simultaneously, with a
weight**:

```text
  alias "live" ──90%──► v1
               ──10%──► v2
```

Lambda rolls the dice per invocation. **This is the entire canary mechanism.** A
canary is: set the weight to 10%, wait, set it to 100%.

### So what is CodeDeploy for?

You could do this yourself with `update-alias` calls and `sleep`. CodeDeploy is
the thing that runs the sequence on a schedule and, more importantly, **reverses
it automatically when something goes wrong**. It is a state machine that owns
the alias weight over time.

A real run, annotated:

```text
  t=0     live → 100% v1              deployment created
  t=1s    live →  90% v1 / 10% v2     canary phase begins
  t=60s   live → 100% v2              interval elapsed, remainder shifts
```

### Two consequences worth understanding

**Terraform must formally yield the alias.** Terraform believes it owns
everything it declares, including where `live` points. CodeDeploy actively moves
it. Two systems, one value — so one must yield explicitly, which is the
`ignore_changes` lifecycle block in `demo_app.tf`. Without it, the next
`terraform apply` — even for an unrelated resource — resets the alias to
whatever version Terraform last recorded. That is a silent, unreviewed rollback
of production that surfaces hours later. (DECISIONS.md D-010.)

**The deployment group holds policy; the AppSpec holds the target.** The group
says *how* to shift traffic and *when* to roll back. The AppSpec, supplied per
deployment, says *which function, which alias, from which version to which*.
Recipe versus ingredients. That separation is why one deployment group can later
deploy the executor and the decision service without being redefined.

---

## Lever 2 — the halt

Entirely separate machinery. Nothing to do with CodeDeploy.

CodePipeline is a sequence of stages, and a stage can be "invoke this Lambda."
When it does, it hands the Lambda a **job ID** and blocks.

```text
  Pipeline stage "Gate"
        │
        ├── invokes Gate Lambda, passing a jobId
        │
        │   pipeline now WAITS.
        │   it ignores the Lambda's return value entirely.
        │
        └── Gate Lambda calls back out-of-band:
              PutJobSuccessResult(jobId)  → next stage runs
              PutJobFailureResult(jobId)  → pipeline stops here
```

The halt is not a special AWS feature. It is a Lambda that can say no.

### The detail that belongs on a slide

**CodePipeline ignores the Lambda's return value.** It waits for the callback.

So a gate that returns a perfectly structured `{"decision": "halt"}` and never
calls `PutJobFailureResult` **reports success, and the deploy ships**. The logs
show a halt. The audit record shows a halt. The change goes out anyway.

That failure is silent and survives casual review, which is why
`services/gate_stub/handler.py` carries a comment about it at the callback site.

### Fail closed, concretely

The gate resolves its verdict by matching an allow-list. Everything else halts:

| Configured value | Result |
|---|---|
| `allow` | allow |
| `halt` | halt |
| `yes`, `true`, `1`, `ALLOWED`, `deny` | **halt** |
| unset, empty, whitespace | **halt** |
| exception during evaluation | **halt** |

`yes` is the instructive one. It is a plausible thing for a human to write, it
means "allow" to any reader, and it halts.

This is a *default branch*, not an exception handler. The distinction matters:
an exception handler only covers the failures you anticipated, and this project
has already hit one nobody would have listed (an unsubscribed AWS Marketplace
agreement, FAILURES.md F-004). A catch-all default covers the failures nobody
has met yet, which is the population that actually matters.
(DECISIONS.md D-014.)

### Mode is separate from verdict

Two independent questions:

- **Verdict** — what does the gate think? (`allow` / `halt`)
- **Mode** — is anyone allowed to act on it? (`shadow` / `advisory` / `enforcing`)

Keeping them apart is what makes shadow mode a real mode rather than an off
switch. In shadow the verdict is computed and recorded in full, then
deliberately not acted upon — so every evaluation populates
`would_have_halted`, and counting those is how Phase 4 measures over-flagging.
A design where shadow short-circuits before producing a verdict would measure
nothing. (DECISIONS.md D-015.)

---

## How the two levers connect

At the end of increment 2 both levers exist and neither is wired to anything.
Increment 3 adds the pipeline:

```text
  GitHub push
       │
       ▼
  ┌──────────────────────────────────────────────────────┐
  │  CodePipeline                                        │
  │                                                      │
  │   Source  ──►  Build   ──►  Gate   ──►  Deploy       │
  │   GitHub      CodeBuild    Lambda      CodeDeploy    │
  │                              │             │         │
  │                         says yes/no    canaries      │
  └──────────────────────────────────────────────────────┘
```

The gate sits **before** deploy. Say no and the deploy stage never runs. Say yes
and CodeDeploy canaries.

From Phase 5, the verdict selects the lever:

| Verdict | Lever pulled |
| --- | --- |
| low risk | deploy all at once |
| medium risk | canary |
| high risk | halt and escalate |

---

## Why build this before the AI

Because debugging needs a control.

If the model goes in first and a deploy misbehaves, there are two suspects — the
model and the plumbing — and no way to separate them. Having watched a traffic
split happen and a pipeline get blocked, both by hand, there are now zero
suspects. Anything that breaks after Phase 3 is the model or the signals.

This is the phase CLAUDE.md warns is skipped and then debugged forever.

---

## What "proven" means here

Worth being precise, because it is a habit the whole project repeats.

The canary is not proven by CodeDeploy reporting `Succeeded`. That only means
CodeDeploy is satisfied with itself. It is proven by **observing both versions
serve real requests at the same time** — which is what `scripts/deploy_canary.py`
samples for.

The distinction is not pedantic. The most common way to get this wrong is an
alias reference or function URL that quietly resolves to `$LATEST`, which
CodeDeploy never touches. That produces a green deployment, a satisfied status,
and a traffic split of exactly zero. Nothing in the deployment status
distinguishes it from a working canary.

Same discipline as `scripts/check_bedrock_access.py`, which asserts that a
Converse call returns schema-valid JSON rather than that Bedrock is reachable.
**Assert the outcome you care about, not the nearest thing that is easy to
check.**

### And a caveat on the observation itself

Sampling the split is noisy. At a true 10% split with 20 samples per poll, about
one poll in eight catches zero canary responses purely by chance. A healthy
deployment will show `v1=100%` mid-canary and look broken.

Pinning a 10% split to ±2 points needs roughly 900 samples. The consequence for
Phase 5 is real: any rule shaped like "roll back if the canary error rate looks
high" is reading noise unless the canary receives far more traffic than a demo
produces. See the runbook's "How to read the percentages" section for the
arithmetic.
