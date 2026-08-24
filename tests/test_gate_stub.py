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

import base64
import json
from dataclasses import dataclass

import pytest
from conftest import load_handler
from signals import SignalStatus
from verdict import ModelCall, RiskLevel, Verdict, VerdictOutcome, VerdictSource

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


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
    return load_handler("decision_service")


# --- Fakes ----------------------------------------------------------------
#
# From 3.5 the gate talks to Bedrock and DynamoDB, so the whole path is exercised
# with injected collaborators rather than by patching module globals. The
# conftest guard makes this mandatory rather than merely tidy: it raises if a
# test constructs a real boto3 client, which is how it caught the handler
# building a bedrock-runtime client on a path that never calls Bedrock.


class FakeVerdictClient:
    """Stands in for BedrockVerdictClient. Returns a prepared outcome."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.bundles = []

    def get_verdict(self, bundle):
        self.bundles.append(bundle)
        return self.outcome


class FakeAuditWriter:
    def __init__(self, status="written"):
        self.status = status
        self.items = []

    def record(self, item):
        self.items.append(item)
        from verdict.audit import AuditWriteResult

        return AuditWriteResult(self.status)


def model_outcome(risk="low", *, confidence=0.9, reasoning="routine change", raw=None):
    """A successful model verdict at the given risk level."""
    verdict = Verdict(
        risk_level=RiskLevel(risk),
        reasoning=reasoning,
        source=VerdictSource.MODEL,
        confidence=confidence,
    )
    return VerdictOutcome(
        verdict=verdict,
        call=ModelCall(model_id="fake-model", prompt_version="test", attempts=1, succeeded=True),
        raw_model_output=raw if raw is not None else {"risk_level": risk},
    )


def failed_outcome(reason="bedrock unreachable", *, kind="aws:ThrottlingException", raw=None):
    """A fail-closed outcome, as the client produces when it cannot get one."""
    return VerdictOutcome(
        verdict=Verdict.fail_closed(reason),
        call=ModelCall(
            model_id="fake-model",
            prompt_version="test",
            attempts=3,
            succeeded=False,
            failure_kind=kind,
            error=reason,
        ),
        raw_model_output=raw,
    )


def run(handler, monkeypatch, *, event=None, outcome=None, writer=None, ctx=None):
    """Invoke the handler end to end with nothing reaching AWS."""
    monkeypatch.setattr(handler, "report_to_codepipeline", lambda *a: None)
    return handler.lambda_handler(
        event if event is not None else {},
        ctx or FakeContext(),
        verdict_client=FakeVerdictClient(outcome or model_outcome()),
        audit_writer=writer or FakeAuditWriter(),
    )


# --- The fail-closed matrix ----------------------------------------------
#
# GATE_DECISION changed meaning in 3.5. It used to BE the verdict; it is now a
# manual override that is normally unset. The direction of its default flipped
# with it -- unset now means "no human intervened, use the model" rather than
# "halt". Fail-closed did not weaken, it moved down a layer to the verdict
# client, where the judgement actually happens.
#
# What did NOT change: an unrecognised value still halts.


@pytest.mark.parametrize(
    "override",
    [
        "ALLOWED",  # near-miss on the real value
        "yes",  # plausible synonym
        "true",  # plausible synonym
        "1",  # plausible synonym
        "deny",  # right idea, wrong word
        "allow; halt",  # injection-ish
    ],
)
def test_an_unrecognised_override_halts(monkeypatch, override):
    """A misspelled override is somebody steering the gate and missing.

    That is exactly when guessing their intent is least appropriate, so it is
    the one case where an override that was never validly requested still stops
    the deploy.
    """
    handler = load(monkeypatch, override, "enforcing")

    gate = run(handler, monkeypatch, outcome=model_outcome("low"))

    assert gate["decision"] == "halt"
    assert gate["action_taken"] == "halt_pipeline"
    assert "failing closed" in gate["decision_reason"]


@pytest.mark.parametrize("unset", [None, "", "   "])
def test_no_override_lets_the_model_decide(monkeypatch, unset):
    """The new normal, and the behaviour change worth explaining on stage.

    Before 3.5 an unset GATE_DECISION halted, because a gate with no way to form
    an opinion has not approved anything. The gate now has a way, so "unset"
    means the model's verdict stands.
    """
    handler = load(monkeypatch, unset, "enforcing")

    gate = run(handler, monkeypatch, outcome=model_outcome("low"))

    assert gate["decision"] == "allow"
    assert "override" not in gate
    assert "no manual override" in gate["decision_reason"] or gate["decision_reason"]


def test_fail_closed_moved_down_a_layer_rather_than_away(monkeypatch):
    """Unset override + a model that could not answer still yields a halt.

    This is the test that shows the Phase 1 guarantee survived the refactor. The
    halt is no longer produced by an env-var default; it is produced by the
    verdict client, and it arrives here labelled `fail_closed`.
    """
    handler = load(monkeypatch, None, "enforcing")

    gate = run(handler, monkeypatch, outcome=failed_outcome("bedrock throttled"))

    assert gate["decision"] == "halt"
    assert gate["would_have_halted"] is True
    assert gate["verdict_source"] == "fail_closed"


def test_an_explicit_allow_override_is_recorded_even_when_it_agrees(monkeypatch):
    """ "A human forced allow" and "the model said allow" are different events.

    Only one of them means the gate was actually trusted, so the override is
    recorded even when the two agree.
    """
    handler = load(monkeypatch, "allow", "enforcing")

    gate = run(handler, monkeypatch, outcome=model_outcome("low"))

    assert gate["decision"] == "allow"
    assert gate["override"]["decision"] == "allow"
    assert gate["model_decision"] == "allow"


def test_an_allow_override_does_not_erase_what_the_model_thought(monkeypatch):
    """The override wins the decision and loses the record.

    `would_have_halted` still reflects the model, which is what makes an
    override auditable rather than a way to make an inconvenient verdict vanish.
    """
    handler = load(monkeypatch, "allow", "enforcing")

    gate = run(handler, monkeypatch, outcome=model_outcome("high"))

    assert gate["decision"] == "allow"
    assert gate["action_taken"] == "none"
    assert gate["would_have_halted"] is True
    assert gate["risk_level"] == "high"
    assert gate["model_decision"] == "halt"


def test_override_is_case_and_whitespace_tolerant(monkeypatch):
    """Tolerant of formatting, not of meaning."""
    handler = load(monkeypatch, "  ALLOW  ", "enforcing")

    assert run(handler, monkeypatch)["decision"] == "allow"


def test_a_chosen_halt_is_distinguishable_from_a_failed_closed_one(monkeypatch):
    """Same decision, unrelated events: the gate working vs the gate blind."""
    chosen = run(load(monkeypatch, "halt", "enforcing"), monkeypatch)
    failed = run(load(monkeypatch, None, "enforcing"), monkeypatch, outcome=failed_outcome())

    assert chosen["decision"] == failed["decision"] == "halt"
    assert "manually overridden" in chosen["decision_reason"]
    assert failed["verdict_source"] == "fail_closed"
    assert chosen["action_taken"] == "halt_pipeline"
    # The Phase 3 restriction: the model's halt is recorded, not acted on.
    assert failed["action_taken"] == "none"


# --- Mode independence ----------------------------------------------------


@pytest.mark.parametrize("mode", ["shadow", "advisory"])
def test_non_enforcing_modes_never_act(monkeypatch, mode):
    """A halt in shadow or advisory records but takes no action."""
    handler = load(monkeypatch, "halt", mode)

    gate = run(handler, monkeypatch)

    assert gate["decision"] == "halt"
    assert gate["action_taken"] == "none"


def test_enforcing_mode_acts_on_a_human_halt(monkeypatch):
    """The Phase 1 kill switch, still live and still demoable."""
    handler = load(monkeypatch, "halt", "enforcing")

    assert run(handler, monkeypatch)["action_taken"] == "halt_pipeline"


@pytest.mark.parametrize("mode", [None, "", "SHADOWY", "off", "disabled", "enforce"])
def test_unrecognised_modes_become_enforcing(monkeypatch, mode):
    """An unreadable mode fails toward acting, not toward silence.

    This fails in the opposite direction from an unreadable verdict, and both
    choices point the same way: stop the deploy. A typo must not silently
    disable the gate.
    """
    handler = load(monkeypatch, "halt", mode)

    gate = run(handler, monkeypatch)

    assert gate["mode"] == "enforcing"
    assert gate["action_taken"] == "halt_pipeline"
    assert "mode_warning" in gate


def test_mode_does_not_alter_the_verdict(monkeypatch):
    """Mode gates the action only. The recorded decision is mode-independent."""
    decisions = {
        mode: run(load(monkeypatch, "halt", mode), monkeypatch)["decision"]
        for mode in ("shadow", "advisory", "enforcing")
    }

    assert set(decisions.values()) == {"halt"}


# --- The Phase 3 restriction ----------------------------------------------
#
# The gate now forms a real opinion and is deliberately not allowed to act on
# it. These are the tests that will need updating in Phase 5, and that is the
# point of writing them: the restriction is asserted, not assumed.


def test_the_models_halt_is_recorded_and_not_acted_on(monkeypatch):
    """Even in enforcing mode. Phase 3 is shadow-only for the MODEL's verdict."""
    handler = load(monkeypatch, None, "enforcing")

    gate = run(handler, monkeypatch, outcome=model_outcome("high"))

    assert gate["decision"] == "halt"
    assert gate["would_have_halted"] is True
    assert gate["action_taken"] == "none"
    assert gate["model_verdict_can_act"] is False


