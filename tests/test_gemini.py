"""The second provider behaves like the first. Phase 4c.

WHAT THESE TESTS ARE REALLY ASSERTING

Not "Gemini works" -- that needs a key and a network. They assert the thing that
makes a four-model comparison meaningful: **the two providers are asked the same
question and held to the same standard.**

A benchmark where each provider got its own prompt, its own schema, or its own
idea of what counts as a failure measures the adapters, not the models. So:

  1. the prompt text is shared, byte for byte
  2. the schema crosses untranslated
  3. every failure kind has the same name it has on Bedrock
  4. the retry loop, deadline and floor are inherited, not reimplemented

Point 4 is why this file is shorter than `test_bedrock_client.py` and should
stay that way. If it ever needs to re-test the retry budget, the abstraction has
sprung a leak.

Nothing here reaches the network. The SDK response objects are duck-typed
stand-ins, which is also a small guard in its own right: the transport reads
responses through `getattr`, so it cannot depend on a concrete SDK class that a
version bump might rename.
"""

from __future__ import annotations

import pytest
from signals.scenarios import bundle_for
from verdict import RiskLevel, VerdictSource
from verdict.client import ModelResponseError
from verdict.gemini import (
    RETRYABLE_STATUS_CODES,
    GeminiTransport,
    GeminiVerdictClient,
    extract_function_call,
    verdict_function_declaration,
)
from verdict.prompt import SYSTEM_PROMPT, render_bundle, render_correction
from verdict.schema import VERDICT_SCHEMA, VERDICT_TOOL_NAME

GOOD_ARGS = {
    "risk_level": "medium",
    "confidence": 0.8,
    "reasoning": "A dependency bump into a healthy target, with one open finding.",
    "primary_concerns": ["one open critical finding"],
}


# --- Duck-typed stand-ins for the SDK's response objects --------------------


class FakeCall:
    def __init__(self, name=VERDICT_TOOL_NAME, args=None):
        self.name = name
        self.args = GOOD_ARGS if args is None else args


class FakePart:
    def __init__(self, function_call=None, text=None):
        self.function_call = function_call
        self.text = text


class FakeContent:
    def __init__(self, parts):
        self.parts = parts


class FakeFinish:
    def __init__(self, name):
        self.name = name


class FakeCandidate:
    def __init__(self, parts=None, finish="STOP"):
        self.content = FakeContent(parts if parts is not None else [FakePart(FakeCall())])
        self.finish_reason = FakeFinish(finish)


class FakeUsage:
    def __init__(self, prompt=1300, output=150):
        self.prompt_token_count = prompt
        self.candidates_token_count = output


# A sentinel, because `usage=None` is a case these tests need to express: the
# API returning no usage metadata at all is distinct from "the caller did not
# say", and conflating them is the absent-vs-default confusion this whole
# project keeps tripping over.
_DEFAULT = object()


class FakeResponse:
    def __init__(self, candidates=None, usage=_DEFAULT):
        self.candidates = [FakeCandidate()] if candidates is None else candidates
        self.usage_metadata = FakeUsage() if usage is _DEFAULT else usage
        self.prompt_feedback = None


class FakeModels:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [FakeResponse()]
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeGenai:
    def __init__(self, *outcomes):
        self.models = FakeModels(*outcomes)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self):
        self.now += 0.1
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def api_error(code: int):
    """An SDK-shaped error without importing the SDK."""
    exc = Exception(f"HTTP {code}")
    exc.code = code
    return exc


def transport(*outcomes, interval=0.0):
    fake = FakeGenai(*outcomes)
    clock = FakeClock()
    return (
        GeminiTransport(
            "gemini-test",
            fake,
            min_interval_seconds=interval,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        ),
        fake,
        clock,
    )


def bundle():
    return bundle_for("safe_dependency_bump")


# --- The question is the same question -------------------------------------


def test_the_prompt_is_the_shared_prompt_not_a_gemini_variant():
    """THE POINT OF THE WHOLE EXERCISE.

    If this drifts, the eval stops comparing models and starts comparing
    prompts, and nobody reading the results table would be able to tell.
    """
    tr, fake, _ = transport()

    tr.invoke(bundle(), None)

    sent = fake.models.calls[0]
    assert sent["contents"] == render_bundle(bundle())
    assert sent["config"].system_instruction == SYSTEM_PROMPT


