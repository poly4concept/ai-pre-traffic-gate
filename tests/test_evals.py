"""Tests for the eval harness itself. Phase 4a.

These matter more than they look. Everything downstream -- the model choice, the
prompt iteration, the number that ends up on a conference slide -- rests on this
arithmetic being right, and a benchmark that miscounts is worse than no
benchmark, because it produces a figure people believe.

Four clusters:

  * label integrity -- every scenario labelled, every label coherent
  * the weighting of the set, which decides what the over-flagging rate means
  * the scoring rules, especially the ones that could silently flatter a model
  * the baseline's known failures, locked in as a regression test on the claim
    the talk makes about them
"""

from __future__ import annotations

import pytest
from signals.scenarios import SCENARIOS, bundle_for, scenario_names
from verdict import ModelCall, RiskLevel, Verdict, VerdictOutcome, VerdictSource

from evals.harness import (
    OVER_FLAG_KINDS,
    UNDER_FLAG_KINDS,
    Attempt,
    EvalRun,
    ScenarioResult,
    run_eval,
)
from evals.labels import BY_SCENARIO, LABELS, PAIRS, Kind, Label
from evals.report import format_run
from evals.stub import AttributeCountingClient

LOW, MEDIUM, HIGH = RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH


class ScriptedClient:
    """Returns a prepared risk level per scenario, cycling through a list.

    Cycling is what lets a single client drive the repeat-sensitive rules --
    give it ["low", "high"] with repeats=2 and it produces an unstable scenario
    on demand.
    """

    def __init__(self, levels_by_scenario, reasoning="because"):
        self._levels = {k: list(v) for k, v in levels_by_scenario.items()}
        self._calls = {}
        self._reasoning = reasoning
        self.seen = []

    def get_verdict(self, bundle):
        # The scenario is identified by its commit SHA, which is unique per
        # fixture -- the bundle does not carry its own name.
        name = self._name_for(bundle)
        self.seen.append(name)
        levels = self._levels.get(name, [LOW])
        index = self._calls.get(name, 0)
        self._calls[name] = index + 1
        level = levels[index % len(levels)]

        return VerdictOutcome(
            verdict=Verdict(
                risk_level=level,
                reasoning=self._reasoning,
                source=VerdictSource.MODEL,
                confidence=0.5,
            ),
            call=ModelCall(model_id="scripted", prompt_version="test", attempts=1, succeeded=True),
            raw_model_output={"risk_level": str(level)},
        )

    @staticmethod
    def _name_for(bundle):
        change = bundle.change.data
        if change is None:
            return "no_change_context"
        for name, scenario in SCENARIOS.items():
            fixture = scenario.get("change")
            if fixture is not None and fixture.commit_sha == change.commit_sha:
                return name
        return "unknown"


def attempt(level, *, attempts=1, reasoning="r", concerns=()):
    return Attempt(
        scenario="x",
        risk_level=level,
        source=VerdictSource.MODEL,
        reasoning=reasoning,
        concerns=tuple(concerns),
        attempts=attempts,
        failure_kind=None,
        input_tokens=None,
        output_tokens=None,
        latency_ms=None,
    )


def label(**kw):
    base = {
        "scenario": "x",
        "kind": Kind.BENIGN,
        "acceptable": frozenset({LOW}),
        "rationale": "test",
    }
    return Label(**{**base, **kw})


# --- Label integrity ------------------------------------------------------


def test_every_scenario_has_a_label():
    """An unlabelled fixture is an unfinished thought.

    Without this, the set can grow while the measurement quietly narrows -- new
    scenarios get skipped and nobody notices the denominator shrinking.
    """
    unlabelled = sorted(set(scenario_names()) - set(BY_SCENARIO))

    assert not unlabelled, f"scenarios with no label: {unlabelled}"


def test_every_label_points_at_a_real_scenario():
    """Catches a rename on one side of the fixture/label split."""
    orphans = sorted(set(BY_SCENARIO) - set(SCENARIOS))

    assert not orphans, f"labels for non-existent scenarios: {orphans}"


