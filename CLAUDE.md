# Project: Agentic Deployment Gate on AWS

## What we're building

An autonomous deployment gate that sits inside an AWS CI/CD pipeline and decides
whether a given change is safe to deploy. A passing test suite tells you the code
compiles and tests pass; it tells you nothing about whether *this* deploy, to
*this* service, at *this* moment, is risky. This gate adds that missing judgment
layer.

On each pipeline run it gathers real signals about the change and the target
environment, asks an Amazon Bedrock foundation model for a structured risk
verdict, and then acts on that verdict: deploy fully, roll out a canary, or halt
and escalate to a human.

This is the reference implementation for a conference talk at AWS Community Day
West Africa 2026 (Lagos, 16-17 Oct 2026), titled "Giving Your Pipeline Judgment:
An Agentic Deployment Gate with Amazon Bedrock". So the code needs to be
demo-able live on stage, readable by an intermediate-level audience, and honest
about its own failure modes.

## Who I am / context

DevOps engineer, ~5 years, AWS Certified DevOps Engineer Professional, AWS
Community Builder. Comfortable with IaC, CI/CD, Lambda, containers. Less
experienced with Bedrock and agentic patterns specifically — explain model-side
choices (prompt structure, tool use, schema validation) as much as you explain
AWS plumbing.

## Target architecture

Pipeline stage (AWS CodePipeline + CodeBuild)
  → invokes DECISION SERVICE (AWS Lambda, Python)
      ├── collects signals:
      │     - change context: commit history, diff size, files touched,
      │       time of day / day of week, deploy frequency to this service
      │     - security findings for the change (Amazon Inspector)
      │     - live target health (Amazon CloudWatch metrics: error rate,
      │       latency, recent alarm state for the service being deployed)
      ├── calls Amazon Bedrock for a risk verdict
      │     - structured output enforced via the Converse API's tool-use
      │       interface, NOT free prose that we then parse
      │     - strict JSON schema validation on whatever comes back
      │     - FAIL CLOSED: any schema failure, timeout, throttle, or
      │       ambiguous result routes to human review. Never fail open.
      └── writes an immutable verdict record (Amazon DynamoDB) + structured logs

  → EXECUTOR (separate Lambda, separate IAM role) reads the verdict and acts:
      - low risk    → full deploy
      - medium risk → canary via AWS CodeDeploy (Lambda alias traffic shifting)
      - high risk   → halt pipeline + escalate

  → ESCALATION: Amazon SNS → Amazon Q Developer in chat applications (Slack) or if there's a simple path
    with the verdict, the reasoning, and the signals it was based on

## Hard design constraints

1. **The decision service has NO deploy permissions.** It can read signals and
   write a verdict. That's it. The executor holds the deploy permissions and can
   only act on verdict records that pass validation. A malicious commit message
   or prompt injection must not be able to cause a deploy.
2. **Fail closed, always.** Model unavailable, schema invalid, signals missing,
   Bedrock throttled → route to human. Encode this as the default branch, not an
   exception handler bolted on later.
3. **Every verdict is auditable and overridable.** Store the full input signals,
   the raw model output, the validated verdict, the action taken, and any human
   override. This audit trail is a demo asset, not just hygiene.
