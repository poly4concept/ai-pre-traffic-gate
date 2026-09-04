"""Tests for the human override path. Phase 5.3.

Three properties, in descending order of how much damage getting them wrong
would do:

  1. AN OVERRIDE CANNOT OUTLIVE ITS DEPLOY. The failure that makes
     `gate_decision=allow` dangerous is that it applies to every execution
     until somebody remembers to revert. These are keyed by pipeline execution
     ID and expire on a clock.

  2. THE MOST RESTRICTIVE SOURCE WINS. Not the most specific. A per-execution
     `allow` must not defeat an infrastructure-level `halt`, or a global stop is
     a suggestion.

  3. A MALFORMED OVERRIDE HALTS; AN EXPIRED ONE IS IGNORED. Different
     asymmetries for different reasons, and both are deliberate.

Nothing here reaches AWS. The DynamoDB client is a fake.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from conftest import load_handler

overrides = importlib.import_module("overrides")

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
EXEC_ID = "abcd-1234-exec"


def item(
    *,
    decision="allow",
    reason="known flaky alarm",
    actor="arn:aws:iam::594380318102:user/poly4",
    created="2026-09-04T11:59:00+00:00",
    expires_in_minutes=30,
):
    raw = {
        "pipeline_execution_id": {"S": EXEC_ID},
        "decision": {"S": decision},
        "reason": {"S": reason},
        "actor_arn": {"S": actor},
        "created_at": {"S": created},
    }
    if expires_in_minutes is not None:
        expiry = NOW + timedelta(minutes=expires_in_minutes)
        raw["expires_at"] = {"N": str(int(expiry.timestamp()))}
    return raw


class FakeDynamo:
    def __init__(self, stored=None, raises=None):
        self.stored = stored
        self.raises = raises
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return {"Item": self.stored} if self.stored else {}


# --- Parsing --------------------------------------------------------------


def test_a_valid_override_is_honoured():
    result = overrides.parse_override(item(), now=NOW)

    assert result.decision == "allow"
    assert result.reason == "known flaky alarm"
    assert result.source == overrides.OverrideSource.RECORD
    assert "poly4" in result.actor


def test_an_absent_row_means_no_override():
    assert overrides.parse_override({}, now=NOW) is None


def test_an_expired_override_is_ignored_rather_than_honoured():
    """Somebody steered the gate an hour ago; this is a different moment.

    Ignoring falls back to the model's verdict, which fails closed on its own,
    so there is nothing to be gained by inventing a decision here.
    """
    assert overrides.parse_override(item(expires_in_minutes=-1), now=NOW) is None


def test_an_override_expiring_exactly_now_is_expired():
    """Boundary picked deliberately: `>=`, not `>`. An override whose validity
    ended this instant has ended."""
    assert overrides.parse_override(item(expires_in_minutes=0), now=NOW) is None


def test_an_override_with_an_unrecognised_decision_halts():
    """A row exists, so somebody meant something by it, and we cannot tell
    whether they meant allow or halt.

    Ignoring it risks shipping a change a human tried to stop. Halting risks
    blocking one they tried to release. Only the second is recoverable by
    trying again.
    """
    result = overrides.parse_override(item(decision="yes please"), now=NOW)

    assert result.decision == "halt"
    assert "not" in result.reason


def test_an_override_with_no_reason_halts():
    """The reason is the only explanation the audit trail will ever have for why
    the gate was bypassed. A bypass with no recorded reason is not a bypass we
    should honour."""
    result = overrides.parse_override(item(reason="   "), now=NOW)

    assert result.decision == "halt"
    assert "no reason" in result.reason


def test_a_row_with_no_expiry_is_still_honoured():
    """Belt and braces: a row written by hand, without the script, should not
    be silently ignored -- the script always sets one."""
    result = overrides.parse_override(item(expires_in_minutes=None), now=NOW)

    assert result.decision == "allow"


def test_an_overlong_reason_is_bounded():
    result = overrides.parse_override(item(reason="x" * 5_000), now=NOW)

    assert len(result.reason) <= overrides.MAX_REASON_CHARS


@pytest.mark.parametrize("value", ["ALLOW", " allow ", "Allow"])
def test_the_decision_is_case_and_whitespace_insensitive(value):
    """A human typed this into a terminal."""
    assert overrides.parse_override(item(decision=value), now=NOW).decision == "allow"


def test_a_garbage_expiry_does_not_crash_the_lookup():
    raw = item()
    raw["expires_at"] = {"N": "not-a-number"}

    # Unreadable expiry falls through to the validity checks rather than
    # exploding: the alternative is an exception inside the gate's own
    # fail-closed handler, which is a much worse place to be.
    assert overrides.parse_override(raw, now=NOW).decision == "allow"


# --- Reading ---------------------------------------------------------------


def test_the_read_is_strongly_consistent():
    """An override is read seconds after a human wrote it, while they watch.

    DynamoDB's default eventually-consistent read would occasionally miss it,
    which to that person is indistinguishable from the feature not working.
    """
    fake = FakeDynamo(item())

    overrides.OverrideReader("t", fake).load(EXEC_ID, now=NOW)

    assert fake.calls[0]["ConsistentRead"] is True


def test_the_lookup_is_keyed_by_pipeline_execution():
    """THE PROPERTY THAT MAKES THIS SAFE.

    A CodePipeline execution ID is unique and never reused, so there is no way
    to write a row that affects the next deploy. That is the whole difference
    from `gate_decision=allow`, which stays on until somebody remembers.
    """
    fake = FakeDynamo(item())

    overrides.OverrideReader("t", fake).load(EXEC_ID, now=NOW)

    assert fake.calls[0]["Key"] == {"pipeline_execution_id": {"S": EXEC_ID}}


def test_an_unreadable_table_means_no_override_rather_than_an_exception():
    fake = FakeDynamo(raises=RuntimeError("table gone"))

    assert overrides.OverrideReader("t", fake).load(EXEC_ID, now=NOW) is None


def test_no_execution_id_means_no_lookup():
    """A manual test invoke has no pipeline execution. Nothing to look up, and
    a GetItem on an empty key is an error rather than an empty result."""
    fake = FakeDynamo(item())

    assert overrides.OverrideReader("t", fake).load("", now=NOW) is None
    assert fake.calls == []


# --- Precedence, in the handler -------------------------------------------


def load(monkeypatch, gate_decision=None):
    if gate_decision is None:
        monkeypatch.delenv("GATE_DECISION", raising=False)
    else:
        monkeypatch.setenv("GATE_DECISION", gate_decision)
    monkeypatch.setenv("GATE_MODE", "enforcing")
    return load_handler("decision_service")


def rec(decision, reason="because"):
    return overrides.Override(
        decision=decision,
        reason=reason,
        source=overrides.OverrideSource.RECORD,
        actor="arn:aws:iam::1:user/poly4",
    )


def test_no_override_from_either_source_leaves_the_model_in_charge(monkeypatch):
    handler = load(monkeypatch)

    decision, reason = handler.resolve_override("", None)

    assert decision is None
    assert "model's verdict stands" in reason


def test_a_record_override_alone_is_honoured(monkeypatch):
    handler = load(monkeypatch)

    decision, reason = handler.resolve_override("", rec("allow"))

    assert decision == "allow"
    assert "poly4" in reason


def test_an_env_halt_beats_a_record_allow(monkeypatch):
    """THE PRECEDENCE RULE, and the case that decides it.

    If somebody set GATE_DECISION=halt they have stopped all deploys. A
    per-execution row saying `allow` must not defeat that, or the global stop
    is a suggestion and the person who set it has no way to know.
    """
    handler = load(monkeypatch, "halt")

    decision, _ = handler.resolve_override("halt", rec("allow"))

    assert decision == "halt"


def test_a_record_halt_beats_an_env_allow(monkeypatch):
    """It runs the other way too. Whoever is closest to the specific change gets
    to be more cautious than the default, never less."""
    handler = load(monkeypatch, "allow")

    decision, _ = handler.resolve_override("allow", rec("halt"))

    assert decision == "halt"


def test_both_saying_allow_allows(monkeypatch):
    handler = load(monkeypatch, "allow")

    decision, _ = handler.resolve_override("allow", rec("allow"))

    assert decision == "allow"


def test_an_unrecognised_env_value_still_halts(monkeypatch):
    """Unchanged from Phase 3.5. A misspelled override is somebody trying to
    steer the gate and failing."""
    handler = load(monkeypatch, "yes")

    decision, reason = handler.resolve_override("yes", None)

    assert decision == "halt"
    assert "not a recognised override" in reason


def test_the_reason_says_when_two_sources_disagreed(monkeypatch):
    handler = load(monkeypatch, "allow")

    _, reason = handler.resolve_override("allow", rec("halt"))

    assert "most restrictive" in reason


# --- The whole path -------------------------------------------------------


def test_a_record_override_reaches_the_gate_record(monkeypatch):
    """Including who wrote it. The old GATE_DECISION path could say a human
    overrode the gate and could never say which human."""
    monkeypatch.setenv("OVERRIDE_TABLE", "overrides-test")
    handler = load(monkeypatch)

    class Reader:
        def load(self, execution_id, now=None):
            return rec("allow", "shipping the fix")

    gate = handler.build_gate_record(
        outcome=_low_outcome(),
        request_id="req-1",
        record_override=Reader().load(EXEC_ID),
    )

    assert gate["override"]["decision"] == "allow"
    assert gate["override"]["source"] == "record"
    assert "poly4" in gate["override"]["actor"]
    assert gate["override"]["reason"].endswith("shipping the fix")


def test_an_override_that_agrees_with_the_model_is_still_recorded(monkeypatch):
    """ "A human forced allow and the model also said allow" and "the model said
    allow" are different events, and only one of them means the gate was
    trusted. Unchanged from 3.5, asserted again now there are two sources."""
    handler = load(monkeypatch)

    gate = handler.build_gate_record(
        outcome=_low_outcome(), request_id="r", record_override=rec("allow")
    )

    assert "override" in gate
    assert gate["model_decision"] == "allow"


def test_a_missing_override_table_is_a_degraded_mode_not_a_failure(monkeypatch):
    monkeypatch.delenv("OVERRIDE_TABLE", raising=False)
    handler = load(monkeypatch)

    assert handler.load_record_override(EXEC_ID) is None


def test_a_reader_that_raises_does_not_break_the_gate(monkeypatch):
    """`OverrideReader.load` does not raise, so reaching this means something
    stranger happened. Still not grounds to invent a decision."""
    handler = load(monkeypatch)

    class Exploding:
        def load(self, execution_id, now=None):
            raise RuntimeError("boom")

    assert handler.load_record_override(EXEC_ID, reader=Exploding()) is None


def _low_outcome():
    from verdict import ModelCall, RiskLevel, Verdict, VerdictOutcome, VerdictSource

    return VerdictOutcome(
        verdict=Verdict(
            risk_level=RiskLevel.LOW,
            reasoning="routine",
            source=VerdictSource.MODEL,
            confidence=0.9,
        ),
        call=ModelCall(model_id="m", prompt_version="v", attempts=1, succeeded=True),
    )
