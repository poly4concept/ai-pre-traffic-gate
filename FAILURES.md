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
| 5 | Quota | Does the account have a non-zero daily token allowance? | `ThrottlingException` |
| 6 | Request | Is the model ID valid and invokable in this region? | `ValidationException` |

Six gates, four error codes, and **three different gates share
`AccessDeniedException`** — the code cannot distinguish them. Only the message
can, and the message is prose that AWS is free to reword.

Gate 5 was discovered later, in Phase 2, while trying to route around gate 3 by
switching to Amazon's own models. See F-009: it is zero and non-adjustable on an
account with no payment instrument, for every model from every provider.

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

## F-007 — One API call, three ARN types: an IAM policy that looked complete

**Phase:** 1, increment 3

**Symptom:** the first pipeline run reached the Deploy stage and failed there.
Source, Build and Gate all succeeded.

```text
AccessDeniedException: User: .../ai-pre-traffic-gate-executor is not authorized
to perform: codedeploy:RegisterApplicationRevision on resource:
arn:aws:codedeploy:us-east-1:594380318102:application:ai-pre-traffic-gate-demo-app
```

**What happened:** the executor's policy was written by asking "what resource am
I acting on?" The answer seemed obviously *the deployment group* — that is what
`CreateDeployment` names in its arguments. So the policy granted
`codedeploy:CreateDeployment` and `GetDeployment` on the `deploymentgroup:` ARN,
plus `GetDeploymentConfig` on the `deploymentconfig:` ARN, and looked thorough.

But `CreateDeployment` with an inline AppSpec spans **three** resource types.
Before a deployment can reference an AppSpec, that AppSpec must be registered as
a *revision of the application*, which is a separate resource with its own ARN:

| Action | ARN type | Was granted |
| --- | --- | --- |
| `CreateDeployment` | `deploymentgroup:` | yes |
| `GetDeploymentConfig` | `deploymentconfig:` | yes |
| `RegisterApplicationRevision` | `application:` | **no** |

**Fix:** added `RegisterApplicationRevision` and `GetApplicationRevision` on the
application ARN. Still one application, revision operations only.

**Lessons:**

1. **A single API call is not a single authorisation.** Deriving a policy from
   the resource named in the call's arguments is a reasonable-sounding heuristic
   that fails whenever the service does internal work on adjacent resources.
   The reliable method is to attempt the call and read what is denied, which is
   an argument for exercising a least-privilege policy end to end rather than
   reviewing it.
2. **Read the resource in the error, not just the action.** This is now the
   third time in this project the message named the answer exactly and the
   temptation was to look elsewhere: F-001 (the account ID in the ARN was wrong),
   F-004 (the code said `ResourceNotFound`, the message said "use case form"),
   and here (`application:` where the policy said `deploymentgroup:`). AWS
   AccessDenied messages are unusually precise — they state the principal, the
   action, and the exact resource ARN. All three failures were diagnosable from
   the text alone.
3. **The failure landed in the right place, which is the encouraging part.** The
   executor caught the `ClientError`, reported `PutJobFailureResult`, and the
   pipeline stopped at Deploy with the reason visible. The alias never moved. A
   permissions gap in the component holding deploy permissions produced a clean
   halt rather than a partial deployment — which is what the fail-closed design
   in D-014 is for, tested here by accident on a real failure rather than by a
   unit test.
4. **Least-privilege policies are built iteratively, and that is normal.** The
   alternative is a wildcard that never fails and never protects anything. Each
   round trip costs a pipeline run; each one also documents precisely why a
   permission is present. Worth saying out loud in the talk, because the usual
   reason people ship `codedeploy:*` is that the iteration felt like failure.

---

## F-008 — A partial apply, caused by DNS, that looked like a permissions problem

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

---

## F-009 — Switching to Amazon's own models did not avoid the payment gate

**Phase:** 2

**The hypothesis, which was reasonable:** Anthropic models are third-party and
delivered through AWS Marketplace, which is what drags in the subscription and
the `INVALID_PAYMENT_INSTRUMENT` failure of F-004. Amazon Nova is AWS's own
first-party model and should not touch Marketplace at all. So develop on Nova,
defer the card until Phase 4, and compare models later — which CLAUDE.md
already wanted anyway.

**Half of that turned out to be true.** Nova did get past the Marketplace gate.
The error changed from `AccessDeniedException` about Marketplace subscriptions
to something entirely different:

```text
ThrottlingException: Too many tokens per day, please wait before trying again.
```

That is a *quota*, not a permission and not a subscription — which means the
call reached the actual inference path. Progress, and it confirmed the
first-party reasoning.

**But the same error appeared for every model tested**, across three providers
and both invocation styles:

| Model | Result |
| --- | --- |
| `us.amazon.nova-micro-v1:0` | `ThrottlingException: Too many tokens per day` |
| `us.amazon.nova-lite-v1:0` | `ThrottlingException: Too many tokens per day` |
| `us.amazon.nova-pro-v1:0` | `ThrottlingException: Too many tokens per day` |
| `openai.gpt-oss-20b-1:0` | `ThrottlingException: Too many tokens per day` |

Account-wide, not per-model. Service Quotas gave the reason:

```text
Model invocation max tokens per day for Amazon Nova Pro ......... 0.0  Adjustable: False
Model invocation max tokens per day for Anthropic Claude Opus 4.5  0.0  Adjustable: False
Model invocation max tokens per day for GPT OSS Safeguard 20B .... 0.0  Adjustable: False
```

**Every daily token quota is zero, for every model, and none of them are
adjustable.** A support request cannot raise them. The account has no Bedrock
token allowance at all.

**Conclusion:** the payment instrument is not a Marketplace-specific
requirement that model choice can route around. It gates Bedrock inference
account-wide. Nova avoids one symptom of it and not the cause.

**Lessons:**

1. **The hypothesis was right about mechanism and wrong about consequence.**
   Nova genuinely does bypass the Marketplace subscription path — that part was
   correctly reasoned. It simply did not matter, because a second, unrelated
   restriction sat behind it. Being right about the mechanism is not the same as
   being right about the outcome, and only running the call distinguishes them.
2. **A changed error message is evidence of progress, not of success.** Watching
   the failure move from `AccessDenied/Marketplace` to `Throttling/quota` was the
   thing that proved the first-party reasoning held. Errors are a signal about
   *where you are in the stack*, and treating them only as noise discards that.
3. **Service Quotas answered in one call what four invocations only implied.**
   The invocations showed the same symptom four times; the quota listing showed
   `0.0 / Adjustable: False` and ended the question. When something looks
   account-wide, ask the account-wide API.
4. **This is now gate 6 in F-004's ladder**, and the second one that no amount of
   IAM or code can clear. Two of the six barriers between this project and a
   model response are commercial rather than technical, and both surface through
   error codes that describe something else.

---

**Update (Phase 3.1) — the card was added, and two of the four claims above
were wrong.**

A payment method was attached to the account. Re-running the smoke test moved
things forward and also corrected two conclusions recorded above.

