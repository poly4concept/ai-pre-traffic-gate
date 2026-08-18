"""Tests for the CloudWatch target-health collector. Phase 2.4.

The first group is the reason the collector is not four lines long. CloudWatch
answers an idle function's metrics query with `StatusCode: "Complete"` and an
empty `Values` array -- a successful query containing nothing. `sum(values) or 0`
turns that into a 0% error rate and a 0ms p99, which is not merely a missing
signal but a glowing one.

Fake client throughout; the behaviour under test is our interpretation of
CloudWatch's responses, not boto3's serialisation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from signals import SignalStatus, TargetHealthCloudWatchCollector

FUNCTION = "ai-pre-traffic-gate-demo-app"
NOW = datetime(2026, 8, 17, 16, 46, tzinfo=UTC)


def metric_result(qid: str, values: list[float] | None, status: str = "Complete") -> dict:
    return {"Id": qid, "StatusCode": status, "Values": values if values is not None else []}


def alarm(name: str, state: str, function: str = FUNCTION, reason: str = "") -> dict:
    return {
        "AlarmName": name,
        "StateValue": state,
        "StateReason": reason,
        "Dimensions": [{"Name": "FunctionName", "Value": function}],
    }


class FakeCloudWatch:
    def __init__(
        self,
        inv: list[float] | None = None,
        err: list[float] | None = None,
        dur: list[float] | None = None,
        thr: list[float] | None = None,
        statuses: dict | None = None,
        alarms: list[dict] | None = None,
        messages: list[dict] | None = None,
    ):
        self._data = {"inv": inv, "err": err, "dur": dur, "thr": thr}
        self._statuses = statuses or {}
        self._alarms = alarms if alarms is not None else []
        self._messages = messages or []
        self.calls: list[str] = []

    def get_metric_data(self, **kwargs):
        self.calls.append("get_metric_data")
        self.last_queries = kwargs["MetricDataQueries"]
        self.window = (kwargs["StartTime"], kwargs["EndTime"])
        return {
            "MetricDataResults": [
                metric_result(qid, vals, self._statuses.get(qid, "Complete"))
                for qid, vals in self._data.items()
            ],
            "Messages": self._messages,
        }

    def describe_alarms(self, **_):
        self.calls.append("describe_alarms")
        return {"MetricAlarms": self._alarms}


def collect(fake, **kwargs):
    return TargetHealthCloudWatchCollector(FUNCTION, client=fake, now=NOW, **kwargs).collect()


# --- THE trap -------------------------------------------------------------


def test_an_idle_function_has_no_error_rate_rather_than_zero():
    """The exact response the real account gave: Complete, and empty.

    A 0% error rate and 0ms p99 for an uninvoked function is not a missing
    signal, it is a fabricated excellent one.
    """
    result = collect(FakeCloudWatch(alarms=[alarm("errors", "OK")]))

    assert result.data.invocations_last_hour == 0
    assert result.data.error_rate_pct is None
    assert result.data.p99_latency_ms is None
    assert not result.data.has_health_evidence


def test_an_idle_function_produces_a_degraded_signal_explaining_why():
    result = collect(FakeCloudWatch(alarms=[alarm("errors", "OK")]))

    assert result.status is SignalStatus.DEGRADED
    assert "undefined rather than zero" in result.error
    # Degraded still carries the data -- 0 invocations is itself a real fact.
    assert result.data is not None
    assert result.is_usable


def test_measured_health_and_absent_health_are_distinguishable():
    """Both look like an absence of problems. Only one is evidence."""
    idle = collect(FakeCloudWatch(alarms=[alarm("errors", "OK")]))
    busy = collect(
        FakeCloudWatch(
            inv=[40000.0], err=[8.0], dur=[180.0], thr=[0.0], alarms=[alarm("errors", "OK")]
        )
    )

    assert idle.data.error_rate_pct is None
    assert not idle.data.has_health_evidence

    assert busy.data.error_rate_pct == pytest.approx(0.02)
    assert busy.data.has_health_evidence


def test_invocations_without_an_errors_datapoint_leaves_the_rate_unknown():
    """Guessing zero errors would invent the most reassuring possible answer."""
    result = collect(
        FakeCloudWatch(
            inv=[1000.0], err=None, dur=[200.0], thr=[0.0], alarms=[alarm("errors", "OK")]
        )
    )

    assert result.data.invocations_last_hour == 1000
    assert result.data.error_rate_pct is None
    assert result.status is SignalStatus.DEGRADED
    assert "error rate unknown" in result.error


# --- Alarm coverage -------------------------------------------------------


def test_no_alarms_configured_is_not_the_same_as_nothing_firing():
    """An empty alarm list means whichever of the two you assume it means."""
    result = collect(FakeCloudWatch(inv=[100.0], err=[0.0], dur=[50.0], thr=[0.0], alarms=[]))

    assert result.data.has_alarm_coverage is False
    assert result.status is SignalStatus.DEGRADED
    assert "no CloudWatch alarms are configured" in result.error
    assert "unknown, not clear" in result.error


def test_alarms_present_and_quiet_is_real_evidence():
    result = collect(
        FakeCloudWatch(
            inv=[100.0],
            err=[0.0],
            dur=[50.0],
            thr=[0.0],
            alarms=[alarm("errors", "OK"), alarm("latency", "OK")],
        )
    )

    assert result.status is SignalStatus.OK
    assert result.data.has_alarm_coverage is True
    assert not result.data.has_active_alarm


def test_an_active_alarm_is_surfaced():
    result = collect(
        FakeCloudWatch(
            inv=[38500.0],
            err=[2800.0],
            dur=[3200.0],
            thr=[0.0],
            alarms=[alarm("errors", "ALARM", reason="Threshold Crossed")],
        )
    )

    assert result.data.has_active_alarm
    assert result.data.alarms_in_alarm[0].reason == "Threshold Crossed"


def test_insufficient_data_alarm_is_not_an_active_alarm_nor_reassurance():
    result = collect(
        FakeCloudWatch(
            inv=[5.0],
            err=[0.0],
            dur=[40.0],
            thr=[0.0],
            alarms=[alarm("errors", "INSUFFICIENT_DATA")],
        )
    )

    assert not result.data.has_active_alarm
    assert result.data.has_alarm_coverage is True
    assert result.data.alarms[0].state == "INSUFFICIENT_DATA"


def test_alarms_for_other_functions_are_ignored():
    """Matched on the metric dimension, not the alarm name.

    An alarm called `demo-app-errors` that actually watches a different function
    would otherwise be reported as evidence about this one.
    """
    result = collect(
        FakeCloudWatch(
            inv=[100.0],
            err=[0.0],
            dur=[50.0],
            thr=[0.0],
            alarms=[alarm("demo-app-errors", "ALARM", function="some-other-function")],
        )
    )

    assert result.data.alarms == ()
    assert result.data.has_alarm_coverage is False


def test_metric_math_alarm_dimensions_are_found():
    """Dimensions live under Metrics[] for metric-math alarms."""
    fake = FakeCloudWatch(
        inv=[100.0],
        err=[0.0],
        dur=[50.0],
        thr=[0.0],
        alarms=[
            {
                "AlarmName": "math-alarm",
                "StateValue": "ALARM",
                "StateReason": "",
                "Metrics": [
                    {
                        "MetricStat": {
                            "Metric": {"Dimensions": [{"Name": "FunctionName", "Value": FUNCTION}]}
                        }
                    }
                ],
            }
        ],
    )

    result = collect(fake)

    assert result.data.has_active_alarm


# --- CloudWatch status codes ----------------------------------------------


def test_partial_data_is_reported_as_a_floor():
    result = collect(
        FakeCloudWatch(
            inv=[500.0],
            err=[1.0],
            dur=[100.0],
            thr=[0.0],
            statuses={"inv": "PartialData"},
            alarms=[alarm("errors", "OK")],
        )
    )

    assert result.status is SignalStatus.DEGRADED
    assert "PartialData" in result.error
    assert "floor" in result.error


def test_an_internal_error_on_one_metric_does_not_fabricate_a_value():
    result = collect(
        FakeCloudWatch(
            inv=[500.0],
            err=[1.0],
            dur=[100.0],
            thr=[0.0],
            statuses={"dur": "InternalError"},
            alarms=[alarm("errors", "OK")],
        )
    )

    assert result.data.p99_latency_ms is None
    assert result.status is SignalStatus.DEGRADED
    assert "StatusCode=InternalError" in result.error


def test_a_missing_query_result_is_unavailable():
    """If CloudWatch omits a metric entirely we cannot describe health."""

    class Incomplete(FakeCloudWatch):
        def get_metric_data(self, **kwargs):
            self.calls.append("get_metric_data")
            return {"MetricDataResults": [metric_result("inv", [1.0])]}

    result = collect(Incomplete())

    assert result.status is SignalStatus.UNAVAILABLE
    assert result.data is None
    assert "no result for" in result.error


def test_cloudwatch_messages_are_surfaced():
    result = collect(
        FakeCloudWatch(
            inv=[10.0],
            err=[0.0],
            dur=[20.0],
            thr=[0.0],
            messages=[{"Code": "MaxMetricsExceeded", "Value": "..."}],
            alarms=[alarm("errors", "OK")],
        )
    )

    assert "MaxMetricsExceeded" in result.error


# --- Query construction ---------------------------------------------------


def test_the_window_matches_the_configured_minutes():
    fake = FakeCloudWatch(inv=[1.0], err=[0.0], dur=[1.0], thr=[0.0], alarms=[alarm("e", "OK")])

    collect(fake, window_minutes=30)

    start, end = fake.window
    assert (end - start).total_seconds() == 30 * 60
    assert end == NOW


def test_p99_is_requested_for_duration():
    fake = FakeCloudWatch(inv=[1.0], err=[0.0], dur=[1.0], thr=[0.0], alarms=[alarm("e", "OK")])

    collect(fake)

    stats = {q["Id"]: q["MetricStat"]["Stat"] for q in fake.last_queries}
    assert stats["dur"] == "p99"
    assert stats["inv"] == "Sum"


def test_all_metrics_come_from_a_single_api_call():
    """Three separate calls would triple every verdict's latency."""
    fake = FakeCloudWatch(inv=[1.0], err=[0.0], dur=[1.0], thr=[0.0], alarms=[alarm("e", "OK")])

    collect(fake)

    assert fake.calls.count("get_metric_data") == 1


# --- Throttles count as errors --------------------------------------------


def test_throttles_are_counted_in_the_error_rate():
    """A throttled invocation is a failed request from a caller's perspective."""
    result = collect(
        FakeCloudWatch(inv=[1000.0], err=[10.0], dur=[100.0], thr=[40.0], alarms=[alarm("e", "OK")])
    )

    assert result.data.error_rate_pct == pytest.approx(5.0)


# --- API failure ----------------------------------------------------------


def test_an_api_failure_is_unavailable():
    class Broken(FakeCloudWatch):
        def get_metric_data(self, **_):
            raise RuntimeError("AccessDeniedException")

    result = collect(Broken())

    assert result.status is SignalStatus.UNAVAILABLE
    assert result.data is None
    assert "AccessDeniedException" in result.error


def test_an_alarm_api_failure_is_unavailable():
    class BrokenAlarms(FakeCloudWatch):
        def describe_alarms(self, **_):
            raise RuntimeError("ThrottlingException")

    result = collect(BrokenAlarms(inv=[1.0], err=[0.0], dur=[1.0], thr=[0.0]))

    assert result.status is SignalStatus.UNAVAILABLE
    assert "ThrottlingException" in result.error
