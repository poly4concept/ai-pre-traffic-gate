# Agentic Deployment Gate on AWS

An autonomous deployment gate that sits inside an AWS CI/CD pipeline and decides
whether a given change is safe to deploy *right now*.

A green test suite tells you the code compiles and the tests pass. It tells you
nothing about whether this change, to this service, at this moment, is risky.
This gate adds the missing judgment layer: it gathers real signals about the
change and the target environment, asks an Amazon Bedrock model for a
structured risk verdict, and acts on it — full deploy, canary, or halt and
escalate.

Reference implementation for **"Giving Your Pipeline Judgment: An Agentic
Deployment Gate with Amazon Bedrock"**, AWS Community Day West Africa 2026
(Lagos, 16–17 October 2026).

> **Status: Phase 1 complete. Starting Phase 2.** There is still no AI anywhere
> in the system, by design — Phase 1 existed to prove the deploy path works
> before a model could be blamed for anything.
>
> **Proven end to end against real AWS:**
>
> - Phase 0 groundwork — state bucket, $20/month budget, read-only agent identity
> - Demo app Lambda, `live` alias, IAM-authenticated function URL
> - **Canary** — observed two versions serving the alias simultaneously while
>   CodeDeploy shifted traffic. Not merely reported as succeeded; watched.
> - **Halt** — pipeline set to `halt`/`enforcing` failed at the Gate stage, the
>   Deploy stage never ran, and the alias did not move.
> - Full pipeline: GitHub → CodeBuild → Gate → executor → CodeDeploy canary
>
> Enforcement today is **structural**: the gate fails the pipeline job and
> CodePipeline declines to start the next stage. The executor is never consulted,
> so nothing in it has to be trusted to honour a halt. Verdict-driven *branching*
> — low risk deploys straight, medium canaries, high halts — is Phase 5.
>
> **Outstanding:**
>
> - **Bedrock invocation** — blocked on a billing gate, not a permissions one.
>   The account needs a valid payment instrument before AWS Marketplace will
>   complete the Anthropic model subscription; promotional credits do not
>   satisfy it. See [FAILURES.md](FAILURES.md) F-004. Nothing before Phase 3
>   depends on this, so it is not blocking Phase 2.

---

## How it works

```
CodePipeline
    │
    ├─► DECISION SERVICE  (Lambda — NO deploy permissions)
    │     ├── collect signals
    │     │     • change context — diff size, files touched, time of day,
    │     │       deploy frequency to this service
    │     │     • security findings — Amazon Inspector
    │     │     • target health — CloudWatch error rate, latency, alarm state
    │     ├── Bedrock Converse + forced tool use  ──►  structured verdict
    │     ├── strict JSON schema validation       ──►  fail closed on any doubt
    │     └── write immutable verdict record to DynamoDB
    │
    └─► EXECUTOR  (separate Lambda, separate IAM role — HOLDS deploy permissions)
          • low risk    → full deploy
          • medium risk → canary via CodeDeploy Lambda alias traffic shifting
          • high risk   → halt pipeline, escalate via SNS
```

### The four ideas worth stealing

**The decider cannot deploy.** The decision service can read signals and write
a verdict. That is the whole of its authority. The executor holds the deploy
permissions and acts only on verdict records that pass validation. A malicious
commit message cannot talk its way into a deploy, because the component reading
that commit message has no deploy permission to give away.

**Fail closed, as the default branch.** Model unavailable, schema invalid,
signals missing, Bedrock throttled, verdict ambiguous — all route to human
review. This is written as the default path, not as an exception handler added
after the first incident.