*What worked exactly as designed.* Running the check once as the admin profile
created the Marketplace subscription (confirmed by an
`aws-marketplace@amazonaws.com` subscription email for "Claude Haiku 4.5 (Amazon
Bedrock Edition)"). The read-only `ai-agent` identity then got **past** that gate
on the next run without holding any Marketplace permission of its own — the
subscription is account-wide state, and only its *creation* needs
`aws-marketplace:Subscribe`. The error changed from `AccessDeniedException` to
`ThrottlingException`, which is again the "a changed error message is progress"
lesson from point 2 above, now observed a second time.

*Correction 1 — "every daily token quota is zero" was true but was the wrong
question.* Listing quotas for Claude Haiku 4.5 and Nova Lite specifically showed
that **tokens per MINUTE is also `0.0`**, along with requests per minute, for
both models and in every routing variant (on-demand, cross-region, global
cross-region). So the account does not have a used-up daily allowance; it has no
on-demand inference entitlement at all. The `ThrottlingException` message names
the daily limit, but the daily limit is not the binding constraint — it is merely
the one the error text mentions.

*Correction 2 — "none of them are adjustable, a support request cannot raise
them" was overstated.* It holds for the daily family. It does not hold for the
per-minute family:

```text
L-CCA5DF70  Cross-region model inference requests per minute, Claude Haiku 4.5  0.0  Adjustable: True
L-58BE175A  Cross-region model inference tokens per minute,   Claude Haiku 4.5  0.0  Adjustable: True
L-6120CF2D  Model invocation max tokens per day,              Claude Haiku 4.5  0.0  Adjustable: False
```

A self-service quota increase *is* possible, on the per-minute limits. The daily
limits appear to be derived rather than set independently, so the per-minute
grant is the lever.

**The lesson, and it is a better one than the original four:** the shape of the
query decided the shape of the conclusion. The error said *"Too many tokens per
day"*, so the quota list was filtered on `tokens per day` — and every row that
came back said `0.0 / Adjustable: False`, which felt like a complete answer
precisely because it was so uniform. The binding constraint, and the only
adjustable lever, sat in a family that filter never returned.

Point 3 above congratulates Service Quotas for "answering in one call what four
invocations only implied". That was true and also premature: the one call
answered the question that had been asked, and the question had been copied
verbatim from an error message written by the service that was refusing to
explain itself. An error message tells you which limit tripped first, not which
limit is lowest, and those are different facts.

Worth carrying into the gate's own design, because it is the same failure the
whole project is about: **a confident, uniform-looking answer to a narrow query
is the most convincing kind of wrong.** Every `0.0 / Adjustable: False` row was
accurate. The aggregate conclusion drawn from them was not.

---

## F-010 — Amazon Inspector reports "no vulnerabilities" for a service it has never scanned

**Phase:** 2.3

**Symptom:** none. That is the entire problem.

```text
$ aws inspector2 list-findings
{ "findings": [] }
```

No error. No warning. An empty list — the identical response a thoroughly
scanned, genuinely clean resource produces.

**The actual state of the account:**

```text
$ aws inspector2 batch-get-account-status
594380318102   DISABLED   lambda: DISABLED   lambdaCode: DISABLED   ecr: DISABLED
```

Amazon Inspector had never been switched on. Not misconfigured, not mid-scan —
disabled entirely. And the API for "show me the vulnerabilities in this account"
answered "there are none".

**Why this is the most dangerous instance of the absent-versus-negative rule.**
The naive collector is three lines:

```python
def _collect(self):
    resp = client.list_findings(filterCriteria=...)
    return SecurityFindings(findings=tuple(parse(f) for f in resp["findings"]))
```

It is obviously correct, passes review, and reports that a service nobody has
ever scanned has zero known vulnerabilities. The verdict layer then has positive
evidence of safety where it should have none.

Worse than having no security signal at all: **"no signal" prompts a question,
"no findings" closes it.** A gate with a missing collector gets fixed. A gate
confidently reporting a clean scan does not.

**Fix:** three calls, in order, and the first two are not optional:

| Step | Call | Establishes |
| --- | --- | --- |
| 1 | `BatchGetAccountStatus` | Inspector is enabled **and** Lambda scanning is on |
| 2 | `ListCoverage` | *this* function is covered and actively scanned |
| 3 | `ListFindings` | only now does an empty list mean "clean" |

Any of those failing yields `UNAVAILABLE` with a reason, never zero. There is a
test asserting that `list_findings` is not even *called* when the guard fails —
because a collector that asks the question before it has earned the right to
interpret the answer is one refactor away from using it.

**Two separate switches, not one.** Inspector can be `ENABLED` for an account
while Lambda scanning specifically is `DISABLED`. Checking only the account
status would have looked thorough and caught nothing.

**Lessons:**

1. **An API that returns a collection will return an empty collection for
   reasons that have nothing to do with your question.** Not scanned, no
   permission, wrong filter, wrong region, feature disabled — all render as
   `[]`. Before trusting emptiness, establish that somebody was looking.
2. This was found by *running the call against a real account*, not by reading
   the API reference — which documents the response shape perfectly and says
   nothing about what an empty list means. Third time this project has been
   saved by that habit (F-002, F-004, F-010).
3. The naive version is shorter, reads better, and would survive code review.
   Defensive code that looks like over-engineering is sometimes just code
   written by someone who has seen the failure.

---

## F-011 — The test suite quietly started calling AWS for real

**Phase:** 2.3

**Symptom:** the suite's runtime went from **2 seconds to 88 seconds**, and two
tests failed with genuine `InspectorNotEnabledError` traces from the live
account.

**What happened:** wiring the real Inspector collector into the gate meant
`collect_bundle()` constructed `InspectorFindingsCollector(SERVICE_NAME)` with no
client argument. The collector builds its boto3 client lazily — so every test
that exercised the gate handler silently made three live Inspector API calls.

**Why the slowness was the least of it.** CLAUDE.md constraint 5 requires the
whole flow to run with zero real AWS dependencies. Tests that reach AWS:

- depend on credentials, so they fail in CI and pass on a laptop;
- depend on live account state, so enabling Inspector later would have *changed
  test outcomes* without a line of code changing;
- can pass for entirely the wrong reason.

The 88 seconds was the only visible symptom of all three.

**Fix:** two changes, one structural and one design.

1. An autouse fixture in `conftest.py` replaces `boto3.client` and
   `boto3.resource` with a function that raises, unless a test is marked
   `@pytest.mark.aws`. The offline constraint is now enforced rather than
   intended, and a test asserts the guard itself works — a guard nobody verifies
   is not a guard (F-003).
2. `collect_bundle()` now accepts injected collectors, defaulting to the real
   ones. Same dependency-injection discipline `collect_signals()` already used
   one level down. Production and test run identical code with no `is_mock`
   branch inside it.

**Lessons:**

1. **Lazy client construction hides where the network calls are.** It is the
   right pattern for import-time testability, and it makes "does this code touch
   AWS?" invisible at the call site. Those are the same property.
2. Performance regressions in a test suite are worth investigating rather than
   tolerating. An 88-second suite is annoying; the reason it was 88 seconds was
   a correctness problem.
3. Dependency injection stopped being a style preference at exactly the moment a
   collector grew a network dependency. The pattern was already there one layer
   down and had not been carried up.