def test_no_duplicate_labels():
    names = [lab.scenario for lab in LABELS]

    assert len(names) == len(set(names))


@pytest.mark.parametrize("lab", LABELS, ids=lambda lab: lab.scenario)
def test_every_label_is_coherent(lab):
    assert lab.acceptable, f"{lab.scenario} accepts nothing, so it can never pass"
    if lab.ideal is not None:
        assert lab.ideal in lab.acceptable, (
            f"{lab.scenario}: ideal {lab.ideal} is not in its own acceptable set"
        )
    assert len(lab.rationale) > 80, f"{lab.scenario}: rationale is too thin to review"


def test_only_contested_labels_lack_an_ideal():
    for lab in LABELS:
        if lab.kind is Kind.CONTESTED:
            assert lab.ideal is None, f"{lab.scenario} is contested but names an ideal"
        else:
            assert lab.ideal is not None, f"{lab.scenario} has no ideal and is not contested"


def test_every_kind_counts_toward_exactly_one_rate():
    """A kind in neither bucket silently shrinks a denominator.

    Contested is the one deliberate exception -- it is excluded from scoring
    entirely rather than counted in a direction.
    """
    for kind in Kind:
        if kind is Kind.CONTESTED:
            continue
        in_over = kind in OVER_FLAG_KINDS
        in_under = kind in UNDER_FLAG_KINDS
        assert in_over != in_under, f"{kind} belongs to neither or both rates"


def test_pairs_reference_real_labelled_scenarios():
    for lax, strict, why in PAIRS:
        assert lax in BY_SCENARIO, lax
        assert strict in BY_SCENARIO, strict
        assert len(why) > 40, f"{lax}/{strict}: unexplained ordering assertion"


# --- The weighting of the set --------------------------------------------


def test_the_set_is_weighted_toward_benign_changes():
    """The reason this matters is the whole point of Phase 4.

    A real pipeline is mostly boring commits. An eval set full of risky
    scenarios rewards a gate that flags everything, and measures the
    over-flagging rate -- the number that decides whether the gate survives
    contact with colleagues -- against almost nothing.
    """
    over_pool = [lab for lab in LABELS if lab.kind in OVER_FLAG_KINDS]
    under_pool = [lab for lab in LABELS if lab.kind in UNDER_FLAG_KINDS]

    assert len(over_pool) >= 10, "too few benign scenarios to measure over-flagging"
    assert len(under_pool) >= 8, "too few risky scenarios to measure under-flagging"


def test_at_least_one_scenario_inverts_the_obvious_reading():
    """Without one, the eval cannot distinguish reading from attribute-scoring."""
    assert any(lab.kind is Kind.INVERTED for lab in LABELS)


def test_the_injection_scenario_cannot_be_passed_by_obeying_it():
    """`low` must be wrong on the merits, not only because it obeyed.

    If the injected change were otherwise benign, a `low` verdict would be
    correct-by-accident and the scenario would measure nothing.
    """
    lab = BY_SCENARIO["prompt_injection_in_commit_message"]

    assert LOW not in lab.acceptable
    change = SCENARIOS["prompt_injection_in_commit_message"]["change"]
    assert change.total_lines_changed > 100
    assert change.is_off_hours
    assert any("payments/" in p or "auth/" in p for p in change.paths)


# --- Scoring rules --------------------------------------------------------


@pytest.mark.parametrize(
    ("acceptable", "got", "expected"),
    [
        (frozenset({LOW}), LOW, None),
        (frozenset({LOW}), MEDIUM, "over"),
        (frozenset({LOW}), HIGH, "over"),
        (frozenset({HIGH}), LOW, "under"),
        (frozenset({MEDIUM, HIGH}), LOW, "under"),
        (frozenset({MEDIUM, HIGH}), HIGH, None),
        (frozenset({LOW, MEDIUM}), HIGH, "over"),
    ],
)
def test_failure_direction_is_measured_against_the_extremes(acceptable, got, expected):
    """Against the edges of `acceptable`, never against `ideal`.

    A scenario where both medium and high are defensible must not report a
    failure for choosing the one that is not the ideal.
    """
    assert label(acceptable=acceptable).direction(got) is expected


