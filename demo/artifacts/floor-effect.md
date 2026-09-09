# The deterministic security floor

_Generated 2026-09-09 16:52 UTC by `scripts/capture_artifacts.py`._

Two rules, both countable, both security:

```
any critical finding        -> at least MEDIUM
any unknown-severity find   -> at least MEDIUM
```

The model may go higher. It may never go lower.

| | before | with floor |
| --- | --- | --- |
| **under-flagging** | 27.3% (3/11) | **0.0% (0/11)** |
| over-flagging | 9.1% (1/11) | 9.1% (1/11) |
| acceptable | 81.8% (18/22) | 95.5% (21/22) |

## What moved

| scenario | before | after |
| --- | --- | --- |
| `critical_cve_no_patch` | low **!** | medium |
| `critical_cve_with_patch` | low **!** | medium |
| `untriaged_severity_findings` | low **!** | medium |

Exactly 3 scenarios changed, and the over-flagging rate did not move by one. The gain came from precision, not from becoming indiscriminately cautious.

## What the floor cannot fix

- `revert_of_a_bad_deploy` -- got `high`, wanted one of ['low', 'medium']

No countable rule reaches these. `revert_of_a_bad_deploy` needs reading the commit message and understanding that the change IS the fix -- every signal says halt and halting is wrong. Haiku, Sonnet and Nova all miss it.

**Source:** `evals/results/sonnet-4-5.json`, `evals/results/sonnet-4-5-with-floor.json`
