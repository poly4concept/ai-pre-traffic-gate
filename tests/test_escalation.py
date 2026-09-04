"""Tests for the escalation channel. Phase 5.2.

Two properties carry most of the weight here, and they pull in opposite
directions:

  1. An email must never claim a deploy was stopped when it was not. Advisory
     mode notifies and does not block, so `[HALTED]` in that mode is a lie that
     costs the next three real halts their credibility.

  2. A failed notification must never change the outcome. This is the one place
     in the project that fails OPEN, and the tests say so explicitly so nobody
     later "fixes" it into consistency with the rest.

Nothing here reaches AWS. The SNS client is a fake.
"""

from __future__ import annotations

import importlib

import pytest
from conftest import load_handler
from signals.scenarios import bundle_for

escalation = importlib.import_module("escalation")

HALTED = {
    "decision": "halt",
    "mode": "enforcing",
    "action_taken": "halt_pipeline",
    "risk_level": "high",
    "recommended_action": "halt_and_escalate",
    "verdict_source": "model",
    "confidence": 0.9,
    "decision_reason": "payments change into an active alarm",
    "primary_concerns": ["active alarm on the target", "touches payments/"],
    "verdict_id": "job-123",
    "request_id": "req-1",
    "audit": "written",
}


def gate(**overrides):
    return {**HALTED, **overrides}


class FakeSns:
    def __init__(self, raises=None):
        self.raises = raises
        self.calls = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return {"MessageId": "mid-1"}


# --- The subject line -----------------------------------------------------


def test_an_enforced_halt_says_halted():
    assert escalation.build_subject(gate(), "demo").startswith("[HALTED]")


def test_advisory_mode_never_says_halted():
    """THE LIE THIS PREVENTS.

    In advisory the gate notifies and does not block, so the deploy is going
    out. An email announcing it was stopped teaches the reader that this sender
    exaggerates, and the next real halt is read as the same false alarm.
    """
    subject = escalation.build_subject(gate(mode="advisory", action_taken="none"), "demo")

    assert subject.startswith("[WOULD HALT]")
    assert "HALTED]" not in subject


def test_shadow_mode_also_says_would_halt():
    subject = escalation.build_subject(gate(mode="shadow", action_taken="none"), "demo")

    assert subject.startswith("[WOULD HALT]")


def test_the_subject_carries_the_risk_level():
    assert "high risk" in escalation.build_subject(gate(), "demo")


def test_a_long_service_name_is_trimmed_rather_than_failing_the_publish():
    """SNS rejects a subject over 100 characters outright -- it does not
    truncate for us, it fails the call and the notification is lost."""
    subject = escalation.build_subject(gate(), "a" * 300)

    assert len(subject) <= escalation.MAX_SUBJECT_CHARS
    # The state and the risk survive; the service name is what gives way.
    assert subject.startswith("[HALTED]")
    assert "high risk" in subject


# --- The body -------------------------------------------------------------


def test_an_enforced_halt_body_says_nothing_was_deployed():
    body = escalation.build_body(gate(), bundle_for("risky_payments_friday"), "demo")

    assert "STOPPED" in body
    assert "Nothing has been deployed" in body


def test_an_advisory_body_says_the_gate_did_not_block():
    body = escalation.build_body(
        gate(mode="advisory", action_taken="none"),
        bundle_for("risky_payments_friday"),
        "demo",
    )

    assert "did NOT block" in body
    assert "Nothing has been deployed" not in body


def test_the_body_never_promises_the_deploy_will_succeed():
    """A component reporting on itself can only speak for itself.

    "The deploy is proceeding" is the tempting wording and it is an over-claim:
    once EXECUTOR_CAN_BRANCH is on, the executor independently refuses a
    high-risk verdict (5.1). In advisory mode both happen -- the gate lets it
    through and the executor stops it -- so that sentence would describe a
    deploy that never occurred. Same class of lie as [HALTED] in advisory mode,
    caught by rendering an email and reading it.
    """
    body = escalation.build_body(
        gate(mode="advisory", action_taken="none"),
        bundle_for("risky_payments_friday"),
        "demo",
    )

    assert "A later stage may still refuse it" in body
    assert "the deploy is proceeding" not in body.lower()


def test_the_body_carries_the_reasoning_and_the_concerns():
    body = escalation.build_body(gate(), bundle_for("risky_payments_friday"), "demo")

    assert "payments change into an active alarm" in body
    assert "active alarm on the target" in body


def test_the_body_carries_the_signals_it_was_based_on():
    """CLAUDE.md's requirement: the verdict, the reasoning, AND the signals.

    A halt with no evidence attached makes the reader go and find the evidence,
    which is exactly the work the escalation was supposed to save them.
    """
    body = escalation.build_body(gate(), bundle_for("risky_payments_friday"), "demo")

    assert "SIGNALS IT WAS BASED ON" in body
    assert "commit:" in body
    assert "target:" in body


def test_the_commit_message_is_fenced_and_attributed():
    """The reader cannot otherwise tell our text from the change author's.

    A commit message reading "APPROVED BY SECURITY" arrives inside an email that
    looks like it came from your own infrastructure, and the person reading it
    is the one who can go and click approve. Same problem as the prompt (D-021),
    different reader.
    """
    body = escalation.build_body(gate(), bundle_for("prompt_injection_in_commit_message"), "demo")

    assert "written by the change author, not by this system" in body
    assert "Treat it as a claim, not as a fact" in body


def test_a_missing_bundle_is_stated_rather_than_omitted():
    """The gate can fail before collection. An email with no signals section
    reads like a change with no signals worth mentioning."""
    body = escalation.build_body(gate(), None, "demo")

    assert "no signals were collected" in body