def test_a_scenario_passes_only_if_every_repeat_was_acceptable():
    """Four out of five is not a pass.

    In production that fifth run is a wrong decision about a real deploy.
    Scoring the modal answer would average it away, which is how a benchmark
    starts flattering the thing it measures.
    """
    result = ScenarioResult(
        label=label(acceptable=frozenset({LOW})),
        attempts=(attempt(LOW), attempt(LOW), attempt(LOW), attempt(LOW), attempt(HIGH)),
    )

    assert not result.passed
    assert not result.is_stable


def test_modal_level_breaks_ties_toward_the_stricter_answer():
    """A 50/50 split must not be reported as the lenient half."""
    result = ScenarioResult(
        label=label(acceptable=frozenset({LOW, HIGH})),
        attempts=(attempt(LOW), attempt(HIGH)),
    )

    assert result.modal_level is HIGH


def test_modal_level_is_the_most_common_not_the_strictest():
    result = ScenarioResult(
        label=label(),
        attempts=(attempt(LOW), attempt(LOW), attempt(HIGH)),
    )

    assert result.modal_level is LOW


def test_exactly_ideal_requires_every_repeat_to_match():
    lab = label(acceptable=frozenset({LOW, MEDIUM}), ideal=LOW)

    assert ScenarioResult(label=lab, attempts=(attempt(LOW), attempt(LOW))).exactly_ideal
    assert not ScenarioResult(label=lab, attempts=(attempt(LOW), attempt(MEDIUM))).exactly_ideal


def test_concern_coverage_is_a_fraction_of_the_expected_keywords():
    lab = label(expected_concerns=("alarm", "payments", "friday"))
    result = ScenarioResult(
        label=lab,
        attempts=(attempt(LOW, reasoning="an ALARM is firing", concerns=("payments/",)),),
    )

    assert result.concern_coverage == pytest.approx(2 / 3)


def test_concern_coverage_is_none_rather_than_zero_when_nothing_is_expected():
    """The project's own invariant, applied to its report.

    A label with no keywords has not scored 0% -- there was nothing to measure,
    and averaging a fabricated zero into a coverage figure is the same mistake
    the signals package exists to prevent.
    """
    assert ScenarioResult(label=label(), attempts=(attempt(LOW),)).concern_coverage is None


def test_a_scenario_that_must_not_reach_the_model_reports_it_if_it_did():
    lab = label(kind=Kind.FAIL_CLOSED, acceptable=frozenset({HIGH}), expect_no_model_call=True)

    called = ScenarioResult(label=lab, attempts=(attempt(HIGH, attempts=1),))
    refused = ScenarioResult(label=lab, attempts=(attempt(HIGH, attempts=0),))

    assert called.model_call_violation
    assert not refused.model_call_violation


# --- Run-level arithmetic -------------------------------------------------


def test_rates_use_the_right_denominators():
    """Over-flagging is measured on benign scenarios only, and vice versa.

    Mixing the pools produces a number that moves when the SET changes rather
    than when the MODEL does, which makes it useless for tracking prompt drift.
    """
    run = run_eval(ScriptedClient({}), model_id="test")

    _, over_d = run.over_flagging
    _, under_d = run.under_flagging
    over_expected = sum(1 for lab in LABELS if lab.kind in OVER_FLAG_KINDS and lab.scored)
    under_expected = sum(1 for lab in LABELS if lab.kind in UNDER_FLAG_KINDS and lab.scored)

    assert over_d == over_expected
    assert under_d == under_expected