def test_a_correction_uses_the_shared_renderer():
    tr, fake, _ = transport()

    tr.invoke(bundle(), "fix your formatting")

    contents = fake.models.calls[0]["contents"]
    assert contents.endswith(render_correction("fix your formatting"))
    assert contents.startswith(render_bundle(bundle()))


def test_the_schema_crosses_untranslated():
    """No adapter, so no second description of the verdict contract to drift.
    `parameters_json_schema` is the field that makes this possible -- the older
    `parameters` field would need VERDICT_SCHEMA rewritten into an OpenAPI
    subset."""
    declaration = verdict_function_declaration()

    assert declaration["parameters_json_schema"] is VERDICT_SCHEMA
    assert declaration["name"] == VERDICT_TOOL_NAME


def test_the_function_call_is_forced_not_merely_requested():
    """Same structural constraint as Bedrock's toolChoice, by a different name.
    ANY forces a call; naming the function leaves no other one to pick."""
    tr, fake, _ = transport()

    tr.invoke(bundle(), None)

    config = fake.models.calls[0]["config"]
    calling = config.tool_config.function_calling_config
    assert calling.mode.name == "ANY"
    assert calling.allowed_function_names == [VERDICT_TOOL_NAME]


def test_temperature_and_token_ceiling_match_the_bedrock_path():
    from verdict.client import MAX_TOKENS, TEMPERATURE

    tr, fake, _ = transport()

    tr.invoke(bundle(), None)

    config = fake.models.calls[0]["config"]
    assert config.temperature == TEMPERATURE
    assert config.max_output_tokens == MAX_TOKENS


# --- Failures have the same names they have on Bedrock ---------------------


def test_the_arguments_come_back_on_the_happy_path():
    assert extract_function_call(FakeResponse()) == GOOD_ARGS


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (FakeResponse(candidates=[]), "content_filtered"),
        (FakeResponse(candidates=[FakeCandidate(finish="MAX_TOKENS")]), "max_tokens"),
        (FakeResponse(candidates=[FakeCandidate(finish="SAFETY")]), "content_filtered"),
        (FakeResponse(candidates=[FakeCandidate(finish="PROHIBITED_CONTENT")]), "content_filtered"),
        (
            FakeResponse(candidates=[FakeCandidate(finish="MALFORMED_FUNCTION_CALL")]),
            "malformed_response",
        ),
        (
            FakeResponse(candidates=[FakeCandidate(parts=[FakePart(text="I think it is fine")])]),
            "no_tool_call",
        ),
        (
            FakeResponse(
                candidates=[FakeCandidate(parts=[FakePart(FakeCall()), FakePart(FakeCall())])]
            ),
            "multiple_tool_calls",
        ),
        (
            FakeResponse(candidates=[FakeCandidate(parts=[FakePart(FakeCall(name="other"))])]),
            "wrong_tool",
        ),
    ],
)
def test_each_failure_keeps_the_name_phase_4_counts(response, kind):
    """The audit record's failure_kind has to mean the same thing on both
    providers, or the two columns of the report cannot be added up."""
    with pytest.raises(ModelResponseError) as exc:
        extract_function_call(response)

    assert exc.value.kind == kind


def test_truncated_arguments_are_refused_even_though_they_might_parse():
    """MAX_TOKENS means the model was mid-call when it hit the ceiling. The
    arguments below may well be valid JSON and may well validate -- while being
    half a sentence of reasoning. Same trap, same branch, as Bedrock."""
    truncated = FakeResponse(
        candidates=[FakeCandidate(parts=[FakePart(FakeCall())], finish="MAX_TOKENS")]
    )

    with pytest.raises(ModelResponseError, match="truncated"):
        extract_function_call(truncated)


# --- Retry classification --------------------------------------------------


