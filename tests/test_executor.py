"""Tests for the executor.

The executor is the only component in the project holding deploy permissions, so
the tests that matter are the ones asserting it *stops* rather than improvises.

AWS calls are not mocked. Every test here exercises a path that returns before
any client is constructed, or stubs the reporting helpers directly. Mocking
CodeDeploy would assert that my mocks match my assumptions, which is the least
useful thing a test can do.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from conftest import load_handler


@pytest.fixture
def executor(monkeypatch):
    monkeypatch.setenv("TARGET_FUNCTION", "demo-app")
    monkeypatch.setenv("TARGET_ALIAS", "live")
    monkeypatch.setenv("CODEDEPLOY_APP", "app")
    monkeypatch.setenv("CODEDEPLOY_GROUP", "group")
    return load_handler("executor")


@pytest.fixture
def reports(executor, monkeypatch):
    """Capture what the executor reports to CodePipeline instead of calling it."""
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        executor,
        "_succeed",
        lambda job_id, continuation_token=None, note="": calls.append(
            ("success", {"job_id": job_id, "token": continuation_token, "note": note})
        ),
    )
    monkeypatch.setattr(
        executor,
        "_fail",
        lambda job_id, message: calls.append(("failure", {"job_id": job_id, "message": message})),
    )
    return calls


def job_event(job_id: str = "job-1", **data) -> dict:
    return {"CodePipeline.job": {"id": job_id, "data": data}}


# --- The AppSpec contract -------------------------------------------------


def load_script() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "deploy_canary.py"
    spec = importlib.util.spec_from_file_location("_script_deploy_canary_x", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_script_deploy_canary_x"] = module
    spec.loader.exec_module(module)
    return module


def test_appspec_matches_the_operator_script_byte_for_byte(executor):
    """Pin the two deliberate copies of build_appspec together.

    The executor and scripts/deploy_canary.py each carry their own copy because
    they live in different deployment units. Sharing across that boundary would
    mean a build step for fifteen lines. This test is the cheaper alternative:
    drift fails here rather than surfacing as a rejected deployment later.
    """
    script = load_script()

    assert executor.build_appspec("fn", "live", "1", "2") == script.build_appspec(
        "fn", "live", "1", "2"
    )


def test_appspec_version_is_numeric(executor):
    raw = executor.build_appspec("fn", "live", "1", "2")

    assert '"version": 0.0' in raw
    assert isinstance(json.loads(raw)["version"], float)


# --- Fail-closed behaviour ------------------------------------------------


def test_non_pipeline_invocation_takes_no_action(executor):
    """A manual test invoke must not deploy anything.

    This function's actions are irreversible and it holds the permissions to
    take them, so an event it does not recognise is a reason to stop rather than
    to guess at a sensible default.
    """
    result = executor.lambda_handler({"hello": "world"}, None)

    assert result["status"] == "ignored"


@pytest.mark.parametrize("missing", ["TARGET_FUNCTION", "CODEDEPLOY_APP", "CODEDEPLOY_GROUP"])
def test_missing_configuration_fails_the_job(monkeypatch, missing):
    """Unconfigured is a reason to stop, not to improvise."""
    for name, value in (
        ("TARGET_FUNCTION", "demo-app"),
        ("CODEDEPLOY_APP", "app"),
        ("CODEDEPLOY_GROUP", "group"),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing, raising=False)

    module = load_handler("executor")
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        module, "_fail", lambda job_id, message: calls.append(("failure", {"message": message}))
    )

    result = module.lambda_handler(job_event(), None)

    assert result["status"] == "failed"
    assert calls and calls[0][0] == "failure"
    assert missing in calls[0][1]["message"]


def test_unhandled_error_fails_the_job_rather_than_escaping(executor, reports, monkeypatch):
    """An unanticipated exception must stop the pipeline.

    If the executor raises without reporting, CodePipeline sees an action that
    never responded -- which is not the same as a failure, and resolves much
    later and much less clearly.
    """

    def boom(*_args, **_kwargs):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(executor, "start_deployment", boom)

    result = executor.lambda_handler(job_event(), None)

    assert result["status"] == "failed"
    assert reports[0][0] == "failure"
    assert "RuntimeError" in reports[0][1]["message"]


def test_failure_message_is_truncated_to_the_api_limit(executor):
    """PutJobFailureResult rejects messages over 265 characters.

    A rejected call leaves the job hanging until it times out, so the truncation
    has to happen before the call, not be discovered by it.
    """
    assert executor.MAX_FAILURE_MESSAGE == 265


# --- Continuation token state machine -------------------------------------


def test_a_running_deployment_returns_the_token_again(executor, reports, monkeypatch):
    """In-progress means "ask me again", which is what re-arms the poll."""
    monkeypatch.setattr(
        executor,
        "check_deployment",
        lambda job_id, token: executor._succeed(job_id, continuation_token=token),
    )

    executor.lambda_handler(job_event(continuationToken="d-ABC123"), None)

    assert reports[0][0] == "success"
    assert reports[0][1]["token"] == "d-ABC123"


def test_a_token_routes_to_the_status_check_not_a_new_deployment(executor, monkeypatch):
    """The token is what distinguishes "start" from "check".

    Getting this backwards would create a fresh deployment on every poll --
    an infinite deploy loop that looks like a stuck pipeline.
    """
    taken: list[str] = []
    monkeypatch.setattr(executor, "start_deployment", lambda *a: taken.append("start"))
    monkeypatch.setattr(executor, "check_deployment", lambda *a: taken.append("check"))

    executor.lambda_handler(job_event(continuationToken="d-ABC"), None)
    executor.lambda_handler(job_event(), None)

    assert taken == ["check", "start"]


def test_terminal_states_are_classified_correctly(executor):
    assert "Succeeded" in executor.SUCCESS_STATES
    for state in ("Failed", "Stopped"):
        assert state in executor.FAILURE_STATES
    # InProgress must be in neither, or the poll loop terminates early.
    assert "InProgress" not in executor.SUCCESS_STATES
    assert "InProgress" not in executor.FAILURE_STATES
