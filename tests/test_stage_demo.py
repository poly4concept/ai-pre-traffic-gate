"""The stage demo refuses to run a demo that will not work. Phase 6.1.

The orchestration itself is not the interesting part. What matters is the
GUARD: this script pushes a commit, which starts a pipeline, which is not
undoable. Pushing at the wrong moment produces a `medium` verdict, no halt, and
an anticlimax explained live.

So the tests are about the conditions under which it refuses, and about the
teardown running whatever happens.

Nothing here reaches AWS. The clients are fakes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"_test_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sd = load("stage_demo")


class FakeLambda:
    """Returns a scripted outcome for every invoke."""

    def __init__(self, outcome="ok", raises=None):
        self.outcome = outcome
        self.raises = raises
        self.calls = 0

    def invoke(self, **kwargs):
        self.calls += 1
        if self.raises:
            raise self.raises
        if self.outcome == "faulted":
            return {"StatusCode": 200, "FunctionError": "Unhandled"}
        return {"StatusCode": 200}


class FakeCloudWatch:
    def __init__(self, states):
        self.states = states

    def describe_alarms(self, **_):
        return {
            "MetricAlarms": [
                {"AlarmName": f"alarm-{i}", "StateValue": s, "StateReason": ""}
                for i, s in enumerate(self.states)
            ]
        }


# --- The alarm gate -------------------------------------------------------


def test_firing_alarms_reports_only_those_in_alarm():
    cw = FakeCloudWatch(["OK", "ALARM", "INSUFFICIENT_DATA"])

    assert sd.firing_alarms(cw) == ["alarm-1"]


def test_insufficient_data_does_not_count_as_firing():
    """The project invariant, in the one place it decides whether to push.

    `INSUFFICIENT_DATA` means the alarm has no measurement. Treating it as
    firing would push a commit against a target whose health is unknown, which
    is the demo failing in the direction that looks like success.
    """
    cw = FakeCloudWatch(["INSUFFICIENT_DATA", "INSUFFICIENT_DATA"])

    assert sd.firing_alarms(cw) == []


def test_a_quiet_target_reports_nothing_firing():
    assert sd.firing_alarms(FakeCloudWatch(["OK", "OK", "OK"])) == []


# --- The traffic driver ---------------------------------------------------


def test_the_driver_counts_faulted_invocations_separately(monkeypatch):
    fake = FakeLambda(outcome="faulted")
    monkeypatch.setattr(sd.boto3, "client", lambda *a, **k: fake)
    monkeypatch.setattr(sd, "TRAFFIC_INTERVAL_SECONDS", 0.001)

    driver = sd.TrafficDriver("us-east-1")
    driver.start()
    while driver.sent < 5:
        pass
    driver.stop()

    assert driver.faulted == driver.sent
    assert driver.observed_error_rate == pytest.approx(100.0)


def test_the_driver_stops_itself_when_invocations_are_refused(monkeypatch):
    """A permissions problem will not improve by retrying it 400 times.

    It also has to be VISIBLE: the caller detects this by `sent` staying at
    zero, which is why the counter is only incremented on a successful invoke.
    """
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "AccessDeniedException"}}, "Invoke")
    fake = FakeLambda(raises=err)
    monkeypatch.setattr(sd.boto3, "client", lambda *a, **k: fake)
    monkeypatch.setattr(sd, "TRAFFIC_INTERVAL_SECONDS", 0.001)

    driver = sd.TrafficDriver("us-east-1")
    driver.start()
    driver._thread.join(timeout=5)

    assert driver.sent == 0
    assert fake.calls == 1, "should stop after the first refusal, not keep trying"


def test_an_error_rate_with_no_invocations_is_none_not_zero():
    """Same rule as everywhere else: a ratio with no denominator is undefined.
    Reporting 0% would say the app is healthy when nothing reached it."""
    assert sd.TrafficDriver("us-east-1").observed_error_rate is None


# --- Teardown -------------------------------------------------------------


def test_teardown_says_nothing_was_removed_when_nothing_was(monkeypatch, capsys, tmp_path):
    """A teardown you cannot trust is one you run twice.

    It reported "synthetic commits removed" whether or not any existed, which
    is the same small dishonesty as a 0% error rate over zero requests.
    """
    monkeypatch.setattr(sd.craft_commit, "SYNTHETIC_DIR", tmp_path / "absent")
    monkeypatch.setattr(sd.craft_commit, "cleanup", lambda **kw: 0)
    monkeypatch.setattr(sd.inject_fault, "write", lambda *a, **k: None)

    sd.teardown(None, "us-east-1", cleanup_commits=True)

    out = capsys.readouterr().out
    assert "no synthetic commits to remove" in out
    assert "synthetic commits removed" not in out


def test_teardown_reports_removal_when_there_was_something_to_remove(monkeypatch, capsys, tmp_path):
    present = tmp_path / "synthetic"
    present.mkdir()
    monkeypatch.setattr(sd.craft_commit, "SYNTHETIC_DIR", present)
    monkeypatch.setattr(sd.craft_commit, "cleanup", lambda **kw: 0)
    monkeypatch.setattr(sd.inject_fault, "write", lambda *a, **k: None)

    sd.teardown(None, "us-east-1", cleanup_commits=True)

    assert "synthetic commits removed" in capsys.readouterr().out


def test_teardown_clears_fault_injection_even_with_no_driver(monkeypatch):
    """`--teardown` on its own has no traffic thread, and clearing the fault is
    the part that matters -- leaving it on makes every later deploy roll itself
    back (D-057)."""
    cleared = []
    monkeypatch.setattr(sd.inject_fault, "write", lambda t, k, r, c: cleared.append(c))
    monkeypatch.setattr(sd.craft_commit, "cleanup", lambda **kw: 0)

    sd.teardown(None, "us-east-1", cleanup_commits=False)

    assert len(cleared) == 1
    assert cleared[0].get("error_rate", 0) == 0


def test_teardown_survives_a_failure_to_clear_the_fault(monkeypatch, capsys):
    """It has to keep going and tell you what to run by hand. A teardown that
    aborts halfway leaves exactly the state it exists to prevent."""

    def boom(*a, **k):
        raise SystemExit(2)

    monkeypatch.setattr(sd.inject_fault, "write", boom)
    monkeypatch.setattr(sd.craft_commit, "cleanup", lambda **kw: 0)

    sd.teardown(None, "us-east-1", cleanup_commits=False)

    assert "inject_fault.py off" in capsys.readouterr().out


# --- Guardrails -----------------------------------------------------------


def test_the_traffic_rate_is_fast_enough_to_hold_a_sixty_second_alarm():
    """At the 1/minute heartbeat each alarm period is a sample of size one and
    the alarm flaps every minute (D-077). This is the constant that fixes it,
    and it is easy to 'tidy' upward without realising what it is for."""
    per_minute = 60 / sd.TRAFFIC_INTERVAL_SECONDS

    assert per_minute >= 10, "too slow: the alarm will flap rather than hold"


def test_the_alarm_wait_allows_for_cloudwatch_lag():
    """Metrics lag one to two minutes and the alarm needs a full period after
    that. A short timeout would give up before the system had a chance."""
    assert sd.ALARM_TIMEOUT_SECONDS >= 240


def test_only_the_demo_app_is_driven():
    """Driving traffic at the GATE would write junk verdicts and spend Bedrock
    tokens. Asserted so a copy-paste cannot quietly repoint it."""
    assert sd.DEMO_APP.endswith("-demo-app")


def test_the_fault_table_is_the_one_inject_fault_owns():
    """THE BUG THIS ENCODES.

    `stage_demo` originally declared `FAULT_TABLE = f"{PROJECT}-faults"` -- a
    reasonable guess, and wrong. The real table is
    `ai-pre-traffic-gate-demo-app-faults`, because the faults belong to the demo
    app rather than to the project.

    Preflight passed, step 2 died on ResourceNotFoundException, and the demo was
    already half-started. Reusing the constant is the fix; this asserts it stays
    reused rather than drifting back into a literal.
    """
    assert sd.FAULT_TABLE == sd.inject_fault.DEFAULT_TABLE
    assert sd.FAULT_KEY == sd.inject_fault.DEFAULT_KEY
    assert sd.FAULT_TABLE.endswith("-demo-app-faults")


def test_the_fault_table_actually_exists_in_the_terraform():
    """The other half of the contract: the name the scripts use is the name
    Terraform creates. Both sides live in this repo, so it is checkable --
    the same shape as test_pipeline_wiring.py."""
    from pathlib import Path

    tf = (Path(__file__).resolve().parents[1] / "infra" / "personal").glob("*.tf")
    declared = "".join(p.read_text(encoding="utf-8") for p in tf)

    # Terraform builds it by interpolation, so match the suffix rather than the
    # resolved string.
    assert '"${local.demo_app_name}-faults"' in declared or "-demo-app-faults" in declared, (
        "no Terraform resource creates the fault table the scripts write to"
    )


# --- Replay ---------------------------------------------------------------
#
# The point of replay is removing a dependency: a live demo needs AWS to behave
# for four minutes in front of a room. These assert it genuinely has no such
# dependency, and that it does not pretend to be live.


RECORDED = {
    "captured_at": "2026-09-09T12:06:11+00:00",
    "commit_sha": "11a8dba84a01b2c3",
    "mode": "enforcing",
    "action_taken": "halt_pipeline",
    "verdict_id": "b8378efe-236a",
    "pipeline_execution_id": "d3ec7158-0240-476b",
    "risk_level": "high",
    "reasoning": "Deploying any change\u2014even a safe one\u2014during an alarm is unwise.",
    "confidence": "0.92",
    "source": "model",
    "floor_raised_from": "",
    "primary_concerns": ["Service is in ALARM with 50.7% error rate"],
    "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "prompt_version": "2026-09-08.1",
    "latency_ms": "3814",
    "input_tokens": "3019",
    "health": {"error_rate_pct": "50.66", "invocations_last_hour": "381", "alarms_firing": 1},
}


def write_replay(tmp_path, monkeypatch, payload=None, name="halt"):
    import json

    monkeypatch.setattr(sd, "REPLAYS", tmp_path)
    (tmp_path / f"{name}.json").write_text(json.dumps(payload or RECORDED), encoding="utf-8")
    return type("Args", (), {"name": name, "speed": 0.0})()


def test_replay_makes_no_aws_calls(tmp_path, monkeypatch, capsys):
    """THE WHOLE POINT.

    boto3.client is replaced with something that raises. If replay touches AWS
    at all -- even to look something up -- this fails.
    """

    def forbidden(*a, **k):
        raise AssertionError("replay must not construct an AWS client")

    monkeypatch.setattr(sd.boto3, "client", forbidden)
    args = write_replay(tmp_path, monkeypatch)

    assert sd.replay(args) == 0
    assert "RISK: HIGH" in capsys.readouterr().out


def test_replay_always_says_it_is_a_replay(tmp_path, monkeypatch, capsys):
    """Not suppressible, and deliberately not offered as a flag.

    Showing a recording is respectable. Showing one while implying it is live is
    not, and a talk arguing for honest measurement cannot open by faking its own
    demo.
    """
    sd.replay(write_replay(tmp_path, monkeypatch))

    out = capsys.readouterr().out
    assert "REPLAY" in out
    assert "No AWS calls" in out
    assert "2026-09-09T12:06" in out, "the banner should say when it was recorded"


def test_replay_folds_model_prose_to_ascii(tmp_path, monkeypatch, capsys):
    """The model writes em-dashes; the Windows console renders them as a
    replacement glyph. Mojibake on a projector is a bad look for a talk about
    careful measurement."""
    sd.replay(write_replay(tmp_path, monkeypatch))

    out = capsys.readouterr().out
    assert "change--even a safe one--during" in out
    assert "\u2014" not in out


def test_replay_reports_a_halt_as_a_halt(tmp_path, monkeypatch, capsys):
    sd.replay(write_replay(tmp_path, monkeypatch))

    out = capsys.readouterr().out
    assert "STOPPED the pipeline" in out


def test_replay_of_a_non_blocking_run_does_not_claim_a_halt(tmp_path, monkeypatch, capsys):
    """Same honesty rule as the escalation email (D-070): a component reports
    what it did, not what it would have liked to."""
    recorded = {**RECORDED, "action_taken": "none", "mode": "advisory"}
    sd.replay(write_replay(tmp_path, monkeypatch, recorded))

    out = capsys.readouterr().out
    assert "STOPPED the pipeline" not in out
    assert "did not block" in out or "would have been refused" in out


def test_replay_shows_when_the_floor_raised_the_verdict(tmp_path, monkeypatch, capsys):
    """A `medium` the arithmetic insisted on and a `medium` the model reasoned
    its way to are different stories, and the second is the interesting one."""
    recorded = {**RECORDED, "risk_level": "medium", "floor_raised_from": "low"}
    sd.replay(write_replay(tmp_path, monkeypatch, recorded))

    assert "raised from low by the deterministic floor" in capsys.readouterr().out


def test_a_missing_replay_says_how_to_make_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sd, "REPLAYS", tmp_path)
    args = type("Args", (), {"name": "absent", "speed": 0.0})()

    assert sd.replay(args) == 1
    assert "capture" in capsys.readouterr().out