---

## F-012 — CloudWatch reports a successful query and returns nothing

**Phase:** 2.4

**Symptom:** none, again, and this one is the most flattering.

```text
$ aws cloudwatch get-metric-data --start-time ... --end-time ...
[["inv", "Complete", []], ["err", "Complete", []], ["dur", "Complete", []]]
```

`StatusCode: "Complete"` — the query ran successfully. `Values: []` — there is
nothing in it. The demo app simply had not been invoked in the two-hour window.

**What the obvious implementation produces.** `sum(values) or 0` on each metric,
then divide:

| Field | Reported | Reality |
| --- | --- | --- |
| invocations | 0 | 0 (correct) |
| error rate | **0%** | undefined |
| p99 latency | **0ms** | undefined |

That is not a missing signal. **A 0% error rate and a 0ms p99 latency is an
outstanding health report** — better than any real service would ever produce.
Inspector's empty list at least merely failed to raise a concern; these numbers
actively assert excellence, and `StatusCode: Complete` invites you to believe
them.

`0/0` is also either a `ZeroDivisionError` or, if guarded with `or 0`, a
fabricated zero. The guard that stops the crash is what creates the lie.

**Fix:** an error rate is a ratio, and a ratio with a zero denominator is
*undefined*, not zero. `error_rate_pct` and `p99_latency_ms` are `float | None`,
`None` whenever there is no traffic to compute them from. `has_health_evidence`
distinguishes "measured and fine" from "nothing to measure", and the collector
returns DEGRADED with the reason spelled out:

```text
no invocations in the last 60 minutes;
error rate and latency are undefined rather than zero
```

**A second ambiguity in the same collector.** `DescribeAlarms` returned an empty
list too — the account has no alarms at all. Empty is ambiguous between
"monitored, nothing firing" (good news) and "not monitored" (no news), and those
have opposite implications. Recorded explicitly as `has_alarm_coverage`.

**Lessons:**

1. **Three collectors, three APIs, three different ways to say "nothing" that
   look like "fine".** Inspector: an empty findings list. CloudWatch metrics: a
   `Complete` status with no datapoints. CloudWatch alarms: an empty alarm list.
   None of them error. This is not a quirk of one service — it is what "no data"
   looks like across AWS, and the pattern is now expected rather than discovered.
2. **A rate is not a number, it is a pair.** Any ratio a verdict sees needs its
   denominator attached. The Phase 1 canary taught this with 20-sample polls
   (D-019); it is the same lesson at a different layer, and it will recur.
3. **The defensive guard can be the bug.** `sum(values) or 0` exists to prevent a
   crash on empty input. It succeeds, and in doing so converts a loud failure
   into a silent falsehood. Preferring the crash would have been safer.

---

## F-013 — Two eval fixtures described states that cannot exist

**Phase:** 3.2

**How it surfaced:** not from a test. From printing one rendered prompt to check
the formatting looked sane, and reading this:

```text
<deploy_cadence>
  deploys_in_last_24h: 1
  hours_since_last_deploy: 71.0
```

One deploy inside the last 24 hours, and the last deploy 71 hours ago. Both
numbers are individually plausible. Together they are impossible.

`SAFE_DEPENDENCY_BUMP` had the same shape — `deploys_last_24h=1` with
`hours_since_last_deploy=26.5` — and being the default change in four of the ten
scenarios, it was the more widely spread of the two.

**Why nothing caught it.** Every existing test on these fixtures asserted single
values: that timestamps were fixed, that cadence rendered at all, that
`is_rapid_succession` fired at the right threshold. Nothing asserted a
*relationship between two fields*, and the contradiction lived entirely in the
relationship. The dataclass was happy, the renderer was happy, the type checker
was happy, and 312 tests were green.

**Why it mattered more than a typo.** These ten scenarios are the seed of the
Phase 4 eval harness — the measuring instrument for over-flagging and prompt
drift. Two of the five change fixtures, feeding six of the ten scenarios, would
have presented the model with a state the real world cannot produce. Any strange
verdict on those scenarios would then have been attributed to the prompt, and
"fix the prompt until the eval score improves" against a broken fixture is a
loop that converges on nonsense.

**The fix:** both set to `deploys_last_24h=0`, which is what "last deploy was
over a day ago" actually means, plus
`test_scenario_cadence_is_internally_consistent` parameterised over every
scenario. It found the second instance immediately — the first was found by eye,
the second by the test written because of the first.

**Deliberately a test over fixtures, not an invariant on `ChangeContext`.** The
tempting fix is to make the dataclass raise when the two fields disagree. That
would be wrong in production: both numbers come from the same CodeDeploy query,
and a deploy landing exactly on the 24-hour boundary, or a little clock skew,
would then fail the entire change context and halt a pipeline over a rounding
edge. Fixtures are held to a stricter standard than live data, because fixtures
are the instrument and live data is the measurement.

**Lessons:**

1. **Rendering the artifact found what testing the artifact did not.** The tests
   checked that fields appeared. Reading the output checked whether the output
   made sense. Those are different questions, and only the second one notices
   that two correct-looking numbers cannot both be true. Print the thing and
   read it, at least once, especially for anything a model consumes.
2. **Field-level assertions cannot see relational bugs.** Every test was correct
   and the data was still wrong. Where two fields are derived from one
   underlying fact, the invariant lives *between* them, and nothing that tests
   them separately will ever look there.
3. **A contradictory fixture is more dangerous than a malformed one.** Malformed
   input fails loudly and gets fixed in minutes. This produced a real,
   confident, plausible verdict on an impossible world — the same failure shape
   as every other entry in this file, which is why it belongs here despite being
   a two-character fix.
4. **The measuring instrument needs its own tests.** The eval harness will be
   trusted to say whether the prompt is getting better. Its fixtures were, until
   now, the only part of the repository holding data that nothing verified.

---

## F-016 — Terraform delivered the switch and the pipeline never delivered the wiring

**What happened:** `terraform apply` added the `FAULT_TABLE` environment
variable and the DynamoDB read policy to the demo app. `inject_fault.py` set
`error_rate: 0.5` and reported success. Sixty invocations later: **sixty
successes, zero faults, every alarm calmly `OK`.**

The deployed code contained no fault support at all:

```
files in the deployed zip: ['handler.py']
handler imports faults:    False
handler calls apply_faults: False
```

**Why:** `demo_app.tf` carries `lifecycle { ignore_changes = [filename,
source_code_hash] }` — a deliberate choice (D-010) so that the PIPELINE owns
the demo app's code and Terraform does not fight it.

The consequence nobody had thought through: an apply delivers **configuration**
faithfully while leaving **code** untouched. So the env var arrived, the IAM
policy arrived, the switch was set — and the code that would read any of it was
never deployed. A perfectly wired switch connected to nothing.

### Why it was silent, which is the part worth keeping

The demo app fails **safe** by design: if it cannot read its fault config, it
serves normally. That was the right call and it is argued for at length in
`faults.py` — a performer that collapses whenever its script is unreadable can
never be told apart from one that is genuinely broken.

