"""Tests for the Phase 1 gate stub.

These are the fail-closed tests. They matter more than their size suggests:
every one of them asserts that some form of misconfiguration produces a HALT,
and collectively they are the evidence for the claim that the gate cannot be
bypassed by accident.

Runs offline with no credentials. The CodePipeline path is not exercised here --
it needs a job context and a stubbed client, and it arrives with the pipeline in
increment 3.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from conftest import load_handler


@dataclass
class FakeContext:
    aws_request_id: str = "test-request-id"


def load(monkeypatch, decision: str | None, mode: str | None):
    """Load the handler with a given env, since values are read at import."""
    for name, value in (("GATE_DECISION", decision), ("GATE_MODE", mode)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return load_handler("gate_stub")


# --- The fail-closed matrix ----------------------------------------------
#
# Every unrecognised, missing, or malformed verdict must halt. Parametrised
# rather than written out so that adding a new way to be wrong is a one-line
# change, and so the list reads as a specification.
@pytest.mark.parametrize(
    "decision",
    [
        None,  # unset
        "",  # set but empty
        "   ",  # whitespace
        "ALLOWED",  # near-miss on the real value
        "yes",  # plausible synonym
        "true",  # plausible synonym
        "1",  # plausible synonym
        "deny",  # right idea, wrong word
        "allow; halt",  # injection-ish
    ],
)
def test_unrecognised_decisions_all_halt(monkeypatch, decision):
    handler = load(monkeypatch, decision, "enforcing")
    verdict = handler.lambda_handler({}, FakeContext())

    assert verdict["decision"] == "halt"
    assert verdict["action_taken"] == "halt_pipeline"
    assert "failing closed" in verdict["decision_reason"]


def test_only_exact_allow_permits(monkeypatch):
    handler = load(monkeypatch, "allow", "enforcing")
    verdict = handler.lambda_handler({}, FakeContext())

    assert verdict["decision"] == "allow"
    assert verdict["action_taken"] == "none"
    assert verdict["would_have_halted"] is False


def test_allow_is_case_and_whitespace_tolerant(monkeypatch):
    """Tolerant of formatting, not of meaning. `` ALLOW `` is still `allow``."""
    handler = load(monkeypatch, "  ALLOW  ", "enforcing")

    assert handler.lambda_handler({}, FakeContext())["decision"] == "allow"


def test_explicit_halt_is_distinguishable_from_failed_closed(monkeypatch):
    """A chosen halt and a defaulted halt must not look identical in the audit.

    Same decision, different reason. Operationally these are unrelated events:
    one is the gate working, the other is the gate not knowing what it was asked.
    """
    chosen = load(monkeypatch, "halt", "enforcing").lambda_handler({}, FakeContext())
    defaulted = load(monkeypatch, None, "enforcing").lambda_handler({}, FakeContext())

    assert chosen["decision"] == defaulted["decision"] == "halt"
    assert "explicitly configured" in chosen["decision_reason"]
    assert "failing closed" in defaulted["decision_reason"]


# --- Mode independence ----------------------------------------------------


@pytest.mark.parametrize("mode", ["shadow", "advisory"])
def test_non_enforcing_modes_never_act(monkeypatch, mode):
    """A halt verdict in shadow or advisory records but takes no action."""
    handler = load(monkeypatch, "halt", mode)
    verdict = handler.lambda_handler({}, FakeContext())

    assert verdict["decision"] == "halt"
    assert verdict["action_taken"] == "none"
    # The field that makes shadow mode measurable rather than merely inert.
    assert verdict["would_have_halted"] is True


def test_enforcing_mode_acts_on_halt(monkeypatch):
    handler = load(monkeypatch, "halt", "enforcing")

    assert handler.lambda_handler({}, FakeContext())["action_taken"] == "halt_pipeline"


@pytest.mark.parametrize("mode", [None, "", "SHADOWY", "off", "disabled", "enforce"])
def test_unrecognised_modes_become_enforcing(monkeypatch, mode):
    """An unreadable mode fails toward acting, not toward silence.

    Note this fails in the opposite direction from an unreadable verdict, and
    both choices point the same way: stop the deploy. A typo must not silently
    disable the gate.
    """
    handler = load(monkeypatch, "halt", mode)
    verdict = handler.lambda_handler({}, FakeContext())

    assert verdict["mode"] == "enforcing"
    assert verdict["action_taken"] == "halt_pipeline"
    assert "mode_warning" in verdict


def test_mode_does_not_alter_the_verdict(monkeypatch):
    """Mode gates the action only. The recorded decision is mode-independent."""
    decisions = {
        mode: load(monkeypatch, "halt", mode).lambda_handler({}, FakeContext())["decision"]
        for mode in ("shadow", "advisory", "enforcing")
    }

    assert set(decisions.values()) == {"halt"}


# --- Audit record ---------------------------------------------------------


def test_verdict_carries_the_fields_the_audit_trail_needs(monkeypatch):
    handler = load(monkeypatch, "allow", "shadow")
    verdict = handler.lambda_handler({}, FakeContext(aws_request_id="abc-123"))

    for field in (
        "schema_version",
        "source",
        "decision",
        "decision_reason",
        "mode",
        "action_taken",
        "would_have_halted",
        "request_id",
        "timestamp",
    ):
        assert field in verdict, f"missing audit field: {field}"

    assert verdict["request_id"] == "abc-123"
    assert verdict["source"] == "hardcoded-stub"


def test_survives_a_context_missing_its_attributes(monkeypatch):
    handler = load(monkeypatch, "allow", "shadow")

    class BareContext:
        pass

    assert handler.lambda_handler({}, BareContext())["request_id"] == "unknown"


def test_internal_failure_halts(monkeypatch):
    """If verdict construction itself breaks, the result is still a halt."""
    handler = load(monkeypatch, "allow", "enforcing")
    monkeypatch.setattr(handler, "build_verdict", lambda _: 1 / 0)

    verdict = handler.lambda_handler({}, FakeContext())

    assert verdict["decision"] == "halt"
    assert verdict["action_taken"] == "halt_pipeline"
    assert "internal error" in verdict["decision_reason"]
