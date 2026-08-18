# Architectural decisions

Running log of choices and *why*. The reasoning matters more than the choice —
it is the substance of the talk.

Format: what was decided, what the alternatives were, why this one won, and
what would make us revisit.

---

## D-001 — Terraform for infrastructure as code

**Decided:** Terraform (>= 1.11) for all infrastructure.

**Alternatives:** AWS CDK, AWS SAM, CloudFormation.

**Why:** Existing preference and existing fluency. SAM would be a better fit
for the Lambda-and-pipeline core specifically, but the project also needs
Budgets, Inspector wiring and IAM across several services, and mixing two IaC
tools for one demo is a worse story on stage than one slightly verbose tool.

**Revisit if:** the CodeDeploy canary wiring in Terraform proves materially
harder than SAM's `AutoPublishAlias` + `DeploymentPreference` shorthand. Note
that SAM generates the same CodeDeploy resources underneath.

---

## D-002 — Region: us-east-1

**Decided:** us-east-1 for everything.

**Alternatives:** eu-west-1, af-south-1 (closest to the Lagos venue).

**Why:** Bedrock model and inference-profile coverage is widest in us-east-1
and it is the cheapest region. Latency from Lagos is irrelevant — the gate is
Lambda-to-Lambda inside AWS, with no interactive user in the loop. af-south-1
has limited Bedrock availability and would likely have blocked Phase 3.

**Revisit if:** the company environment has a data-residency requirement. This
is worth naming on stage: the demo's region choice is a cost-and-availability
call that a real deployment might not be free to make.

---

## D-003 — Terraform state in S3 with native locking, no DynamoDB lock table

**Decided:** S3 backend with `use_lockfile = true`. No DynamoDB lock table.

**Why:** Terraform 1.10+ supports S3-native state locking via a `.tflock`
object written next to the state. Most tutorials still tell you to create a
DynamoDB table; that is now obsolete. One fewer resource to create, explain
and tear down.

**Consequence:** anyone with read-only credentials must run
`terraform plan -lock=false`, because acquiring the lock is an S3 *write*.

---

## D-004 — Two-identity split: read-only agent, human-held admin

**Decided:** Claude operates as IAM user `ai-agent` (`ReadOnlyAccess` plus a
narrow Bedrock invoke policy). All `terraform apply` runs are performed by a
human using the `poly4` admin profile in a separate terminal.

**Why:** The project's central thesis is that an autonomous component should
hold the narrowest possible permissions and never be able to cause a deploy on
its own. Applying that to the *construction* of the system, not just its
runtime, keeps the argument honest. It also means a bad suggestion from the
agent is caught at human review rather than at apply time.

**Enforced by:** `ReadOnlyAccess` on the IAM side; `AWS_PROFILE=ai-agent` plus
explicit `terraform apply|destroy` denials in `.claude/settings.json` on the
tooling side. IAM is the real boundary — the settings file is only a guardrail
against wasted effort.

**This mirrors the runtime design:** decision service reads and writes a
verdict; executor holds the deploy permissions. Same shape, different layer.

---

## D-005 — Bedrock: `us.*` inference profiles only, not `global.*`

**Decided:** Target `us.*` cross-region inference profiles. Do not use
`global.*` profiles.

**Why:** Global profiles may route a request to regions outside the US, which
widens the IAM resource surface and makes the data-residency answer "it
depends". `us.*` keeps inference inside three known regions.

**The non-obvious part:** a `us.*` profile invocation is authorised against
*both* the inference-profile ARN in the calling account **and** the underlying
`foundation-model` ARN in whichever region actually serves the request. Granting
only us-east-1 produces intermittent `AccessDeniedException`s that look like
throttling. The policy therefore lists us-east-1, us-east-2 and us-west-2.

See `docs/iam/ai-agent-bedrock-policy.json`.

---

## D-006 — Structured output via forced tool use, not prose parsing

