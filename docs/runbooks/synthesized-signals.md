# Runbook — manufacturing real signals on demand

Phase 2.5. Every recipe here has a teardown, and the teardown is tested by using
it, not by reading it.

**The distinction this runbook exists to hold onto.** A *mocked* signal is
hardcoded JSON returned by a collector — cheap, instant, and blind to the shape
of real AWS responses. A *synthesized-real* signal is a condition we engineer in
the account so a real AWS service genuinely emits a real finding, which the real
collector then parses. Everything below is the second kind. That is the whole
point: mocked signals alone would hide parsing bugs and produce a demo that
could not be honestly defended.

---

## Quick reference

| Want | Command |
| --- | --- |
| break the app | `python scripts/inject_fault.py errors --rate 0.5` |
| slow the app | `python scripts/inject_fault.py slow --ms 3000` |
| break only the canary | `python scripts/inject_fault.py errors --rate 1.0 --version N` |
| **undo all faults** | `python scripts/inject_fault.py off` |
| check fault state | `python scripts/inject_fault.py status` |
| list commit recipes | `python scripts/craft_commit.py list` |
| make a risky commit | `python scripts/craft_commit.py create payments-friday` |
| **undo all commits** | `python scripts/craft_commit.py cleanup` |

**If you remember two commands, remember the two teardowns:**

```powershell
python scripts/inject_fault.py off
python scripts/craft_commit.py cleanup
```

---

## 2.5a — Error rate, latency, memory

### Recipe

```powershell
# half of all requests raise
python scripts/inject_fault.py errors --rate 0.5

# every request takes 3 extra seconds
python scripts/inject_fault.py slow --ms 3000

# hold 256MB per container
python scripts/inject_fault.py memory --mb 256

# combine them
python scripts/inject_fault.py errors --rate 0.3 --ms 1500
```

Settings live in one DynamoDB row and are read at request time, so they take
effect within the cache TTL (5s) with no redeploy.

### The trap

The Lambda's `FAULT_TABLE` setting reaches the alias only through a **published
version**. If `inject_fault.py status` reports the table but nothing changes in
behaviour, the alias is serving a version published before the setting existed —
**run the pipeline once**.

### Teardown

```powershell
python scripts/inject_fault.py off
python scripts/inject_fault.py status   # expect: HEALTHY  no faults configured
```

Leaves the DynamoDB row in place with everything zeroed. Nothing to delete, no
standing cost.

---

## 2.5b — Alarm state

Three alarms watch the demo app. They exist so `has_active_alarm` can ever be
true — without them the gate reads metrics only, and "no alarms firing" is
indistinguishable from "nobody is watching" (D-027).

| Alarm | Fires when | Tuned against |
| --- | --- | --- |
| `…-error-rate` | error rate > 5% over 60s | `errors --rate 0.5` clears it 10x |
| `…-p99-latency` | p99 > 2000ms over 60s | `slow --ms 3000` clears it easily |
| `…-throttles` | any throttle in 60s | reserved-concurrency squeeze |

All three use `treat_missing_data = "missing"`, so an idle function sits in
`INSUFFICIENT_DATA` rather than `OK`. **That is deliberate** — `notBreaching`
would report a service nobody has invoked as healthy, which is the absent-vs-zero
trap in CloudWatch's own vocabulary.

### Recipe — make an alarm fire

```powershell
python scripts/inject_fault.py errors --rate 0.5

# Drive traffic. Alarms need datapoints -- an idle function stays
# INSUFFICIENT_DATA forever, which is correct and useless for a demo.
#
# NOT curl. The function URL is AWS_IAM authorised, so an unsigned request is
# rejected at the edge with 403 and the Lambda is never invoked at all -- no
# Invocations metric, no Errors metric, no alarm, and an hour of confusion.
# `aws lambda invoke` signs the request for you.
1..60 | ForEach-Object {
  aws lambda invoke --function-name ai-pre-traffic-gate-demo-app:live `
    --cli-binary-format raw-in-base64-out --payload '{}' $env:TEMP\out.json | Out-Null
  Start-Sleep -Milliseconds 500
}

