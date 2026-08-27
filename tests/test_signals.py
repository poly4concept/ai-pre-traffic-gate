"""Tests for the Phase 2.1 signal collectors.

The centre of gravity is the fail-closed group. Those tests are the evidence for
the claim that a broken collector cannot be mistaken for good news, which is the
single property the rest of the gate's safety rests on.

All offline, no credentials, no clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from signals import (
    ChangeContext,
    DisabledCollector,
    MockChangeContextCollector,
    MockSecurityFindingsCollector,
    MockTargetHealthCollector,
    SecurityFinding,
    SecurityFindings,
    Severity,
    SignalResult,
    SignalStatus,
    collect_signals,
)
from signals import scenarios as sc

FIXED_NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def bundle_from(change=None, security=None, health=None, **kwargs):
    """Assemble a bundle from prepared values, defaulting to the safe scenario."""
    return collect_signals(
        target=sc.DEMO_TARGET,
        change_collector=MockChangeContextCollector(change or sc.SAFE_DEPENDENCY_BUMP, **kwargs),
        security_collector=MockSecurityFindingsCollector(security or sc.NO_FINDINGS),
        health_collector=MockTargetHealthCollector(health or sc.HEALTHY_TARGET),
        now=FIXED_NOW,
    )


# --- Fail closed ----------------------------------------------------------
#
# The property under test throughout: an absent signal is never a negative one.


def test_a_failing_collector_yields_no_data_rather_than_a_default():
    """The whole point. An empty SecurityFindings() would read as 'all clear'."""
    result = MockSecurityFindingsCollector(raises=RuntimeError("Inspector throttled")).collect()

    assert result.status is SignalStatus.UNAVAILABLE
    assert result.data is None
    assert not result.is_usable
    assert "Inspector throttled" in result.error


def test_no_findings_and_failed_scan_are_distinguishable():
    """The distinction this package exists to preserve.

    Both produce zero critical findings if you only count. Only one of them is
    evidence of anything.
    """
    clean = MockSecurityFindingsCollector(sc.NO_FINDINGS).collect()
    broken = MockSecurityFindingsCollector(raises=ConnectionError("timeout")).collect()

    assert clean.status is SignalStatus.OK
    assert clean.data.critical_count == 0
    assert clean.is_usable

    assert broken.status is SignalStatus.UNAVAILABLE
    assert broken.data is None
    assert not broken.is_usable


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("boom"),
        ConnectionError("network unreachable"),
        TimeoutError("timed out"),
        KeyError("unexpected response shape"),
        ValueError("could not parse"),
        Exception("something nobody anticipated"),
    ],
)
def test_every_exception_type_fails_closed(exc):
    """Deliberately broad, per D-014: catch-all default, not a list of knowns."""
    result = MockChangeContextCollector(raises=exc).collect()

    assert result.status is SignalStatus.UNAVAILABLE
    assert result.data is None
    assert type(exc).__name__ in result.error


def test_error_records_the_exception_type_not_only_the_message():
    """F-004's lesson: one message can mean several unrelated things."""
    result = MockChangeContextCollector(raises=ConnectionError("denied")).collect()

    assert "ConnectionError" in result.error


def test_signal_result_rejects_ok_without_data():
    with pytest.raises(ValueError, match="status OK but data is None"):
        SignalResult(collector="x", status=SignalStatus.OK, data=None)


def test_signal_result_rejects_unavailable_carrying_data():
    with pytest.raises(ValueError, match="UNAVAILABLE but data was supplied"):
        SignalResult(collector="x", status=SignalStatus.UNAVAILABLE, data="something")


def test_disabled_collector_is_skipped_not_unavailable():
    """Chose not to look, versus tried and failed. Different operationally."""
    result = DisabledCollector[str]("security_findings").collect()

    assert result.status is SignalStatus.SKIPPED
    assert result.data is None
    assert not result.is_usable


# --- Bundle completeness --------------------------------------------------


def test_bundle_assembles_even_when_every_collector_fails():
    """A broken collector must not take the bundle down with it."""
    bundle = collect_signals(
        target=sc.DEMO_TARGET,
        change_collector=MockChangeContextCollector(raises=RuntimeError("a")),
        security_collector=MockSecurityFindingsCollector(raises=RuntimeError("b")),
        health_collector=MockTargetHealthCollector(raises=RuntimeError("c")),
        now=FIXED_NOW,
    )

    assert set(bundle.missing) == {"change_context", "security_findings", "target_health"}
    assert not bundle.is_complete
    assert not bundle.has_required_signals