**Decided:** Get the risk verdict by declaring a tool with a JSON input schema
and setting `toolChoice: {"tool": {"name": ...}}` to force the model to call
it. Never ask for JSON in the prompt and parse the reply.

**Why:** "Reply with JSON" is a request; a forced tool call is a structural
constraint applied during decoding. It removes the entire class of failures
where the model wraps JSON in a markdown fence, prefixes it with "Certainly!",
or trails an explanation after the closing brace.

**What it does NOT give you:** Bedrock does not validate tool input against the
declared schema. The model is steered by the schema but can still return
something that violates it. Validation on our side is mandatory and is the
fail-closed boundary. This distinction is a slide.

**Schema shape choices** (see `scripts/check_bedrock_access.py`):

- `risk_level` is an enum — the executor branches on it, so an unknown value
  must be a hard failure rather than a default.
- `confidence` is recorded but never routed on. Self-reported model confidence
  is not calibrated, and treating it as a probability would be false precision.
- `reasoning` is length-capped. Unbounded free text is the vector by which
  prompt-injected content would reach logs and notifications.
- `additionalProperties: false` and all fields required — a partial verdict is
  an ambiguous verdict, and ambiguous means human review.

---

## D-007 — temperature 0 for verdicts

**Decided:** `temperature: 0.0` on all verdict calls.

**Why:** Constraint 6 requires deterministic replay — feed a fixed scenario in,
get a comparable verdict out, so prompt changes can be measured. Sampling noise
would show up in Phase 4 as prompt drift that is not real. Temperature 0 is not
a guarantee of identical output, but it removes the largest source of variance.

**Being honest on stage:** even at temperature 0, Bedrock does not promise
bit-identical responses across time or capacity. Phase 4 measures verdict
stability on repeated identical inputs precisely so we can report the real
number rather than claim determinism we do not have.

---

## D-008 — Account-wide budget rather than tag-filtered

**Decided:** The Phase 0 budget covers the whole account, not just resources
tagged for this project.

**Why:** A tag filter would miss anything created outside Terraform, which is
exactly the spend most likely to surprise you. Cost allocation tags also have
to be activated by hand in the Billing console and take roughly 24 hours to
start populating, so a tag-filtered budget would report zero on day one and
look reassuring while being blind.

**Notification-only for now.** The hard budget action needs its own IAM role
and lands in Phase 8, before the soak test authorises any standing cost.

---

## D-009 — Escalation to SNS email, chat destination deferred

**Decided:** Phase 5 escalation targets SNS with email subscribers. No Slack.

**Why:** No Slack workspace is available. Amazon Q Developer in chat
applications (the service formerly called AWS Chatbot) requires a workspace
and an admin-approved app install.

**Design consequence:** escalation sits behind an interface so a chat
destination is a configuration change, not a rewrite. SNS is the right seam
regardless — Q Developer in chat applications subscribes to an SNS topic, so
adding Slack later means adding a subscriber, not changing the publisher.

---

## D-010 — CodeDeploy owns the Lambda alias, Terraform yields it

**Decision:** `aws_lambda_alias.live` declares `lifecycle { ignore_changes = [function_version, routing_config] }` from the moment it is created, before CodeDeploy exists.

**Why:** Two systems both have a legitimate claim on which version the alias
points at. Terraform believes it owns everything it declares; CodeDeploy
actively rewrites `function_version` on every deploy and `routing_config`
during a canary. Without `ignore_changes`, any subsequent `terraform apply` —
including one for a completely unrelated resource elsewhere in the stack —
resets the alias to the version Terraform last recorded.

That failure is nasty specifically because it is *silent and delayed*. The
deploy succeeds, CodeDeploy reports green, and the rollback happens later when
someone applies an unrelated change. The symptom is "production reverted
itself", with the actual cause several hours and one unrelated commit away.

**The general rule:** Terraform declares intent; some state is owned by a
runtime system. Where both claim a value, one must yield explicitly and the
yield belongs in the code from the start, not added after the first incident.