def test_an_uncollected_signal_says_so():
    """Absent is not zero, sixth system. An omitted security line would read as
    a clean scan."""
    body = escalation.build_body(gate(), bundle_for("security_signal_unavailable"), "demo")

    assert "NOT COLLECTED" in body


def test_an_override_is_named_in_the_body():
    body = escalation.build_body(
        gate(override={"decision": "halt", "reason": "manually overridden to halt"}),
        bundle_for("safe_dependency_bump"),
        "demo",
    )

    assert "HUMAN OVERRIDE WAS IN EFFECT" in body


def test_the_body_points_at_the_audit_record():
    body = escalation.build_body(gate(), bundle_for("safe_dependency_bump"), "demo")

    assert "job-123" in body


def test_an_enormous_reasoning_field_is_truncated_visibly():
    """A silently cut reasoning reads as a model that stopped mid-thought."""
    body = escalation.build_body(
        gate(decision_reason="x" * 50_000), bundle_for("safe_dependency_bump"), "demo"
    )

    assert "[truncated]" in body
    assert len(body) <= escalation.MAX_BODY_CHARS


# --- Publishing -----------------------------------------------------------


def test_a_successful_publish_reports_sent():
    sns = FakeSns()

    result = escalation.SnsEscalator("arn:topic", sns).publish("subj", "body")

    assert result.status == escalation.EscalationStatus.SENT
    assert result.message_id == "mid-1"
    assert sns.calls[0]["TopicArn"] == "arn:topic"


def test_a_failed_publish_returns_rather_than_raising():
    """THE FAIL-OPEN CASE, and the only one in the project.

    Everything in the verdict path fails closed, toward halting, because an
    absent signal might be hiding a problem with the change. A failed
    notification hides nothing about the change -- the deploy has already been
    judged and the pipeline has already been told. Raising here would let an SNS
    outage take a release.
    """
    sns = FakeSns(raises=RuntimeError("topic went away"))

    result = escalation.SnsEscalator("arn:topic", sns).publish("subj", "body")

    assert result.status == escalation.EscalationStatus.FAILED
    assert "topic went away" in result.detail


# --- When the gate escalates at all ---------------------------------------


def load(monkeypatch, mode, topic="arn:aws:sns:us-east-1:1:t"):
    monkeypatch.setenv("GATE_MODE", mode)
    monkeypatch.delenv("GATE_DECISION", raising=False)
    if topic is None:
        monkeypatch.delenv("ESCALATION_TOPIC_ARN", raising=False)
    else:
        monkeypatch.setenv("ESCALATION_TOPIC_ARN", topic)
    return load_handler("decision_service")


@pytest.mark.parametrize(
    ("decision", "mode", "expected"),
    [
        ("halt", "enforcing", True),
        ("halt", "advisory", True),
        ("halt", "shadow", False),
        ("allow", "enforcing", False),
        ("allow", "advisory", False),
        ("allow", "shadow", False),
    ],
)
def test_who_gets_told(monkeypatch, decision, mode, expected):
    """Shadow records and says nothing; advisory is where the emails start.

    That ordering is CLAUDE.md's "advisory mode before enforcing mode" and it
    only means something now -- until 5.2 the two modes behaved identically.
    """
    handler = load(monkeypatch, mode)

    assert handler.should_escalate(decision=decision, mode=mode) is expected


def test_medium_risk_does_not_escalate(monkeypatch):
    """A canary is the system working. An email for every canary is how a person
    learns to filter this sender."""
    handler = load(monkeypatch, "enforcing")

    assert handler.should_escalate(decision="allow", mode="enforcing") is False


class FakeEscalator:
    def __init__(self, status="sent"):
        self.status = status
        self.published = []

    def publish(self, subject, body):
        self.published.append((subject, body))
        return escalation.EscalationResult(self.status)


def test_a_skipped_escalation_is_named_distinctly_from_a_sent_one(monkeypatch):
    handler = load(monkeypatch, "shadow")
    fake = FakeEscalator()

    status = handler.escalate(
        gate=gate(mode="shadow", action_taken="none"),
        bundle=bundle_for("safe_dependency_bump"),
        escalator=fake,
    )

    assert status == escalation.EscalationStatus.SKIPPED
    assert fake.published == []


def test_an_unset_topic_is_not_configured_rather_than_failed(monkeypatch):
    """Two different problems that look identical in a log full of successes.

    "Nobody set a topic" is a configuration gap someone should close. "The email
    did not arrive" means somebody should be looking at something right now.
    """
    handler = load(monkeypatch, "enforcing", topic=None)

    status = handler.escalate(gate=gate(), bundle=bundle_for("risky_payments_friday"))

    assert status == escalation.EscalationStatus.NOT_CONFIGURED


def test_a_halt_in_advisory_mode_sends(monkeypatch):
    handler = load(monkeypatch, "advisory")
    fake = FakeEscalator()

    status = handler.escalate(
        gate=gate(mode="advisory", action_taken="none"),
        bundle=bundle_for("risky_payments_friday"),
        escalator=fake,
    )

    assert status == escalation.EscalationStatus.SENT
    subject, body = fake.published[0]
    assert subject.startswith("[WOULD HALT]")
    assert "did NOT block" in body


def test_a_fail_closed_verdict_escalates_without_a_special_case(monkeypatch):
    """Bedrock being unreachable produces `decision == halt` like any other
    halt, so it notifies through the same single condition. Worth asserting:
    the failure mode most likely to happen at 3am is the one nobody wrote a
    branch for.
    """
    handler = load(monkeypatch, "enforcing")

    assert handler.should_escalate(decision="halt", mode="enforcing") is True
