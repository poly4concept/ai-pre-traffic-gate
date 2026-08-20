"""Tests for the verdict contract. Phase 3.1.

These are the highest-stakes tests in the repository. Everything else decides
what the gate knows; this decides what it is allowed to conclude, and it is the
only thing between a language model's output and a decision about production.

Four clusters carry the weight:

  * schema/validator agreement -- the two live in different files and drift
    silently, so agreement is asserted rather than assumed
  * reject vs repair -- the central judgement call in validation.py
  * the fail-closed verdict's invariants, especially that it has no confidence
  * the action mapping, which the model must never be able to reach
"""

from __future__ import annotations

import pytest
from verdict import (
    MAX_CONCERN_CHARS,
    MAX_CONCERNS,
    MAX_REASONING_CHARS,
    VERDICT_FIELDS,
    VERDICT_SCHEMA,
    VERDICT_TOOL_NAME,
    Action,
    RiskLevel,
    Verdict,
    VerdictSource,
    VerdictValidationError,
    action_for,
    parse_verdict,
    verdict_tool_config,
)

GOOD = {
    "risk_level": "medium",
    "confidence": 0.72,
    "reasoning": "Touches auth/ and the target has one alarm in ALARM state.",
    "primary_concerns": ["change touches auth/", "target service unhealthy"],
}


def raw(**overrides):
    return {**GOOD, **overrides}


# --- The schema and the validator must not drift --------------------------


def test_schema_and_validator_agree_on_the_field_set():
    """The schema is in one file and the enforcement in another.

    Nothing about a passing test suite would notice them disagreeing, so the
    agreement is asserted directly. This test failing means somebody added a
    field to one side only.
    """
    assert set(VERDICT_SCHEMA["properties"]) == set(VERDICT_FIELDS)
    assert set(VERDICT_SCHEMA["required"]) == set(VERDICT_FIELDS)


def test_schema_enum_is_generated_from_the_risk_level_enum():
    """A hand-written enum list in the schema would be the drift bug."""
    assert VERDICT_SCHEMA["properties"]["risk_level"]["enum"] == [str(level) for level in RiskLevel]


def test_schema_bounds_are_the_bounds_the_validator_enforces():
    props = VERDICT_SCHEMA["properties"]

    assert props["reasoning"]["maxLength"] == MAX_REASONING_CHARS
    assert props["primary_concerns"]["items"]["maxLength"] == MAX_CONCERN_CHARS
    assert props["primary_concerns"]["maxItems"] == MAX_CONCERNS


def test_schema_forbids_extra_properties():
    assert VERDICT_SCHEMA["additionalProperties"] is False


def test_tool_choice_forces_our_one_tool():
    """Offering a choice of tools would be offering a way to decline."""
    config = verdict_tool_config()

    assert len(config["tools"]) == 1
    assert config["toolChoice"] == {"tool": {"name": VERDICT_TOOL_NAME}}
    assert config["tools"][0]["toolSpec"]["inputSchema"]["json"] is VERDICT_SCHEMA


# --- The happy path -------------------------------------------------------


def test_parses_a_well_formed_verdict():
    verdict = parse_verdict(raw(), model_id="us.amazon.nova-lite-v1:0")

    assert verdict.risk_level is RiskLevel.MEDIUM
    assert verdict.confidence == pytest.approx(0.72)
    assert verdict.primary_concerns == ("change touches auth/", "target service unhealthy")
    assert verdict.source is VerdictSource.MODEL
    assert verdict.model_id == "us.amazon.nova-lite-v1:0"
    assert verdict.truncated_fields == ()


def test_an_empty_concern_list_is_valid_for_a_low_risk_change():
    verdict = parse_verdict(raw(risk_level="low", primary_concerns=[]))

    assert verdict.risk_level is RiskLevel.LOW
    assert verdict.primary_concerns == ()


@pytest.mark.parametrize("value", ["LOW", " low ", "Medium", "HIGH"])
def test_case_and_whitespace_in_risk_level_are_normalised_not_rejected(value):
    """Halting a deploy over capitalisation would be a self-inflicted outage.

    "LOW" is unambiguous. Normalising is safe precisely because there is no
    guessing involved -- contrast the test below, where there would be.
    """
    verdict = parse_verdict(raw(risk_level=value))

    assert verdict.risk_level is RiskLevel(value.strip().lower())


@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0])
def test_confidence_accepts_the_range_endpoints_and_integers(value):
    verdict = parse_verdict(raw(confidence=value))

    assert verdict.confidence == pytest.approx(float(value))


# --- Reject: shape and type errors ----------------------------------------


@pytest.mark.parametrize("field", VERDICT_FIELDS)
def test_a_missing_field_is_rejected(field):
    """Partial verdicts are the ambiguous case that must fail closed."""
    payload = {k: v for k, v in GOOD.items() if k != field}

    with pytest.raises(VerdictValidationError) as exc:
        parse_verdict(payload)

    assert exc.value.field == field
    assert "missing" in str(exc.value)