The cost is that an app with **no fault subsystem whatsoever** behaves
identically to one that is healthy. `faulted == 0` turned out to have three
completely different causes and no way to distinguish them:

| observed | actual cause | fix |
| --- | --- | --- |
| nothing invoked | shell quoting, or missing IAM | fix the caller |
| invoked, no `faults` field in the response | **deployed code predates 2.5a** | run the pipeline |
| invoked, `faults` field present | injection off or set to zero | `inject_fault.py status` |

Two afternoons went into the first and second rows, both of which looked like
"the alarm thresholds are wrong."

### The fix, and the general lesson

`drive_traffic.py` now reads the response body and reports whether a `faults`
field was present at all. The demo app was already returning one — that field
existed to prove to an audience which requests were deliberately degraded, and
it turns out to double as a capability probe. Three causes, three distinct
messages, tested.

**The reusable rule:** whenever a component fails safe, something else has to
be able to observe that it is failing. Fail-safe converts a loud problem into a
silent one, which is the correct trade for the component and a hole in the
system unless somebody is watching from outside. "Fail safe" and "fail
observably" are different properties and you need both.

**And the narrower one, specific to this architecture:** when Terraform and a
pipeline own different halves of the same resource, `terraform apply` reporting
success means only that Terraform's half is current. Anything that depends on
the other half needs a deploy, and the two are easy to confuse because both are
"I applied my changes."

### Also found along the way

`aws logs filter-log-events --log-group-name "/aws/lambda/..."` fails under Git
Bash on Windows with a regex validation error, because MSYS rewrites any
argument starting with `/` into a Windows path — the API receives
`C:/Program Files/Git/aws/lambda/...`. `MSYS_NO_PATHCONV=1` disables it. Nothing
to do with AWS, and the error message points squarely at AWS.

---

## F-014 — Three stacked gates, and an error that was not about the thing being tested

**What happened:** four days of Bedrock being "not working" turned out to be
three unrelated blockers stacked behind one another, each invisible until the one
in front of it was cleared. Along the way I read one error as evidence for a
conclusion it said nothing about, and told Mubaraka the problem was solved when
it was not.

**The three gates, in the order they surfaced:**

| # | Error | Actual cause | Fix |
| --- | --- | --- | --- |
| 1 | `AccessDeniedException` … marketplace | no AWS Marketplace subscription for that model | one invocation by an admin |
| 2 | `AccessDeniedException` … IAM | *our own* policy never listed AI21 or OpenAI | widen the policy |
| 3 | `ThrottlingException: Too many tokens per day` | account daily token quota is `0` and non-adjustable | only AWS Support |

Each fix revealed the next. That is the whole reason this took days: at no point
was there a single wrong thing to find, and every fix felt like progress while
the symptom stayed identical.

### The mistake worth putting on a slide

Testing AI21 Jamba, the response was:

```
ValidationException: This model doesn't support the toolConfig.toolChoice.tool field.
```

I read that as *"the request reached the model, so we are past the quota"* and
said so. It was not. Re-running the same request without any `toolConfig` gave
`AccessDeniedException` — the model had never been authorised at all.

**Bedrock validates in stages: request shape → marketplace entitlement → quota.**
The malformed `toolConfig` died at stage 1. An error from an early stage says
nothing whatsoever about the later ones, and I treated a stage-1 rejection as a
stage-3 pass.

**The general lesson, which is the reusable part:** when probing whether access
works, strip the request to the absolute minimum. Anything extra you send is
another thing that can fail *first*, and a failure at stage 1 is
indistinguishable — from the caller's seat — from success at stages 2 and 3.
`scripts/subscribe_model.py` now sends a bare message with no tool config for
exactly this reason.

This is the same class of error as the absent-vs-zero trap that runs through
Phase 2, arriving from a different direction: **an error that is not about the
thing you are testing is worse than no error**, because it looks like data.

### How the real cause was finally isolated

The account showed `0` for every daily token quota, and `0` for almost every
per-minute quota too — so a throttle on Claude or Nova was ambiguous. Two
hypotheses fit equally well:

* **A** — the daily `0` is a placeholder and the per-minute quotas bind
* **B** — the daily `0` is genuinely enforced

The sweep found exactly two models on the account with *non-zero* per-minute
quota: AI21 Jamba 1.5 Mini and Large (3,000 TPM, 1 RPM). That made Jamba a
controlled experiment — the only model where the two hypotheses predict
different outcomes.

With the subscription created and access confirmed working:

```
ai21.jamba-1-5-mini-v1:0
  L-5A778346  tokens per minute      = 3,000   (non-zero)
  L-0449ADC5  requests per minute    = 1       (non-zero)
  L-103822BF  tokens per DAY         = 0       (non-adjustable)

  -> ThrottlingException: Too many tokens per day
```

Hypothesis B, conclusively. The daily quota is enforced, it is zero, and it is
not adjustable through Service Quotas — so no amount of per-minute quota helps
and no self-service request can fix it.

Note also what this rules out: consumption. The account has never completed a
single successful inference call, so this is not an exhausted allowance. **The
allowance is zero, which means the first token is already too many.**

### The actual diagnosis, found by a rejected quota request

Everything above assumed the account needed a quota INCREASE. It does not. The
request was rejected with:

```
IllegalArgumentException: You must provide a quota value greater than
the default quota value of 5000000.0
```

Service Quotas was refusing the request because 50,000 is *below* the default.
Which meant the default was five million, and this account was sitting at zero.
Comparing the two directly:

| Quota | AWS default | Applied to this account |
| --- | --- | --- |
| Claude Haiku 4.5 cross-region TPM | 5,000,000 | **0** |
| Nova Lite cross-region TPM | 8,000,000 | **0** |
| Claude Haiku 4.5 cross-region RPM | 10,000 | **0** |
| Claude Haiku 4.5 tokens per day | 3,600,000,000 | **0** |
| AI21 Jamba 1.5 Mini TPM | 300,000 | **3,000** (1%) |

Not a capacity problem. Every applied value on the account is either zero or one
percent of the AWS default, across every provider including Amazon's own models.
Jamba at exactly 1% is what rules out coincidence -- a single suppressed
provisioning step, not a series of per-model decisions.

**And there is no self-service route out of it.** `RequestServiceQuotaIncrease`
only accepts values ABOVE the default, so the one API that could restore a
normal quota refuses every value between 0 and 5,000,000 -- which is the entire
range that would help. The mechanism for fixing this is structurally unable to
fix this.

**The lesson, and it generalises past AWS:** a rejected request is data. That
`IllegalArgumentException` was not an obstacle to work around, it was the first
thing in four days to state a number we had not already seen -- and the number
was the whole diagnosis. Two commands (`get-aws-default-service-quota` and
`get-service-quota`) then turned it into a table, and the table is the support
case. Before that we had "it is throttled" and a plausible story; after it we
had "applied is 0, default is 5,000,000, and your API will not let me change
it", which is not arguable.

**What we should have run on day one:** compare applied against default. Reading
the applied value alone -- which is what the console shows and what the first
sweep collected -- makes 0 look like a small number. Against a default of five
million it is obviously a broken one.

### The root cause, found in a region we had never tried

