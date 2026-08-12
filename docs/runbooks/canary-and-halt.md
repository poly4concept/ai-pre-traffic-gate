# Runbook — canary a deploy, and halt one

Phase 1 increment 2. Recipes for the two things this increment exists to prove,
plus teardown. Every command needs write access, so run them under the admin
profile:

```powershell
$env:AWS_PROFILE = "poly4"
```

Console navigation is deliberately absent — see FAILURES.md F-004 for why an
undated console path is a liability in a runbook. Everything here is API-level.

---

## Recipe 1 — watch a canary shift traffic

**What this proves:** two Lambda versions serve the `live` alias simultaneously
while CodeDeploy moves the split. This is the increment's actual deliverable.

### 1. Publish a second version

The alias needs somewhere to shift *to*. Bump the version label so the two
builds are distinguishable in responses — **by editing the default in
`variables.tf`**, not with a `-var` flag:

```hcl
variable "demo_app_version" {
  default = "1.2.0"   # was 1.1.0
}
```

```powershell
cd infra/personal
terraform apply
```

Passing `-var demo_app_version=1.2.0` instead would work exactly once and then
silently revert on the next apply, because the flag is persisted nowhere.
FAILURES.md F-006 has the full account — it is worth reading before you reach
for `-var` again.

Confirm two versions now exist:

```powershell
aws lambda list-versions-by-function `
  --function-name ai-pre-traffic-gate-demo-app `
  --query 'Versions[].Version' --output text
```

Expect `$LATEST  1  2`. The `live` alias still points at `v1` — Terraform does
not move it, by design (DECISIONS.md D-010).

### 2. Run the canary

```powershell
python scripts/deploy_canary.py --to 2
```

The script prints the current state, creates the deployment, then samples the
alias every 5 seconds. Expected output shape:

```text
3. Watching the traffic shift

         InProgress    v1=100%
  SPLIT  InProgress    v1=90%  v2=10%
  SPLIT  InProgress    v1=85%  v2=15%
         InProgress    v2=100%
  PASS  Deployment Succeeded
  PASS  Observed BOTH versions serving the alias during the deployment
```

The `SPLIT` marker is the thing to point at. **Both versions appearing at once is
the signal; the ratio is not.**

### How to read the percentages (and why they look wrong)

A real run looked like this:

```text
  SPLIT  Created       v1=95%  v2=5%
  SPLIT  InProgress    v1=80%  v2=20%
  SPLIT  InProgress    v1=90%  v2=10%
         InProgress    v1=100%
         InProgress    v1=100%
  SPLIT  InProgress    v1=40%  v2=60%
         Succeeded     v2=100%
```

Two polls showed **no `v2` at all**, mid-canary, on a working deployment. That
looks like a bug and is not one. It is worth understanding, because the same
statistics govern every automated canary judgement this project will later make.

At a true 10% split with 20 samples, the chance a single poll happens to catch
zero `v2` responses is `0.9^20`, about **12%** — roughly one poll in eight. Across
the five canary-phase polls above, seeing at least one all-`v1` poll was more
likely than not. Nothing was wrong.

The width is the point. The standard error on a 10% proportion at n=20 is
`sqrt(0.1 × 0.9 / 20)` ≈ 6.7 percentage points, so a single poll's 95% range is
roughly **0% to 23%**. Every reading above sits inside that except `v2=60%`, and
that one is not noise — it is the post-canary phase, where CodeDeploy shifts the
remaining traffic after the interval elapses.

| Samples per poll | 95% range around a true 10% |
|---|---|
| 20 (default) | 0% – 23% |
| 100 | 4% – 16% |
| 900 | 8% – 12% |

To pin a 10% split to ±2 points you need roughly **900 samples**. This is the
single most useful thing in this runbook: eyeballing a canary requires far more
traffic than intuition suggests, and any rule of the form "roll back if the
canary error rate looks high" is measuring noise until the sample size is large.
Phase 5's automated canary analysis has to reckon with this explicitly rather
than trusting a ratio.

Two smaller observations from the same run:

- The split appeared while the deployment still reported `Created`. CodeDeploy
  writes the alias routing config before advancing its own status, so traffic
  moves slightly earlier than the status implies.
- `Succeeded` and `v2=100%` landed on the same poll. The final shift is not
  gradual — once the interval elapses, the remainder moves at once.

**Useful flags:**

| Flag | Effect |
|---|---|
| `--dry-run` | Print the AppSpec and exit; creates nothing |
| `--samples 50` | More invocations per poll — tighter percentages, slower |
| `--poll 2` | Poll more often; helps if the split is being missed |
| `--rollback` | Shift to the version *below* current |

### 3. Roll back

```powershell
python scripts/deploy_canary.py --rollback
```

This is a forward deployment to an earlier version, not an undo — CodeDeploy
canaries the rollback the same way, so it is equally observable.