**Structured output is a constraint, not a request.** The verdict comes back
through the Converse API's tool-use interface with a declared JSON schema, and
is then validated on our side. Asking a model to "reply with JSON" is a
request. Forcing a tool call is structural. Neither one means the arguments are
valid, which is why we still validate. See
[D-006](DECISIONS.md#d-006--structured-output-via-forced-tool-use-not-prose-parsing).

**Shadow mode is permanent.** Three modes, and the first is not a stepping
stone to be deleted:

| Mode | Records verdict | Notifies | Blocks deploy |
|------|:---:|:---:|:---:|
| `shadow` | yes | no | no |
| `advisory` | yes | yes | no |
| `enforcing` | yes | yes | yes |

---

## Repository layout

```
infra/
  bootstrap/        Local state. Creates the TF state bucket + cost budget. Apply once.
  personal/         Main stack. S3 backend. Everything else lives here.
services/
  demo_app/         Trivial Lambda that exists only to be deployed and canaried.
  decision_service/ Signal collection, Bedrock call, verdict record.  (Phase 3)
  executor/         Reads verdicts, performs deploys.                  (Phase 5)
scripts/            Operational helpers and preflight checks.
docs/
  iam/              Policy documents applied outside Terraform.
  runbooks/         Reproducible recipes for synthesised signal scenarios.
tests/              Offline by default. Anything touching AWS is marked.
```

`DECISIONS.md` records architectural choices and their rationale.
`FAILURES.md` records what did not work. Both are talk deliverables.

---

## Prerequisites

- Terraform >= 1.11 (needs S3-native state locking)
- Python >= 3.12
- AWS CLI v2
- An AWS account with Bedrock access to Anthropic models in `us-east-1`

### Credentials

This project uses two identities, deliberately:

| Profile | Permissions | Used by | For |
|---|---|---|---|
| `ai-agent` | `ReadOnlyAccess` + narrow Bedrock invoke | the AI assistant | reading state, `terraform plan`, Bedrock calls |
| `poly4` | Admin | a human | `terraform apply`, IAM changes |

The split mirrors the runtime architecture — the component that decides is not
the component that acts. See
[D-004](DECISIONS.md#d-004--two-identity-split-read-only-agent-human-held-admin).

```powershell
aws configure --profile ai-agent      # configure interactively; never paste keys into a chat
aws sts get-caller-identity --profile ai-agent
```

Because `ai-agent` cannot write the S3 lock object, read-only planning needs:

```powershell
terraform plan -lock=false
```

---

## Getting started

### 1. Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 2. Confirm Bedrock is actually usable

Do this before anything else. It checks three separate things that fail in
three different ways: the model exists in the region, you are allowed to invoke
it, and forced tool use returns schema-valid JSON.

```powershell
# Enumerate models and profiles. Makes no billable call.
python scripts/check_bedrock_access.py --list-only

# Full check, including one real Converse call (costs a fraction of a cent).
python scripts/check_bedrock_access.py
```

### 3. Bootstrap — state bucket and cost budget

**Run by a human on the admin profile.** Creates the budget guardrail before
anything billable exists.

```powershell
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars   # set budget_alert_email
$env:AWS_PROFILE = "poly4"
terraform init
terraform plan
terraform apply
```

Then **click the confirmation link AWS emails you**. An unconfirmed budget
subscription silently never delivers.

### 4. Main stack

```powershell
cd ../personal
terraform init      # fails until step 3 has created the bucket
terraform plan
```

---

## Cost

Designed to sit near zero on a personal account.

| Resource | Cost |
|---|---|
| S3 state bucket | Pennies per month |
| AWS Budgets | First two budgets per account are free |
| Lambda, DynamoDB on-demand | Free tier at demo volume |
| Bedrock | On-demand tokens only; a verdict is a fraction of a cent |
| CodePipeline V2 | Per action-execution-minute, not a monthly pipeline charge |
| CodeDeploy (Lambda) | No charge |

**Nothing with a standing hourly cost is created** — no NAT gateway, no
always-on Fargate, no provisioned concurrency. Phase 8's soak test is the one
authorised exception, and it requires a costed plan and sign-off first.

Verify current pricing before relying on these figures; they move.

---

## Build phases

Each phase must work before the next begins.

| | Phase | State |
|---|---|---|
| 0 | Groundwork — scaffold, IaC skeleton, budget, Bedrock access | done |
| 1 | Boring deploy path, zero AI — pipeline, canary, hardcoded halt | done |
| 2 | Signal collectors — common interface, mocks first, then real | done |
| 2.5 | Synthesised-real signals — controllable faults, genuine findings | 2.5a/b/d done; 2.5c (Inspector CVEs) deferred |
| 3 | Verdict layer, shadow mode only | code complete, awaiting a live model call |
| 4 | Eval harness — ~20 labelled scenarios, over-flagging measurement | **done.** Haiku 4.5 measured at 81.0% acceptable / 30% under-flagging, losing to the arithmetic baseline (85.7% / 20%). Results and the reasoning in [evals/results/](evals/results/) |
| 5 | Enforcement + escalation — executor, canary/halt, human override | 5.1 done (executor reads the verdict, branches on risk); 5.2 done (SNS escalation, advisory mode now notifies). Both behind switches that are still off |
| 6 | Demo and talk assets | not started |
| 7 | Company environment hardening | not started |
| 8 | Soak — simulated traffic, measured results | not started |

Phase 1 comes before any model call on purpose. Proving you can deploy, canary
and halt reliably *without* AI is the step people skip and then spend the rest
of the project debugging, unable to tell infrastructure faults from model
faults.

---

## Safety notes

From Phase 2.5 the demo app carries **deliberately vulnerable dependencies** so
that Amazon Inspector produces genuine findings. Ground rules, without
exception:

- Never internet-reachable without authentication.
- Vulnerable *dependencies* only — no deliberately exploitable application
  logic.
- No real secrets and no real data, ever.
- Isolated from everything else in the account, and tagged for teardown.
- Every CVE used is documented in `DECISIONS.md` with the reason.

Every synthesised condition ships with a documented recipe **and** a teardown.
