# Metrics -- every number, and where it came from

_Generated 2026-09-09 19:33 UTC by `scripts/capture_artifacts.py`._

## The two rates (fixture set, 23 scenarios, 3 repeats)

| model | under-flagging | over-flagging | acceptable |
| --- | --- | --- | --- |
| Haiku 4.5 | 27.3% (3/11) | 9.1% (1/11) | 77.3% (17/22) |
| Sonnet 4.5 | 27.3% (3/11) | 9.1% (1/11) | 81.8% (18/22) |
| Nova Pro | 18.2% (2/11) | 9.1% (1/11) | 86.4% (19/22) |
| **Sonnet 4.5 + floor** | 0.0% (0/11) | 9.1% (1/11) | 95.5% (21/22) |
| arithmetic baseline | run `python -m evals.run --baseline` | | |

_Source: `evals/results/*.json`._

## The build

- tests: **818** across 27 files
- architectural decisions: **82**
- recorded failures: **28**

_Source: `DECISIONS.md`, `FAILURES.md`, `pytest --collect-only`._

## Real deploys (not fixtures)

- verdicts from real pipeline executions: **23**
- actually halted: **4**
- risk distribution: high 11, low 4, medium 8

This is the number that decides whether the gate is deployable. A gate that
scores well on 23 fixtures and halts half your real deploys is not.

## Latency and size

- median verdict latency: **3587ms**
- slowest observed: 6824ms
- mean input tokens: 2,846

_Source: the `ai-pre-traffic-gate-verdicts` table, 23 real executions._

## Cost

Last 90 days, gross:

| service | spend |
| --- | --- |
| Claude Haiku 4.5 (Amazon Bedrock Edition) | $1.5923 |
| Amazon Simple Storage Service | $0.0153 |
| Amazon DynamoDB | $0.0000 |
| AWS Data Transfer | $-0.0000 |
| **total** | **$1.6076** |

Bedrock's Anthropic models bill as an AWS **Marketplace** purchase
(`USE1-MP:` usage types), which most AWS credits exclude -- so model spend
is real money while the rest of the stack is largely credited (D-081).

_Source: Cost Explorer, `GetCostAndUsage` grouped by SERVICE._