**Cost of being wrong:** none — if we later decide Terraform should own the
alias, removing the lifecycle block is a one-line change.

---

## D-011 — Function URL pinned to the alias, authenticated with AWS_IAM

**Decision:** `aws_lambda_function_url` sets `qualifier = "live"` and
`authorization_type = "AWS_IAM"`.

**Why the qualifier:** an unqualified function URL serves `$LATEST`, which
CodeDeploy never touches. The canary would shift traffic between versions that
no caller ever reaches — a deployment that reports success while changing
nothing observable. This is a quietly common misconfiguration because
everything looks correct until you try to prove the canary worked.

**Why AWS_IAM now, while the app is harmless:** in Phase 2.5 this same function
gains dependencies with published CVEs so Amazon Inspector emits genuine
findings. CLAUDE.md's safety rule is that it must never be internet-reachable
without auth. Setting auth at creation means there is no later moment where
someone has to remember to lock it down — the vulnerable dependencies land in a
service that is already closed.

**Consequence:** every call needs SigV4 signing. That is friction during
development, so the stack emits a ready-to-run signed `curl` as an output; an
unsigned request returns a bare 403 that explains nothing.

---

## D-012 — Scoped log policy instead of AWSLambdaBasicExecutionRole

**Decision:** Lambda execution roles get an inline policy scoped to their own
log group ARN, not the AWS-managed `AWSLambdaBasicExecutionRole`.

**Why:** the managed policy grants `logs:CreateLogGroup` on `*`, letting the
function write to any log group in the account. Since Terraform pre-creates the
one log group each function needs — which it does anyway, to set retention and
avoid the never-expire default Lambda applies — `CreateLogGroup` is not needed
at all.

For the demo app this is a trivial win; it is harmless either way. It matters
because Phase 3's central constraint is that the decision service holds no
deploy permissions, and that argument is far easier to make from a codebase
that already scopes permissions everywhere than from one that reaches for the
convenient managed policy by default and makes an exception for the important
case.

---

## D-013 — arm64 for Lambda

**Decision:** demo app runs on `arm64` rather than `x86_64`.

**Why:** roughly 20% cheaper per GB-second at identical performance for pure
Python with no compiled dependencies. Free-tier usage makes the saving
negligible now, but Phase 8's soak runs continuous synthetic traffic for weeks
and the same default carries over.

**Watch for:** any dependency with compiled native extensions must be built for
arm64. The demo app has no dependencies today. When Phase 2.5 pins libraries
with known CVEs, confirm arm64 wheels exist or switch that function to x86_64 —
this is a plausible source of a confusing `Runtime.ImportModuleError`.

---

## D-014 — Fail-closed is the default branch, not an exception handler

**Decision:** the gate resolves its verdict by matching against an explicit
allow-list of recognised values; **every** other input — unset, empty,
whitespace, misspelled, unexpected exception — falls through to `halt`. There is
no code path that reaches `allow` other than by exact match, and no
`except: return allow` anywhere in the codebase.

**Why the distinction matters:** "fail closed" implemented as an exception
handler only covers the failures you thought of. CLAUDE.md's constraint 2 lists
model unavailable, schema invalid, signals missing, and throttling — and F-004
had already produced a failure outside that list (an unsubscribed AWS
Marketplace agreement) before any model existed. A catch-all default covers the
failure modes nobody has met yet, which is the entire population that matters.

**The tell that this is right:** the test file is a table of nine wrong values,
all asserting `halt`. Adding a tenth way to be wrong is a one-line change and
requires no new branch in the handler. If fail-closed were an exception handler,
each new failure mode would need its own `except` clause — and the ones nobody
anticipated would return whatever the happy path returns.

**Cost:** a misconfigured gate blocks deploys rather than passing them. That is
noisy and visible within minutes, which is the failure direction we want.

---

## D-015 — Mode is separate from verdict; an unreadable mode fails to `enforcing`