def test_every_missing_field_is_named_at_once():
    """One prompt fix, not four round trips."""
    with pytest.raises(VerdictValidationError, match="confidence, primary_concerns, reasoning"):
        parse_verdict({"risk_level": "low"})


def test_an_extra_field_is_rejected():
    """Evidence the model answered a different question than it was asked."""
    with pytest.raises(VerdictValidationError) as exc:
        parse_verdict(raw(action="deploy"))

    assert exc.value.field == "action"


@pytest.mark.parametrize("payload", [None, "high", 7, ["high"]])
def test_non_object_tool_input_is_rejected(payload):
    with pytest.raises(VerdictValidationError, match="not an object"):
        parse_verdict(payload)


@pytest.mark.parametrize("value", ["critical", "medium-high", "unknown", "", "safe"])
def test_an_out_of_enum_risk_level_is_rejected(value):
    """Not repairable: there is no safe guess between medium and high."""
    with pytest.raises(VerdictValidationError) as exc:
        parse_verdict(raw(risk_level=value))

    assert exc.value.field == "risk_level"


@pytest.mark.parametrize("value", [None, 2, ["low"], {"level": "low"}])
def test_a_non_string_risk_level_is_rejected(value):
    with pytest.raises(VerdictValidationError):
        parse_verdict(raw(risk_level=value))


def test_a_boolean_confidence_is_rejected_despite_being_an_int():
    """The Python trap: isinstance(True, int) is True.

    Without an explicit bool check, a model returning `true` for a number field
    would validate as 1.0 -- inside the allowed range, and utterly meaningless.
    """
    with pytest.raises(VerdictValidationError, match="bool"):
        parse_verdict(raw(confidence=True))


@pytest.mark.parametrize("value", ["0.9", None, [0.9], {}])
def test_a_non_numeric_confidence_is_rejected(value):
    """Rejected not because the number matters, but because the error does.

    Confidence never routes anything. A type error here is evidence the model
    did not follow the schema, and that evidence applies to the risk_level in
    the same object -- which we do act on.
    """
    with pytest.raises(VerdictValidationError) as exc:
        parse_verdict(raw(confidence=value))

    assert exc.value.field == "confidence"


@pytest.mark.parametrize("value", [-0.1, 1.5, 100])
def test_an_out_of_range_confidence_is_rejected(value):
    with pytest.raises(VerdictValidationError, match="outside the stated range"):
        parse_verdict(raw(confidence=value))


@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_empty_reasoning_is_rejected_as_a_refusal_to_justify(value):
    """An unexplained verdict is useless in an audit trail."""
    with pytest.raises(VerdictValidationError, match="not auditable"):
        parse_verdict(raw(reasoning=value))


@pytest.mark.parametrize("value", [None, 42, ["because"]])
def test_non_string_reasoning_is_rejected(value):
    with pytest.raises(VerdictValidationError) as exc:
        parse_verdict(raw(reasoning=value))

    assert exc.value.field == "reasoning"


@pytest.mark.parametrize("value", ["a concern", None, {"0": "a"}])
def test_non_array_concerns_are_rejected(value):
    with pytest.raises(VerdictValidationError, match="expected an array"):
        parse_verdict(raw(primary_concerns=value))


def test_a_non_string_concern_is_rejected_and_names_its_index():
    with pytest.raises(VerdictValidationError, match=r"primary_concerns\[1\]"):
        parse_verdict(raw(primary_concerns=["fine", 7]))


# --- Repair: length and count overruns ------------------------------------


def test_over_long_reasoning_is_truncated_not_rejected():
    """Models cannot count characters, and that is not confusion.

    Punishing a 610-character answer with a halted deploy would make the gate
    less trustworthy than the thing it replaced.
    """
    verdict = parse_verdict(raw(reasoning="x" * (MAX_REASONING_CHARS + 200)))

    assert len(verdict.reasoning) <= MAX_REASONING_CHARS
    assert verdict.reasoning.endswith("...")
    assert verdict.truncated_fields == ("reasoning",)


def test_truncation_is_recorded_so_the_audit_record_does_not_lie():
    """Without this field, a cut string looks like one written short."""
    untouched = parse_verdict(raw(reasoning="short and sweet"))
    cut = parse_verdict(raw(reasoning="x" * (MAX_REASONING_CHARS + 1)))

    assert untouched.to_dict()["truncated_fields"] == []
    assert cut.to_dict()["truncated_fields"] == ["reasoning"]


def test_reasoning_exactly_at_the_limit_is_left_alone():
    """The off-by-one that would truncate every well-behaved answer."""
    verdict = parse_verdict(raw(reasoning="x" * MAX_REASONING_CHARS))

    assert len(verdict.reasoning) == MAX_REASONING_CHARS
    assert verdict.truncated_fields == ()


def test_an_over_long_concern_is_truncated_within_its_own_bound():
    verdict = parse_verdict(raw(primary_concerns=["y" * (MAX_CONCERN_CHARS + 50)]))

    assert len(verdict.primary_concerns[0]) <= MAX_CONCERN_CHARS
    assert verdict.truncated_fields == ("primary_concerns[0]",)


