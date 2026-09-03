"""Tests for the verdict audit record. Phase 3.4.

Four clusters:

  * the record contains what CLAUDE.md constraint 3 asks for, including the raw
    model output BEFORE validation -- the disagreements are the point
  * immutability: a duplicate write is refused, not applied
  * a storage failure degrades the audit trail rather than halting a judged deploy
  * DynamoDB serialisation, where floats and empty strings are both traps
"""

from __future__ import annotations

import json
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
    MAX_FIELD_CHARS,
    PROMPT_VERSION,
    Action,
    AuditWriteStatus,
    ModelCall,
    RiskLevel,
    Verdict,
    VerdictAuditWriter,
    VerdictSource,
    build_record,
    parse_verdict,
)

TABLE = "ai-pre-traffic-gate-verdicts"
FIXED_NOW = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
JOB_ID = "11111111-2222-3333-4444-555555555555"

RAW_OUTPUT = {
    "risk_level": "medium",
    "confidence": 0.72,
    "reasoning": "Large diff touching payments during an active alarm.",
    "primary_concerns": ["active alarm on target"],
}


def bundle(change=None, security=None, health=None, **kwargs):
    return collect_signals(
        target=sc.DEMO_TARGET,
        change_collector=MockChangeContextCollector(
            change if change is not None else sc.RISKY_PAYMENTS_CHANGE, **kwargs
        ),
        security_collector=MockSecurityFindingsCollector(
            security if security is not None else sc.NO_FINDINGS
        ),
        health_collector=MockTargetHealthCollector(
            health if health is not None else sc.TARGET_IN_ALARM
        ),
        now=FIXED_NOW,
    )


def call(**overrides):
    base = {
        "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "prompt_version": PROMPT_VERSION,
        "attempts": 1,
        "succeeded": True,
        "latency_ms": 840,
        "input_tokens": 1200,
        "output_tokens": 95,
        "stop_reason": "tool_use",
    }
    base.update(overrides)
    return ModelCall(**base)


def record(**overrides):
    kwargs = {
        "verdict_id": JOB_ID,
        "bundle": bundle(),
        "verdict": parse_verdict(RAW_OUTPUT),
        "call": call(),
        "mode": "shadow",
        "action_taken": "none",
        "raw_model_output": RAW_OUTPUT,
        "pipeline_execution_id": "exec-1",
        "now": FIXED_NOW,
    }
    kwargs.update(overrides)
    return build_record(**kwargs)


class FakeDynamo:
    """Records put_item calls, or raises a queued exception."""

    def __init__(self, raises=None):
        self._raises = raises
        self.puts: list[dict] = []

    def put_item(self, **kwargs):
        self.puts.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return {}


def aws_error(code: str):
    exc = Exception(f"An error occurred ({code})")
    exc.response = {"Error": {"Code": code, "Message": "boom"}}
    return exc


# --- The record contains what constraint 3 asks for -----------------------


def test_the_record_carries_the_verdict_and_the_action_taken():
    item = record()

    assert item["verdict"]["risk_level"] == "medium"
    assert item["verdict"]["action"] == str(Action.CANARY)
    assert item["mode"] == "shadow"
    assert item["action_taken"] == "none"


def test_the_record_carries_the_signals_it_was_based_on():
    item = record()

    assert item["signals"]["signals"]["change_context"]["status"] == "ok"
    assert item["signals"]["signals"]["target_health"]["status"] == "ok"
    assert item["signals"]["completeness"]["has_required_signals"] is True


def test_the_record_carries_the_rendered_prompt_but_not_the_system_prompt():
    """Version pointer, not a copy.

    The system prompt is 2.5 KB of text that is identical in every record and
    already versioned in git. `prompt_version` answers "what was it asked?"
    without doubling the item size. The rendered USER message IS stored, because
    that changes every run and cannot be rebuilt from the signals once the
    renderer changes.
    """
    item = record()

    assert "<deployment_target>" in item["prompt"]
    assert "You are a deployment risk assessor" not in item["prompt"]
    assert item["model_call"]["prompt_version"] == PROMPT_VERSION


def test_the_raw_model_output_is_kept_alongside_the_validated_verdict():
    """They disagree sometimes, and the disagreements are the interesting part."""
    item = record()

    assert json.loads(item["raw_model_output"]) == RAW_OUTPUT