def test_partial_collection_keeps_what_worked():
    """Losing one signal must not discard the other two."""
    bundle = collect_signals(
        target=sc.DEMO_TARGET,
        change_collector=MockChangeContextCollector(sc.SAFE_DEPENDENCY_BUMP),
        security_collector=MockSecurityFindingsCollector(raises=RuntimeError("throttled")),
        health_collector=MockTargetHealthCollector(sc.HEALTHY_TARGET),
        now=FIXED_NOW,
    )

    assert bundle.missing == ("security_findings",)
    assert not bundle.is_complete
    # Still enough to attempt a verdict, honestly labelled.
    assert bundle.has_required_signals
    assert bundle.change.is_usable
    assert bundle.health.is_usable


def test_missing_change_context_blocks_a_verdict_entirely():
    """No subject means no verdict. Phase 3 routes this to human review."""
    bundle = bundle_from(raises=RuntimeError("no source metadata"))

    assert not bundle.has_required_signals


def test_skipped_signals_count_as_missing_but_not_failed():
    bundle = collect_signals(
        target=sc.DEMO_TARGET,
        change_collector=MockChangeContextCollector(sc.SAFE_DEPENDENCY_BUMP),
        security_collector=DisabledCollector("security_findings"),
        health_collector=MockTargetHealthCollector(sc.HEALTHY_TARGET),
        now=FIXED_NOW,
    )

    assert bundle.missing == ("security_findings",)
    assert bundle.failed == ()


def test_completeness_summary_names_what_is_missing():
    bundle = bundle_from(raises=RuntimeError("gone"))

    assert "change_context" in bundle.completeness_summary
    assert "2/3" in bundle.completeness_summary


def test_complete_bundle_says_so():
    assert bundle_from().completeness_summary == "all 3 signals collected"


# --- Deterministic replay (constraint 6) ----------------------------------


def test_identical_inputs_serialise_byte_identically():
    """Constraint 6. Without this, Phase 4 cannot detect prompt drift."""
    first = json.dumps(bundle_from().to_dict(), sort_keys=True)
    second = json.dumps(bundle_from().to_dict(), sort_keys=True)

    assert first == second


def test_serialisation_excludes_timing():
    """duration_ms varies run to run and would break byte-identical replay."""
    assert "duration_ms" not in json.dumps(bundle_from().to_dict())


def test_serialised_bundle_is_json_safe():
    """Datetimes and enums must already be primitives, not need a custom encoder."""
    payload = bundle_from(security=sc.CRITICAL_FIXABLE_CVE).to_dict()

    json.dumps(payload)  # would raise on a stray datetime or Enum

    findings = payload["signals"]["security_findings"]["data"]["findings"]
    assert findings[0]["severity"] == "critical"


def test_serialised_bundle_records_why_a_signal_is_absent():
    """The audit trail must explain gaps, not merely have them."""
    payload = bundle_from(raises=RuntimeError("Inspector unreachable"))
    entry = payload.to_dict()["signals"]["change_context"]

    assert entry["status"] == "unavailable"
    assert entry["data"] is None
    assert "Inspector unreachable" in entry["error"]


# --- Derived properties ---------------------------------------------------
#
# Computed deterministically rather than left for the model to infer. Anything
# a plain function can decide should not be delegated to a probabilistic one.


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 8, 11, 10, 0, tzinfo=UTC), False),  # Tue 10:00
        (datetime(2026, 8, 11, 7, 59, tzinfo=UTC), True),  # Tue 07:59
        (datetime(2026, 8, 11, 18, 0, tzinfo=UTC), True),  # Tue 18:00
        (datetime(2026, 8, 14, 19, 42, tzinfo=UTC), True),  # Fri evening
        (datetime(2026, 8, 15, 11, 0, tzinfo=UTC), True),  # Saturday
        (datetime(2026, 8, 16, 11, 0, tzinfo=UTC), True),  # Sunday
    ],
)
def test_off_hours_detection(when, expected):
    change = ChangeContext(
        commit_sha="x",
        commit_message="m",
        branch="main",
        author="a",
        committed_at=when,
        files_changed=1,
        lines_added=1,
        lines_removed=0,
    )

    assert change.is_off_hours is expected


def test_fixable_and_unfixable_findings_are_distinguishable():
    """Same severity, different remedy, different decision."""
    fixable = SecurityFinding(
        id="CVE-1", severity=Severity.CRITICAL, title="t", fixed_version="1.1"
    )
    unfixable = SecurityFinding(id="CVE-2", severity=Severity.CRITICAL, title="t")

    findings = SecurityFindings(findings=(fixable, unfixable))

    assert findings.critical_count == 2
    assert findings.fixable_critical_or_high == (fixable,)