Sweeping all fourteen Nova regions as the admin profile produced an error
message that had never appeared in us-east-1:

```
eu-west-1        amazon.nova-micro-v1:0   DENIED
    Your account is currently being verified. Verification norma...
```

**The account is under AWS verification.** Not a quota bug, not a provisioning
defect, not a payment problem -- an account-level hold that suppresses inference
capacity while it runs. Everything observed over five days is downstream of
that one fact:

* applied quotas of 0 against defaults in the millions
* Jamba at exactly 1% of its default rather than 0
* a single successful call in us-east-2, then nothing
* `RequestServiceQuotaIncrease` refusing every value that would help

And crucially it explains why **no amount of correct configuration helped**. We
fixed three real problems on the way here -- the Marketplace subscription, the
IAM policy, the payment method -- and each fix was genuinely necessary and
changed nothing observable, because a fourth gate sat behind all of them.

The sweep also found two regions that DID serve a request:

```
eu-north-1       amazon.nova-lite-v1:0    WORKS   10 tokens used
ap-southeast-2   amazon.nova-lite-v1:0    WORKS   10 tokens used
```

**Why us-east-1 never showed the verification message** is the part worth
keeping. It reported `ThrottlingException: Too many tokens per day` -- a
capacity error -- for what is actually an entitlement hold. Only regions where
the model was NOT already provisioned surfaced the real reason. So the most
heavily used region gave the least informative error, and it was the only region
we looked at for five days.

**The generalisable lesson, and it is the same one as the `ValidationException`
above:** an error message describes the first gate that rejected you, not the
underlying condition. Sweeping the same call across many regions was worth more
than any amount of re-reading a single region's response, because a different
environment fails at a different stage and tells you something new.

**Also worth noting for the talk:** the fix for this is to wait. Every technical
avenue -- IAM, subscriptions, quota requests, support cases -- was orthogonal to
the actual cause. The engineering skill on display here was not solving it; it
was building all of Phase 3 and Phase 4a against fakes so that five days of
being blocked cost nothing.

### Cost of the detour

Roughly four days of Phase 3 believing the blocker was a payment instrument,
then a subscription, then IAM. The build was never actually blocked — 3.1
through 3.5 were all written and tested offline against realistic fake
responses — but "Bedrock works" was asserted three times before it was true, and
each assertion was based on one gate clearing rather than on a successful call.

**The rule that would have prevented all three premature claims:** the only
evidence that a model call works is a model call that worked. Not a cleared
error, not a subscription email, not a quota page. `check_bedrock_access.py`
already encoded this — its step 3 is a real Converse call — and I talked past
its FAIL output three separate times by explaining why the *next* thing would
fix it.

---

---

## F-015 — An IAM policy grown by debugging, and a 2048-byte wall

**What happened:** applying the `ai-agent` Bedrock policy failed with

```
LimitExceeded: Maximum policy size of 2048 bytes exceeded for user ai-agent
```

**Why it had grown:** every dead end in the quota investigation added ARNs. AI21
went in to test Jamba, DeepSeek and OpenAI to test whether any provider was
unaffected, then five more regions when the region sweep found two that worked.
Twenty-four resource ARNs, each individually justified at the moment it was
added, none ever removed. 2,039 bytes of file and over the limit as submitted.

**The real problem is not the limit.** It is that the policy had stopped
describing what the identity *does* and started recording what we had *tried*.
Nothing in it was wrong; it was a debugging log that happened to be enforced by
IAM. A least-privilege review of it would have been meaningless, because the
answer to "why can this identity invoke DeepSeek?" was "we were curious once".

**Fix:** cut to four wildcarded ARNs covering the two model families this
project will actually use, at 470 bytes.

```json
"arn:aws:bedrock:*::foundation-model/anthropic.*",
"arn:aws:bedrock:*::foundation-model/amazon.nova-*",
"arn:aws:bedrock:*:594380318102:inference-profile/*.anthropic.*",
"arn:aws:bedrock:*:594380318102:inference-profile/*.amazon.nova-*"
```

**The region wildcard is a deliberate widening,** and worth being explicit about
rather than quietly enjoying. Enumerating regions was the more precise thing to
do, and it was also what made the policy unmaintainable -- every new region to
test meant another edit, another apply, another line nobody would later remove.
For an invoke-only grant to a read-only development identity on two named model
families, `*` on the region is a good trade. It would not be for the gate's own
execution role, which stays enumerated in `gate_stub.tf` for exactly that
reason.

**The generalisable bit:** inline user policies cap at 2048 bytes, which is
small enough to hit by accident. Anything expected to grow belongs in a managed
policy (6,144 bytes, attachable, versioned). But hitting the cap here was
useful — it forced a cleanup that should have happened anyway, and the fact that
a policy grew 6x during a debugging session without anyone noticing is the
finding, not the limit.


---

## F-017 — The prompt's XML style leaked into a JSON field, and nothing upstream noticed

**Symptom.** First live eval run: 4 of 22 scenarios failed closed, each after
burning all three attempts on the identical error:

```
invalid_verdict:primary_concerns — primary_concerns is str, expected an array
```

**What the model actually sent.** Not garbage. A well-formed list, in the wrong
format:

```
'\n  <item>Target service currently in ALARM state with elevated error rate (7.4%)</item>
  <item>Large refactor (1057 lines) to payment settlement and authentication logic</item>
  <item>Deployed outside business hours on Friday, reducing support availability</item>\n'
```

Four correct concerns, in `<item>` tags, inside a single string, in a field
declared `{"type": "array", "items": {"type": "string"}}`.

**Root cause: ours.** The evidence in the user message is rendered as XML —
`<change_metrics>`, `<untrusted_text>`, `<target_health>` — chosen deliberately
in Phase 3.2 so untrusted text could be escaped at a delimiter boundary. The
tool schema is JSON. We put the model in a document where lists are expressed as
tags and then declared a field where they are not, and it followed the format it
could see over the one it had been handed.

The prompt never contains the literal string `<item>`; it renders its own lists
with dashes. The model invented the tag. It was not copying an example — it was
matching a register.

**Why nothing caught it.** Bedrock does not validate tool input against the
declared schema (D-031). The schema is a strongly-worded suggestion; the
validator is the contract. This is the failure mode that fact predicts, arriving
exactly as predicted, and it still took a live run to see — because every
offline test supplies its own tool input and so can only test the validator,
never the model's willingness to satisfy it.

**Correlated with list length, which is the tell.** Scenarios wanting 0–2
concerns returned a proper JSON array. Scenarios wanting 3–4 returned tags.
Below some threshold it is a couple of values; above it, it feels like a list,
and the model reaches for the list format in scope.

**The fix, both halves cheap.** The schema description now says *"A JSON array
of plain sentences. No XML tags, no markup, no numbering — the evidence you were
given is XML, this field is not."* The system prompt says it again in prose
before the evidence begins. Result: 4 of 22 failing became 0 of 66, and
fail-closed attempts dropped from 22.7% to 4.5% — all of the remainder being the
intended `no_change_context` refusal.

**Two lessons, and the second is the one worth the stage:**

