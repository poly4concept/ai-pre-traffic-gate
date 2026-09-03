# Eval results — Phase 4b, first live runs

Raw output from `python -m evals.run --json`, kept in the repo on purpose. The
talk claims specific numbers about what the gate got wrong, and a number with no
artifact behind it is an anecdote.

Model: `us.anthropic.claude-haiku-4-5-20251001-v1:0`, us-east-1, temperature 0.
Baseline: `evals/stub.py`, the attribute-counting client — fifteen lines of
arithmetic that reads no prose.

## The headline

| | baseline | Haiku 4.5 |
| --- | --- | --- |
| acceptable verdict | **85.7%** (18/21) | 81.0% (17/21) |
| under-flagging | **20.0%** (2/10) | 30.0% (3/10) |
| over-flagging | 9.1% (1/11) | 9.1% (1/11) |
| stable across 3 repeats | 100% | 100% |
| cost per verdict | $0 | ~2,530 in / ~240 out tokens |

**The model loses to the arithmetic on this fixture set.** Not by much, and not
on the number that decides whether a gate is worth having — it loses on exactly
that number. All three of its extra under-flags are security-signal scenarios.

What the arithmetic cannot do is explain itself. Its reasoning field reads
`Attribute score 8. 1057 lines changed; touches a sensitive path`. Haiku's reads
like a colleague's review, and scored 100% concern coverage on
`friday_deploy_into_active_alarm` against the baseline's 25%. Since every
escalation puts that text in front of a human, the comparison is not as
one-sided as the table makes it look — but the table is the table.

## The four runs

| file | prompt | what changed |
| --- | --- | --- |
| `pass1` | 2026-08-20.1 | as Phase 4a left it. **22.7% of attempts failed closed** on a schema violation |
| `pass2` | 2026-09-02.1 | XML leak fixed. Fail-closed drops to 4.5%, all of it intended |
| `pass3` | 2026-09-02.2 | security framing corrected, ~90 words |
| `pass4` | 2026-09-02.3 | same correction, ~40 words. **Shipped** |

`pass1` is kept because it is the evidence for F-017 and F-018, not because its
numbers mean anything — a fifth of its attempts contained no assessment at all,
and three of those were scored as correct answers.

## The finding worth the slide

Three prompt versions. Identical headline rates every time. Different scenarios
failing each time.

| scenario | .1 | .2 | .3 |
| --- | --- | --- | --- |
| `critical_cve_with_patch` | low | **medium** | low |
| `huge_refactor_healthy_target` | medium | **low** | medium |
| under-flagging | 30% | 30% | 30% |

Editing the **security** paragraph moved the verdict on a scenario that has no
security findings, and the wording that fixed one scenario broke the other. The
rate did not move because the two cancelled.

Two things follow, and both are load-bearing:

1. **A prompt is not modular.** There is no such thing as editing the part about
   security. Anyone who has refactored a global variable has met this before;
   it is stranger here only because the prompt reads like documentation.
2. **21 scenarios cannot resolve a 5% difference.** One scenario is 4.8%. Two
   more cycles of edit-and-remeasure and the prompt would be fitted to these
   fixtures rather than to the problem. Stopped at three deliberately.

## What is still wrong, and is not a wording problem

**All three persistent under-flags are the security signal.**
`critical_cve_no_patch` and `untriaged_severity_findings` come back `low`, stably,
with the model arguing — coherently — that a pre-existing finding is not this
deploy's fault. That is a weighting disagreement, not a misunderstanding, and
three attempts at rewording moved it once out of three tries. It needs a
different model or a deterministic pre-check ahead of the model, which is a
Phase 5 decision.

**`revert_of_a_bad_deploy` is the one over-flag, for both the model and the
baseline.** Every signal says halt: 1057 lines, payments paths, active alarms,
0.6 hours since the last deploy. Halting is wrong, because the change is the
fix. Neither reads intent from the commit message. This is the scenario the
whole fixture set exists to contain, and nothing has beaten it yet.

## Reproducing

```powershell
python -m evals.run --baseline                 # free, instant
python -m evals.run --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 --repeats 3
```

Comparing another model needs admin credentials: the read-only `ai-agent`
identity cannot perform `aws-marketplace:ViewSubscriptions`, so it gets
`AccessDeniedException` whether or not the model is subscribed -- the error does
not distinguish the two.

```powershell
$env:AWS_PROFILE = 'poly4'
python -m evals.run --model-id us.anthropic.claude-sonnet-4-5-20250929-v1:0 --repeats 3
```

Sonnet 5 (`us.anthropic.claude-sonnet-5`) returns *"not available for this
account"* — a subscription that has not been granted, which is a different
problem from the permissions one above and needs a console model-access request.
Worth knowing the two errors apart: this project already lost five days to
reading one Bedrock gate's error as evidence about a later one (F-014).
