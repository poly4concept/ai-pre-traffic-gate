"""Run the scenario set through a verdict client and score the result. Phase 4a.

WHAT THIS IS FOR

Two numbers, mainly:

    over-flagging rate   how often the gate blocks something fine
    under-flagging rate  how often it waves something dangerous through

Everything else here exists to make those two trustworthy. They are reported
separately and never averaged, because a single "accuracy" figure hides which
one is happening and they need opposite fixes -- and because a gate at 85%
accuracy is either nearly deployable or completely useless depending entirely on
which failures make up the 15%.

WHY THE CLIENT IS INJECTED

`run_eval` takes anything with `get_verdict(bundle) -> VerdictOutcome`. That is
the real `BedrockVerdictClient`, or a stub, or a recorded-response replayer. The
harness never constructs a client and never imports boto3, so the whole scoring
path is testable offline -- which matters more than usual here, because a
benchmark whose own arithmetic is unverified is worse than no benchmark. It
produces a number people believe.

WHY REPEATS EXIST

Temperature 0 reduces variance; it does not remove it. Running each scenario
more than once measures what is left. `stability` is the fraction of scenarios
that returned the same risk level every time -- and it belongs on a slide next
to the accuracy figure, because an 80% accurate gate that answers differently
on identical input is not 80% accurate, it is a coin flip with a bias.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from signals.scenarios import SCENARIOS, bundle_for, scenario_names
from verdict import RISK_ORDER, RiskLevel, VerdictSource

from .labels import BY_SCENARIO, PAIRS, Kind, Label

logger = logging.getLogger(__name__)

# Which kinds contribute to which rate. Split as data rather than as a chain of
# `if`s so that adding a kind forces a decision about where it counts, instead
# of silently falling into neither bucket and quietly shrinking a denominator.
OVER_FLAG_KINDS = frozenset({Kind.BENIGN, Kind.INVERTED, Kind.ABSENT_SIGNAL})
UNDER_FLAG_KINDS = frozenset({Kind.RISKY, Kind.ADVERSARIAL, Kind.FAIL_CLOSED})


@dataclass(frozen=True, slots=True)
class Attempt:
    """One scenario, run once."""

    scenario: str
    risk_level: RiskLevel
    source: VerdictSource
    reasoning: str
    concerns: tuple[str, ...]
    attempts: int
    failure_kind: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None

    @property
    def reached_the_model(self) -> bool:
        """Did an inference actually happen?

        `attempts == 0` is the signal the verdict client emits when it refuses to
        call Bedrock at all -- currently only when required signals are missing.
        """
        return self.attempts > 0

    @property
    def searchable_text(self) -> str:
        return " ".join((self.reasoning, *self.concerns)).lower()


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    label: Label
    attempts: tuple[Attempt, ...]

    @property
    def scenario(self) -> str:
        return self.label.scenario

    @property
    def levels(self) -> tuple[RiskLevel, ...]:
        return tuple(a.risk_level for a in self.attempts)

    @property
    def modal_level(self) -> RiskLevel:
        """The most common answer, ties broken toward the stricter one.

        Strict tie-breaking is deliberate: when a gate is genuinely undecided
        between medium and high, reporting the cautious reading is the honest
        summary of its behaviour, and it stops a 50/50 split from being scored
        as a clean pass.
        """
        counts = Counter(self.levels)
        top = max(counts.values())
        tied = [level for level, n in counts.items() if n == top]
        return max(tied, key=lambda level: RISK_ORDER[level])

    @property
    def is_stable(self) -> bool:
        return len(set(self.levels)) == 1

    @property
    def passed(self) -> bool:
        """Every attempt must be acceptable, not just the modal one.

        A scenario that comes back acceptable four times out of five is not a
        pass -- in production that fifth run is a wrong decision about a real
        deploy, and averaging it away is how a benchmark starts flattering the
        thing it measures.
        """
        return all(self.label.verdict_is_acceptable(a.risk_level) for a in self.attempts)

    @property
    def exactly_ideal(self) -> bool:
        if self.label.ideal is None:
            return False
        return all(a.risk_level is self.label.ideal for a in self.attempts)

    @property
    def direction(self) -> str | None:
        """Failure direction of the modal answer, or None if it was acceptable."""
        return self.label.direction(self.modal_level)

    @property
    def concern_coverage(self) -> float | None:
        """Fraction of expected concern keywords the reasoning touched.

        None when the label lists no keywords. Reported, never scored -- keyword
        matching on model prose is far too brittle to gate a build on, but it is
        the only cheap signal about WHY a verdict was reached, and a correct
        level for the wrong reason will not survive a change of fixture.
        """
        wanted = self.label.expected_concerns
        if not wanted:
            return None
        text = " ".join(a.searchable_text for a in self.attempts)
        return sum(1 for word in wanted if word.lower() in text) / len(wanted)

    @property
    def fail_closed_attempts(self) -> int:
        return sum(1 for a in self.attempts if a.source is VerdictSource.FAIL_CLOSED)

    @property
    def is_measured(self) -> bool:
        """Did the gate actually form an opinion about this scenario?

        THE FLATTERY THIS CLOSES, found on the first live run in Phase 4b.

        Four scenarios failed schema validation and fell back to a fail-closed
        HIGH. Three of them carried labels where `high` was acceptable, so they
        were scored as passes -- the gate got credit for judgement it had
        explicitly declined to exercise. The eval reported 85.7% acceptable
        while a fifth of its attempts contained no assessment at all.

        A fail-closed verdict is a refusal to answer, and a refusal is neither
        right nor wrong about risk. Excluding it from the judgement rates is not
        letting the gate off: the refusals are counted and named separately, and
        an operator cares about them at least as much. What they are not is
        evidence about the model's calibration.

        The exception is a scenario whose LABEL is `fail_closed` -- there,
        refusing is the behaviour under test, so it is measured normally.
        """
        if self.label.kind is Kind.FAIL_CLOSED:
            return True
        return self.fail_closed_attempts == 0

    @property
    def model_call_violation(self) -> bool:
        """Set when a scenario that must not reach the model did."""
        if not self.label.expect_no_model_call:
            return False
        return any(a.reached_the_model for a in self.attempts)


@dataclass
class PairViolation:
    lax: str
    strict: str
    lax_level: RiskLevel
    strict_level: RiskLevel
    why: str


@dataclass
class EvalRun:
    results: tuple[ScenarioResult, ...]
    repeats: int
    model_id: str
    pair_violations: list[PairViolation] = field(default_factory=list)

    # --- the two numbers that matter -------------------------------------

    def _rate(self, kinds: frozenset[Kind], direction: str) -> tuple[int, int]:
        """(failures, denominator) for one failure direction.

        Unmeasured scenarios are excluded from BOTH the numerator and the
        denominator. See `ScenarioResult.is_measured`: these are attempts where
        the gate refused to answer, and a refusal is not a calibration error in
        either direction. Counting them would make the judgement rates move with
        Bedrock's availability.
        """
        pool = [
            r for r in self.results if r.label.kind in kinds and r.label.scored and r.is_measured
        ]
        bad = [r for r in pool if r.direction == direction]
        return len(bad), len(pool)

    @property
    def over_flagging(self) -> tuple[int, int]:
        """Benign changes the gate treated as riskier than defensible.

        The number that decides whether the gate survives contact with
        colleagues. A gate switched off in week three prevents nothing.
        """
        return self._rate(OVER_FLAG_KINDS, "over")

    @property
    def under_flagging(self) -> tuple[int, int]:
        """Risky changes the gate waved through. The number that decides whether
        the gate is worth having at all."""
        return self._rate(UNDER_FLAG_KINDS, "under")

    # --- supporting figures ----------------------------------------------

    @property
    def scored_results(self) -> tuple[ScenarioResult, ...]:
        """Scenarios with a label worth scoring, whether or not the gate
        answered. `measured_results` is the subset it actually answered."""
        return tuple(r for r in self.results if r.label.scored)

    @property
    def measured_results(self) -> tuple[ScenarioResult, ...]:
        return tuple(r for r in self.scored_results if r.is_measured)

    @property
    def unmeasured(self) -> tuple[ScenarioResult, ...]:
        """Scored scenarios where the gate refused rather than assessed.

        Reported by name, not just counted: which scenarios went unmeasured
        decides whether the rest of the numbers mean anything. Four refusals
        spread across the benign fixtures and four concentrated on the risky
        ones are the same figure describing opposite situations.
        """
        return tuple(r for r in self.scored_results if not r.is_measured)

    @property
    def passes(self) -> tuple[int, int]:
        measured = self.measured_results
        return sum(1 for r in measured if r.passed), len(measured)

    @property
    def exact(self) -> tuple[int, int]:
        measured = [r for r in self.measured_results if r.label.ideal is not None]
        return sum(1 for r in measured if r.exactly_ideal), len(measured)

    @property
    def stability(self) -> tuple[int, int]:
        return sum(1 for r in self.results if r.is_stable), len(self.results)

    @property
    def failures(self) -> tuple[ScenarioResult, ...]:
        """Wrong judgements. A refusal is not a wrong judgement -- it appears
        under `unmeasured` instead, where its cause can be read."""
        return tuple(r for r in self.measured_results if not r.passed)

    @property
    def model_call_violations(self) -> tuple[ScenarioResult, ...]:
        return tuple(r for r in self.results if r.model_call_violation)

    @property
    def tokens(self) -> tuple[int, int]:
        """(input, output) totals. Cost per verdict needs the model's published
        rate, which is deliberately not hardcoded here -- Bedrock pricing moves,
        and a stale constant in a benchmark is worse than an absent one."""
        ins = sum(a.input_tokens or 0 for r in self.results for a in r.attempts)
        outs = sum(a.output_tokens or 0 for r in self.results for a in r.attempts)
        return ins, outs

    @property
    def fail_closed_count(self) -> int:
        """Attempts that produced a fail-closed verdict rather than a model one.

        A run with a high count here is measuring the gate's error handling, not
        its judgement, and its accuracy figure means nothing. Worth checking
        before believing any other number on the report.
        """
        return sum(
            1 for r in self.results for a in r.attempts if a.source is VerdictSource.FAIL_CLOSED
        )


def _to_attempt(scenario: str, outcome: Any) -> Attempt:
    verdict = outcome.verdict
    call = outcome.call
    return Attempt(
        scenario=scenario,
        risk_level=verdict.risk_level,
        source=verdict.source,
        reasoning=verdict.reasoning,
        concerns=verdict.primary_concerns,
        attempts=call.attempts,
        failure_kind=call.failure_kind,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        latency_ms=call.latency_ms,
    )


def run_eval(
    client: Any,
    *,
    scenarios: tuple[str, ...] | None = None,
    repeats: int = 1,
    model_id: str = "unknown",
) -> EvalRun:
    """Run every labelled scenario through `client` and score the result.

    Unlabelled scenarios are skipped with a warning rather than scored against a
    default. A fixture added without a label is an unfinished thought, and
    silently giving it a pass would let the set grow while the measurement
    quietly narrowed.
    """
    names = scenarios or scenario_names()
    results: list[ScenarioResult] = []

    for name in names:
        if name not in SCENARIOS:
            raise KeyError(f"unknown scenario {name!r}")
        label = BY_SCENARIO.get(name)
        if label is None:
            logger.warning("scenario %r has no label; skipping", name)
            continue

        bundle = bundle_for(name)
        attempts = tuple(_to_attempt(name, client.get_verdict(bundle)) for _ in range(repeats))
        results.append(ScenarioResult(label=label, attempts=attempts))

    run = EvalRun(results=tuple(results), repeats=repeats, model_id=model_id)
    run.pair_violations = _check_pairs(run)
    return run


def _check_pairs(run: EvalRun) -> list[PairViolation]:
    """Ordering assertions, checked on modal verdicts.

    Robust in a way absolute labels are not: a model uniformly one notch more
    cautious than the labeller fails many absolute checks while remaining
    perfectly self-consistent. Ordering survives that, so a violation here is a
    reasoning error rather than a difference of taste.
    """
    levels = {r.scenario: r.modal_level for r in run.results}
    violations: list[PairViolation] = []

    for lax, strict, why in PAIRS:
        if lax not in levels or strict not in levels:
            # A partial run (--scenario) legitimately omits one side.
            continue
        if RISK_ORDER[levels[lax]] > RISK_ORDER[levels[strict]]:
            violations.append(
                PairViolation(
                    lax=lax,
                    strict=strict,
                    lax_level=levels[lax],
                    strict_level=levels[strict],
                    why=why,
                )
            )
    return violations
