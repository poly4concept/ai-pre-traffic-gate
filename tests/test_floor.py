"""The floor raises and never lowers. Phase 5.5.

The floor exists because three eval scenarios failed on every model tried, in
the same direction, with the same reasoning (D-080). It is fifteen lines of
arithmetic asserting a minimum the model may exceed but not undercut.

Two properties carry the weight, and they are different in kind:

  1. CORRECTNESS -- it raises the right cases to the right level.
  2. SAFETY -- there is no input, from any source, that makes it LOWER a
     verdict. That one matters more, because the floor's whole justification is
     being the part of the decision the model cannot argue with. A floor with a
     downward path is not a floor.

Nothing here reaches AWS.
"""

from __future__ import annotations

import pytest
from signals import scenarios as sc
from signals.scenarios import bundle_for
from verdict import (
    RISK_ORDER,
    RiskLevel,
    Verdict,
    VerdictSource,
    apply_floor,
    required_floor,
)


def verdict(level=RiskLevel.LOW, **kw):
    return Verdict(
        risk_level=level,
        reasoning="the model's own reasoning",
        source=VerdictSource.MODEL,
        confidence=0.8,
        **kw,
    )


# --- What the floor demands -----------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    ["critical_cve_no_patch", "critical_cve_with_patch", "untriaged_severity_findings"],
)
def test_the_three_scenarios_every_model_got_wrong(scenario):
    """THE REASON THIS MODULE EXISTS.

    Haiku 4.5 and Sonnet 4.5 both returned `low` on all three, stably, with the
    same argument. After the floor, Sonnet's under-flagging went from 27.3% to
    0.0% on the full fixture set.
    """
    floor = required_floor(bundle_for(scenario))

    assert floor is not None, f"{scenario} should be floored"
    assert floor.level is RiskLevel.MEDIUM


def test_a_critical_finding_floors_at_medium_not_high():
    """Medium, deliberately. `high` halts the pipeline and summons a human; a
    pre-existing critical in a service that has been running for weeks does not
    warrant that on its own. Medium means canary, which is the proportionate
    response -- and the model remains free to say `high` if the rest of the
    evidence supports it."""
    floor = required_floor(bundle_for("critical_cve_no_patch"))

    assert floor.level is RiskLevel.MEDIUM
    assert "critical" in floor.reason.lower()


def test_untriaged_findings_are_floored_because_unknown_is_not_harmless():
    floor = required_floor(bundle_for("untriaged_severity_findings"))

    assert floor.level is RiskLevel.MEDIUM
    assert "unknown severity" in floor.reason.lower()


def test_a_clean_scan_imposes_no_floor():
    """A scan that ran and found nothing is a real answer, and the floor has
    nothing to say about it. Otherwise every deploy would be medium and the
    gate would be a coin that only lands one way."""
    assert required_floor(bundle_for("safe_dependency_bump")) is None


def test_many_low_severity_findings_impose_no_floor():
    """The floor is for criticals and unknowns. A pile of lows is exactly the
    judgement call the model is there to make."""
    assert required_floor(bundle_for("many_low_severity_findings")) is None


def test_an_absent_security_signal_imposes_no_floor():
    """NOT an oversight, and worth stating.

    A missing signal already pushes the verdict up -- rule 1 of the system
    prompt tells the model to treat absence as risk-raising, and it does. A
    floor here would count the same fact twice and make every unscanned deploy
    medium on top of a model that had already reacted to it.

    The floor is for cases where the model is measurably WRONG, not for every
    case where a rule could be written.
    """
    for scenario in ("security_signal_unavailable", "security_scanning_switched_off"):
        assert required_floor(bundle_for(scenario)) is None, scenario


# --- It raises ---------------------------------------------------------------


def test_a_low_verdict_is_raised_to_the_floor():
    bundle = bundle_for("critical_cve_no_patch")

    raised = apply_floor(verdict(RiskLevel.LOW), bundle)

    assert raised.risk_level is RiskLevel.MEDIUM
    assert raised.floor_raised_from is RiskLevel.LOW
    assert raised.was_raised_by_floor


def test_the_model_keeps_its_own_reasoning_when_raised():
    """The floor changes the level, not the account of why. Overwriting the
    model's reasoning would destroy the evidence that it disagreed -- which is
    the single most interesting row in the audit table."""
    bundle = bundle_for("critical_cve_no_patch")

    raised = apply_floor(verdict(RiskLevel.LOW), bundle)

    assert raised.reasoning == "the model's own reasoning"
    assert "critical" in raised.floor_reason.lower()


