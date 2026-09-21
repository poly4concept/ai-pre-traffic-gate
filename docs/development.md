# Development

Setup, credentials, running the thing, and how it was built. The
[README](../README.md) covers what it is and why; this covers how to work on it.

---

## Prerequisites

- Terraform >= 1.11 (needs S3-native state locking)
- Python >= 3.12
- AWS CLI v2
- An AWS account with Bedrock access to Anthropic models in `us-east-1`

That last one is not a formality. See [FAILURES.md](../FAILURES.md) F-004, F-009
and F-014: getting from a fresh account to a served Bedrock token took four
separate gates, two of which were commercial rather than technical, and every
one of them surfaced through an error code describing something else.

---

## Credentials

This project uses two identities, deliberately:

| Profile | Permissions | Used by | For |
|---|---|---|---|
| read-only agent | `ReadOnlyAccess` + narrow Bedrock invoke | the AI assistant | reading state, `terraform plan`, Bedrock calls |
| admin | Admin | a human | `terraform apply`, IAM changes |

The split mirrors the runtime architecture — the component that decides is not
the component that acts. See
[D-004](../DECISIONS.md#d-004--two-identity-split-read-only-agent-human-held-admin).

It has already paid for itself once. Anthropic models on Bedrock need an AWS
Marketplace subscription, created implicitly by the first successful invocation
by a principal holding `aws-marketplace:Subscribe`. The correct fix is for a
human admin to invoke the model once, subscribing the account for everyone. The
tempting fix is to add Marketplace permissions to the read-only identity — which
quietly hands a deliberately-powerless identity the ability to commit the
account to paid subscriptions. One line of effort apart, very far apart in blast
radius.

```powershell
aws configure --profile <agent-profile>     # interactively; never paste keys into a chat
aws sts get-caller-identity --profile <agent-profile>
```

Because the read-only identity cannot write the S3 lock object, read-only
planning needs:

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

Do this before anything else. It checks three separate things that fail in three
different ways: the model exists in the region, you are allowed to invoke it, and
forced tool use returns schema-valid JSON.

```powershell
# Enumerate models and profiles. Makes no billable call.
python scripts/check_bedrock_access.py --list-only

# Full check, including one real Converse call (costs a fraction of a cent).
python scripts/check_bedrock_access.py
```

**The only evidence that a model call works is a model call that worked.** Not a
cleared error, not a subscription email, not a quota page. This project asserted
"Bedrock works" three times before it was true, each time on the strength of one
gate clearing rather than one call succeeding.

### 3. Bootstrap — state bucket and cost budget

**Run by a human on the admin profile.** Creates the budget guardrail before
anything billable exists.

```powershell
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars   # set budget_alert_email
$env:AWS_PROFILE = "<admin-profile>"
terraform init
terraform plan
terraform apply
```

Then **click the confirmation link AWS emails you**. An unconfirmed budget
subscription silently never delivers.

### 4. Main stack

```powershell
cd ../personal
cp terraform.tfvars.example terraform.tfvars   # set escalation_email, gate_mode
terraform init      # fails until step 3 has created the bucket
terraform plan
terraform apply
```

The defaults in `variables.tf` stay deliberately safe — a fresh clone gets shadow
mode, nothing enforcing, and no email subscriber — so that a stranger running
`terraform apply` does not end up with a gate that can block deploys or mail an
address the author typed in.

### 5. Tests

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

Offline by default — a `conftest.py` guard fails any test that reaches for
boto3 without marking itself. `ruff format` also formats Python code blocks
inside Markdown files, which has broken the build more than once after an
innocent-looking edit to `FAILURES.md`.

---

## Running the demo

```powershell
python scripts/preflight.py                    # is the account in a demo-ready state?
python scripts/stage_demo.py halt --dry-run    # rehearse; pushes nothing
python scripts/stage_demo.py halt              # live: inject faults, trip alarms, push
python scripts/stage_demo.py capture           # save the last real run for replay
python scripts/stage_demo.py replay            # no AWS, no credentials, no network
```

Two things to know before running it live:

- **Leave an hour between full rehearsals.** Health is measured over 60 minutes,
  so a second demo starts with the first one's injected errors still in the
  window and reaches `high` faster, for partly the wrong reason. Use `replay` for
  practice runs.
- **Expect the teardown commit to be halted.** The cleanup commit lands inside
  the same 60-minute window and the gate correctly refuses to deploy into a
  service that was throwing errors ten minutes ago. That is not a bug. See
  [FAILURES.md](../FAILURES.md) F-029.

Other operational scripts:

| Script | What it does |
|---|---|
| `inject_fault.py` | turn controllable faults in the demo app on and off |
| `drive_traffic.py` | generate requests so health signals have a denominator |
| `craft_commit.py` | synthesise commits with specific risk characteristics |
| `override.py` | record a human override against a verdict |
| `gate_history.py` | read the verdict table as a readable log |
| `capture_artifacts.py` | regenerate `demo/artifacts/` from results and the live table |

Reproducible recipes for every synthesised condition, each with a teardown, are
in [runbooks/synthesized-signals.md](runbooks/synthesized-signals.md).

---

## Cost

Designed to sit near zero on a personal account.

| Resource | Cost |
|---|---|
| S3 state bucket | Pennies per month |
| AWS Budgets | First two budgets per account are free |
| Lambda, DynamoDB on-demand | Free tier at demo volume |
| CodePipeline V2 | Per action-execution-minute, not a monthly pipeline charge |
| CodeDeploy (Lambda) | No charge |
| Amazon Inspector (Lambda standard) | ~$0.91/month for 3 functions — the only standing charge |
| Bedrock | On-demand tokens only; a verdict is a fraction of a cent |

**Nothing with a standing hourly cost is created** — no NAT gateway, no always-on
Fargate, no provisioned concurrency. Phase 8's soak test is the one authorised
exception and requires a costed plan first.

One billing detail worth knowing: **Anthropic models on Bedrock bill through AWS
Marketplace** (`USE1-MP:` usage types), and Marketplace charges are **not**
covered by AWS promotional credits. Amazon Nova bills as first-party
`AmazonBedrock` and is credit-eligible. That difference is large enough to
influence model choice on a personal account, and it is invisible until the bill
arrives.

Verify current pricing before relying on any of these figures; they move.

### Pausing everything

To park the project without destroying it:

```powershell
# 1. Stop pipeline runs (instant, no Terraform, fully reversible)
aws codepipeline disable-stage-transition `
  --pipeline-name ai-pre-traffic-gate-pipeline `
  --stage-name Build --transition-type Inbound `
  --reason "paused"

# 2. Stop the standing charges
terraform -chdir=infra/personal apply `
  -var="inspector_enabled=false" -var="baseline_traffic_enabled=false"
```

Reverse with `enable-stage-transition` and an apply with both back to `true`.
Disabling the baseline traffic means target health reports UNKNOWN, which pushes
every verdict upward — correct behaviour, and a reminder that an absent signal is
not a negative signal.

---

## Build phases

Each phase had to work before the next began.

| | Phase | State |
|---|---|---|
| 0 | Groundwork — scaffold, IaC skeleton, budget, Bedrock access | done |
| 1 | Boring deploy path, zero AI — pipeline, canary, hardcoded halt | done |
| 2 | Signal collectors — common interface, mocks first, then real | done |
| 2.5 | Synthesised-real signals — controllable faults, genuine findings | 2.5a/b/d done. 2.5c: Inspector enabled, so the security signal returns a real answer; a branch pinning published CVEs is still outstanding |
| 3 | Verdict layer, shadow mode only | done |
| 4 | Eval harness — 22 labelled scenarios, over-flagging measurement | done |
| 5 | Enforcement + escalation — executor, canary/halt, human override | done, verified end to end |
| 5.5 | Deterministic security floor | done — under-flagging 27.3% → 0.0% |
| 6 | Demo and talk assets — stage script, replay mode, artifact capture | done |
| 7 | Company environment hardening | not started |
| 8 | Soak — simulated traffic, measured results | not started |

**Phase 1 comes before any model call on purpose.** Proving you can deploy,
canary and halt reliably *without* AI is the step people skip and then spend the
rest of the project debugging, unable to tell infrastructure faults from model
faults. It also meant that five days blocked on Bedrock account verification cost
nothing — everything downstream of the model call was already written and tested
against fakes.

### Two kinds of fake signal, and they are not interchangeable

**Mocked signals** are hardcoded responses from a collector's mock
implementation. They unblock development, make tests deterministic, and give the
eval harness its fixtures. They never touch AWS.

**Synthesised-real signals** are conditions deliberately engineered in the
account so that real AWS services genuinely emit real findings, which the real
collectors then parse. They prove the collectors handle actual API response
shapes.

Both are required. Mocked signals alone hide parsing bugs and produce a demo
nobody can honestly defend.

---

## Safety notes

The demo app is built to carry **deliberately vulnerable dependencies** so that
Amazon Inspector produces genuine findings. Ground rules, without exception:

- Never internet-reachable without authentication.
- Vulnerable *dependencies* only — no deliberately exploitable application logic.
- No real secrets and no real data, ever.
- Isolated from everything else in the account, and tagged for teardown.
- Every CVE used is documented in [DECISIONS.md](../DECISIONS.md) with the reason.

Every synthesised condition ships with a documented recipe **and** a teardown.