1. Anywhere a model is asked to produce structure, say what the structure is
   *not*, not only what it is — especially when the surrounding context
   demonstrates a different convention.

2. **A prompt has a house style, and the model will answer in it.** We chose XML
   for a good reason and did not consider that the choice was also an
   instruction. Every formatting decision in a prompt is teaching by example,
   including the ones made for reasons that have nothing to do with the output.

**What it cost while it was invisible:** twelve wasted inferences, and — worse —
three of the four fail-closed verdicts landed on `high`, which was the
*acceptable* answer for those labels, so the eval scored them as correct. The
formatting bug was concealing itself behind the fail-closed design. See F-018.

---

## F-018 — The eval scored the gate's refusals as correct judgement

**Symptom.** The first live run reported **85.7% acceptable** with a warning that
22.7% of attempts had failed closed. Both numbers were computed correctly. The
first one was meaningless.

**What was happening.** Four scenarios failed schema validation (F-017) and fell
back to `Verdict.fail_closed(...)`, which is `high` by construction. Three of
them carried labels where `high` was an acceptable answer — `critical_cve_no_patch`,
`untriaged_severity_findings`, `friday_deploy_into_active_alarm`. The harness
compared the level to the acceptable set, found `high` in it, and recorded a
pass.

So the gate was credited with three correct risk assessments it had explicitly
declined to make.

**Why this is worse than a plain arithmetic bug.** It is directionally biased.
Fail-closed always produces `high`, and `high` is the acceptable answer for
every *risky* fixture and never for a benign one. So refusals inflate the score
on precisely the half of the set that measures under-flagging — the number that
decides whether the gate is worth having. A gate that failed closed on every
single input would have scored 10/10 on the risky scenarios.

**The fix.** `ScenarioResult.is_measured`: a scored scenario with any
fail-closed attempt is excluded from both rates, from the pass denominator, and
from the failures list, and is named in its own `UNMEASURED` block with the
failure kind. The exception is a scenario whose *label* is `fail_closed`, where
refusing is the behaviour under test.

**It caught something real within the hour.** A scouting run against Sonnet 5
failed 66/66 on `AccessDeniedException`. The report named twenty unmeasured
scenarios and printed `n/a` for over-flagging. Under the old arithmetic it would
have printed a confident-looking accuracy figure computed entirely from
refusals — and, given the direction of the bias, a **0% under-flagging rate**
for a model that had never once been reached.

**The general shape, sixth appearance.** This project's recurring bug is a
system converting *"no data"* into *"no problem"*: Inspector's empty findings
list, CloudWatch's `sum(values) or 0`, an omitted prompt section, an alarm's
`notBreaching`, `drive_traffic.py` reporting a 0% error rate over zero
invocations (F-016) — and now the eval harness built to measure the first five,
scoring an absent verdict as a correct one.

Six systems, six vocabularies, one mistake. It is not that the same bug keeps
being written. It is that *every* layer has a natural place to put an absence,
and the safe-looking default in each one is the wrong answer. Worth saying
plainly on stage: I wrote a whole package around this principle, wrote it into
the system prompt as rule 1, and then broke it in the scoring code.

---

## F-019 — The prompt told the model security findings were not about the new code, and it believed us

**Symptom.** Three scenarios with real CVE findings — `critical_cve_no_patch`,
`critical_cve_with_patch`, `untriaged_severity_findings` — came back `low`,
stably, across every repeat. The labels wanted `medium` or `high`. All three of
the model's under-flags were the security signal.

**First read: the model ignores security.** Wrong, and checking the reasoning
took thirty seconds:

> "While there is one critical security finding in the currently deployed
> version (abandoned-lib), this is a pre-existing issue unrelated to the
> requests library bump, and the change itself does not introduce new
> vulnerabilities."

It had not ignored the signal. It had read it, reasoned about it, and reached a
conclusion — the conclusion our own prompt directed. Rule 5 said:

> Security findings may describe the currently deployed version, not the
> candidate. [...] It is still real context [...] **but it is not vulnerability
> data about the new code.**

The model did what it was told. **The labels disagreed with the prompt, and the
model was scored against the labels.**

**Which of the two was wrong.** The prompt, on a point of fact. A vulnerable
dependency in the deployed version is still in the candidate unless the change
updates that dependency — the candidate is built from the same repository.
"Pre-existing" says who introduced the problem; it says nothing about whether
the artifact about to ship contains it. Our sentence conflated those and taught
the model to discount a finding it should have carried forward.

Rule 5 now states the fact and stops there — no instruction about what level to
assign, because writing the expected answer into the prompt would make the eval
measure obedience instead of judgement.

**It did not fix the number.** Under-flagging stayed at 30% across two rewordings
(D-063). `critical_cve_with_patch` moved to `medium` on the long version and back
to `low` on the short one, while `huge_refactor_healthy_target` — which has no
security findings at all — moved the opposite way each time.

**Three lessons, in ascending order of how much they cost:**

1. **Read the reasoning before diagnosing the verdict.** "The model ignores
   security" and "the model weighs security differently than I do" look
   identical in a results table and need completely different fixes.

2. **A prompt is a specification, and specifications contain bugs.** This one
   asserted something false about how Inspector findings relate to a candidate
   build. It survived Phase 3 review, offline tests, and my own reading of the
   file several times, because it reads *sensible* — and it was only exposed by
   a model following it more literally than I had.

3. **When a model disagrees with a label, one of them is wrong and it is not
   automatically the model.** The whole point of labelling blind in Phase 4a was
   to stop me relabelling toward whatever the model said. It works in both
   directions: it also stops me assuming the label is right. Here the honest
   finding was a contradiction between two things I had written, which no amount
   of model-side tuning would have resolved.

---

## F-020 — An empty string is a valid attribute and an invalid key

**Caught before it shipped, by adding the index and then re-reading the writer
rather than trusting it.**

Phase 5.1 makes `pipeline_execution_id` the hash key of a new GSI. The gate had
been writing that field like this since Phase 3.4:

```python
"pipeline_execution_id": pipeline_execution_id or "",
```

Harmless for two phases. DynamoDB has allowed empty strings in ordinary
attributes since 2020, so `""` stored fine and nothing complained.

It is **not** allowed in a key attribute, and index keys are key attributes. So
the moment that GSI existed, any invocation without a CodePipeline job — every
manual `aws lambda invoke` used to test the gate by hand — would have had its
`PutItem` rejected outright.

**Not the field. The whole record.** The gate would have silently stopped
producing an audit trail for exactly the invocations used to check that it
produces one. And because `record()` already swallows write failures by design
(a storage failure must not halt a judged deploy), the symptom would have been
a log line, a green pipeline, and an empty table.

**The fix** is one line and a better answer anyway: omit the attribute rather
than writing `""`. An item with no `pipeline_execution_id` is simply absent from
the index, which is the correct answer to "which deploy did this verdict gate?"
when there was no deploy. A sparse index is a feature here, not a compromise.

**The lesson, and it generalises past DynamoDB:** `"" or None` felt like a
tidying decision — a defaulted string is easier to read than an optional one.
It quietly became a schema decision the day something keyed on it. Fields that
are "just data" become constrained the moment anything indexes, joins, or
partitions on them, and the code that writes them is usually written long
before the code that constrains them.

