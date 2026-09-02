"""Tests for the traffic driver. Phase 2.5b.

The valuable part is the three-way outcome split. `ok` / `faulted` / `refused`
look like bookkeeping and are the difference between "fault injection works" and
"I have no permissions" -- two conclusions that would otherwise look identical
from a loop that only counts requests sent.

Nothing here reaches AWS. The boto3 clients are fakes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from botocore.exceptions import ClientError

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_script_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dt = load("drive_traffic")


def client_error(code: str, message: str = "nope") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Invoke")


class FakePayload:
    """Stands in for the streaming body botocore returns."""

    def __init__(self, body):
        self._body = body

    def read(self):
        return json.dumps({"statusCode": 200, "body": json.dumps(self._body)}).encode()


class FakeLambda:
    """Returns a scripted sequence of outcomes, cycling if exhausted.

    Each element is "ok", "faulted", or an exception to raise.
    """

    def __init__(self, sequence, body=None):
        self._sequence = list(sequence)
        self.calls = []
        # Default mirrors post-2.5a code: a `faults` field is present.
        self.body = {"service": "demo-app", "faults": {"injected": False}} if body is None else body

    def __init_subclass__(cls, **kw):  # pragma: no cover
        super().__init_subclass__(**kw)

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self._sequence[(len(self.calls) - 1) % len(self._sequence)]
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "faulted":
            return {"StatusCode": 200, "FunctionError": "Unhandled"}
        return {"StatusCode": 200, "Payload": FakePayload(self.body)}


class FakeCloudWatch:
    def __init__(self, states_by_poll):
        self._states = list(states_by_poll)
        self.polls = 0

    def describe_alarms(self, **_):
        states = self._states[min(self.polls, len(self._states) - 1)]
        self.polls += 1
        return {
            "MetricAlarms": [
                {"AlarmName": f"alarm-{i}", "StateValue": s, "StateReason": "because"}
                for i, s in enumerate(states)
            ]
        }


# --- The three-way split --------------------------------------------------


def test_a_raised_function_counts_as_faulted_not_as_a_failure_to_invoke():
    """A FunctionError means the function RAN and raised. That is the signal."""
    fake = FakeLambda(["faulted"])

    counts = dt.invoke_many(fake, count=5, interval=0, qualifier="live").counts

    assert counts["faulted"] == 5
    assert counts["ok"] == 0
    assert counts["refused"] == 0


def test_a_successful_invocation_counts_as_ok():
    fake = FakeLambda(["ok"])

    counts = dt.invoke_many(fake, count=4, interval=0, qualifier="live").counts

    assert counts == {"ok": 4}


def test_a_mixed_run_reports_both():
    fake = FakeLambda(["ok", "faulted"])

    counts = dt.invoke_many(fake, count=10, interval=0, qualifier="live").counts

    assert counts["ok"] == 5
    assert counts["faulted"] == 5


def test_a_refused_invoke_is_never_counted_as_a_fault(capsys):
    """The distinction that stops a permissions problem masquerading as success.

    A refused invoke means the function never ran, so it contributes to no
    CloudWatch metric and no alarm can fire from it. Counting it as `faulted`
    would report "fault injection is working" while nothing had been invoked.
    """
    fake = FakeLambda([client_error("ThrottlingException")])

    res = dt.invoke_many(fake, count=3, interval=0, qualifier="live")

    assert res.counts["refused"] == 3
    assert res.counts["faulted"] == 0
    assert res.notes == ["ThrottlingException"]


def test_access_denied_stops_immediately_rather_than_retrying_59_more_times(capsys):
    """Missing permission will not improve on attempt two.

    Also prints the fix, because this is the exact failure the first attempt at
    the 2.5b test hit.
    """
    fake = FakeLambda([client_error("AccessDeniedException", "not authorized")])

    counts = dt.invoke_many(fake, count=60, interval=0, qualifier="live").counts

    assert len(fake.calls) == 1
    assert counts["refused"] == 1
    assert "poly4" in capsys.readouterr().out


def test_the_error_is_reported_once_not_once_per_invocation(capsys):
    """Sixty identical throttling messages would bury the summary."""
    fake = FakeLambda([client_error("ThrottlingException")])

    dt.invoke_many(fake, count=20, interval=0, qualifier="live")

    assert capsys.readouterr().out.count("invoke refused") == 1


# --- What gets invoked ----------------------------------------------------


def test_the_alias_is_invoked_not_latest():
    """`$LATEST` and the alias can be different code AND different config.

    Invoking `$LATEST` would test a version no traffic reaches, which is the
    same photocopy trap that makes the fault switch look broken.
    """
    fake = FakeLambda(["ok"])

    dt.invoke_many(fake, count=1, interval=0, qualifier="live")

    assert fake.calls[0]["FunctionName"] == "ai-pre-traffic-gate-demo-app:live"


def test_a_specific_version_can_be_targeted():
    """Breaking only the canary needs the version, not the alias."""
    fake = FakeLambda(["ok"])

    dt.invoke_many(fake, count=1, interval=0, qualifier="11")

    assert fake.calls[0]["FunctionName"] == "ai-pre-traffic-gate-demo-app:11"


def test_invocations_are_synchronous():
    """Async invokes return before the function runs, so `FunctionError` would
    never be set and the observed error rate would always read 0%."""
    fake = FakeLambda(["ok"])

    dt.invoke_many(fake, count=1, interval=0, qualifier="live")

    assert fake.calls[0]["InvocationType"] == "RequestResponse"


# --- Watching alarms ------------------------------------------------------


def test_watching_returns_true_as_soon_as_an_alarm_fires():
    cw = FakeCloudWatch([["OK", "OK"], ["ALARM", "OK"]])

    assert dt.watch_alarms(cw, timeout_seconds=60, poll_seconds=0) is True
    assert cw.polls == 2


def test_watching_returns_false_on_timeout_rather_than_assuming_success():
    """A timeout is a real answer: the thresholds did not trip.

    Reporting it as success would defeat the entire purpose of tuning them.
    """
    cw = FakeCloudWatch([["INSUFFICIENT_DATA"]])

    assert dt.watch_alarms(cw, timeout_seconds=0, poll_seconds=0) is False


def test_insufficient_data_is_not_treated_as_firing():
    """The Phase 2.5b invariant: not measured is not the same as measured-bad."""
    cw = FakeCloudWatch([["INSUFFICIENT_DATA", "INSUFFICIENT_DATA"]])

    assert dt.watch_alarms(cw, timeout_seconds=0, poll_seconds=0) is False


def test_alarms_are_listed_in_a_stable_order():
    """Output that reorders itself between runs is hard to diff."""
    cw = FakeCloudWatch([["OK", "OK", "OK"]])

    names = [a["AlarmName"] for a in dt.describe_alarms(cw)]

    assert names == sorted(names)


def test_alarm_prefix_scopes_the_query_to_the_demo_app():
    """Without a prefix this would report every alarm in the account."""
    seen = {}

    class Recorder:
        def describe_alarms(self, **kwargs):
            seen.update(kwargs)
            return {"MetricAlarms": []}

    dt.describe_alarms(Recorder())

    assert seen["AlarmNamePrefix"] == "ai-pre-traffic-gate-demo-app"


# --- Guardrails -----------------------------------------------------------


def test_the_function_under_test_is_the_demo_app_not_the_gate():
    """Driving traffic at the GATE would write junk verdict records and cost
    Bedrock tokens. Asserted so a copy-paste cannot quietly repoint it.

    Checked against the gate's own suffix rather than the substring "gate" --
    the project is called ai-pre-traffic-GATE, so that word appears in every
    resource name in the stack and a substring check would always fail.
    """
    assert dt.FUNCTION == "ai-pre-traffic-gate-demo-app"
    assert not dt.FUNCTION.endswith("-gate"), "this is the gate, not the demo app"


@pytest.mark.parametrize("count", [1, 7, 30])
def test_every_requested_invocation_is_attempted(count):
    fake = FakeLambda(["ok"])

    dt.invoke_many(fake, count=count, interval=0, qualifier="live")

    assert len(fake.calls) == count


# --- The diagnosis --------------------------------------------------------
#
# `faulted == 0` had three different causes and no way to tell them apart. This
# is the afternoon that cost, encoded.


def test_nothing_invoked_reports_a_rate_of_none_not_zero():
    """A rate with no denominator is undefined, not 0%.

    Reporting 0% would say "the app is healthy" about an app that was never
    called -- the exact mistake the signals package exists to prevent.
    """
    fake = FakeLambda([client_error("AccessDeniedException")])

    res = dt.invoke_many(fake, count=10, interval=0, qualifier="live")

    assert res.attempted == 0
    assert res.observed_error_rate is None
    assert "Nothing reached the function" in res.diagnose()[0]


def test_old_code_without_a_faults_field_is_diagnosed_as_a_stale_deploy():
    """THE BUG THIS WHOLE METHOD EXISTS FOR.

    Terraform does not own the demo app's code -- the pipeline does -- so an
    apply delivers FAULT_TABLE and the IAM policy while leaving the code
    untouched. The switch ends up wired to nothing, and because the app fails
    SAFE the result is indistinguishable from a healthy app.
    """
    fake = FakeLambda(["ok"], body={"service": "demo-app"})

    res = dt.invoke_many(fake, count=5, interval=0, qualifier="live")

    assert res.fault_field_seen is False
    headline, action = res.diagnose()
    assert "DEPLOYED CODE has no fault support" in headline
    assert "pipeline" in action


def test_new_code_with_injection_off_is_diagnosed_differently():
    """Same symptom, opposite fix. Conflating them is what wasted the time."""
    fake = FakeLambda(["ok"])  # default body carries a `faults` field

    res = dt.invoke_many(fake, count=5, interval=0, qualifier="live")

    assert res.fault_field_seen is True
    headline, action = res.diagnose()
    assert "code supports faults" in headline
    assert "inject_fault.py status" in action


def test_a_working_injection_is_reported_as_working():
    fake = FakeLambda(["faulted", "ok"])

    res = dt.invoke_many(fake, count=10, interval=0, qualifier="live")

    assert res.observed_error_rate == pytest.approx(50.0)
    assert "working" in res.diagnose()[0]


def test_an_unparseable_payload_does_not_abort_the_run():
    """A body we cannot read is a curiosity, not a reason to stop."""

    class Broken:
        def read(self):
            return b"not json"

    class BrokenLambda:
        calls = []

        def invoke(self, **kwargs):
            BrokenLambda.calls.append(kwargs)
            return {"StatusCode": 200, "Payload": Broken()}

    res = dt.invoke_many(BrokenLambda(), count=3, interval=0, qualifier="live")

    assert res.counts["ok"] == 3
    assert res.fault_field_seen is False
