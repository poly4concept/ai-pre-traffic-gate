# Failures

Things that did not work, and why. These are deliverables — the talk explicitly
covers what the gate got wrong. Record the failure even when the fix was
trivial, because the *class* of failure is usually the interesting part.

Format: what happened, what the error actually looked like, why it happened,
the fix, and the generalisable lesson.

---

## F-001 — IAM policy written against the wrong AWS account

**Phase:** 0

**Symptom:**

```
AccessDeniedException: User: arn:aws:iam::594380318102:user/ai-agent is not
authorized to perform: bedrock:InvokeModel on resource:
arn:aws:bedrock:us-east-1:594380318102:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0
because no identity-based policy allows the bedrock:InvokeModel action
```

**What happened:** The Bedrock invoke policy was drafted using the account ID
returned by the `default` AWS CLI profile (`057455321407`). The `ai-agent` and
`poly4` users live in a different account (`594380318102`). The policy was
syntactically valid, applied cleanly, and granted nothing.

**Why it happened:** Multiple AWS profiles on one machine pointing at different
accounts, and an assumption that the default profile represented the project
account. It did not — it belonged to an unrelated account entirely.

**Fix:** Corrected the inference-profile ARN to name `594380318102`.

**Lessons:**

1. Resolve the account ID from the *specific profile you are configuring*, not
   from whatever the default profile happens to be.
2. An IAM policy that grants nothing fails identically to one that was never
   applied. The error message names the resource it wanted — read the account
   ID in that ARN and compare it to the one in the policy.
3. The Terraform `allowed_account_ids` provider argument now guards the same
   mistake on the infrastructure side.

---

## F-002 — Assuming bare foundation-model IDs are invokable

**Phase:** 0

**What happened:** The first draft of the Bedrock IAM policy granted
`bedrock:InvokeModel` on `foundation-model/anthropic.*` ARNs, on the assumption
that a model ID like `anthropic.claude-sonnet-4-5-20250929-v1:0` could be
passed directly to Converse.

**Why it happened:** That used to be true, and most documentation and tutorials
still show it. It is no longer true for current models.

**The actual state of things** (verified by `ListFoundationModels` in
us-east-1, August 2026): every current Anthropic text model reports
`inferenceTypesSupported: [INFERENCE_PROFILE]`. The only model still offering
`ON_DEMAND` is `anthropic.claude-3-haiku-20240307-v1:0`. So the bare model ID
cannot be invoked at all, and a policy granting only `foundation-model/*` ARNs
grants nothing usable.

**Fix:** Target `us.*` inference profile IDs, and grant on the
`inference-profile/us.anthropic.*` ARN in the calling account *plus* the
`foundation-model/anthropic.*` ARNs in every region the profile can route to.

**Lesson:** "the model is listed in the region" and "I can invoke the model"
are different claims, and so is "my IAM policy names the right resource type".
`scripts/check_bedrock_access.py` tests all three separately for this reason.
This is exactly the class of stale-training-data assumption CLAUDE.md warns
about, and it was caught by running the call rather than by reasoning about it.

---

## F-003 — `.claude/settings.json` env vars need a session restart

**Phase:** 0

**What happened:** Added `env: { AWS_PROFILE: "ai-agent" }` to the project's
`.claude/settings.json` to pin the agent to the read-only identity. The very
next command still ran as the `default` profile and reached the wrong account.

**Why it happened:** Settings are read at session start. Writing the file
mid-session does not re-export the environment for already-running tooling.

**Fix:** Pass `--profile ai-agent` explicitly until the session is restarted.

**Lesson:** A guardrail that is not verified is not a guardrail. The check is
one command — `aws sts get-caller-identity` — and it takes a second. This is a
small version of the same point the whole project makes: assert the state you
believe you are in.

---

## F-004 — `ResourceNotFoundException` that has nothing to do with a missing resource

**Phase:** 0

**Symptom:**

```text
ResourceNotFoundException: Model use case details have not been submitted for
this account. Fill out the Anthropic use case details form before using the
model. If you have already filled out the form, try again in 15 minutes.
```

**What happened:** With the corrected IAM policy applied (F-001) and a valid
`us.*` inference profile ID (F-002), `Converse` still failed. The error code is
`ResourceNotFoundException`, which reads as "that model ID does not exist" —
the natural next move is to go hunting for a typo in the model ID, or to
re-check the region. Both would have been dead ends.

**Why it happened:** Anthropic models on Bedrock sit behind an account-level
gate that is separate from IAM: a use case details form submitted once per
account via the Bedrock console. Until it is approved, every Anthropic model in
the account is un-invokable regardless of how correct the IAM policy is.

**The distinction that matters here** — there are now *three* independent
things that must be true before a Bedrock call succeeds, and each one fails
with a different error:

| Layer | Question | Failure mode |
| --- | --- | --- |
| Account | Has the use case form been submitted and approved? | `ResourceNotFoundException` |
| IAM | Does the principal have `bedrock:InvokeModel` on the right ARNs? | `AccessDeniedException` |
| Request | Is the model ID a valid, invokable target in this region? | `ValidationException` |

Only the middle one is guessable from the error text.

**Fix:** Submit the Anthropic use case details form in the Bedrock console
(Model access → Anthropic), wait for approval, retry.

**Lessons:**

1. Trust the error *message* over the error *code*. Boto3 surfaces both; the
   code is a coarse bucket and here it actively misleads. Any operational
   runbook for this system needs to log the full message, not just the code.
2. This is directly relevant to the gate's fail-closed design. A
   `ResourceNotFoundException` from Bedrock at verdict time is ambiguous
   between "the model access was revoked", "the model was deprecated", and
   "someone typo'd the model ID in config". All three must route to human
   review — which is precisely why the fail-closed default is a catch-all
   rather than a list of handled exception types. If we had enumerated
   "throttling and timeouts" as the failure cases to guard, this one would have
   sailed through.
3. Phase 0's job is to find these before Phase 3. It did.