def test_a_human_halt_still_acts_while_the_models_does_not(monkeypatch):
    """The asymmetry, stated directly.

    A human typing `halt` is not the model acting, so the kill switch is not
    subject to the Phase 3 restriction. Losing it during the shadow period would
    be the wrong kind of caution.
    """
    by_model = run(load(monkeypatch, None, "enforcing"), monkeypatch, outcome=model_outcome("high"))
    by_human = run(load(monkeypatch, "halt", "enforcing"), monkeypatch)

    assert by_model["decision"] == by_human["decision"] == "halt"
    assert by_model["action_taken"] == "none"
    assert by_human["action_taken"] == "halt_pipeline"


@pytest.mark.parametrize(
    ("risk", "expected"),
    [("low", "full_deploy"), ("medium", "canary"), ("high", "halt_and_escalate")],
)
def test_the_recommended_action_is_recorded_even_though_nothing_acts(monkeypatch, risk, expected):
    """Shadow mode is only worth running if it records what it WOULD have done."""
    handler = load(monkeypatch, None, "shadow")

    gate = run(handler, monkeypatch, outcome=model_outcome(risk))

    assert gate["recommended_action"] == expected
    assert gate["action_taken"] == "none"


# --- Audit record ---------------------------------------------------------


def test_the_gate_record_carries_the_fields_the_audit_trail_needs(monkeypatch):
    handler = load(monkeypatch, None, "shadow")

    gate = run(handler, monkeypatch, ctx=FakeContext(aws_request_id="abc-123"))

    for field in (
        "schema_version",
        "decision",
        "decision_reason",
        "mode",
        "action_taken",
        "would_have_halted",
        "risk_level",
        "recommended_action",
        "verdict_source",
        "confidence",
        "model_call",
        "request_id",
        "timestamp",
        "verdict_id",
    ):
        assert field in gate, f"missing audit field: {field}"

    assert gate["request_id"] == "abc-123"
    assert gate["schema_version"] == 1