def test_low_traffic_target_is_flagged_as_such():
    """A 0% error rate over 12 requests is not evidence of health."""
    assert sc.QUIET_TARGET.error_rate_pct == 0.0
    assert sc.QUIET_TARGET.is_low_traffic
    assert not sc.HEALTHY_TARGET.is_low_traffic


def test_active_alarms_are_separated_from_all_alarms():
    assert len(sc.TARGET_IN_ALARM.alarms) == 2
    assert len(sc.TARGET_IN_ALARM.alarms_in_alarm) == 2
    assert sc.TARGET_IN_ALARM.has_active_alarm
    assert not sc.HEALTHY_TARGET.has_active_alarm


def test_insufficient_data_alarm_is_not_an_active_alarm():
    """INSUFFICIENT_DATA means the alarm cannot tell. It is not reassurance."""
    assert not sc.QUIET_TARGET.has_active_alarm
    assert sc.QUIET_TARGET.alarms[0].state == "INSUFFICIENT_DATA"


# --- Scenarios ------------------------------------------------------------


def test_every_scenario_assembles_into_a_bundle():
    """Fixtures must be usable, not merely defined.

    Assembly, not completeness. From Phase 4a three scenarios deliberately carry
    an absent signal -- that is what they exist to test -- so requiring every
    bundle to be complete would forbid the eval set from covering the case the
    whole signals package was built around.
    """
    for name in sc.scenario_names():
        bundle = sc.bundle_for(name)

        assert bundle.target.service_name, name
        assert bundle.collected_at.tzinfo is not None, name


def test_only_the_scenarios_that_intend_an_absence_have_one():
    """Guards against a fixture losing a signal by accident.

    Without this, a typo in a scenario key silently produces a degraded bundle
    and the eval quietly starts measuring something else.
    """
    intended = {
        name
        for name, s in sc.SCENARIOS.items()
        if any(k.endswith(("_unavailable", "_skipped")) for k in s)
    }
    actual = {name for name in sc.scenario_names() if not sc.bundle_for(name).is_complete}

    assert actual == intended


def test_scenarios_are_frozen_in_time():
    """No scenario may depend on when the suite runs."""
    for name, s in sc.SCENARIOS.items():
        change = s.get("change")
        if change is None:  # the deliberately absent-change scenario
            continue
        assert change.committed_at.year == 2026, name
        assert change.committed_at.tzinfo is not None, name


def test_scenario_set_spans_the_risk_space():
    """A fixture set that only contains easy cases measures nothing."""
    changes = [s["change"] for s in sc.SCENARIOS.values() if s.get("change") is not None]

    assert any(c.is_off_hours for c in changes)
    assert any(not c.is_off_hours for c in changes)
    assert any(c.total_lines_changed > 1000 for c in changes)
    assert any(c.total_lines_changed < 10 for c in changes)
    assert any(c.deploys_last_24h > 5 for c in changes)
    # Phase 4a: the set must contain genuinely boring changes too, or the
    # over-flagging rate is measured against nothing.
    assert any(
        c.total_lines_changed < 250 and not c.is_off_hours and "payments" not in " ".join(c.paths)
        for c in changes
    )

    healths = [s["health"] for s in sc.SCENARIOS.values()]
    assert any(h.has_active_alarm for h in healths)
    assert any(h.is_low_traffic for h in healths)


# --- The offline guard ----------------------------------------------------


def test_the_suite_cannot_make_a_real_aws_call():
    """Tests the test infrastructure, deliberately.

    The autouse `_no_real_aws` fixture in conftest silently protects every test in
    the suite, and a guard nobody verifies is not a guard (FAILURES.md F-003).
    Before it existed, Phase 2.3 quietly started calling Inspector for real and
    the suite went from 2 seconds to 88.
    """
    import boto3

    with pytest.raises(RuntimeError, match="tried to create a real boto3 client"):
        boto3.client("inspector2")


def test_the_real_inspector_collector_stays_offline_in_tests():
    """A collector built without a client must not reach AWS from the suite."""
    from signals import InspectorFindingsCollector

    result = InspectorFindingsCollector("some-function").collect()

    # Blocked by the guard, absorbed by the fail-closed handler, reported as
    # unavailable. Never as "no findings".
    assert result.status is SignalStatus.UNAVAILABLE
    assert result.data is None