Worth noting how it was found: not by a test, and not by `terraform validate`.
By writing the index, then going back to check what actually gets written into
the column it keys on. The test asserting it now exists because of that read,
not the other way round.

---

## F-021 — A comma in a tag value, found after typing `yes`

**Symptom.** `terraform validate` passed. `terraform plan` passed and printed
`1 to add, 3 to change, 0 to destroy`. The apply then failed on the first
resource it tried to create:

```
Error: creating AWS DynamoDB Table (ai-pre-traffic-gate-overrides):
api error ValidationException: The Tag Value provided is invalid,
Value: Human overrides of gate verdicts, scoped to one pipeline execution
```

**Cause.** The comma. AWS tag values permit unicode letters, digits, whitespace
and `_ . : / = + - @`, and nothing else. I had written a sentence into a tag.

**Why nothing local caught it, which is the part worth keeping.** The constraint
belongs to the *service*, not to the Terraform provider's schema:

  * `terraform validate` checks syntax and provider schema. A tag value is a
    string, and this was a string.
  * `terraform plan` diffs desired state against real state. It sends nothing
    to DynamoDB's `CreateTable` validator, because nothing is being created yet.

So there was no local step that *could* have known. The first thing capable of
rejecting it was an apply that had already begun creating resources — which is
the expensive place to find out, and in a multi-resource apply is where you get
a half-built stack.

**Second-order cause, and the more useful one:** I put documentation in a tag.
The value read like a comment because I was writing it like a comment. A tag is
an index key — something you filter and group by in a cost report — and the
explanation belonged in the file, three lines above, where it already was.

**Fix.** `Purpose = "Human overrides of gate verdicts"`, plus
`tests/test_terraform_tags.py`: scan every `tags` block in the stack and assert
each value matches AWS's character set. Fifteen lines that move this class of
error from apply time to test time.

Two details in that test worth noting, because a guard written carelessly is
worse than none:

  * It asserts it **found** tags at all. The real risk with a regex scanner is
    not a false failure, it is silently matching nothing and passing forever.
  * It caught a false positive on its first run — `${var.project_name}-verdicts`
    contains `$ { }`, which AWS never sees because Terraform substitutes it
    first. Interpolations now collapse to a placeholder and the literal text
    around them is what gets checked. The limit is stated in the file: it cannot
    see inside a variable.

**The general lesson.** Local validation covers syntax and schema; services have
their own validators and only run them when you call them. Any constraint that
lives on the far side of an API call is a constraint your plan cannot see, and
the cheap way to pull it forward is a test that encodes the rule — not a
carefully-worded comment asking the next person to remember.

---

## F-022 — The field shadow mode exists to produce was never written down

**Symptom.** Phase 5.4 set out to read `would_have_halted` out of the verdict
table. It is not in the table. It has never been in the table.

**What was actually happening.** `build_gate_record` computes it and puts it in
the structured log line. `build_record` -- the function that assembles the
DynamoDB item -- never carried it. So it reached CloudWatch Logs and stopped.

**Why that is worse than an ordinary missing field.** The handler's own module
docstring, written in Phase 3.5, says:

> Shadow mode is worth more than it sounds. `would_have_halted` accumulates in
> DynamoDB from today, so by the time enforcement is switched on there is a real
> measured over-flagging rate to switch it on *with* -- rather than a guess and
> an apology.

That is the entire justification for running shadow mode, it is quoted from
CLAUDE.md, and it was false for two phases. Nothing was accumulating. The plan
was to turn enforcement on using evidence that did not exist, and the plan said
so in a comment nobody re-read against the data.

**Recoverable, as it happens.** `would_have_halted` is derivable from
`verdict.risk_level` for the existing rows, and `gate_history.py` does that and
labels those rows as derived. But deriving is strictly worse than storing,
because the risk-to-blocking mapping is a POLICY that can change: if `medium`
ever becomes blocking, every historical row would be silently reinterpreted
under the new rule and the over-flagging trend would move for reasons that have
nothing to do with the gate. Storing freezes what the gate actually thought at
the time -- the same argument as `prompt_version`.

**Fix.** `"would_have_halted": verdict.is_blocking` in `build_record`.

**The lesson, and it is not "write more tests".** There were tests on
`build_record`. They asserted the fields it does write. No test can notice a
field nobody asked for. What found this was **opening the table and reading a
row** -- which took one command and had not been done in two phases.

A comment claiming a system accumulates evidence is not evidence that it does.

---

## F-023 — Two phases of work keyed on a field CodePipeline never sends

**Symptom.** Investigating F-022 above, every verdict record turned out to be
missing `pipeline_execution_id` too. Then the executor's logs, on all four real
pipeline runs:

```
VERDICT SHADOW: would have STOPPED this deploy -- job carries no pipelineExecutionId
```

**Cause.** Both handlers read
`job["data"]["pipelineContext"]["pipelineExecutionId"]`. That key does not exist
in a CodePipeline **Lambda-invoke** event. `pipelineContext` belongs to the
**custom action** job structure returned by `PollForJobs`. A Lambda invoke
receives `actionConfiguration`, `inputArtifacts`, `outputArtifacts`,
`artifactCredentials` and `continuationToken`, and nothing else.

**What that silently disabled.** Everything keyed on the execution ID, which is
two increments of work:

  * **5.1** -- the executor's verdict lookup. `find_verdict` was never once
    called. The risk branching, the deployment-config mapping, the fail-closed
    refusal: none of it has ever executed.
  * **5.3** -- the human override path. An override row written by
    `scripts/override.py` could never have been found.
  * The `by_pipeline_execution` GSI has been empty since it was created.

**Why nothing caught it, and this is the part worth the slide.** From
`tests/test_executor.py`:

```python
def verdict_job(execution_id="exec-1", job_id="job-1"):
    return {
        "CodePipeline.job": {
            "id": job_id,
            "data": {"pipelineContext": {"pipelineExecutionId": execution_id}},
        }
    }
```

I wrote that fixture from the same wrong assumption as the code. **The fixture
and the bug agreed with each other**, so the tests passed, and passed
convincingly -- there were assertions about lookup, about retries, about
fail-closed behaviour on a missing verdict, all exercising a path that in
production was never reached.

> A test built from an event shape you invented validates your assumption, not
> the integration.

**Why the logs did not give it away either.** The executor fails safe. With the
switch off, "could not find a verdict" and "found a verdict, taking no action"
both emit a line starting `VERDICT SHADOW`. I read the line, saw the prefix I
expected, and confirmed to Mubarak that the wiring worked. It did not. This is
**F-016 exactly**, second occurrence: *whenever a component fails safe,
something else has to be able to observe that it is failing.* I wrote that
sentence and then read past its violation.

**How close this came to being much worse.** The next planned step was flipping
`EXECUTOR_ENFORCES_VERDICT` on. With the lookup broken, "no verdict found"
correctly means refuse -- so enforcement would have blocked **every single
deploy**, immediately, and the cause would have been a key name three files
away from the switch.