def test_too_many_concerns_are_trimmed_from_the_tail():
    """The schema asks for most important first, so the tail is what we lose."""
    many = [f"concern {i}" for i in range(MAX_CONCERNS + 4)]

    verdict = parse_verdict(raw(primary_concerns=many))

    assert len(verdict.primary_concerns) == MAX_CONCERNS
    assert verdict.primary_concerns[0] == "concern 0"
    assert "primary_concerns" in verdict.truncated_fields


def test_blank_concerns_are_dropped_rather_than_rejected():
    """A trailing "" is a formatting artefact, not a claim about the deploy."""
    verdict = parse_verdict(raw(primary_concerns=["real concern", "", "   "]))

    assert verdict.primary_concerns == ("real concern",)
    assert verdict.truncated_fields == ()


# --- The fail-closed verdict ----------------------------------------------


def test_fail_closed_is_high_risk_and_halts():
    verdict = Verdict.fail_closed("Bedrock returned ThrottlingException after 3 attempts")

    assert verdict.risk_level is RiskLevel.HIGH
    assert verdict.action is Action.HALT_AND_ESCALATE
    assert verdict.is_blocking


def test_fail_closed_has_no_confidence_rather_than_zero_confidence():
    """The Phase 2 invariant, one layer up.

    The model never supplied a confidence. Recording 0.0 would fabricate a
    measurement that Phase 4 would then average in alongside real ones.
    """
    verdict = Verdict.fail_closed("schema validation failed")

    assert verdict.confidence is None


def test_a_fail_closed_verdict_may_not_claim_a_confidence():
    with pytest.raises(ValueError, match="fabricate a measurement"):
        Verdict(
            risk_level=RiskLevel.HIGH,
            reasoning="throttled",
            source=VerdictSource.FAIL_CLOSED,
            confidence=0.0,
        )


def test_a_model_verdict_must_carry_a_confidence():
    with pytest.raises(ValueError, match="must carry the confidence"):
        Verdict(
            risk_level=RiskLevel.LOW,
            reasoning="looks fine",
            source=VerdictSource.MODEL,
            confidence=None,
        )


def test_every_verdict_must_state_its_basis():
    with pytest.raises(ValueError, match="record why"):
        Verdict(risk_level=RiskLevel.HIGH, reasoning="", source=VerdictSource.FAIL_CLOSED)


def test_source_distinguishes_a_caught_risk_from_a_broken_gate():
    """Identical behaviour, completely different meaning for measurement.

    Without this, a week of Bedrock throttling reads as a week of the gate
    correctly catching risky changes -- the most flattering possible way to be
    wrong.
    """
    broke = Verdict.fail_closed("Bedrock unreachable")
    judged = parse_verdict(raw(risk_level="high"))

    assert broke.action is judged.action
    assert broke.source is not judged.source


def test_a_verdict_is_immutable():
    """A verdict is a record of a decision. Mutating it corrupts the audit."""
    verdict = parse_verdict(raw())

    with pytest.raises(AttributeError):
        verdict.risk_level = RiskLevel.LOW  # type: ignore[misc]


# --- The action mapping ---------------------------------------------------


def test_risk_maps_to_the_actions_claude_md_specifies():
    assert action_for(RiskLevel.LOW) is Action.FULL_DEPLOY
    assert action_for(RiskLevel.MEDIUM) is Action.CANARY
    assert action_for(RiskLevel.HIGH) is Action.HALT_AND_ESCALATE


def test_the_mapping_is_exhaustive_over_the_enum():
    """A fourth risk level must fail loudly here, not deploy quietly.

    `ACTION_FOR_RISK.get(level, FULL_DEPLOY)` would have been shorter and is
    exactly the fail-open bug this project exists to talk about.
    """
    for level in RiskLevel:
        assert action_for(level) is not None


def test_the_model_vocabulary_contains_no_action_words():
    """The structural mitigation against prompt injection.

    If the model could emit "deploy", a successful injection would need to
    produce one token. It can only describe risk; the mapping to an action is
    code it cannot reach.
    """
    schema_text = str(VERDICT_SCHEMA["properties"]["risk_level"]["enum"])

    for action in Action:
        assert str(action) not in schema_text


def test_risk_level_ordering_uses_risk_order_not_comparison():
    """The StrEnum trap, asserted so nobody 'simplifies' it away later.

    Alphabetically "high" < "low", so comparing members directly ranks a
    high-risk change as safer than a low-risk one.
    """
    from verdict import RISK_ORDER

    assert RiskLevel.HIGH < RiskLevel.LOW  # alphabetical, and nonsense
    assert RISK_ORDER[RiskLevel.HIGH] > RISK_ORDER[RiskLevel.LOW]


# --- Audit form -----------------------------------------------------------


def test_to_dict_is_json_safe_and_carries_the_derived_action():
    import json

    verdict = parse_verdict(raw(), model_id="us.amazon.nova-lite-v1:0")
    record = verdict.to_dict()

    assert json.loads(json.dumps(record)) == record
    assert record["risk_level"] == "medium"
    assert record["action"] == "canary"
    assert record["source"] == "model"