**Decision:** the verdict (what the gate concluded) and the mode (whether anyone
acts on it) are computed independently. An unrecognised **verdict** becomes
`halt`; an unrecognised **mode** becomes `enforcing`.

**Why they are separate:** it is what makes shadow mode a first-class mode
rather than a disabled feature (constraint 4). In shadow the verdict is computed
and recorded in full, and then deliberately not acted upon — so
`would_have_halted` is populated on every evaluation and is the field Phase 4
and Phase 8 count to get an over-flagging rate. A design where shadow mode
short-circuits before producing a verdict would measure nothing.

**Why the mode default is `enforcing`:** both defaults point the same way — stop
the deploy. The failure we are unwilling to accept is a change reaching
production because a config value was misspelled.

**The cost, stated plainly because it is a real trade:** a typo in `GATE_MODE`
turns an intended shadow-mode rollout into an enforcing one, and the gate starts
blocking deploys nobody expected it to touch. That is worse than it sounds
during a company-wide Phase 7 rollout. We accept it because an over-eager gate
announces itself immediately, whereas a silently disabled one is discovered by
the incident it failed to prevent. Phase 7 should add an explicit assertion on
resolved mode at pipeline start rather than relying on this default.

---

## D-016 — The gate holds no deploy permissions from increment 2, not from Phase 5

**Decision:** the gate stub is a separate Lambda with its own role whose entire
permission set is two log actions and the two CodePipeline job-result calls. No
`lambda:UpdateAlias`, no `lambda:UpdateFunctionCode`, no `codedeploy:*`, no
`iam:PassRole`.

**Why now rather than in Phase 5:** the separation is the project's central
security claim. Building it as one function and splitting it later would mean
the split had never been tested, and the Phase 5 diff would be the first time
anyone found out which permissions the decision path had quietly come to depend
on.

**What it actually buys, and it is worth being precise:** from Phase 2 the gate's
input includes commit messages, branch names, and file paths — attacker-
influenced text on any repository that accepts pull requests. From Phase 3 that
text is fed to a language model whose output steers a decision. The design
assumption is that prompt injection against that model will sometimes succeed.
What makes that survivable is that the most a successful injection can achieve
is a wrong verdict *record*: it cannot deploy, because the credentials to deploy
are not in the process. The blast radius is bounded by IAM, not by the model
behaving well.

`PutJobSuccessResult` is the one action that lets the gate influence a pipeline,
and it is bounded too — it can only report on a job the pipeline already handed
it, and reporting success is exactly what happens if the gate is absent
entirely. It cannot start a deploy that was not already running.

---

## D-017 — Managed policy for the CodeDeploy service role: a stated exception to D-012

**Decision:** the CodeDeploy service role attaches the AWS-managed
`AWSCodeDeployRoleForLambdaLimited` rather than a hand-written inline policy.

**Why this does not contradict D-012:** D-012 applies to roles we own, where we
know the complete set of actions our own code performs. This is a role AWS
assumes to operate its own service. The required permissions belong to
CodeDeploy and change when CodeDeploy changes; hand-rolling them buys a
marginally tighter policy today in exchange for a deployment that fails months
from now with an `AccessDenied` on an action that did not exist when we wrote it.

**Why the `Limited` variant:** it grants four actions — `lambda:UpdateAlias`,
`GetAlias`, `GetProvisionedConcurrencyConfig`, `cloudwatch:DescribeAlarms` —
plus S3 reads for S3-sourced AppSpecs. The unrestricted
`AWSCodeDeployRoleForLambda` additionally grants `sns:Publish` and broader Lambda
access we have no use for. Taking the tighter of the two AWS-provided options is
the D-012-consistent choice within the constraint.

**The general principle:** least privilege means owning the permissions you can
reason about and delegating the ones the service owns. Writing a worse version of
someone else's policy is not a security win.

