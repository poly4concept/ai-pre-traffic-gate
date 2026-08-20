"""Tests for the deploy cadence collector. Phase 2.4b.

Cadence is the last field in the bundle that used to be honestly empty, and the
first one the change being judged has no route to influence -- it comes from the
CodeDeploy control plane rather than from a script inside the repository.

Two clusters carry the weight: the AWS_API provenance, which is the security
claim, and the rule that a cadence failure degrades one field rather than the
whole change context.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from signals import (
    DeployCadence,
    DeployCadenceCollector,
    PipelineChangeContextCollector,
    Provenance,
    SignalStatus,
)

APP = "ai-pre-traffic-gate-demo-app"
GROUP = "ai-pre-traffic-gate-demo-app"
NOW = datetime(2026, 8, 17, 16, 46, tzinfo=UTC)
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


class FakeCodeDeploy:
    def __init__(self, ids_by_call=None, created=None, fail=None):
        self._ids_by_call = ids_by_call if ids_by_call is not None else [[]]
        self._created = created
        self._fail = fail
        self.calls: list[str] = []
        self._call_index = 0

    def list_deployments(self, **kwargs):
        self.calls.append("list_deployments")
        self.last_kwargs = kwargs
        if self._fail == "list":
            raise RuntimeError("AccessDeniedException")
        idx = min(self._call_index, len(self._ids_by_call) - 1)
        self._call_index += 1
        return {"deployments": self._ids_by_call[idx]}

    def batch_get_deployments(self, **_):
        self.calls.append("batch_get_deployments")
        if self._fail == "get":
            raise RuntimeError("ThrottlingException")
        if self._created is None:
            return {"deploymentsInfo": []}
        return {"deploymentsInfo": [{"createTime": self._created}]}


def collect(fake, **kwargs):
    return DeployCadenceCollector(APP, GROUP, client=fake, now=NOW, **kwargs).collect()


def pipeline_event(**payload_overrides):
    payload = {
        "commit_sha": SHA,
        "commit_message": "m",
        "branch": "main",
        "author": "a",
        "committed_at": "2026-08-11T10:14:00+00:00",
        "diff_stats_ok": True,
        "files_changed": 3,
        "lines_added": 10,
        "lines_removed": 2,
    }
    payload.update(payload_overrides)
    params = {
        "trusted_commit_sha": SHA,
        "change_context_b64": base64.b64encode(json.dumps(payload).encode()).decode(),
    }
    return {
        "CodePipeline.job": {
            "data": {
                "actionConfiguration": {"configuration": {"UserParameters": json.dumps(params)}}
            }
        }
    }


# --- Counting -------------------------------------------------------------


def test_counts_deployments_in_the_window():
    fake = FakeCodeDeploy(
        ids_by_call=[["d-7", "d-6", "d-5", "d-4", "d-3", "d-2", "d-1"]],
        created=NOW - timedelta(minutes=24),
    )

    result = collect(fake)

    assert result.status is SignalStatus.OK
    assert result.data.deploys_in_window == 7
    assert result.data.hours_since_last_deploy == pytest.approx(0.4, abs=0.01)


def test_a_quiet_service_reports_zero_deploys_which_is_a_real_fact():
    """Unlike an error rate, zero deploys IS a meaningful measured value.

    Nothing was divided by anything -- CodeDeploy was asked for a list and
    returned an empty one for a resource it genuinely tracks. That differs from
    Inspector's empty findings list, and it is why this is OK rather than
    DEGRADED.
    """
    fake = FakeCodeDeploy(ids_by_call=[[], ["d-old"]], created=NOW - timedelta(days=8))

    result = collect(fake)

    assert result.status is SignalStatus.OK
    assert result.data.deploys_in_window == 0
    assert result.data.hours_since_last_deploy == pytest.approx(192.0, abs=0.1)


def test_a_never_deployed_service_has_no_gap_to_report():
    """No deployments anywhere in the lookback. The gap is unknown, not huge."""
    fake = FakeCodeDeploy(ids_by_call=[[], []])

    result = collect(fake)

    assert result.data.deploys_in_window == 0
    assert result.data.hours_since_last_deploy is None


def test_the_gap_search_widens_when_the_window_is_empty():
    """A service last deployed weeks ago has 0 in 24h and a meaningful gap."""
    fake = FakeCodeDeploy(ids_by_call=[[], ["d-old"]], created=NOW - timedelta(days=21))

    collect(fake)

    assert fake.calls.count("list_deployments") == 2


def test_the_gap_search_is_skipped_when_the_window_has_deployments():
    fake = FakeCodeDeploy(ids_by_call=[["d-1"]], created=NOW - timedelta(hours=2))

    collect(fake)

    assert fake.calls.count("list_deployments") == 1


# --- Derived judgements ---------------------------------------------------


def test_rapid_succession_is_computed_not_inferred():
    """Deterministic, so the model cannot get it wrong and Phase 4 can assert."""
    assert DeployCadence(7, 0.4).is_rapid_succession
    assert DeployCadence(5, 1.0).is_rapid_succession
    assert not DeployCadence(4, 1.0).is_rapid_succession
    assert not DeployCadence(0, None).is_rapid_succession


def test_a_long_gap_is_flagged():
    assert DeployCadence(0, 192.0).is_first_in_a_long_time
    assert not DeployCadence(0, 26.0).is_first_in_a_long_time
    assert not DeployCadence(0, None).is_first_in_a_long_time


# --- Query construction ---------------------------------------------------


def test_the_window_is_scoped_to_the_deployment_group():
    fake = FakeCodeDeploy(ids_by_call=[["d-1"]], created=NOW)

    collect(fake)

    assert fake.last_kwargs["applicationName"] == APP
    assert fake.last_kwargs["deploymentGroupName"] == GROUP


def test_failed_and_stopped_deployments_still_count():
    """Cadence measures attempts to change the service, not successes.

    A failed deploy an hour ago is strong evidence somebody is mid-incident --
    arguably stronger evidence than a successful one.
    """
    fake = FakeCodeDeploy(ids_by_call=[["d-1"]], created=NOW)

    collect(fake)

    statuses = fake.last_kwargs["includeOnlyStatuses"]
    assert "Failed" in statuses
    assert "Stopped" in statuses
    assert "Succeeded" in statuses


def test_a_naive_create_time_is_treated_as_utc():
    naive = datetime(2026, 8, 17, 14, 46)  # noqa: DTZ001
    fake = FakeCodeDeploy(ids_by_call=[["d-1"]], created=naive)

    result = collect(fake)

    assert result.data.hours_since_last_deploy == pytest.approx(2.0, abs=0.01)


# --- Failure --------------------------------------------------------------


def test_an_api_failure_is_unavailable():
    result = collect(FakeCodeDeploy(fail="list"))

    assert result.status is SignalStatus.UNAVAILABLE
    assert result.data is None
    assert "AccessDeniedException" in result.error


def test_a_batch_get_failure_is_unavailable():
    result = collect(FakeCodeDeploy(ids_by_call=[["d-1"]], fail="get"))

    assert result.status is SignalStatus.UNAVAILABLE
    assert "ThrottlingException" in result.error


# --- Integration with change context --------------------------------------


def test_cadence_reaches_change_context_with_aws_api_provenance():
    """The claim: this is the one field the change cannot lie about."""
    cadence = DeployCadenceCollector(
        APP,
        GROUP,
        client=FakeCodeDeploy(ids_by_call=[["d-1", "d-2"]], created=NOW - timedelta(hours=3)),
        now=NOW,
    )

    change = (
        PipelineChangeContextCollector(pipeline_event(), cadence_collector=cadence).collect().data
    )

    assert change.deploys_last_24h == 2
    assert change.hours_since_last_deploy == pytest.approx(3.0, abs=0.01)
    assert change.cadence_provenance is Provenance.AWS_API
    # Diff stats remain the only thing the change says about itself.
    assert change.self_reported_fields == ("diff statistics",)


def test_a_cadence_failure_degrades_one_field_not_the_whole_signal():
    """A CodeDeploy hiccup must not block a pipeline.

    Change context has already been established by this point -- we are declining
    to ADD to it, not accepting an unknown in place of it. That is a different
    trade from the fail-closed default, and deliberately so.
    """
    broken = DeployCadenceCollector(APP, GROUP, client=FakeCodeDeploy(fail="list"), now=NOW)

    result = PipelineChangeContextCollector(pipeline_event(), cadence_collector=broken).collect()

    assert result.status is SignalStatus.OK
    assert result.data.files_changed == 3
    assert result.data.deploys_last_24h is None
    assert result.data.cadence_provenance is Provenance.NONE


def test_a_build_supplied_cadence_is_ignored_even_with_a_collector_present():
    """The build has no legitimate route to this fact, so it may not supply it."""
    cadence = DeployCadenceCollector(
        APP, GROUP, client=FakeCodeDeploy(ids_by_call=[["d-1"]], created=NOW), now=NOW
    )

    change = (
        PipelineChangeContextCollector(
            pipeline_event(deploys_last_24h=99), cadence_collector=cadence
        )
        .collect()
        .data
    )

    assert change.deploys_last_24h == 1
    assert change.cadence_provenance is Provenance.AWS_API