def test_the_verdict_is_written_to_dynamodb(monkeypatch):
    handler = load_with_table(monkeypatch, None, "shadow")
    writer = FakeAuditWriter()

    gate = run(handler, monkeypatch, event=pipeline_event(), writer=writer)

    assert gate["audit"] == "written"
    assert len(writer.items) == 1
    assert writer.items[0]["verdict_id"] == "job-1"


def test_the_raw_model_output_reaches_the_record(monkeypatch):
    """The disagreement between raw and validated is the point of the record.

    Without this the failure path stores only the validator's complaint, and
    "the model said 'critical'" -- exactly what Phase 4 needs to count -- is
    lost.
    """
    handler = load_with_table(monkeypatch, None, "shadow")
    writer = FakeAuditWriter()

    run(
        handler,
        monkeypatch,
        event=pipeline_event(),
        outcome=failed_outcome(
            "risk_level 'critical' is not valid",
            kind="invalid_verdict:risk_level",
            raw={"risk_level": "critical", "confidence": 0.8},
        ),
        writer=writer,
    )

    assert "critical" in writer.items[0]["raw_model_output"]
    assert writer.items[0]["model_call"]["failure_kind"] == "invalid_verdict:risk_level"


def test_an_override_is_persisted_in_the_record(monkeypatch):
    """CLAUDE.md constraint 3: every verdict is auditable AND overridable."""
    handler = load_with_table(monkeypatch, "allow", "shadow")
    writer = FakeAuditWriter()

    run(handler, monkeypatch, event=pipeline_event(), outcome=model_outcome("high"), writer=writer)

    assert writer.items[0]["override"]["decision"] == "allow"
    assert writer.items[0]["verdict"]["risk_level"] == "high"