def test_a_gate_that_says_low_to_everything_under_flags_completely():
    """The useless-gate end of the scale, asserted so the metric has a floor."""
    run = run_eval(ScriptedClient({name: [LOW] for name in scenario_names()}), model_id="test")

    under_n, under_d = run.under_flagging
    over_n, _ = run.over_flagging

    assert under_n == under_d, "every risky scenario should be under-flagged"
    assert over_n == 0


def test_a_gate_that_says_high_to_everything_over_flags_completely():
    """The unusable-gate end. Note it scores 100% on under-flagging -- which is
    exactly why the two rates are never averaged into one figure."""
    run = run_eval(ScriptedClient({name: [HIGH] for name in scenario_names()}), model_id="test")

    over_n, over_d = run.over_flagging
    under_n, _ = run.under_flagging

    assert over_n == over_d
    assert under_n == 0


def test_contested_scenarios_are_recorded_but_not_scored():
    run = run_eval(ScriptedClient({}), model_id="test")

    contested = [r for r in run.results if r.label.kind is Kind.CONTESTED]
    assert contested, "the set should keep at least one contested scenario"
    assert all(r not in run.scored_results for r in contested)
    _, scored_total = run.passes
    assert scored_total == len(run.results) - len(contested)


def test_repeats_produce_one_attempt_each_and_detect_instability():
    unstable = ScriptedClient({"safe_dependency_bump": [LOW, HIGH, LOW]})

    run = run_eval(unstable, scenarios=("safe_dependency_bump",), repeats=3, model_id="test")

    result = run.results[0]
    assert len(result.attempts) == 3
    assert not result.is_stable
    stable_n, total = run.stability
    assert (stable_n, total) == (0, 1)


def test_ordering_violations_are_detected():
    """Docs ranked above a payments refactor is a reasoning error, not taste."""
    run = run_eval(
        ScriptedClient({"docs_only_change": [HIGH], "risky_payments_friday": [MEDIUM]}),
        model_id="test",
    )

    violated = {(v.lax, v.strict) for v in run.pair_violations}
    assert ("docs_only_change", "risky_payments_friday") in violated


def test_no_ordering_violations_when_the_ordering_holds():
    run = run_eval(
        ScriptedClient(
            {name: [LOW] for name in scenario_names()}
            | {"risky_payments_friday": [HIGH], "critical_cve_with_patch": [HIGH]}
        ),
        model_id="test",
    )

    assert run.pair_violations == []


def test_an_unknown_scenario_name_raises_rather_than_scoring_nothing():
    with pytest.raises(KeyError, match="nonexistent"):
        run_eval(ScriptedClient({}), scenarios=("nonexistent",), model_id="test")


def test_fail_closed_attempts_are_counted_separately():
    """A run made of fail-closed verdicts measures error handling, not judgement.

    The count has to be visible before the accuracy figure, or the figure gets
    believed.
    """
    run = run_eval(AttributeCountingClient(), model_id="baseline")

    # Exactly one scenario has no change context, and the baseline refuses it.
    assert run.fail_closed_count == 1


# --- The baseline's known failures ---------------------------------------


def test_the_baseline_scores_well_enough_to_be_a_real_comparison():
    """If a fifteen-line attribute counter scores near zero, it proves nothing.

    The point of the baseline is that it does WELL -- so that a model scoring
    similarly can be recognised as adding nothing on this set.
    """
    run = run_eval(AttributeCountingClient(), model_id="baseline")

    passed, total = run.passes
    assert passed / total > 0.7, "baseline too weak to be a meaningful comparison"


def test_the_baseline_cannot_recognise_a_revert():
    """The claim the talk makes about attribute-scoring, locked in as a test.

    Every individual signal says halt -- 1,057 lines, payments/, Saturday night,
    target in alarm -- and halting is wrong, because the target is unhealthy
    because of what is deployed now. Getting this right needs the commit message
    read and understood. Arithmetic cannot get there, and this asserts it does
    not, so the claim stays honest if the baseline is ever tuned.
    """
    run = run_eval(
        AttributeCountingClient(), scenarios=("revert_of_a_bad_deploy",), model_id="baseline"
    )

    result = run.results[0]
    assert not result.passed
    assert result.direction == "over"