**Noted for Phase 5:** the `Limited` policy scopes hook invocation to functions
named `CodeDeployHook_*`. A `BeforeAllowTraffic` validation hook — the natural
place for the executor to verify a canary — must either adopt that name prefix or
get its own role. Easier to know now than to discover from an `AccessDenied`
mid-deployment.

---

## D-018 — A custom canary config, because the fastest built-in one is five minutes

**Decision:** a custom `aws_codedeploy_deployment_config` shifting 10% for 1
minute, rather than `CodeDeployDefault.LambdaCanary10Percent5Minutes`.

**Why:** the fastest built-in Lambda canary holds for five minutes. That is a
sensible bake time and an unusable stage demo — five minutes of narrating a
progress bar. The custom config makes the shift watchable in about a minute.

**Why this is not a hack:** the deployment config is a separate resource from the
deployment group precisely so the *shape* of a rollout can change without
touching what is deployed or how rollback works. Demo cadence and production
cadence being different numbers in the same code is the intended use. Both are
variables; Phase 7 should raise them substantially.

**Related:** for the Lambda compute platform the deployment **target** (function
and alias) comes from the AppSpec supplied per deployment, not from the
deployment group — the group holds only policy. That is why
`scripts/deploy_canary.py` constructs an AppSpec rather than just naming a
target, and it is why the same group can later deploy the executor without being
redefined.

---

## D-019 — The canary script observes the traffic split rather than trusting the deployment status

**Decision:** `scripts/deploy_canary.py` samples the alias while the deployment
runs and reports whether both versions were seen serving simultaneously,
separately from CodeDeploy's own status.

**Why:** "CodeDeploy reported Succeeded" and "callers received a mix of both
versions" are different claims, and only the second one means the canary works.
The most common way to get this wrong — an alias reference or function URL that
quietly resolves to `$LATEST` — produces a green deployment and a traffic split
of exactly 0%. Nothing in the deployment status distinguishes that from a
correct canary.

This is the same discipline as `scripts/check_bedrock_access.py`, which asserts
that a Converse call returns schema-valid JSON rather than that Bedrock is
reachable. Assert the outcome you actually care about, not the nearest thing
that is easy to check.

**What the first real run taught us, and it changes Phase 5.** The script reports
*whether both versions were seen*, not the ratio — and that turned out to be the
only defensible claim it can make at 20 samples per poll. At a true 10% split,
a single 20-sample poll catches zero canary responses about 12% of the time, and
its 95% range is roughly 0–23%. The first live run duly showed two consecutive
polls at `v1=100%` mid-canary, on a completely healthy deployment.

Pinning a 10% split to ±2 points needs on the order of 900 samples. The
consequence for Phase 5 is concrete: any automated canary judgement of the form
"roll back if the canary's error rate looks elevated" is reading noise unless the
canary is receiving substantially more traffic than a demo generates. The
executor must either bake long enough to accumulate real samples, or defer to
CloudWatch alarms with their own statistical windows, rather than sampling and
comparing. Recorded here because it is the kind of thing that looks like a bug in
the observer and is actually a limit on what can be known.

---

## D-020 — Lambda over ECS as the deployment target, reconsidered on purpose

**Decision:** the demo app stays a Lambda. Revisited deliberately at the end of
Phase 1, once the cost of the choice was visible rather than hypothetical.

**What prompted the review:** CodePipeline has no action that drives a CodeDeploy
*Lambda* deployment, so increment 3 required a custom executor Lambda. ECS has a
native `CodeDeployToECS` action. The reasonable question was whether we had
picked a target that fights the tooling.

**First, a correction worth keeping straight.** CodeDeploy has first-class Lambda
support — weighted alias shifting, canary configs, auto-rollback. The gap is one
seam narrower: *CodePipeline* has no action for it. The documented AWS path for
Lambda-in-a-pipeline is CloudFormation/SAM, where `DeploymentPreference`
generates the CodeDeploy deployment. Our executor does by hand what SAM would
generate.

**The comparison:**