@pytest.mark.parametrize("code", sorted(RETRYABLE_STATUS_CODES))
def test_transient_statuses_are_retried(code):
    tr, _, _ = transport()

    kind, retryable = tr.retryable(api_error(code))

    assert retryable
    assert kind == f"gemini:{code}"


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_misconfiguration_is_not_retried(code):
    """Repeating a bad key or a wrong model ID three times only makes the caller
    wait longer to be told the same thing."""
    tr, _, _ = transport()

    _, retryable = tr.retryable(api_error(code))

    assert not retryable


def test_the_free_tier_quota_is_retryable_because_it_is_indistinguishable():
    """429 covers both "slow down" and "nothing until midnight Pacific", and the
    caller cannot tell them apart. The deadline bounds the pointless case, which
    is exactly how Bedrock's ThrottlingException is handled."""
    tr, _, _ = transport()

    assert tr.retryable(api_error(429)) == ("gemini:429", True)


def test_an_unrecognised_exception_is_handed_back_to_the_shared_classifier():
    tr, _, _ = transport()

    assert tr.retryable(ValueError("something else entirely")) is None


# --- Free-tier pacing ------------------------------------------------------


def test_the_first_call_is_not_delayed():
    tr, _, clock = transport(interval=4.5)

    tr.invoke(bundle(), None)

    assert clock.slept == []


def test_later_calls_are_paced_to_stay_inside_the_per_minute_ceiling():
    """22 scenarios fired back to back is a reliable way to turn a sufficient
    quota into a wall of 429s."""
    tr, _, clock = transport(FakeResponse(), FakeResponse(), interval=4.5)

    tr.invoke(bundle(), None)
    tr.invoke(bundle(), None)

    assert clock.slept and clock.slept[0] > 0


def test_a_failed_call_still_paces_the_next_one():
    """Otherwise a run that spends its quota on errors hammers the rate limiter
    at full speed, which is when pacing matters most."""
    tr, _, clock = transport(api_error(429), FakeResponse(), interval=4.5)

    with pytest.raises(Exception, match="429"):
        tr.invoke(bundle(), None)
    tr.invoke(bundle(), None)

    assert clock.slept, "the retry was not paced"


# --- What is inherited, and therefore must not be reimplemented ------------


def test_the_client_never_raises():
    """Same guarantee as the Bedrock client, and it is inherited rather than
    restated -- this test exists to prove the inheritance is wired up, not to
    re-test the loop."""
    client = GeminiVerdictClient("gemini-test", FakeGenai(api_error(403)))

    outcome = client.get_verdict(bundle())

    assert outcome.verdict.risk_level is RiskLevel.HIGH
    assert outcome.verdict.source is VerdictSource.FAIL_CLOSED
    assert outcome.call.failure_kind == "gemini:403"


def test_the_security_floor_applies_on_this_provider_too():
    """The floor lives in the shared client, so a Gemini verdict cannot dodge
    it. If it could, the four-model comparison would be measuring a gate that
    exists on one provider and not the other."""
    low = dict(GOOD_ARGS, risk_level="low")
    client = GeminiVerdictClient(
        "gemini-test",
        FakeGenai(FakeResponse(candidates=[FakeCandidate(parts=[FakePart(FakeCall(args=low))])])),
    )

    outcome = client.get_verdict(bundle_for("critical_cve_no_patch"))

    assert outcome.verdict.risk_level is RiskLevel.MEDIUM
    assert outcome.verdict.was_raised_by_floor


def test_usage_and_latency_are_recorded():
    client = GeminiVerdictClient("gemini-test", FakeGenai())

    outcome = client.get_verdict(bundle())

    assert outcome.call.input_tokens == 1300
    assert outcome.call.output_tokens == 150
    assert outcome.call.latency_ms is not None
    assert outcome.call.succeeded


def test_missing_usage_metadata_is_recorded_as_missing_not_as_zero():
    """An absent token count is absent. A cost chart built from silent zeroes
    understates the bill and nothing in it looks wrong -- the same rule the
    signal collectors follow."""
    client = GeminiVerdictClient("gemini-test", FakeGenai(FakeResponse(usage=None)))

    outcome = client.get_verdict(bundle())

    assert outcome.call.input_tokens is None
    assert outcome.call.output_tokens is None