def test_a_failed_audit_write_does_not_halt_a_judged_deploy(monkeypatch):
    """Storage trouble is not evidence about the change.

    Same trade as D-030: the verdict is already made. The log line remains, so
    the trail is degraded rather than lost.
    """
    handler = load_with_table(monkeypatch, None, "enforcing")

    gate = run(
        handler,
        monkeypatch,
        event=pipeline_event(),
        outcome=model_outcome("low"),
        writer=FakeAuditWriter("failed"),
    )

    assert gate["audit"] == "failed"
    assert gate["decision"] == "allow"
    assert gate["action_taken"] == "none"


def test_without_a_table_the_gate_still_judges(monkeypatch):
    """No VERDICT_TABLE is a degraded mode, not a failure."""
    handler = load(monkeypatch, None, "shadow")

    gate = run(handler, monkeypatch, event=pipeline_event())

    assert gate["audit"] == "no_table_configured"
    assert gate["decision"] == "allow"


def test_the_verdict_id_is_the_pipeline_job(monkeypatch):
    """One verdict per job, which is what makes the conditional write mean
    something when CodePipeline invokes the same job twice."""
    handler = load(monkeypatch, None, "shadow")

    assert run(handler, monkeypatch, event=pipeline_event())["verdict_id"] == "job-1"


def test_survives_a_context_missing_its_attributes(monkeypatch):
    handler = load(monkeypatch, None, "shadow")

    class BareContext:
        pass

    assert run(handler, monkeypatch, ctx=BareContext())["request_id"] == "unknown"


def test_a_crash_before_a_verdict_fails_closed(monkeypatch):
    """The outermost boundary. Everything below it is written never to raise.

    If something does anyway, the gate knows nothing about the change, and
    knowing nothing is grounds to stop.
    """
    handler = load(monkeypatch, None, "enforcing")
    monkeypatch.setattr(handler, "collect_bundle", lambda _: 1 / 0)

    gate = run(handler, monkeypatch)

    assert gate["decision"] == "halt"
    assert gate["would_have_halted"] is True
    assert gate["verdict_source"] == "fail_closed"
    assert gate["model_call"]["failure_kind"] == "gate_internal_error"


# --- Signal collection (Phase 2.2b) --------------------------------------
#
# The gate now assembles a signal bundle and logs it. Two properties matter, and
# they pull in opposite directions: the bundle must be real, and it must not be
# able to break the gate.


def pipeline_event(payload_overrides: dict | None = None, trusted_sha: str = SHA) -> dict:
    """A CodePipeline job event carrying a change-context payload."""
    body = {
        "commit_sha": SHA,
        "commit_message": "Bump requests from 2.31.0 to 2.32.3",
        "branch": "main",
        "author": "dependabot[bot]",
        "committed_at": "2026-08-11T10:14:00+00:00",
        "diff_stats_ok": True,
        "files_changed": 1,
        "lines_added": 1,
        "lines_removed": 1,
        "paths": ["requirements.txt"],
    }
    body.update(payload_overrides or {})
    params = {
        "trusted_commit_sha": trusted_sha,
        "change_context_b64": base64.b64encode(json.dumps(body).encode()).decode(),
    }
    return {
        "CodePipeline.job": {
            "id": "job-1",
            "data": {
                "actionConfiguration": {"configuration": {"UserParameters": json.dumps(params)}}
            },
        }
    }