4. **Shadow mode is a first-class mode, not a flag we remove.** In shadow mode
   the gate records what it would have done and takes no action. Three modes:
   `shadow` | `advisory` (comments/notifies but doesn't block) | `enforcing`.
5. **Signal collectors sit behind a common interface** with a mock
   implementation for each. I must be able to run the whole flow end to end with
   zero real AWS signal dependencies, so development isn't blocked on Inspector
   setup or on generating real production traffic.
6. **Deterministic replay for testing.** I need to feed a fixed scenario in and
   get a comparable verdict out, so I can measure prompt changes.

## Environment & constraints

- Testing on my **personal AWS account** first, then adapting for a company-wide
  environment. Keep cost near zero: Lambda + DynamoDB on-demand + Bedrock
  on-demand only. Do not introduce anything with a standing hourly cost (no NAT
  gateway, no always-on Fargate, no provisioned concurrency) without flagging it
  and asking me first.
- Everything as infrastructure-as-code. Ask me which I'm using before you
  scaffold (preference:  Terraform).
- The demo app being deployed should be trivial — a single Lambda behind a
  function URL is enough. It exists only to be deployed and canaried.
- Python for Lambdas unless you argue otherwise.

## Things that have changed recently — verify, don't assume

Your training data may be stale on these. Check current AWS docs before writing
code that depends on them:
- **CodeGuru Security is discontinued** (end of support Nov 20 2025) and
  **CodeGuru Reviewer is in maintenance mode** (no new repo associations since
  Nov 7 2025). Do not build on either. Use **Amazon Inspector** for code/
  dependency security scanning and **Amazon Q Developer** for code review.
- **AWS Chatbot was renamed Amazon Q Developer in chat applications** (Feb 2025).
- Bedrock model IDs, region availability, and the Converse API's tool-use /
  structured-output surface all shift. Verify current model IDs and confirm
  model access is enabled in my region before writing invocation code.
- Amazon Bedrock AgentCore is the managed agentic platform (runtime, memory,
  policy, observability). We are deliberately NOT using it in phase 1 — plain
  Bedrock + Lambda first. We may layer AgentCore Policy in later as the
  deterministic guardrail. Design so that swap is possible; don't build for it
  now.

## Implementation plan — follow this order

Do not jump ahead. Each phase must work before the next starts.

- **Phase 0 — Groundwork.** Repo scaffold, IaC skeleton, AWS Budgets alarm on my
  personal account, confirm Bedrock model access in region, README.
- **Phase 1 — Boring deploy path, zero AI.** Trivial demo app, CodePipeline +
  CodeBuild, CodeDeploy canary deploying via Lambda alias traffic shifting, plus
  a hardcoded halt. Prove I can deploy, canary, and halt reliably *before* any
  model is involved. This is the phase people skip and then debug forever.
- **Phase 2 — Signal collectors.** Common interface, mock implementations first,
  then real ones (change context → Inspector → CloudWatch, in that order of
  difficulty). Output a single normalized signal bundle.
- **Phase 3 — Verdict layer, shadow mode only.** Bedrock Converse + tool use for
  schema-enforced JSON, validation, fail-closed defaults, DynamoDB audit
  records. Takes no action on anything.
- **Phase 4 — Eval harness.** A fixture set of ~20 labeled scenarios (safe
  dependency bump, risky payments change, Friday-evening deploy into an active
  alarm, huge refactor, config-only change, etc.) with expected verdicts. I need
  to measure over-flagging and detect prompt drift as the prompt evolves. This
  gates everything after it.
- **Phase 5 — Enforcement + escalation.** Executor Lambda with its own narrow
  role, canary/halt execution, SNS → Slack escalation, human override path.
  Advisory mode before enforcing mode.
- **Phase 6 — Demo & talk assets.** A reliably-reproducible risky scenario that
  trips the gate on stage, plus captured verdict records and metrics.
- **Phase 7 — Company environment hardening.** Multi-account, least privilege
  review, secrets, observability, rollout plan starting in shadow mode.

## How I want you to work

- Start by asking me the open questions (IaC choice, region, repo host, whether
  a Slack workspace is available) rather than guessing. Then confirm the Phase 0
  + Phase 1 plan before writing code.
- Small, reviewable increments. I want to understand every piece, because I have
  to explain it on stage to an intermediate audience. So you should also explain to me what we are doing at each phase and increment, again because I need to understand what I'll teaching other people
- When you make a model-side design choice (prompt structure, how the schema is
  shaped, temperature, which signals go in which order), explain the reasoning
  briefly — that reasoning is talk content.
- Flag anything that will cost money before creating it.
- Keep a running `DECISIONS.md` of architectural choices and their rationale,
  and a `FAILURES.md` of things that didn't work and why. The talk explicitly
  covers what the gate got wrong, so the failures are deliverables, not
  embarrassments.






  ## Signal synthesis — two distinct kinds, don't conflate them

**Mocked signals** = hardcoded JSON returned by a collector's mock
implementation. Purpose: unblock development, deterministic tests, eval
fixtures. Cheap, instant, and never touches AWS.

**Synthesized-real signals** = we deliberately engineer conditions in the
account so real AWS services genuinely emit real findings, which the real
collectors then parse. Purpose: prove the collectors handle actual API response
shapes, and generate credible evidence for the talk.

Both are required. Mocked signals alone will hide parsing bugs and produce a
demo I can't honestly defend on stage.

### Phase 2.5 — Synthesized-real signal generation

The demo app must be built with deliberate, *controllable* failure modes so we
can manufacture each signal type on demand:

- **Error rate / latency:** endpoints with configurable fault injection — a
  throw-rate parameter, an artificial-delay parameter, a memory-growth mode.
  Driven by env var or a DynamoDB config record so I can change behaviour
  without redeploying.
- **Alarm state:** CloudWatch alarms on the demo app tuned tight enough that
  the fault injection reliably trips them, so the gate can observe a service
  that is *actually* unhealthy right now.
- **Security findings:** a branch pinning dependency versions with known,
  published CVEs so Amazon Inspector produces genuine findings. Use
  well-documented inert vulnerabilities in libraries we don't actually exercise
  — the goal is a real finding in a real report, not an exploitable app.
- **Change context:** a script generating crafted commits — oversized diffs,
  changes to files matching sensitive path patterns (payments/, auth/),
  off-hours commit timestamps, rapid successive deploys to the same service.

Every synthesized condition needs a documented recipe and a teardown. I must be
able to reproduce any scenario on demand for the stage demo, and revert the
account to a clean state afterwards.

### Safety rules for the deliberately vulnerable app

- Never internet-reachable without auth, or don't expose it at all.
- Vulnerable dependencies only; no deliberately exploitable application logic,
  no real secrets, no real data, ever.
- Isolated from anything else in the account. Tagged for teardown.
- Document exactly which CVEs and why, in DECISIONS.md.

## Phase 8 — Soak: simulated production traffic (post-build, budgeted)

After the build works end to end, run the gate in shadow mode against
continuous simulated traffic for several days to weeks to produce real
measurements. Budget: tens of USD, covered by AWS credits. This is the ONE
phase where standing cost is authorized — and it still needs a plan before
anything is created.

- **Load generator:** EventBridge-scheduled Lambda firing synthetic requests.
  Do NOT stand up EC2 or always-on Fargate for load generation.
- **Traffic shape:** vary by time of day and inject fault windows on a schedule,
  so the signal history has realistic texture rather than a flat line.
- **Pipeline runs:** drive deploys on a schedule across the full scenario mix so
  every verdict class gets exercised many times.
- **What we're measuring:** verdict distribution, over-flagging rate on benign
  changes, false-negative rate on injected-risk changes, verdict stability on
  repeated identical inputs, prompt drift over time, end-to-end latency, and
  cost per verdict.
- **Before starting:** verify current pricing for CloudWatch custom metrics,
  CloudWatch Logs ingestion, Inspector, CodePipeline, DynamoDB, and Bedrock
  tokens at the projected volumes. Present me a projected daily and total cost
  estimate for sign-off before creating anything. Cost is dominated by
  observability and pipeline executions, not by model tokens — watch custom
  metric cardinality and log ingestion volume specifically.
- **Guardrails:** AWS Budgets with a hard action (not just an email alarm), a
  scheduled kill switch on the load generator, and a documented teardown that
  returns the account to near-zero standing cost.
- **Output:** a results dataset and summary that can be charted for the talk,
  kept separate from and later aggregated with company pilot data.