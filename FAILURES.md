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