# watch it flip, usually within 2-3 minutes
aws cloudwatch describe-alarms `
  --alarm-names ai-pre-traffic-gate-demo-app-error-rate `
  --query "MetricAlarms[0].{state:StateValue,reason:StateReason}"
```

Then confirm the gate can see it:

```powershell
aws lambda invoke --function-name ai-pre-traffic-gate-gate `
  --cli-binary-format raw-in-base64-out --payload '{}' response.json
Get-Content response.json
```

### Teardown

```powershell
python scripts/inject_fault.py off
```

Alarms return to `OK` (or `INSUFFICIENT_DATA` once traffic stops) on their own.
The alarms themselves stay — they cost ≈$0.40/month and are part of the stack.
To remove them entirely, `terraform destroy -target=aws_cloudwatch_metric_alarm.demo_app_error_rate`
and siblings, but there is rarely a reason.

---

## 2.5c — Security findings

**Not built yet.** Requires enabling Amazon Inspector (≈$0.31/month per function)
and a branch pinning dependencies with published CVEs. Deferred until end-to-end
testing.

Until then `SECURITY_SCANNING=false` yields a `SKIPPED` signal — *"we chose not
to look"* — which is honest and distinguishable from a failure.

---

## 2.5d — Change context

Manufactured commits. Synthetic in content, **real in form**: real git objects,
real SHAs, real committer timestamps, real diff statistics computed by the real
build script, and a real SHA cross-check between CodePipeline and the build.

### Recipes

```powershell
python scripts/craft_commit.py list
```

| Recipe | Produces | Expect |
| --- | --- | --- |
| `safe-bump` | 1 line, Tuesday 10:42 | **low** — the over-flagging canary |
| `docs-only` | 200 lines of markdown | **low** — tests size-vs-risk |
| `payments-friday` | 620 lines, `payments/` + `auth/`, Friday 19:42 | medium/high |
| `huge-refactor` | 40 files, 1800 lines, Tuesday | medium |
| `revert` | revert-shaped, Saturday 23:42 | low/medium — the inversion case |
| `injection` | prompt injection in the message | medium/high, **never low** |

```powershell
# see what it would do, change nothing
python scripts/craft_commit.py create payments-friday --dry-run

# commit locally (not pushed)
python scripts/craft_commit.py create payments-friday

# commit and start the pipeline
python scripts/craft_commit.py create payments-friday --push
```

### Safety rules

1. Only ever writes under `demo/synthetic/`.
2. Never rewrites history — no amend, no rebase, no force-push. Even the
   teardown is an ordinary commit.
3. Pushing is opt-in, because a local commit is undone with `git reset` and a
   pushed one has already started a pipeline.
4. Refuses to run on a dirty tree, so real work in progress cannot be swept into
   a commit labelled synthetic.

### Teardown

```powershell
# not pushed yet
git reset --hard HEAD~1

# already pushed
python scripts/craft_commit.py cleanup --push
```

---

## The full stage demo

The two mechanisms combine into the scenario worth showing:

```powershell
# 1. arm alarm-driven rollback (default is off)
terraform -chdir=infra/personal apply -var canary_rollback_on_alarm=true

# 2. manufacture a change the gate should be uneasy about
python scripts/craft_commit.py create payments-friday --push

# 3. as the canary goes out, break ONLY the new version
python scripts/inject_fault.py errors --rate 1.0 --version <new>

# 4. drive traffic; alarms fire; CodeDeploy reverses the traffic shift live
```

Two independent things are on display: the **gate** judging the change before it
ships, and **CodeDeploy** catching what the gate let through. Worth saying out
loud that the second is not AI at all — it is the boring safety net from Phase 1,
and the gate is a layer on top of it rather than a replacement for it.

### Full teardown

```powershell
python scripts/inject_fault.py off
python scripts/craft_commit.py cleanup --push
terraform -chdir=infra/personal apply -var canary_rollback_on_alarm=false
```

Account returns to near-zero standing cost: three CloudWatch alarms (≈$0.40/mo),
two on-demand DynamoDB tables (≈$0), and the Lambdas.