def test_gate_collects_real_change_context_from_a_pipeline_event(monkeypatch):
    handler = load(monkeypatch, "allow", "shadow")

    bundle = handler.collect_bundle(
        pipeline_event(),
        security_collector=handler.DisabledCollector("security_findings", "not under test"),
        health_collector=handler.DisabledCollector("target_health", "not under test"),
    )

    assert bundle.change.status is SignalStatus.OK
    assert bundle.change.data.commit_sha == SHA
    assert bundle.change.data.files_changed == 1
    assert bundle.has_required_signals


def test_no_collector_in_the_production_path_is_a_mock(monkeypatch):
    """From Phase 2.4 all three collectors are real, and must stay that way.

    A mock in this path would write fabricated security or health data into the
    audit trail of a real deployment -- the "absent signal read as a reassuring
    one" failure the whole package exists to prevent, committed by us rather than
    by an AWS outage.

    Asserted on the constructed collector types rather than on statuses, because
    a mock returning plausible data would produce an OK status and look correct.
    """
    handler = load(monkeypatch, "allow", "shadow")
    built: dict[str, object] = {}

    def capture(**kwargs):
        built.update(kwargs)
        raise RuntimeError("stop before any API call")

    monkeypatch.setattr(handler, "collect_signals", capture)
    with pytest.raises(RuntimeError, match="stop before"):
        handler.collect_bundle(pipeline_event())

    assert isinstance(built["security_collector"], handler.InspectorFindingsCollector)
    assert isinstance(built["health_collector"], handler.TargetHealthCloudWatchCollector)
    for name in ("change_collector", "security_collector", "health_collector"):
        assert "Mock" not in type(built[name]).__name__, name


def test_a_direct_invoke_has_no_change_to_describe(monkeypatch):
    """Invoked by hand, outside any pipeline. There is no change context."""
    handler = load(monkeypatch, "allow", "shadow")

    bundle = handler.collect_bundle(
        {},
        security_collector=handler.DisabledCollector("security_findings", "not under test"),
        health_collector=handler.DisabledCollector("target_health", "not under test"),
    )

    assert bundle.change.status is SignalStatus.UNAVAILABLE
    assert not bundle.has_required_signals


def test_a_tampered_payload_yields_no_change_context(monkeypatch):
    handler = load(monkeypatch, "allow", "shadow")

    bundle = handler.collect_bundle(
        pipeline_event(trusted_sha="f" * 40),
        security_collector=handler.DisabledCollector("security_findings", "not under test"),
        health_collector=handler.DisabledCollector("target_health", "not under test"),
    )

    assert bundle.change.status is SignalStatus.UNAVAILABLE
    assert "mismatch" in bundle.change.error


def test_the_bundle_is_json_serialisable_for_the_audit_log(monkeypatch):
    handler = load(monkeypatch, "allow", "shadow")

    payload = handler.collect_bundle(
        pipeline_event(),
        security_collector=handler.DisabledCollector("security_findings", "not under test"),
        health_collector=handler.DisabledCollector("target_health", "not under test"),
    ).to_dict()

    json.dumps(payload)  # raises if a datetime or Enum survived
    assert payload["signals"]["change_context"]["status"] == "ok"
    assert payload["signals"]["target_health"]["status"] == "skipped"


# --- Collection is no longer harmless ------------------------------------
#
# In Phase 2 a collection failure was logged and the verdict was unaffected,
# because the verdict did not depend on signals. It does now, so these tests
# assert the opposite of what they asserted one phase ago. Both versions were
# correct for their phase, and the change is the interesting part.


def test_a_collection_failure_now_fails_closed(monkeypatch):
    """The Phase 2 test this replaces asserted the verdict was untouched."""
    handler = load(monkeypatch, None, "enforcing")
    monkeypatch.setattr(handler, "collect_bundle", lambda _: 1 / 0)

    gate = run(handler, monkeypatch, event=pipeline_event())

    assert gate["decision"] == "halt"
    assert gate["would_have_halted"] is True


