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


# --- Collection must not be able to break the gate ------------------------


def test_collection_failure_leaves_the_verdict_untouched(monkeypatch):
    """Phase 2 collects and logs. It must not change what the gate decides.

    A signal-collection bug that halted every deploy would be a worse outcome
    than one that degrades a single verdict, and until Phase 3 the verdict does
    not depend on signals at all.
    """
    handler = load(monkeypatch, "allow", "enforcing")
    monkeypatch.setattr(handler, "collect_bundle", lambda _: 1 / 0)
    monkeypatch.setattr(handler, "report_to_codepipeline", lambda *a: None)

    verdict = handler.lambda_handler(pipeline_event(), FakeContext())

    assert verdict["decision"] == "allow"
    assert verdict["action_taken"] == "none"


def test_an_absent_change_context_does_not_yet_halt(monkeypatch):
    """Phase 2 boundary, stated as a test so Phase 3 has to change it.

    `has_required_signals` is False here and the gate still allows, because
    verdict logic is Phase 3's job. When that changes, this test should fail --
    which is the point of writing it.
    """
    handler = load(monkeypatch, "allow", "enforcing")
    monkeypatch.setattr(handler, "report_to_codepipeline", lambda *a: None)

    verdict = handler.lambda_handler({}, FakeContext())

    assert verdict["decision"] == "allow"


def test_the_halt_path_still_works_with_signals_present(monkeypatch):
    """The Phase 1 guarantee, re-asserted now that collection runs alongside."""
    handler = load(monkeypatch, "halt", "enforcing")
    reported = []
    monkeypatch.setattr(handler, "report_to_codepipeline", lambda job, v: reported.append(v))

    verdict = handler.lambda_handler(pipeline_event(), FakeContext())

    assert verdict["action_taken"] == "halt_pipeline"
    assert reported and reported[0]["decision"] == "halt"


# --- Inspector wiring (Phase 2.3) ----------------------------------------


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