def test_a_rejected_model_output_is_still_recorded():
    """The record that proves the design, and the one worth showing an audience.

    The model said "critical", validation refused it, the gate failed closed.
    A record holding only the validated verdict could not tell you any of that.
    """
    rejected = {**RAW_OUTPUT, "risk_level": "critical"}
    item = record(
        verdict=Verdict.fail_closed("invalid_verdict:risk_level"),
        call=call(succeeded=False, failure_kind="invalid_verdict:risk_level"),
        raw_model_output=rejected,
    )

    assert json.loads(item["raw_model_output"])["risk_level"] == "critical"
    assert item["verdict"]["risk_level"] == "high"
    assert item["verdict"]["source"] == str(VerdictSource.FAIL_CLOSED)
    assert item["model_call"]["failure_kind"] == "invalid_verdict:risk_level"


def test_the_record_is_keyed_for_both_lookup_patterns():
    """By job for the executor; by service and time for the demo and Phase 8."""
    item = record()

    assert item["verdict_id"] == JOB_ID
    assert item["service_name"] == sc.DEMO_TARGET.service_name
    assert item["recorded_at"] == FIXED_NOW.isoformat()


def test_the_commit_sha_is_denormalised_onto_the_record():
    """So a human can find the verdict for a commit without parsing signals."""
    item = record()

    assert item["commit_sha"] == sc.RISKY_PAYMENTS_CHANGE.commit_sha


def test_a_record_can_be_built_when_change_context_is_missing():
    """The fail-closed path still has to produce an auditable record.

    If build_record raised here, the one case most worth auditing -- the gate
    halting because it could not see the change -- would be the case that
    recorded nothing.
    """
    incomplete = collect_signals(
        target=sc.DEMO_TARGET,
        change_collector=MockChangeContextCollector(raises=RuntimeError("no context")),
        security_collector=MockSecurityFindingsCollector(sc.NO_FINDINGS),
        health_collector=MockTargetHealthCollector(sc.HEALTHY_TARGET),
        now=FIXED_NOW,
    )

    item = record(
        bundle=incomplete,
        verdict=Verdict.fail_closed("required signals are missing"),
        call=call(succeeded=False, attempts=0, failure_kind="required_signals_missing"),
        raw_model_output=None,
    )

    assert item["commit_sha"] == ""
    assert item["verdict"]["risk_level"] == str(RiskLevel.HIGH)


def test_the_record_is_json_serialisable():
    assert json.loads(json.dumps(record()))


# --- Immutability ---------------------------------------------------------


def test_the_write_is_conditional_on_the_record_not_existing():
    fake = FakeDynamo()

    VerdictAuditWriter(TABLE, fake).record(record())

    assert fake.puts[0]["ConditionExpression"] == "attribute_not_exists(verdict_id)"
    assert fake.puts[0]["TableName"] == TABLE


def test_a_duplicate_write_is_a_success_not_a_failure():
    """CodePipeline can invoke a Lambda twice for one job.

    The first verdict is the one that counts, and "already recorded" is the state
    we wanted -- so it is `ok`, not an error to be retried into a contradiction.
    """
    fake = FakeDynamo(raises=aws_error("ConditionalCheckFailedException"))

    result = VerdictAuditWriter(TABLE, fake).record(record())

    assert result.status == AuditWriteStatus.DUPLICATE
    assert result.ok


def test_a_second_write_never_overwrites_the_first():
    fake = FakeDynamo(raises=aws_error("ConditionalCheckFailedException"))
    writer = VerdictAuditWriter(TABLE, fake)

    writer.record(record())
    writer.record(record(verdict=Verdict.fail_closed("a different answer")))

    # Both attempts carried the guard, so neither could have replaced the other.
    assert all(p["ConditionExpression"] == "attribute_not_exists(verdict_id)" for p in fake.puts)


# --- A storage failure does not halt a judged deploy ----------------------


@pytest.mark.parametrize(
    "boom",
    [
        aws_error("ProvisionedThroughputExceededException"),
        aws_error("AccessDeniedException"),
        aws_error("ResourceNotFoundException"),
        aws_error("ValidationException"),
        ConnectionError("connection reset"),
        TimeoutError("timed out"),
        RuntimeError("unexpected"),
    ],
    ids=lambda e: getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__,
)
def test_no_exception_escapes_record(boom):
    """The verdict is already decided by the time this runs.

    Failing the pipeline because DynamoDB was briefly unavailable would halt a
    deploy the gate had just judged safe, on the basis of a storage problem that
    says nothing at all about the change. Same reasoning as D-030.
    """
    result = VerdictAuditWriter(TABLE, FakeDynamo(raises=boom)).record(record())

    assert result.status == AuditWriteStatus.FAILED
    assert not result.ok
    assert result.error


def test_a_failed_write_reports_why():
    fake = FakeDynamo(raises=aws_error("AccessDeniedException"))

    result = VerdictAuditWriter(TABLE, fake).record(record())

    assert "AccessDeniedException" in result.error


# --- DynamoDB serialisation ----------------------------------------------