def test_the_audit_record_says_the_floor_did_it():
    """Without this field a `medium` the code insisted on and a `medium` the
    model reasoned its way to are identical in the table -- and Phase 4 would
    measure the arithmetic and call it the model's judgement."""
    raised = apply_floor(verdict(RiskLevel.LOW), bundle_for("critical_cve_no_patch"))

    record = raised.to_dict()

    assert record["risk_level"] == "medium"
    assert record["floor_raised_from"] == "low"
    assert record["floor_reason"]


def test_an_unfloored_verdict_records_no_floor():
    record = apply_floor(verdict(RiskLevel.LOW), bundle_for("safe_dependency_bump")).to_dict()

    assert record["floor_raised_from"] is None
    assert record["floor_reason"] == ""


# --- It never lowers ---------------------------------------------------------


def test_a_high_verdict_is_left_alone():
    """THE PROPERTY THAT MAKES IT A FLOOR.

    The model is more cautious than the arithmetic here, and the arithmetic does
    not get to argue it down.
    """
    bundle = bundle_for("critical_cve_no_patch")
    original = verdict(RiskLevel.HIGH)

    result = apply_floor(original, bundle)

    assert result is original
    assert result.risk_level is RiskLevel.HIGH
    assert not result.was_raised_by_floor


def test_a_verdict_already_at_the_floor_is_untouched():
    original = verdict(RiskLevel.MEDIUM)

    result = apply_floor(original, bundle_for("critical_cve_no_patch"))

    assert result is original
    assert not result.was_raised_by_floor


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (RiskLevel.HIGH, RiskLevel.LOW),
        (RiskLevel.HIGH, RiskLevel.MEDIUM),
        (RiskLevel.MEDIUM, RiskLevel.LOW),
        (RiskLevel.MEDIUM, RiskLevel.MEDIUM),
    ],
)
def test_raised_to_refuses_to_lower_or_flatten(start, target):
    """Enforced on the Verdict itself, not only in the caller.

    `apply_floor` is careful, but `raised_to` is public and the safety property
    should not depend on every future caller being careful too.
    """
    with pytest.raises(ValueError, match="only raise"):
        verdict(start).raised_to(target, "because")


def test_no_scenario_in_the_fixture_set_is_lowered_by_the_floor():
    """Swept rather than argued. Every scenario, every starting level."""
    checked = 0
    for name in sc.scenario_names():
        bundle = bundle_for(name)
        for level in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH):
            result = apply_floor(verdict(level), bundle)
            # RISK_ORDER, not `>=` -- RiskLevel is a StrEnum and compares
            # alphabetically, so `HIGH < LOW` is True and a direct comparison
            # would pass while asserting nonsense (see types.py).
            assert RISK_ORDER[result.risk_level] >= RISK_ORDER[level], (
                f"{name}: floor lowered {level} to {result.risk_level}"
            )
            checked += 1

    assert checked == 3 * len(sc.scenario_names()), "the sweep did not cover what it claims"


# --- It cannot be steered ----------------------------------------------------


def test_the_floor_ignores_everything_the_change_says_about_itself():
    """The floor reads the security signal and nothing else.

    Diff statistics are computed by a script inside the repository being judged,
    and the commit message is written by whoever wants the deploy. A floor built
    on either would be a floor the change could lower -- which is the one thing
    it must not be. The prompt-injection fixture is the sharpest version of the
    test: it is a bundle explicitly trying to steer the gate.
    """
    injected = required_floor(bundle_for("prompt_injection_in_commit_message"))
    clean = required_floor(bundle_for("safe_dependency_bump"))

    # Same security posture in both, so the same answer, regardless of what the
    # commit message is attempting.
    assert injected == clean


def test_a_fail_closed_verdict_is_never_lowered_by_the_floor():
    """Fail-closed is HIGH, and the floor is MEDIUM. If the comparison were
    written the wrong way round, the floor would quietly downgrade every
    infrastructure failure into a canary."""
    failed = Verdict.fail_closed("bedrock unreachable")

    result = apply_floor(failed, bundle_for("critical_cve_no_patch"))

    assert result is failed
    assert result.risk_level is RiskLevel.HIGH
    assert result.source is VerdictSource.FAIL_CLOSED