| | Lambda | ECS Fargate |
| --- | --- | --- |
| Pipeline action | none; ~150 lines of Python | native `CodeDeployToECS` |
| Infrastructure | function, alias, log group | VPC, subnets, IGW, routes, SGs, ALB, listener, 2 target groups, cluster, task def, service, ECR |
| Artifact | a zip | a container image, plus `taskdef.json`/`appspec.yaml` placeholder substitution |
| Deploy duration | ~60s | 5–10 min |
| Standing cost | ~$0 | ~$26/month (ALB ~$17 + one 0.25vCPU task ~$9) |

**Why Lambda won, in order of weight:**

1. **Cost.** ECS breaches the hard constraint on standing hourly charges, and
   Phase 8's multi-week soak multiplies it. ALB and Fargate bill whether or not
   anyone is demoing.
2. **The executor is not accidental complexity.** This is the argument that
   actually settles it. The target architecture already specifies a separate
   executor Lambda with its own role. Going native — via SAM or
   `CodeDeployToECS` — would *delete the component the talk is about*, and Phase
   5 would rebuild it to read verdicts and branch on risk level. The custom code
   is Phase 5 arriving early with its permission boundary already tested.
3. **"Native" is not "simpler overall."** ECS removes ~150 lines at one seam and
   adds ~300 lines of networking plus a container build.
4. **Stage reliability.** 60 seconds versus 5–10 minutes, over conference wifi,
   with ALB health checks in the path.
5. **Explainability.** "An alias points at two versions with a weight" is one
   sentence. ECS blue/green needs a diagram and assumes the audience knows ALBs.

**What ECS would genuinely have been better at**, recorded so the trade is
honest: the native pipeline action; richer Amazon Inspector findings, since ECR
image scanning covers OS *and* language packages while Lambda scanning sees only
the deployment package; closer resemblance to what much of the audience runs;
and health-check-driven rollback that feels more real than alias weights.

**The reframe that makes this a small decision.** The gate is
**deployment-target agnostic**. The decision service collects signals and emits
a verdict; it does not know what is being deployed. Only the executor knows. So
the demo target is an implementation detail of the *demo*, not of the
architecture — and "this is Lambda because Lambda was cheap to demo; here are
the twenty lines that would make it ECS" is a better talk moment than either
choice on its own.

**Deferred, not rejected:** an ECR repository holding a deliberately vulnerable
image purely as an Inspector signal source, never executed. It recovers ECS's
strongest advantage for a few cents. Revisit in Phase 2.5 when the Inspector
collector is real and we can see whether Lambda scanning produces enough
findings on its own.

---

## D-021 — An empty findings list is not evidence until coverage is established

**Decision:** `InspectorFindingsCollector` makes three API calls in a fixed
order, and refuses to interpret an empty findings list until the first two have
passed.

```text
BatchGetAccountStatus  ->  is Inspector on, and is Lambda scanning on?
ListCoverage           ->  is THIS function covered and actively scanned?
ListFindings           ->  only now does [] mean "clean"
```

**Why:** `list-findings` against this account returned `{"findings": []}` while
Inspector had never been enabled (FAILURES.md F-010). The three-line naive
collector reports zero known vulnerabilities for a service nobody has ever
scanned, and it reads as positive evidence of safety.

**Two switches, not one.** Inspector can be `ENABLED` account-wide while Lambda
scanning specifically is `DISABLED`. Checking only account status looks thorough
and catches nothing.

**A test asserts `list_findings` is never even called when the guard fails.**
Asserting only on the returned status would pass for a collector that asks the
question first and discards the answer — which is one careless refactor away
from using it.

**Cost:** three API calls instead of one, on every verdict. Both extra calls are
free and fast. That is the entire price of the signal meaning what it says.

---

## D-022 — Inspector answers a different question than the gate asks

**Decision:** `SecurityFindings.is_candidate_artifact` is recorded on every
result, and is `False` for everything Inspector produces.

