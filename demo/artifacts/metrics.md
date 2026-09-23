# Metrics -- every number, and where it came from

_Generated 2026-09-23 19:02 UTC by `scripts/capture_artifacts.py`._

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

- tests: **854** across 28 files
- architectural decisions: **84**
- recorded failures: **30**

_Source: `DECISIONS.md`, `FAILURES.md`, `pytest --collect-only`._

## Live figures

_Not generated: `--offline`. Re-run without it for latency, real-deploy
rates and spend._