**Fix.** CodePipeline exposes the value as the built-in variable
`#{codepipeline.PipelineExecutionId}`. It is now interpolated into
UserParameters on both the Gate and Deploy actions, the same mechanism already
proven by `#{SourceVariables.CommitId}`, and both handlers read it from there
(keeping the old path as a harmless fallback). `MAX_B64_LENGTH` dropped 860 to
780 to keep UserParameters under its 1000-character cap.

**The guard: `tests/test_pipeline_wiring.py`.** No test can know the real shape
of a third-party event. What a test *can* do is assert that both halves of a
contract name the same field -- the pipeline definition writes
`pipeline_execution_id`, the handlers read `pipeline_execution_id` -- because
both sides live in this repository. Verified to fail when the fix is removed,
because a guard that cannot fail is decoration.

**Three lessons, ascending:**

1. Read the actual event. One `logger.info(json.dumps(event))` on the first real
   run would have shown `pipelineContext` was absent.
2. A fixture is a claim about the world and deserves the same scepticism as the
   code. Where a fixture encodes an integration boundary, something has to check
   it against reality -- a real run, or a contract test.
3. **Look at the data.** F-022 and F-023 were both found by scanning a DynamoDB
   table for the first time, in the same five minutes. Two phases of confident,
   tested, reviewed, non-functional work, and the entire cost of finding them was
   `aws dynamodb scan`.

---

## F-024 — The crafted commits could not survive the pipeline they exist to exercise

**Symptom.** `python scripts/craft_commit.py create revert --push` failed the
**Build** stage with ~180 instances of:

```
F841 Local variable `synthetic_value_0` is assigned to but never used
  --> demo/synthetic/payments/retry.py:10:5
```

The Gate stage never ran. No verdict was produced.

**Cause.** `_lines()` wrote its filler as unused local variables inside a
function:

```python
def synthetic() -> None:
    synthetic_value_0 = 0  # generated
    synthetic_value_1 = 1  # generated
```

`buildspec.yml` runs `ruff check .` as its first build command, and F841 is a
real rule this project selects on purpose. So the generator produced code the
repository's own CI rejects.

**Which recipes this disabled: four of six.** `safe-bump` (one line) and
`docs-only` (Markdown) generate no Python and passed. `revert`,
`payments-friday`, `huge-refactor` and `injection` -- every recipe that produces
a *medium* or *high* verdict, which is to say every interesting one -- could
never reach the gate.

**Why it went unnoticed for two phases.** `create <recipe>` had only ever been
run locally, where it writes files, commits them, and nothing lints the result.
The runbook documented `--push` and nobody had pushed. The two recipes exercised
first were, by coincidence, the only two that generate no Python.

**Fix.** Module-level constants instead of function locals:

```python
SYNTHETIC_VALUE_0 = 0  # generated
```

F841 is scoped to locals; a module-level assignment is a constant nobody
imports, which ruff has no opinion about. Identical line count, identical diff
statistics, identical paths -- and it survives CI.

**Guard: `tests/test_craft_commit_lints.py`.** Generates every recipe's content
into a temp directory and runs the two commands `buildspec.yml` runs, per
recipe. It also asserts there *are* recipes and that at least one produces
Python, because a parametrised test over an empty list passes forever.

**The pattern, third time in two days.** F-023 was two increments keyed on a
field AWS never sends. F-022 was a metric computed and never persisted. This is
a generator whose output was never once put through the system it feeds. All
three share a shape:

> The component was tested. The **seam** was not.

Each was found the same way -- by running the thing end to end for real, rather
than by adding another unit test to a suite that was already green. And each is
now guarded by a cheap test that stands at the seam rather than inside either
component.

**One uncomfortable observation for the talk.** This project's failures log now
contains three consecutive entries where the code was correct, the tests passed,
and the integration had never been executed. That ratio is the actual lesson: on
a system assembled from managed services, the defects cluster almost entirely at
the boundaries, and unit tests are structurally incapable of finding them.

---

## F-025 — The override path worked in exactly one configuration, and not the one we shipped

**Found by a question, not by a test.** Mubarak's first enforced block came from
the *executor*, not the gate, and he asked: if the person receiving that email
wants to override, where does the execution ID come from?

Two things were wrong, and the second one is serious.

### The email omitted the instructions in exactly this case

`build_body` gated the override block on `action == "halt_pipeline"` -- that is,
on the GATE being the blocker. In advisory mode the gate does not block; the
executor refuses a high-risk verdict instead. So the email arrived, correctly
said *"a later stage may still refuse it"*, that stage did refuse it, and the
reader was left with no execution ID and no command.

The instructions were missing from precisely the configuration this project
tells people to roll out through. Now the condition is the verdict (`decision ==
halt`) rather than who acted on it, and the block names which stage stopped it.

### The executor never read the override at all

Worse. `read_risk_level` reads `verdict.risk_level`, which is the MODEL's
verdict -- and a human override deliberately does not change it. The audit
record keeps them as separate fields on purpose, because "a human overrode a
high-risk verdict" and "the model said low" are different events and flattening
them would destroy the audit trail (D-062's argument, applied to storage).

Nobody traced the consequence. The executor read only the risk level, so:

1. A human writes an override to `allow`.
2. They retry the Gate stage.
3. The gate re-runs, honours the override, records `decision: allow`.
4. The Deploy stage runs. The executor reads `risk_level: high` -- unchanged,
   correctly -- and **refuses again**.

The whole of Phase 5.3 functioned only when the gate was the blocker. In
advisory mode, where the executor is the blocker, an override could not unblock
anything. There was no error, no warning, and a retry that looked like it should
have worked.

**And there was a comment asserting otherwise.** `find_verdict`'s docstring:

> the executor reads the FULL record including any human override

It fetched the full record and then never looked at the override. A comment
describing an intention rather than the behaviour -- which is F-022 exactly, in
the same codebase, three days later.

### The fix

`read_override` plus `config_for(risk_level, override)`:

| override | result |
| --- | --- |
| `halt` | refuse, whatever the model said |
| `allow` | **canary** -- even for `high` |
| absent / unrecognised | the risk level decides, as before |

`allow` maps to canary rather than a full deploy, and the reasoning belongs on a
slide: **an override says "I accept this risk", not "I am certain there is
none."** The model flagged something and a human chose to proceed anyway.
Shifting 10% of traffic for a minute still catches it if the model was right and
costs a minute if it was wrong. Going straight to a full deploy would treat a
human's willingness to proceed as *evidence about the change*, which it is not.

That is also the only path by which a `high` verdict ever ships, and the
asymmetry is deliberate: it costs a named person, a written reason, and admin
credentials.

### The lesson

The failures log now has four consecutive entries where two components each
worked and the seam between them did not. This one adds a wrinkle: the seam was
**documented as working**. Both F-022 and F-025 were found by reading a comment
that turned out to be a wish.

> A comment is a claim about behaviour that nothing verifies. On an integration
> boundary, that makes it worse than silence -- it stops the next person
> checking.

Nine tests now cover the override path through the executor, including the two
directions (`allow` shipping a `high`, `halt` stopping a `low`) and the refusal
to guess at an unrecognised value.