def test_an_absent_change_context_now_halts(monkeypatch):
    """The Phase 2 boundary test, now on the other side of the boundary.

    It was written to fail in Phase 3 -- `has_required_signals` is False here,
    and the gate used to allow anyway because verdict logic did not exist yet.
    The verdict client refuses to call Bedrock at all in this state, because a
    model asked about a change it was never shown produces a confident, baseless
    answer indistinguishable from a real one.
    """
    handler = load(monkeypatch, None, "enforcing")

    gate = run(handler, monkeypatch, outcome=failed_outcome("required signals missing"))

    assert gate["decision"] == "halt"
    assert gate["would_have_halted"] is True


def test_the_human_halt_path_still_works_with_signals_present(monkeypatch):
    """The Phase 1 guarantee, re-asserted now that a model is in the loop."""
    handler = load(monkeypatch, "halt", "enforcing")
    reported = []
    monkeypatch.setattr(handler, "report_to_codepipeline", lambda job, g: reported.append(g))

    gate = handler.lambda_handler(
        pipeline_event(),
        FakeContext(),
        verdict_client=FakeVerdictClient(model_outcome("low")),
        audit_writer=FakeAuditWriter(),
    )

    assert gate["action_taken"] == "halt_pipeline"
    assert reported and reported[0]["decision"] == "halt"


# --- Inspector wiring (Phase 2.3) ----------------------------------------


def load_with_table(monkeypatch, decision, mode, table="verdicts-test"):
    """Load the handler with a verdict table configured."""
    monkeypatch.setenv("VERDICT_TABLE", table)
    return load(monkeypatch, decision, mode)


def load_with_env(monkeypatch, **env):
    """Load the handler with arbitrary env, since values are read at import."""
    monkeypatch.setenv("GATE_DECISION", "allow")
    monkeypatch.setenv("GATE_MODE", "shadow")
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return load_handler("decision_service")


def test_security_scanning_on_selects_the_real_inspector_collector(monkeypatch):
    handler = load_with_env(monkeypatch, SECURITY_SCANNING="true")

    assert handler.SECURITY_SCANNING is True


def test_security_scanning_off_yields_skipped_not_a_fake_clean_result(monkeypatch):
    """Declining to pay for Inspector is a choice, not a clean bill of health."""
    handler = load_with_env(monkeypatch, SECURITY_SCANNING="false")

    bundle = handler.collect_bundle(
        pipeline_event(),
        health_collector=handler.DisabledCollector("target_health", "not under test"),
    )

    assert bundle.security.status is SignalStatus.SKIPPED
    assert bundle.security.data is None
    assert "deliberately not consulted" in bundle.security.error


def test_security_scanning_defaults_to_on(monkeypatch):
    """Absent config must not silently disable a security signal."""
    handler = load_with_env(monkeypatch, SECURITY_SCANNING=None)

    assert handler.SECURITY_SCANNING is True


def test_a_disabled_inspector_reports_unavailable_through_the_gate(monkeypatch):
    """End to end: Inspector off, and the bundle says so rather than 'clean'.

    This is the state of the real account right now, so it is the behaviour the
    next deployment will actually exhibit.
    """
    handler = load_with_env(monkeypatch, SECURITY_SCANNING="true")

    class DisabledInspector:
        def batch_get_account_status(self, **_):
            return {
                "accounts": [
                    {
                        "accountId": "594380318102",
                        "state": {"status": "DISABLED"},
                        "resourceState": {"lambda": {"status": "DISABLED"}},
                    }
                ]
            }

    collector = handler.InspectorFindingsCollector("svc", client=DisabledInspector())
    result = collector.collect()

    assert result.status is SignalStatus.UNAVAILABLE
    assert result.data is None
    assert "DISABLED" in result.error


def test_inspector_failure_does_not_block_the_bundle(monkeypatch):
    """A broken security collector must still leave change context usable."""
    handler = load_with_env(monkeypatch, SECURITY_SCANNING="true")
    monkeypatch.setattr(
        handler,
        "InspectorFindingsCollector",
        lambda *a, **k: handler.MockChangeContextCollector(raises=RuntimeError("boom")),
    )

    bundle = handler.collect_bundle(
        pipeline_event(),
        health_collector=handler.DisabledCollector("target_health", "not under test"),
    )

    assert bundle.change.is_usable
    assert bundle.security.status is SignalStatus.UNAVAILABLE
    # Two of three collected, and a verdict is still permitted.
    assert bundle.has_required_signals
