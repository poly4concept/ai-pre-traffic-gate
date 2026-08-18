"""Live target health from Amazon CloudWatch. Phase 2.4.

This is the signal a test suite structurally cannot provide, and the reason the
project exists. Tests tell you the code is correct. They cannot tell you the
service you are about to deploy into is currently on fire.

THE TRAP HERE IS THE SHARPEST OF THE THREE, and it was found by running the
query rather than reading the docs:

    aws cloudwatch get-metric-data ...
    [["inv", "Complete", []], ["err", "Complete", []], ["dur", "Complete", []]]

`StatusCode: "Complete"` — the query succeeded. `Values: []` — there is nothing
in it. The demo app simply had not been invoked in the window.

The natural implementation is `sum(values) or 0`, and it yields:

    error rate   0%
    p99 latency  0ms
    invocations  0

A flawless health report for a function nobody has called. Worse than Inspector's
empty list, because these numbers do not merely fail to raise a concern — a 0%
error rate and a 0ms p99 look like *positive evidence of excellent health*, and
`StatusCode: Complete` invites you to trust them.

So this collector:

  * returns `None` rather than 0 for rates it cannot compute -- an error rate is
    a ratio, and a ratio with a zero denominator is undefined, not zero;
  * checks per-query `StatusCode`, because `PartialData` means the window was
    not fully covered and the numbers are a floor;
  * distinguishes "monitored, nothing firing" from "not monitored at all", since
    an empty alarm list means whichever of those you assume it means.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .base import PartialSignal
from .collectors import TargetHealthCollector
from .types import Alarm, TargetHealth

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_MINUTES = 60

# CloudWatch per-query status codes. "Complete" means the query ran, NOT that it
# found anything -- the distinction this whole module is built around.
STATUS_COMPLETE = "Complete"
STATUS_PARTIAL = "PartialData"

# Alarm states that constitute evidence about current health.
STATE_ALARM = "ALARM"
STATE_OK = "OK"
STATE_INSUFFICIENT = "INSUFFICIENT_DATA"


class MetricsUnavailableError(RuntimeError):
    """CloudWatch could not answer the metrics query."""


class TargetHealthCloudWatchCollector(TargetHealthCollector):
    """Collects Lambda health metrics and alarm state for one function."""

    def __init__(
        self,
        function_name: str,
        client: Any = None,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
        now: datetime | None = None,
    ) -> None:
        self._function_name = function_name
        self._client = client
        self._window_minutes = window_minutes
        self._now = now

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "cloudwatch",
                config=Config(
                    retries={"max_attempts": 2, "mode": "standard"},
                    connect_timeout=3,
                    read_timeout=int(self.timeout_seconds),
                ),
            )
        return self._client

    # --- Metrics ----------------------------------------------------------

    def _metric_queries(self) -> list[dict[str, Any]]:
        """One GetMetricData call for all three metrics.

        GetMetricData rather than the older GetMetricStatistics: it batches
        several metrics into a single request, and it supports percentile
        statistics directly. Three separate calls would triple the latency of
        every verdict for no benefit.
        """
        period = self._window_minutes * 60
        dimensions = [{"Name": "FunctionName", "Value": self._function_name}]

        def query(qid: str, metric: str, stat: str) -> dict[str, Any]:
            return {
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Lambda",
                        "MetricName": metric,
                        "Dimensions": dimensions,
                    },
                    "Period": period,
                    "Stat": stat,
                },
                "ReturnData": True,
            }

        return [
            query("inv", "Invocations", "Sum"),
            query("err", "Errors", "Sum"),
            query("dur", "Duration", "p99"),
            query("thr", "Throttles", "Sum"),
        ]

    def _fetch_metrics(self, now: datetime) -> tuple[dict[str, float | None], list[str]]:
        """Return per-metric values and any degradation notes.

        A value is None when CloudWatch returned no datapoints. That is
        deliberately NOT collapsed to zero -- see the module docstring.
        """
        start = now - timedelta(minutes=self._window_minutes)
        resp = self.client.get_metric_data(
            StartTime=start,
            EndTime=now,
            MetricDataQueries=self._metric_queries(),
        )

        values: dict[str, float | None] = {}
        notes: list[str] = []

        for result in resp.get("MetricDataResults", []):
            qid = result.get("Id", "?")
            status = result.get("StatusCode", "")
            points = result.get("Values") or []

            if status == STATUS_PARTIAL:
                notes.append(f"{qid}: CloudWatch returned PartialData; value is a floor")
            elif status != STATUS_COMPLETE:
                # InternalError or something new. We do not know this metric.
                notes.append(f"{qid}: StatusCode={status or 'missing'}")
                values[qid] = None
                continue

            # An empty list here is the whole point. `Complete` plus no
            # datapoints means "nothing happened in this window", which is not
            # the same as "the value was zero".
            values[qid] = float(sum(points)) if points else None

        for message in resp.get("Messages", []) or []:
            code = message.get("Code", "")
            if code:
                notes.append(f"cloudwatch: {code}")

        missing = {"inv", "err", "dur", "thr"} - set(values)
        if missing:
            raise MetricsUnavailableError(
                f"CloudWatch returned no result for {sorted(missing)}; "
                "cannot describe target health"
            )

        return values, notes

    # --- Alarms -----------------------------------------------------------

    def _fetch_alarms(self) -> tuple[tuple[Alarm, ...], bool]:
        """Return alarms for this function, and whether any are configured.

        The second value is the one that matters. `DescribeAlarms` returning an
        empty list is ambiguous: "monitored and quiet" and "not monitored" are
        different facts with opposite implications, and they look identical.
        """
        alarms: list[Alarm] = []
        paginator_pages = 0
        next_token: str | None = None

        while True:
            kwargs: dict[str, Any] = {"AlarmTypes": ["MetricAlarm"], "MaxRecords": 100}
            if next_token:
                kwargs["NextToken"] = next_token
            resp = self.client.describe_alarms(**kwargs)

            for raw in resp.get("MetricAlarms", []):
                if not self._alarm_watches_this_function(raw):
                    continue
                alarms.append(
                    Alarm(
                        name=str(raw.get("AlarmName", "")),
                        state=str(raw.get("StateValue", "")),
                        reason=str(raw.get("StateReason", ""))[:300],
                    )
                )

            next_token = resp.get("NextToken")
            paginator_pages += 1
            if not next_token or paginator_pages >= 5:
                break

        return tuple(alarms), bool(alarms)

    def _alarm_watches_this_function(self, raw: dict[str, Any]) -> bool:
        """Whether an alarm is about our function.

        Matched on the metric's FunctionName dimension rather than on the alarm
        name. Names are a human convention and drift; a dimension is what
        CloudWatch actually evaluates. An alarm named `demo-app-errors` that
        watches a different function would otherwise be reported as evidence
        about this one.
        """
        dimensions = raw.get("Dimensions") or []
        for dim in dimensions:
            if dim.get("Name") == "FunctionName" and dim.get("Value") == self._function_name:
                return True

        # Metric-math and composite-style alarms carry their dimensions inside
        # Metrics[] instead.
        for metric in raw.get("Metrics") or []:
            stat = metric.get("MetricStat") or {}
            for dim in (stat.get("Metric") or {}).get("Dimensions") or []:
                if dim.get("Name") == "FunctionName" and dim.get("Value") == self._function_name:
                    return True
        return False

    # --- Assembly ---------------------------------------------------------

    def _collect(self) -> TargetHealth:
        now = self._now or datetime.now(UTC)

        values, notes = self._fetch_metrics(now)
        alarms, has_coverage = self._fetch_alarms()

        invocations = int(values["inv"] or 0)
        errors = values["err"]
        duration_p99 = values["dur"]
        throttles = values["thr"]

        # The error rate is computed only when both halves of the ratio exist.
        # Requiring the numerator too is not pedantry: if Invocations reported
        # data and Errors did not, we do not know the error count, and guessing
        # zero would be inventing the most reassuring possible answer.
        error_rate: float | None = None
        if invocations > 0 and errors is not None:
            error_rate = 100.0 * (errors + (throttles or 0.0)) / invocations

        health = TargetHealth(
            error_rate_pct=error_rate,
            p99_latency_ms=duration_p99,
            invocations_last_hour=invocations,
            alarms=alarms,
            window_minutes=self._window_minutes,
            has_alarm_coverage=has_coverage,
        )

        # Degradation reasons, worst first. Each one is a specific statement
        # about what this signal cannot tell you.
        reasons: list[str] = []
        if not has_coverage:
            reasons.append(
                f"no CloudWatch alarms are configured for {self._function_name}; "
                "current alarm state is unknown, not clear"
            )
        if invocations == 0:
            reasons.append(
                f"no invocations in the last {self._window_minutes} minutes; "
                "error rate and latency are undefined rather than zero"
            )
        elif error_rate is None:
            reasons.append("invocations recorded but no Errors datapoint; error rate unknown")
        reasons.extend(notes)

        if reasons:
            raise PartialSignal(health, "; ".join(reasons))
        return health