### If you never see `SPLIT`

At a 1-minute interval the canary phase is short and easy to poll straight past.
Retry with `--poll 2 --samples 40`. If it still never appears, check the failure
this is designed to catch:

```powershell
# Is the alias actually moving?
aws lambda get-alias --function-name ai-pre-traffic-gate-demo-app --name live `
  --query '[FunctionVersion,RoutingConfig]'
```

If `FunctionVersion` never changes, the deployment is not reaching the alias. If
callers see one version while the alias reports another, something is addressing
`$LATEST` instead of the alias (DECISIONS.md D-011).

---

## Recipe 2 — halt, and prove it cannot be bypassed

**What this proves:** the gate stops a deploy on command, and — more importantly
— stops one when it is *misconfigured* rather than passing it through.

### First, a trap worth understanding

`gate_mode` defaults to **`shadow`**, so the stack as applied records verdicts
and acts on none of them. If you set `gate_decision=halt` and leave the mode
alone, you get a halt verdict with `"action_taken": "none"` — correct behaviour,
but it demonstrates nothing about halting. **Every acting case below therefore
passes `-var gate_mode=enforcing` explicitly.**

That default is deliberate (DECISIONS.md D-015): a gate that starts blocking the
moment it is deployed is how you lose your colleagues' goodwill in one
afternoon. But it does mean "I set it to halt and nothing halted" is the first
thing you will hit, and it is not a bug.

### The three cases worth demonstrating

```powershell
# 1. Explicitly allowed
terraform apply -var gate_decision=allow -var gate_mode=enforcing
aws lambda invoke --function-name ai-pre-traffic-gate-gate `
  --cli-binary-format raw-in-base64-out --payload '{}' /dev/stdout

# 2. Explicitly halted -- action_taken becomes halt_pipeline
terraform apply -var gate_decision=halt -var gate_mode=enforcing
aws lambda invoke --function-name ai-pre-traffic-gate-gate `
  --cli-binary-format raw-in-base64-out --payload '{}' /dev/stdout
```

Case 3 is the one that matters, and Terraform will not let you create it — the
`gate_decision` variable validates against `allow|halt`, so a typo cannot be
applied through the normal path. Break it at the function directly:

```powershell
# 3. Misconfigured -- the case that matters
aws lambda update-function-configuration `
  --function-name ai-pre-traffic-gate-gate `
  --environment "Variables={GATE_DECISION=yes,GATE_MODE=enforcing}"

aws lambda invoke --function-name ai-pre-traffic-gate-gate `
  --cli-binary-format raw-in-base64-out --payload '{}' /dev/stdout
```

Expected:

```json
{
  "decision": "halt",
  "decision_reason": "GATE_DECISION='yes' is not a recognised verdict; failing closed",
  "action_taken": "halt_pipeline"
}
```

`yes` is a plausible thing for someone to write, means "allow" to any human
reading it, and halts. Restore with `terraform apply`.

Restore afterwards with `terraform apply` — Terraform will detect the
out-of-band environment change and put it back.

### Shadow mode

```powershell
terraform apply -var gate_decision=halt -var gate_mode=shadow
aws lambda invoke --function-name ai-pre-traffic-gate-gate `
  --cli-binary-format raw-in-base64-out --payload '{}' /dev/stdout
```

Expect `"action_taken": "none"` alongside `"would_have_halted": true`. Same
verdict, no action — that pair is what Phase 4 counts to get an over-flagging
rate (DECISIONS.md D-015).

### Verifying the permission boundary

The claim is that the gate cannot deploy. Check it rather than asserting it:

```powershell
aws iam list-role-policies --role-name ai-pre-traffic-gate-gate
aws iam get-role-policy --role-name ai-pre-traffic-gate-gate --policy-name gate `
  --query 'PolicyDocument.Statement[].Action'
aws iam list-attached-role-policies --role-name ai-pre-traffic-gate-gate
```

Expect logs plus the two `codepipeline:PutJob*Result` actions, and an empty
attached-policy list. No `lambda:Update*`, no `codedeploy:*`, no `iam:PassRole`.

---

## Teardown

Increment 2 adds nothing with a standing hourly cost. CodeDeploy is free for
Lambda deployments; the gate Lambda bills per invocation; both log groups expire
at 7 days.

To return the alias to `v1` and drop the extra version:

```powershell
python scripts/deploy_canary.py --rollback
terraform apply -var demo_app_version=1.0.0
```

Terraform will not delete published versions — Lambda versions are immutable and
accumulate. To remove them explicitly:

```powershell
aws lambda delete-function --function-name ai-pre-traffic-gate-demo-app --qualifier 2
```

Full stack teardown: `terraform destroy` in `infra/personal`. The state bucket
and the budget live in `infra/bootstrap` and survive; destroy that separately
only when finishing with the project entirely.
