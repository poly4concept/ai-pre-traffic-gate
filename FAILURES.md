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

```text
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

| # | Gate | Question | Error code |
| --- | --- | --- | --- |
| 1 | Use case | Has the Anthropic use case form been submitted? | `ResourceNotFoundException` |
| 2 | Marketplace IAM | Can the caller perform `aws-marketplace:Subscribe`? | `AccessDeniedException` |
| 3 | Billing | Does the account have a valid payment instrument? | `AccessDeniedException` |
| 4 | Bedrock IAM | Does the principal have `bedrock:InvokeModel` on the right ARNs? | `AccessDeniedException` |
| 5 | Request | Is the model ID valid and invokable in this region? | `ValidationException` |

Five gates, three error codes, and **three different gates share
`AccessDeniedException`** — the code cannot distinguish them. Only the message
can, and the message is prose that AWS is free to reword.

They must be cleared in order, and each is invisible until the one before it is
satisfied. Clearing the use case form did not produce success; it produced a
*different* failure. Clearing that produced a third. There is no API that
reports "here is everything standing between you and an invocation" — you
discover each rung by standing on the previous one.

**Gate 3 is not a permissions problem at all.** `INVALID_PAYMENT_INSTRUMENT`
means the account has no valid payment method on file. AWS Marketplace will not
complete a subscription without one, *even when the subscription is free and
the account holds promotional credits* — credits are not a payment instrument.
Nothing in this repository, and no IAM policy, can clear it.

That is worth dwelling on for a talk about autonomous pipelines. Four of the
five gates are things an engineer can reason about from inside the codebase.
The fifth is a billing fact about the account, it surfaces through the same
error code as two genuine permissions problems, and the only thing separating
them is a substring in an English sentence.

**Fix:** Submit the Anthropic use case details form in the Bedrock console,
wait ~15 minutes for it to propagate, retry.

**A second stale assumption inside the fix.** The obvious instruction — "go to
Bedrock → Model access and enable the model" — is itself out of date. That page
has been **retired**. Serverless foundation models now auto-enable on first
invocation in any commercial region; there is no longer a list of checkboxes to
tick. What survives is the Anthropic-specific use case form, now reached via
**Model catalog → any Anthropic model**. So the console page that every
tutorial and every screenshot points at no longer exists, while the gate it
used to manage still does — in a different place, applying to one vendor only.

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
3. Console navigation is not a stable interface and does not belong in a
   runbook without a date stamp. The API-level check in
   `scripts/check_bedrock_access.py` stays correct across console redesigns
   because it asserts the *outcome* — can I invoke — rather than the procedure.
4. Phase 0's job is to find these before Phase 3. It did, repeatedly — the
   gate, the stale instructions for clearing it, and then a second gate hiding
   behind the first.

---

## F-005 — Two Lambdas, one module name: tests asserting against the wrong service

**Phase:** 1, increment 2

**Symptom:** adding the gate stub broke all four demo app tests, which had been
passing for an increment:

```text
>       body = json.loads(response["body"])
E       KeyError: 'body'
```

**What actually gave it away** was not the assertion but the captured log line
underneath it — a demo app test had somehow produced a *gate verdict*:

```text
INFO root:handler.py:193 {"decision": "halt", "source": "hardcoded-stub", ...}
```

**Why it happened:** every Lambda in this repo names its entrypoint
`handler.py`, because that is what the `handler.lambda_handler` runtime setting
expects and it keeps the Terraform identical across services. Both test files
then did the obvious thing:

```python
sys.path.insert(0, ".../services/<service>")
module = importlib.reload(importlib.import_module("handler"))
```

`sys.modules` is keyed by module *name*, not by path. Whichever test file ran
first claimed the name `handler`; the second file's `import handler` was
answered from cache with the first service's module, and `reload()` faithfully
reloaded the wrong file. Both `sys.path` entries were present and correct, and
neither import raised.

**Fix:** `tests/conftest.py` now loads each handler by explicit path under a
service-qualified module name (`_lambda_demo_app`, `_lambda_gate_stub`) via
`importlib.util.spec_from_file_location`. That also removed the `reload()`
pattern entirely — a fresh `exec_module` re-reads the environment, which is what
these tests actually wanted.

**Lessons:**

1. **The failing assertion was three steps downstream of the cause.** Nothing in
   `KeyError: 'body'` points at module resolution. The log line did — which is
   an argument for tests that emit their subject's structured output even when
   passing, and for reading the captured output rather than only the assertion.
2. **This scales the wrong way.** With one service the pattern works perfectly.
   It breaks on the *second* service, and it breaks the service that was already
   working — so the natural instinct is to look at the code just written rather
   than the code that just stopped passing.
3. **Verify order-independence explicitly, not incidentally.** The bug is a
   function of import order, so the regression test is running the files in both
   orders and alone. All three now pass identically; before the fix, two of the
   four combinations were green, which is how a bug like this survives a casual
   "tests pass" check.
4. Directly relevant to Phase 3: the decision service and the executor will be
   two more `handler.py` files. This was going to happen again, larger, in the
   code where correctness actually matters.

---

## F-006 — `-var` on the command line is a value that reverts itself

**Phase:** 1, increment 3

**Symptom:** the increment 3 plan contained an in-place change nobody asked for,
on a resource that had not been edited:

```text
  ~ resource "aws_lambda_function" "demo_app" {
      ~ environment {
          ~ variables = {
              ~ "APP_VERSION" = "1.1.0" -> "1.0.0"
```

**What happened:** the increment 2 runbook instructed
`terraform apply -var demo_app_version=1.1.0` to publish a second version for
the canary to shift to. That worked. But `-var` applies to exactly one
invocation and is persisted nowhere — not in state as desired configuration, not
in the repository. The repository still said `1.0.0`, so the next plain
`terraform apply` reverted it.

**Why it was worse than a cosmetic revert:** the function has `publish = true`.
Reverting the environment variable would have published a **v3** containing the
*older* label, while the alias still pointed at v2. Version numbers would have
stopped corresponding to anything meaningful, and the next canary would have
shifted to a version that was a regression.

**Why it was caught:** because the plan was read before applying. `17 to add,
1 to change` — the `1 to change` was the tell, on a stack where increment 3 was
supposed to be purely additive. A plan whose shape does not match the intent of
the change is worth reading line by line.

**Fix:** the default in `variables.tf` is now the single place the value lives,
with a comment saying not to use `-var`. The runbook instructs editing the
default and running a bare `terraform apply`.

**Lessons:**

1. **If a value must survive the next apply, it belongs in the repository.**
   Terraform state records what was applied, not what was intended. A `-var`
   flag is an instruction, not a declaration.
2. **This is the same bug as F-005's cousin, D-010, and D-016 — a value with two
   owners.** The alias had Terraform and CodeDeploy. The function code had
   Terraform and the pipeline. This had Terraform and an operator's shell
   history. Each time the resolution is identical: name one owner explicitly.
   That recurrence is a talk point in itself — "who owns this value" is the
   question that catches an entire class of infrastructure bug.
3. **Additive increments should produce additive plans.** Treat any unexpected
   `~` as a finding until explained. The habit costs seconds; here it cost one
   `grep` and saved a confusing rollback.

---

## F-006 — A partial apply, caused by DNS, that looked like a permissions problem

**Phase:** 1, increment 2

**Symptom:** `terraform apply` created three resources, then failed on both IAM
roles:

```text
Error: creating IAM Role (ai-pre-traffic-gate-codedeploy): operation error IAM:
CreateRole, https response error StatusCode: 0, RequestID: , request send
failed, Post "https://iam.amazonaws.com/": dial tcp: lookup
iam.amazonaws.com: no such host
```

**Why the diagnosis is in the resource list, not the error text.** `StatusCode:
0` and an empty `RequestID` mean the request never reached AWS — so this cannot
be permissions, policy, or quota, all of which require a served response. The
more useful clue is *which* calls succeeded: the CodeDeploy application, the
deployment config and the log group all created fine. Those are **regional**
endpoints. IAM is the only **global** endpoint in this stack, and
`iam.amazonaws.com` was the only name that failed to resolve.

**Actual cause:** an unreliable local DNS resolver. Re-checking afterwards, both
`iam.amazonaws.com` and `codedeploy.us-east-1.amazonaws.com` logged `DNS request
timed out` against the router at `192.168.0.1` before eventually resolving. The
regional lookups were merely slow; the IAM lookup happened to land in a window
where it failed outright.

**Fix:** re-run `terraform apply`. Nothing else. State already recorded the three
created resources, and the second plan was exactly the six that had failed —
`6 to add, 0 to change, 0 to destroy`.

**Lessons:**

1. **`StatusCode: 0` with an empty `RequestID` is a network error, always.** It
   rules out the entire class of explanations people reach for first. Worth
   recognising on sight.
2. **Which calls succeeded is diagnostic.** A blanket outage and a single
   unresolvable hostname look similar in one error message and are trivially
   distinguishable across several. Global-vs-regional AWS endpoints is a useful
   axis to notice, because it explains a failure pattern that otherwise looks
   arbitrary.
3. **The partial apply was survivable, and that was not luck.** Terraform's
   state stayed consistent with reality because no resource in this increment
   half-creates: each is a single API call, and the dependent resources
   (`role_policy`, `policy_attachment`, the Lambda) simply had not been attempted
   yet. An increment where the same interruption leaves a resource created but
   unrecorded is a much worse afternoon, and it is worth knowing which of your
   own applies have that property before the network decides for you.
4. Relevant to what this project is building: the gate itself will run in this
   environment. A decision service that cannot reach Bedrock because of a
   transient network failure must fail closed rather than error out ambiguously
   — which is D-014, arrived at from the other direction.

**The Marketplace gate, specifically.** Anthropic models are delivered through
AWS Marketplace, and the account needs a subscription. That subscription is
created implicitly by the *first successful invocation*, by a principal holding
`aws-marketplace:ViewSubscriptions` and `aws-marketplace:Subscribe`.

This interacts badly with the read-only agent identity in D-004 — and the
interaction is the interesting part. The correct fix is for the admin to invoke
the model once, which subscribes the account for everyone. The tempting fix is
to add marketplace permissions to the agent, which quietly hands a
deliberately-read-only identity the ability to commit the account to paid
Marketplace subscriptions. The two are one line apart in effort and very far
apart in blast radius.

This is a small instance of the pattern the whole talk is about: a permission
boundary is only real if the awkward path is taken when it is inconvenient. The
first time an identity split costs you something is the moment it either means
something or does not.

**A diagnostic that confidently pointed at the wrong gate.** The first attempt
to detect gate 2 matched the substring `aws-marketplace` against the error
message. Gate 3's message says "AWS Marketplace" — different case, no hyphen —
so the match failed, execution fell through to the generic
`AccessDeniedException` branch, and the script advised checking an IAM policy
ARN that was entirely correct.

Wrong advice delivered confidently is worse than no advice, because it costs
whatever time is spent acting on it before it is disbelieved. The fix was to
normalise case, match the least ambiguous token, and order the branches
most-specific first. The residual fragility is accepted deliberately: AWS can
reword these strings at any time, and when they do this code degrades to the
generic branch, which is unhelpful rather than misleading. Choosing *which way
a detector fails* is a design decision, and it is the same one the verdict
layer faces in Phase 3 — an ambiguous signal must degrade toward "I do not
know", never toward a confident wrong answer.