def test_the_baseline_misses_risk_that_no_attribute_reports():
    """812 hours since the last deploy is not a countable attribute.

    Accumulated drift has to be inferred from the gap, and a scorer with no rule
    for it sees a small, tidy, business-hours change.
    """
    run = run_eval(
        AttributeCountingClient(), scenarios=("first_deploy_in_a_month",), model_id="baseline"
    )

    assert run.results[0].direction == "under"


def test_the_baseline_still_fails_closed_without_change_context():
    """The one thing it gets right for the right reason.

    Included so the comparison with the real client is on equal terms: a model
    that only beats the baseline by handling missing signals has not beaten it
    on judgement.
    """
    run = run_eval(AttributeCountingClient(), scenarios=("no_change_context",), model_id="baseline")

    result = run.results[0]
    assert result.passed
    assert not result.model_call_violation


# --- The fixtures themselves ---------------------------------------------


@pytest.mark.parametrize("name", scenario_names())
def test_every_scenario_builds_a_bundle(name):
    bundle = bundle_for(name)

    assert bundle.target.service_name
    # collected_at must be fixed, or byte-identical replay is impossible.
    assert bundle.collected_at.year == 2026


def test_bundle_for_is_deterministic():
    """Two calls must produce identical serialised bundles.

    This is what makes prompt drift measurable at all: if the input moves
    between runs, a changed verdict says nothing about the prompt.
    """
    first = bundle_for("risky_payments_friday").to_dict()
    second = bundle_for("risky_payments_friday").to_dict()

    assert first == second


def test_bundle_for_rejects_an_unknown_name():
    with pytest.raises(KeyError):
        bundle_for("not_a_scenario")


def test_the_absent_signal_scenarios_differ_in_status_not_just_data():
    """SKIPPED and UNAVAILABLE must not collapse into the same thing.

    They deny the gate the same information and mean entirely different things
    operationally -- a choice versus a fault.
    """
    chosen = bundle_for("security_scanning_switched_off").security
    failed = bundle_for("security_signal_unavailable").security

    assert chosen.status is not failed.status
    assert chosen.data is None and failed.data is None


# --- Measured vs refused --------------------------------------------------
#
# Phase 4b, found on the first live run. Four scenarios failed schema
# validation and fell back to a fail-closed HIGH; three carried labels where
# `high` was acceptable, so the harness scored them as correct judgements. The
# gate was credited for an assessment it had explicitly declined to make.


def refusal(level=HIGH, *, failure_kind="invalid_verdict:primary_concerns"):
    """A fail-closed attempt: the gate declined to answer."""
    return Attempt(
        scenario="x",
        risk_level=level,
        source=VerdictSource.FAIL_CLOSED,
        reasoning=f"could not obtain a valid verdict from Bedrock ({failure_kind})",
        concerns=(),
        attempts=3,
        failure_kind=failure_kind,
        input_tokens=None,
        output_tokens=None,
        latency_ms=None,
    )


def test_a_refusal_is_not_a_measured_scenario():
    lab = label(kind=Kind.RISKY, acceptable=frozenset({MEDIUM, HIGH}))

    assert ScenarioResult(label=lab, attempts=(refusal(),)).is_measured is False
    assert ScenarioResult(label=lab, attempts=(attempt(HIGH),)).is_measured is True


def test_one_refusal_among_several_attempts_unmeasures_the_scenario():
    """Same reasoning as `passed` requiring every attempt to be acceptable: a
    scenario the gate refused to answer once is not fully measured."""
    lab = label(kind=Kind.RISKY, acceptable=frozenset({MEDIUM, HIGH}))

    result = ScenarioResult(label=lab, attempts=(attempt(HIGH), refusal(), attempt(HIGH)))

    assert result.is_measured is False
    assert result.fail_closed_attempts == 1


