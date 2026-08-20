"""Tests for the Bedrock client. Phase 3.3.

Everything here is offline. No boto3 client is ever constructed -- conftest
raises if one is, and this file passes a fake in instead.

The single most important test in the file is
`test_no_exception_escapes_get_verdict`, which fires every exception type the
call path can produce and asserts the function returns a verdict anyway. That is
the difference between "fail closed" as a design principle and "fail closed" as
a property of the code.

Four clusters:

  * nothing escapes, and every escape route returns a HIGH fail-closed verdict
  * the nine distinct failure modes are told apart in the audit record
  * retry only happens where retrying could help, and the deadline bounds it
  * the model is never asked about a change it was not shown
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from signals import (
    MockChangeContextCollector,
    MockSecurityFindingsCollector,
    MockTargetHealthCollector,
    collect_signals,
)
from signals import scenarios as sc
from verdict import (
    MAX_ATTEMPTS,
    MAX_TOKENS,
    TEMPERATURE,
    VERDICT_TOOL_NAME,
    BedrockVerdictClient,
    ModelResponseError,
    RiskLevel,
    VerdictSource,
    extract_tool_input,
)

MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
FIXED_NOW = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)

GOOD_INPUT = {
    "risk_level": "medium",
    "confidence": 0.7,
    "reasoning": "Large diff into a service with no recent deploys.",
    "primary_concerns": ["1057 lines changed"],
}


def good_response(tool_input=None, **overrides):
    """A realistic Converse response carrying a tool call."""
    response = {
        "stopReason": "tool_use",
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tu-1",
                            "name": VERDICT_TOOL_NAME,
                            "input": GOOD_INPUT if tool_input is None else tool_input,
                        }
                    }
                ],
            }
        },
        "usage": {"inputTokens": 1200, "outputTokens": 95, "totalTokens": 1295},
        "metrics": {"latencyMs": 840},
    }
    response.update(overrides)
    return response


def bundle(change=None, security=None, health=None, **kwargs):
    return collect_signals(
        target=sc.DEMO_TARGET,
        change_collector=MockChangeContextCollector(
            change if change is not None else sc.SAFE_DEPENDENCY_BUMP, **kwargs
        ),
        security_collector=MockSecurityFindingsCollector(
            security if security is not None else sc.NO_FINDINGS
        ),
        health_collector=MockTargetHealthCollector(
            health if health is not None else sc.HEALTHY_TARGET
        ),
        now=FIXED_NOW,
    )


class FakeBedrock:
    """Returns queued responses, or raises queued exceptions, in order."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes) or [good_response()]
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self._outcomes[min(len(self.calls) - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    """Deterministic time. Sleeping advances the clock instead of blocking."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def client(*outcomes, deadline=18.0):
    clock = FakeClock()
    fake = FakeBedrock(*outcomes)
    return (
        BedrockVerdictClient(
            MODEL, fake, deadline_seconds=deadline, sleep=clock.sleep, monotonic=clock.monotonic
        ),
        fake,
        clock,
    )


def aws_error(code: str):
    """A botocore-shaped ClientError without importing botocore."""
    exc = Exception(f"An error occurred ({code})")
    exc.response = {"Error": {"Code": code, "Message": "boom"}}
    return exc


# --- The happy path -------------------------------------------------------


def test_a_valid_response_becomes_a_model_verdict():
    gate, _, _ = client(good_response())

    outcome = gate.get_verdict(bundle())

    assert outcome.verdict.risk_level is RiskLevel.MEDIUM
    assert outcome.verdict.source is VerdictSource.MODEL
    assert outcome.call.succeeded
    assert outcome.call.attempts == 1


def test_operational_metadata_is_captured_for_cost_and_latency_tracking():
    """Phase 8 charts cost per verdict; it needs the token counts to do it."""
    gate, _, _ = client(good_response())

    call = gate.get_verdict(bundle()).call

    assert call.input_tokens == 1200
    assert call.output_tokens == 95
    assert call.latency_ms == 840
    assert call.stop_reason == "tool_use"
    assert call.model_id == MODEL
    assert call.prompt_version


def test_the_request_forces_the_tool_and_pins_temperature():
    gate, fake, _ = client(good_response())

    gate.get_verdict(bundle())

    sent = fake.calls[0]
    assert sent["modelId"] == MODEL
    assert sent["toolConfig"]["toolChoice"] == {"tool": {"name": VERDICT_TOOL_NAME}}
    assert sent["inferenceConfig"] == {"temperature": TEMPERATURE, "maxTokens": MAX_TOKENS}
    assert sent["system"]
    assert sent["messages"][0]["role"] == "user"


# --- Nothing escapes ------------------------------------------------------


@pytest.mark.parametrize(
    "boom",
    [
        aws_error("AccessDeniedException"),
        aws_error("ThrottlingException"),
        aws_error("ValidationException"),
        aws_error("ResourceNotFoundException"),
        aws_error("ModelTimeoutException"),
        aws_error("InternalServerException"),
        aws_error("ServiceUnavailableException"),
        ConnectionError("connection reset by peer"),
        TimeoutError("read timeout"),
        KeyError("output"),
        ValueError("something unexpected"),
        RuntimeError("truly unexpected"),
    ],
    ids=lambda e: getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__,
)
def test_no_exception_escapes_get_verdict(boom):
    """The most important test in the repository.

    Fail closed is only a property of the system if there is no path out of this
    function that is not a verdict. Asserting that against every exception the
    call path can produce is what turns a design principle into a fact.
    """
    gate, _, _ = client(boom, deadline=1.0)

    outcome = gate.get_verdict(bundle())

    assert outcome.verdict.risk_level is RiskLevel.HIGH
    assert outcome.verdict.source is VerdictSource.FAIL_CLOSED
    assert outcome.verdict.is_blocking
    assert not outcome.call.succeeded


def test_a_fail_closed_verdict_names_the_cause_in_its_reasoning():
    """An audit record saying only "it failed" cannot be acted on."""
    gate, _, _ = client(aws_error("AccessDeniedException"))

    verdict = gate.get_verdict(bundle()).verdict

    assert "AccessDeniedException" in verdict.reasoning


def test_a_fail_closed_verdict_still_has_no_confidence():
    gate, _, _ = client(aws_error("ThrottlingException"), deadline=1.0)

    assert gate.get_verdict(bundle()).verdict.confidence is None


# --- The nine failure modes are told apart --------------------------------


def test_a_prose_reply_despite_tool_choice_is_named_as_such():
    """toolChoice is a strong steer, not a guarantee -- same lesson as the schema."""
    prose = {
        "stopReason": "end_turn",
        "output": {"message": {"content": [{"text": "This change looks fine to me."}]}},
    }
    gate, _, _ = client(prose, deadline=1.0)

    call = gate.get_verdict(bundle()).call

    assert call.failure_kind == "no_tool_call"
    assert "This change looks fine" in call.error


def test_hitting_the_token_ceiling_is_not_treated_as_a_usable_answer():
    """Truncated tool arguments can still parse. That is what makes it dangerous."""
    truncated = good_response(stopReason="max_tokens")
    gate, _, _ = client(truncated, deadline=1.0)

    outcome = gate.get_verdict(bundle())

    assert outcome.call.failure_kind == "max_tokens"
    assert outcome.verdict.source is VerdictSource.FAIL_CLOSED


def test_a_content_filter_stop_is_distinguished_from_a_model_failure():
    filtered = {"stopReason": "content_filtered", "output": {"message": {"content": []}}}
    gate, _, _ = client(filtered, deadline=1.0)

    assert gate.get_verdict(bundle()).call.failure_kind == "content_filtered"


def test_calling_the_wrong_tool_fails_closed():
    wrong = good_response()
    wrong["output"]["message"]["content"][0]["toolUse"]["name"] = "something_else"
    gate, _, _ = client(wrong, deadline=1.0)

    assert gate.get_verdict(bundle()).call.failure_kind == "wrong_tool"


def test_two_tool_calls_are_ambiguous_and_fail_closed():
    """Taking the first would be picking one of two disagreeing answers at random."""
    double = good_response()
    block = double["output"]["message"]["content"][0]
    double["output"]["message"]["content"].append({"toolUse": dict(block["toolUse"])})
    gate, _, _ = client(double, deadline=1.0)

    assert gate.get_verdict(bundle()).call.failure_kind == "multiple_tool_calls"


def test_an_invalid_verdict_records_which_field_broke():
    """Phase 4 needs to tell a prompt problem from an infrastructure problem."""
    bad = good_response({**GOOD_INPUT, "risk_level": "critical"})
    gate, _, _ = client(bad, deadline=1.0)

    call = gate.get_verdict(bundle()).call

    assert call.failure_kind == "invalid_verdict:risk_level"


def test_an_aws_error_code_reaches_the_audit_record():
    gate, _, _ = client(aws_error("ValidationException"))

    assert gate.get_verdict(bundle()).call.failure_kind == "aws:ValidationException"


# --- Retry policy ---------------------------------------------------------


def test_a_throttle_then_success_succeeds():
    gate, fake, clock = client(aws_error("ThrottlingException"), good_response())

    outcome = gate.get_verdict(bundle())

    assert outcome.verdict.source is VerdictSource.MODEL
    assert outcome.call.attempts == 2
    assert len(fake.calls) == 2
    assert clock.slept == [0.5]


def test_a_misconfiguration_is_not_retried():
    """Repeating a misconfigured call only makes the pipeline wait longer."""
    gate, fake, _ = client(aws_error("AccessDeniedException"))

    gate.get_verdict(bundle())

    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "code", ["ThrottlingException", "ServiceUnavailableException", "InternalServerException"]
)
def test_transient_aws_errors_are_retried(code):
    gate, fake, _ = client(aws_error(code), good_response())

    gate.get_verdict(bundle())

    assert len(fake.calls) == 2


def test_an_invalid_verdict_is_retried_once_because_models_occasionally_slip():
    """At temperature 0 a model still sometimes emits an out-of-enum value.

    Refusing outright would halt that fraction of deploys for no real reason;
    one retry takes a 1% slip rate to 0.01%. The retry is visible in `attempts`,
    so Phase 4 still sees the underlying rate -- which is what makes the repair
    honest rather than a cover-up.
    """
    bad = good_response({**GOOD_INPUT, "risk_level": "critical"})
    gate, fake, _ = client(bad, good_response())

    outcome = gate.get_verdict(bundle())

    assert outcome.verdict.source is VerdictSource.MODEL
    assert outcome.call.attempts == 2


def test_a_truncated_response_is_not_retried():
    """max_tokens will reproduce identically; retrying only burns the clock."""
    gate, fake, _ = client(good_response(stopReason="max_tokens"), good_response())

    gate.get_verdict(bundle())

    assert len(fake.calls) == 1


def test_the_deadline_stops_retrying_rather_than_an_attempt_count():
    """Attempts are the wrong unit: fast throttles and slow timeouts differ."""
    gate, fake, clock = client(aws_error("ThrottlingException"), deadline=2.0)

    outcome = gate.get_verdict(bundle())

    # 0.5s then 1.5s of backoff exhausts a 2.0s budget before a third sleep.
    assert clock.slept == [0.5]
    assert len(fake.calls) == 2
    assert not outcome.call.succeeded


def test_backoff_grows_between_attempts():
    gate, _, clock = client(aws_error("ThrottlingException"), deadline=100.0)

    gate.get_verdict(bundle())

    assert clock.slept == sorted(clock.slept)
    assert len(set(clock.slept)) > 1


def test_a_systematically_broken_model_is_bounded_by_attempts_not_only_time():
    """The deadline bounds WAITING; MAX_ATTEMPTS bounds SPENDING.

    Not redundant, because they limit different resources and come apart exactly
    where it matters. Every retry of a validation failure means the model
    actually ran and the tokens were actually billed. Under the deadline alone,
    an 18-second budget with this backoff allows roughly seven inferences per
    pipeline run for a model that always misformats its output -- expensive, and
    pointless, since it fails closed at the end regardless.
    """
    always_bad = good_response({**GOOD_INPUT, "risk_level": "critical"})
    gate, fake, _ = client(always_bad, deadline=1000.0)

    outcome = gate.get_verdict(bundle())

    assert len(fake.calls) == MAX_ATTEMPTS
    assert not outcome.call.succeeded


def test_whichever_bound_trips_first_wins():
    """A short deadline stops earlier than the attempt cap would have."""
    gate, fake, _ = client(aws_error("ThrottlingException"), deadline=1.0)

    gate.get_verdict(bundle())

    assert len(fake.calls) < MAX_ATTEMPTS


# --- The model is never asked about a change it was not shown -------------


def test_a_missing_change_context_never_reaches_bedrock():
    """A model asked about a change it was not told about does not refuse.

    It produces a fluent, confident, entirely baseless verdict, and that output
    is indistinguishable from a real one. Not calling is the only safe answer.
    """
    gate, fake, _ = client(good_response())
    incomplete = collect_signals(
        target=sc.DEMO_TARGET,
        change_collector=MockChangeContextCollector(raises=RuntimeError("no change context")),
        security_collector=MockSecurityFindingsCollector(sc.NO_FINDINGS),
        health_collector=MockTargetHealthCollector(sc.HEALTHY_TARGET),
        now=FIXED_NOW,
    )

    outcome = gate.get_verdict(incomplete)

    assert fake.calls == []
    assert outcome.call.attempts == 0
    assert outcome.call.failure_kind == "required_signals_missing"
    assert outcome.verdict.source is VerdictSource.FAIL_CLOSED


def test_missing_optional_signals_still_reach_bedrock():
    """Only change context is required. A degraded bundle is still assessable."""
    gate, fake, _ = client(good_response())
    degraded = collect_signals(
        target=sc.DEMO_TARGET,
        change_collector=MockChangeContextCollector(sc.SAFE_DEPENDENCY_BUMP),
        security_collector=MockSecurityFindingsCollector(raises=RuntimeError("throttled")),
        health_collector=MockTargetHealthCollector(raises=RuntimeError("throttled")),
        now=FIXED_NOW,
    )

    outcome = gate.get_verdict(degraded)

    assert len(fake.calls) == 1
    assert outcome.verdict.source is VerdictSource.MODEL


# --- Response extraction, tested directly ---------------------------------


def test_extract_tool_input_returns_the_arguments():
    assert extract_tool_input(good_response()) == GOOD_INPUT


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        ({"stopReason": "max_tokens"}, "max_tokens"),
        ({"stopReason": "guardrail_intervened"}, "content_filtered"),
        ({"stopReason": "end_turn", "output": {"message": {"content": []}}}, "no_tool_call"),
        (
            {"stopReason": "tool_use", "output": {"message": {"content": "nope"}}},
            "malformed_response",
        ),
    ],
)
def test_extract_tool_input_names_each_failure(response, kind):
    with pytest.raises(ModelResponseError) as exc:
        extract_tool_input(response)

    assert exc.value.kind == kind