**Why it matters more than a boolean suggests.** Amazon Inspector scans
**deployed** resources. The gate runs **before** the deploy. So findings
collected at gate time describe the version currently live — not the candidate
about to replace it.

The gate asks *"is this change safe?"*. Inspector answers *"is the thing this
change would replace currently known to be vulnerable?"*. Both are useful and
they are not the same question, and nothing in the API surface signals the
difference.

It is still worth collecting. Deploying into a service with active criticals is
real context, and a change that remediates them is a point in its favour. But
treating it as vulnerability data about the new code would be wrong, and the
mistake would be invisible.

**What would answer the real question:** an SBOM generated during the build and
scanned before deploy (`inspector-sbomgen` plus the `inspector-scan` API). That
is a separate mechanism with build-sourced provenance, deferred rather than
rejected.

**The general lesson, and it is the one worth teaching:** the obvious signal
source for a question often answers a subtly different question. Recording which
question was actually answered costs one field. Discovering the mismatch after
building a verdict layer on top of it costs considerably more.

---

## D-023 — Unknown severity gets its own value rather than being rounded

**Decision:** `Severity.UNKNOWN` exists. Inspector's `UNTRIAGED`, and any
severity string AWS adds in future, map to it.

**Why:** the two available shortcuts are both wrong. Folding unscored findings
into `LOW` or `INFORMATIONAL` makes an unscored critical vulnerability vanish
into the noise floor. Mapping them up to `CRITICAL` makes the gate cry wolf and
teaches people to ignore it.

A bundle reporting *"3 critical, 1 unknown"* is telling the truth. One reporting
*"3 critical, 1 low"* is not.

**Related, and the same reasoning applied twice more:**

- Inspector writes `"NotAvailable"` in `fixedInVersion` when no patch exists.
  Kept as a literal string it reads as a version number and `is_fixable` reports
  `True` for something with no remedy at all. Normalised to `None`.
- A finding that cannot be parsed is dropped and logged, not raised on. One
  malformed record in a list of forty should not discard the other thirty-nine.

---

## D-024 — Standard scanning only, not code scanning

**Decision:** enable Amazon Inspector **Lambda standard scanning**; leave Lambda
code scanning off.

**Verified pricing** (AWS Price List API, us-east-1, August 2026):

| Scan type | Hourly, per function | ~Monthly |
| --- | --- | --- |
| Lambda standard (dependencies) | $0.00042 | $0.31 |
| Lambda code (application logic) | $0.00084 | $0.61 |

At three Lambdas that is roughly **$0.92/month** versus $2.76. Both dimensions
also have `-free-trial` variants priced at zero.

**Why standard is sufficient:** Phase 2.5 synthesises deliberately vulnerable
*dependencies*, which is precisely what standard scanning detects. CLAUDE.md
forbids deliberately exploitable application logic, so code scanning would cost
double to find nothing by design.

**Flagged because it is a standing hourly cost** — the first collector whose
existence bills whether or not a deploy happens. Inspector appears in the
account's credit-eligible service list.

**Not enabling it is a supported state, not a broken one.** The
`security_scanning` variable distinguishes the two: `false` yields `SKIPPED`
("we chose not to look"), `true` runs the real collector, which reports
`UNAVAILABLE` with a reason while Inspector is off. Neither is ever allowed to
read as "nothing wrong".

---

## D-025 — Tests cannot reach AWS, enforced rather than intended

**Decision:** an autouse pytest fixture replaces `boto3.client` and
`boto3.resource` with a function that raises. Opt out per-test with
`@pytest.mark.aws`. Nothing currently opts out.

**Why:** CLAUDE.md constraint 5 was satisfied by construction until Phase 2.3,
when the gate began building a real Inspector client by default. The suite
silently started making live API calls and its runtime went from 2 seconds to 88
(FAILURES.md F-011).

The slowness was the symptom. The real cost is that such tests depend on
credentials, on network, and on live account state — so they pass on one machine
and fail in CI, or pass for the wrong reason. Enabling Inspector later would have
changed test outcomes with no code change at all.