def test_floats_are_serialised_as_numbers_not_rejected():
    """The trap boto3's TypeSerializer sets: it RAISES on a Python float.

    These records are full of floats -- confidence, error rate, p99 latency,
    hours since last deploy -- so something has to make the Decimal conversion
    explicitly. `Decimal(str(x))` keeps the decimal representation rather than
    the binary one, so 0.72 stores as "0.72" and not 0.71999999999999997.
    """
    fake = FakeDynamo()

    VerdictAuditWriter(TABLE, fake).record(record())

    item = fake.puts[0]["Item"]
    assert item["verdict"]["M"]["confidence"] == {"N": "0.72"}


def test_an_absent_confidence_is_null_and_an_empty_string_is_a_string():
    """Absent and empty are different facts, in storage as everywhere else.

    NULL means the field had no value -- a fail-closed verdict never received a
    confidence. "" means the value was present and empty. Collapsing them would
    lose exactly the distinction the whole project turns on.
    """
    fake = FakeDynamo()
    item = record(verdict=Verdict.fail_closed("throttled"), raw_model_output=None)

    VerdictAuditWriter(TABLE, fake).record(item)

    stored = fake.puts[0]["Item"]
    assert stored["verdict"]["M"]["confidence"] == {"NULL": True}
    assert stored["raw_model_output"] == {"S": ""}


def test_booleans_are_bools_and_not_numbers():
    """isinstance(True, int) again -- checked before the numeric branch."""
    fake = FakeDynamo()

    VerdictAuditWriter(TABLE, fake).record(record())

    assert fake.puts[0]["Item"]["model_call"]["M"]["succeeded"] == {"BOOL": True}


def test_lists_and_nested_maps_survive():
    fake = FakeDynamo()

    VerdictAuditWriter(TABLE, fake).record(record())

    concerns = fake.puts[0]["Item"]["verdict"]["M"]["primary_concerns"]
    assert concerns == {"L": [{"S": "active alarm on target"}]}


def test_unserialisable_raw_output_still_stores_rather_than_breaking_the_write():
    """The field exists to hold whatever the model produced, including nonsense.

    Storing it as a typed map would mean converting something already known to be
    malformed, which can fail on exactly the inputs worth keeping. A string
    always stores.
    """
    item = record(raw_model_output={"weird": {1, 2, 3}})

    assert isinstance(item["raw_model_output"], str)
    assert item["raw_model_output"]


def test_oversized_fields_are_clipped_so_a_hostile_commit_cannot_break_the_write():
    """Item size is an input an attacker has some influence over.

    DynamoDB's hard limit is 400 KB. Without a bound, a commit message large
    enough to blow it would make the write fail -- and thereby remove itself from
    the audit trail, which is a strange and useful thing for a hostile change to
    be able to do.
    """
    item = record(raw_model_output={"reasoning": "x" * (MAX_FIELD_CHARS * 2)})

    assert len(item["raw_model_output"]) <= MAX_FIELD_CHARS + 64
    assert "clipped" in item["raw_model_output"]


# --- The sparse index key -------------------------------------------------
#
# Phase 5.1 made `pipeline_execution_id` the hash key of a GSI so the executor
# can find the verdict for the deploy it is about to perform. DynamoDB allows
# empty strings in ordinary attributes and REJECTS them in key attributes, so
# the previous `or ""` would have failed the whole PutItem for any invocation
# without a CodePipeline job.


@pytest.mark.parametrize("absent", [None, ""])
def test_a_missing_pipeline_execution_id_is_omitted_not_written_as_empty(absent):
    """An empty string in a GSI key attribute fails the entire write.

    Not the field -- the record. So this guards against "the gate silently
    stopped producing an audit trail for manual invokes", which is invisible
    and happens to cover exactly the invocation used to test the gate by hand.

    Both `None` and `""` reach this code by different routes and must behave
    identically.
    """
    item = record(pipeline_execution_id=absent)

    assert "pipeline_execution_id" not in item


def test_a_real_pipeline_execution_id_is_recorded_at_the_top_level():
    """Top level, not nested inside `signals` -- a GSI can only key on a
    top-level attribute, so where this lives is part of the contract."""
    item = record(pipeline_execution_id="exec-abc")

    assert item["pipeline_execution_id"] == "exec-abc"


def test_the_executors_join_key_is_not_the_verdict_id():
    """The two identifiers are easy to confuse and name almost the same thing.

    `verdict_id` is the GATE's CodePipeline job ID. Every pipeline ACTION gets
    its own job ID, so the executor's is a different string for the same
    deploy. If these were ever made equal, the executor's lookup would find
    nothing on every run.
    """
    item = record(verdict_id="job-gate-1", pipeline_execution_id="exec-abc")

    assert item["verdict_id"] != item["pipeline_execution_id"]