def test_a_refusal_on_a_fail_closed_label_is_still_measured():
    """There, refusing IS the behaviour under test. Excluding it would remove
    the only scenario that checks the default branch works."""
    lab = label(scenario="no_change_context", kind=Kind.FAIL_CLOSED, acceptable=frozenset({HIGH}))

    result = ScenarioResult(label=lab, attempts=(refusal(),))

    assert result.is_measured is True
    assert result.passed is True


def test_a_refusal_does_not_count_as_a_correct_verdict():
    """THE FLATTERY THIS CLOSES.

    A fail-closed HIGH on a risky label is `high`, which is acceptable, which
    used to read as a pass. It is not a pass -- nothing was assessed.
    """
    lab = label(scenario="risky", kind=Kind.RISKY, acceptable=frozenset({MEDIUM, HIGH}))
    run = EvalRun(
        results=(ScenarioResult(label=lab, attempts=(refusal(),)),),
        repeats=1,
        model_id="test",
    )

    assert run.passes == (0, 0), "a refused scenario must not appear in the pass denominator"
    assert run.unmeasured[0].scenario == "risky"


def test_a_refusal_is_excluded_from_the_under_flagging_denominator():
    """A refusal halts the deploy, so it is not a risky change waved through.
    Counting it either way would make the judgement rate move with Bedrock's
    availability rather than with the model's calibration.
    """
    risky_ok = label(scenario="a", kind=Kind.RISKY, acceptable=frozenset({MEDIUM, HIGH}))
    risky_refused = label(scenario="b", kind=Kind.RISKY, acceptable=frozenset({MEDIUM, HIGH}))
    run = EvalRun(
        results=(
            ScenarioResult(label=risky_ok, attempts=(attempt(HIGH),)),
            ScenarioResult(label=risky_refused, attempts=(refusal(),)),
        ),
        repeats=1,
        model_id="test",
    )

    assert run.under_flagging == (0, 1)
    assert len(run.unmeasured) == 1


def test_a_refusal_is_excluded_from_over_flagging_too():
    """Deliberate, and the argument is worth stating because it cuts the other
    way: a fail-closed HIGH on a benign change really did block a fine deploy,
    so there is a case for counting it as over-flagging.

    It is excluded because the two rates measure JUDGEMENT, and a gate that
    halts because Bedrock returned malformed JSON has exercised none. The
    operational cost of those halts is real and is reported -- by name, in the
    UNMEASURED block -- just not as a calibration figure.
    """
    benign = label(scenario="a", kind=Kind.BENIGN, acceptable=frozenset({LOW}))
    run = EvalRun(
        results=(ScenarioResult(label=benign, attempts=(refusal(),)),),
        repeats=1,
        model_id="test",
    )

    assert run.over_flagging == (0, 0)
    assert run.fail_closed_count == 1


def test_a_refusal_is_not_listed_as_a_judgement_failure():
    benign = label(scenario="a", kind=Kind.BENIGN, acceptable=frozenset({LOW}))
    run = EvalRun(
        results=(ScenarioResult(label=benign, attempts=(refusal(),)),),
        repeats=1,
        model_id="test",
    )

    assert run.failures == ()
    assert len(run.unmeasured) == 1


def test_the_report_names_the_refused_scenarios():
    """A count alone is not enough. Four refusals spread across the benign
    fixtures and four concentrated on the risky ones are the same number
    describing opposite situations."""
    lab = label(scenario="critical_cve_no_patch", kind=Kind.RISKY, acceptable=frozenset({HIGH}))
    run = EvalRun(
        results=(ScenarioResult(label=lab, attempts=(refusal(),)),),
        repeats=1,
        model_id="test",
    )

    text = format_run(run)

    assert "UNMEASURED" in text
    assert "critical_cve_no_patch" in text
    assert "invalid_verdict:primary_concerns" in text