**Paired with a design fix:** `collect_bundle()` now accepts injected collectors,
defaulting to the real ones. The same dependency injection `collect_signals()`
already used one level down, carried up to where the network dependency actually
appeared.

**A test asserts the guard itself works.** A guardrail nobody verifies is not a
guardrail — the lesson from F-003, applied to test infrastructure.

---

## D-026 — Rates are `None`, not zero, when there is nothing to divide by

**Decision:** `TargetHealth.error_rate_pct` and `p99_latency_ms` are
`float | None`, and are `None` whenever the window contains no invocations.

**Why:** an error rate is a ratio. A ratio with a zero denominator is undefined,
not zero. Reporting `0%` for an uninvoked function states something false, and
states it in the most reassuring possible direction (FAILURES.md F-012).

CloudWatch makes the mistake easy: `GetMetricData` for an idle function returns
`StatusCode: "Complete"` with an empty `Values` array. The query succeeded; there
is simply nothing in it. `sum(values) or 0` then yields a 0% error rate and a 0ms
p99 — a better health report than any real service could produce.

**The uncomfortable part:** the `or 0` exists to prevent a crash on empty input.
It works, and converts a loud failure into a silent falsehood. The crash would
have been safer.

**Also required:** both halves of the ratio. If Invocations reported data and
Errors did not, we do not know the error count, and assuming zero would invent
the most favourable answer available.

**Exposed as** `has_health_evidence`, which separates "measured and fine" from
"nothing to measure". Both otherwise look like an absence of problems.

---

## D-027 — Alarm coverage is recorded separately from alarm state

**Decision:** `TargetHealth.has_alarm_coverage` is a distinct field from the
`alarms` tuple.

**Why:** an empty alarm list is ambiguous between two facts with opposite
meanings — "this service is monitored and nothing is firing" (positive evidence)
and "this service has no alarms at all" (no evidence). They are indistinguishable
from the tuple alone, and the account genuinely has zero alarms today, so the
ambiguity is live rather than theoretical.

No coverage yields a DEGRADED signal: the metrics are real, and the alarm
dimension carries no information however healthy they look.

**Related:** alarms are matched to a service by the **FunctionName dimension**,
not by alarm name. Names are a human convention and drift — an alarm called
`demo-app-errors` that actually watches a different function would otherwise be
reported as evidence about this one. Dimensions are what CloudWatch evaluates.

**Consequence to expect:** every verdict is DEGRADED until Phase 2.5 creates
alarms. That is accurate rather than noisy, and it is the clearest possible
argument for doing Phase 2.5.

---

## D-028 — One `GetMetricData` call, and throttles count as errors

**Decision:** a single `GetMetricData` request covering Invocations, Errors,
Duration (p99) and Throttles. Not `GetMetricStatistics`.

**Why:** `GetMetricData` batches several metrics into one request and supports
percentile statistics directly. Four `GetMetricStatistics` calls would quadruple
the latency of every verdict for no benefit, and the gate sits in a pipeline
where its own latency is visible.

**Per-query `StatusCode` is checked, not assumed.** `PartialData` means the
window was not fully covered, so the value is a floor and the signal is DEGRADED.
`InternalError` on one metric leaves that metric `None` rather than zero, and the
other three still report.

**Throttles are added to the error count.** A throttled invocation never ran, and
from a caller's perspective that is a failed request. Excluding them would let a
service at its concurrency ceiling report a healthy error rate while rejecting
traffic — which is precisely the condition a deployment gate should notice.

---

## Open — model selection for the verdict layer

Not yet decided. `us.anthropic.claude-haiku-4-5-20251001-v1:0` is the default
in the Phase 0 smoke test because it is cheap and the smoke test needs no
judgment quality.

The real choice is a Phase 4 output: run the eval fixture set against several
models and pick on measured over-flagging rate and cost per verdict, not on
reputation. Record the result here when it exists.
