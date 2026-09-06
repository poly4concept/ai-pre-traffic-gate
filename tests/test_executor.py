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
from botocore.exceptions import ClientError
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


# --- Phase 5.1: the verdict reaches the thing that deploys ----------------
#
# The executor is the only component holding deploy permissions, so the tests
# that matter here are the ones asserting it REFUSES. Every "it deploys
# correctly" test below exists to stop the refusal tests passing vacuously.

CANARY = "ai-pre-traffic-gate-canary-10pct-1min"


@pytest.fixture
def enforcing(monkeypatch):
    """An executor with the verdict switch armed."""
    monkeypatch.setenv("TARGET_FUNCTION", "demo-app")
    monkeypatch.setenv("TARGET_ALIAS", "live")
    monkeypatch.setenv("CODEDEPLOY_APP", "app")
    monkeypatch.setenv("CODEDEPLOY_GROUP", "group")
    monkeypatch.setenv("VERDICT_TABLE", "verdicts")
    monkeypatch.setenv("CANARY_CONFIG", CANARY)
    monkeypatch.setenv("EXECUTOR_ENFORCES_VERDICT", "true")
    return load_handler("executor")


@pytest.fixture
def shadow(monkeypatch):
    """The same executor with the switch in its default position."""
    monkeypatch.setenv("TARGET_FUNCTION", "demo-app")
    monkeypatch.setenv("TARGET_ALIAS", "live")
    monkeypatch.setenv("CODEDEPLOY_APP", "app")
    monkeypatch.setenv("CODEDEPLOY_GROUP", "group")
    monkeypatch.setenv("VERDICT_TABLE", "verdicts")
    monkeypatch.setenv("CANARY_CONFIG", CANARY)
    monkeypatch.delenv("EXECUTOR_ENFORCES_VERDICT", raising=False)
    return load_handler("executor")


def verdict_job(execution_id: str = "exec-1", job_id: str = "job-1") -> dict:
    """A job event shaped the way CodePipeline ACTUALLY sends one.

    THIS FIXTURE WAS THE BUG (F-023).

    It used to be `{"data": {"pipelineContext": {"pipelineExecutionId": ...}}}`,
    which is the custom-action job structure returned by `PollForJobs` and is
    NOT what a Lambda-invoke action receives. A Lambda invoke gets
    `actionConfiguration`, `inputArtifacts`, `outputArtifacts`,
    `artifactCredentials` and `continuationToken` -- and no pipelineContext at
    all.

    So the fixture and the code under test were wrong in the same way, and
    agreed. Every test passed. In production the lookup returned None on every
    single run, the risk branching added in 5.1 never once executed, and the
    log line read `VERDICT SHADOW: would have STOPPED this deploy -- job carries
    no pipelineExecutionId`, which looks like shadow mode working.

    A test written from an event shape you invented validates your assumption,
    not the integration. `test_pipeline_wiring.py` is what actually closes this:
    it asserts the pipeline puts the value where this code now reads it.
    """
    return {
        "CodePipeline.job": {
            "id": job_id,
            "data": {
                "actionConfiguration": {
                    "configuration": {
                        "UserParameters": json.dumps({"pipeline_execution_id": execution_id}),
                    }
                },
            },
        }
    }


def record(risk: str = "medium") -> dict:
    """A raw DynamoDB item shaped like the gate's audit record."""
    return {
        "verdict_id": {"S": "job-gate-1"},
        "verdict": {"M": {"risk_level": {"S": risk}, "reasoning": {"S": "because"}}},
    }


class FakeDynamo:
    """Scripted query/get_item pair. Records what it was asked."""

    def __init__(self, pages=None, item=None):
        # `pages` is a list of query results, consumed one per call, so a test
        # can script "empty, empty, found" for the consistency retry.
        self.pages = list(pages) if pages is not None else [[{"verdict_id": {"S": "job-gate-1"}}]]
        self.item = record() if item is None else item
        self.queries: list[dict] = []
        self.gets: list[dict] = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        page = self.pages[min(len(self.queries) - 1, len(self.pages) - 1)]
        return {"Items": page}

    def get_item(self, **kwargs):
        self.gets.append(kwargs)
        return {"Item": self.item} if self.item else {}


def wire_dynamo(module, monkeypatch, fake):
    """Point the module's boto3 at the fake, for dynamodb only."""
    monkeypatch.setattr(module.boto3, "client", lambda name, **kw: fake)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)


# --- The refusals ---------------------------------------------------------


def test_no_verdict_stops_the_deploy_when_enforcing(enforcing, monkeypatch):
    """The single most important assertion in Phase 5.

    A deploy that reaches the executor with no recorded verdict has bypassed
    the gate -- whether by a gate crash, a pipeline edit, or someone invoking
    the executor directly. None of those is a reason to ship.
    """
    wire_dynamo(enforcing, monkeypatch, FakeDynamo(pages=[[]]))

    with pytest.raises(enforcing.VerdictUnavailable) as excinfo:
        enforcing.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert "no verdict recorded" in str(excinfo.value)


def test_a_high_risk_verdict_stops_the_deploy(enforcing, monkeypatch):
    """There is no traffic percentage that makes a high-risk change safe.

    Defence in depth: in enforcing mode the GATE already halts the pipeline
    before this stage runs, so reaching here means the gate is in shadow or
    advisory and the executor is not. Two independent things have to be
    misconfigured for a high-risk change to ship.
    """
    wire_dynamo(enforcing, monkeypatch, FakeDynamo(item=record("high")))

    with pytest.raises(enforcing.VerdictUnavailable) as excinfo:
        enforcing.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert "HIGH risk" in str(excinfo.value)


def test_an_unknown_risk_level_raises_rather_than_defaulting(enforcing):
    """A default in this mapping would be the most dangerous line in the file.

    A typo, a fourth level added to the enum and not here, a corrupted record --
    all of them would silently pick whatever the default was, and the default
    anyone would reach for is `canary`, which ships the change.
    """
    for level in ("", "critical", "LOW ", "unknown", "none"):
        with pytest.raises(enforcing.VerdictUnavailable):
            enforcing.deployment_config_for(level)


def test_a_record_with_no_risk_level_is_unusable(enforcing):
    with pytest.raises(enforcing.VerdictUnavailable):
        enforcing.read_risk_level({"verdict": {"M": {}}})


def test_a_job_without_an_execution_id_stops_the_deploy(enforcing, monkeypatch):
    """No execution ID means no way to identify WHICH verdict applies, and
    deploying on an unrelated verdict is worse than not deploying."""
    wire_dynamo(enforcing, monkeypatch, FakeDynamo())

    with pytest.raises(enforcing.VerdictUnavailable):
        enforcing.resolve_deployment_config({"id": "job-1", "data": {}})


def test_a_dynamodb_failure_stops_the_deploy_when_enforcing(enforcing, monkeypatch):
    """An unreadable verdict store is not a verdict. Same rule as the gate:
    absence of evidence is not evidence of safety."""

    class Broken:
        def query(self, **_):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Q")

    monkeypatch.setattr(enforcing.boto3, "client", lambda name, **kw: Broken())

    with pytest.raises(enforcing.VerdictUnavailable) as excinfo:
        enforcing.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert "could not read the verdict store" in str(excinfo.value)


# --- The mapping ----------------------------------------------------------


def test_low_risk_deploys_all_at_once(enforcing, monkeypatch):
    wire_dynamo(enforcing, monkeypatch, FakeDynamo(item=record("low")))

    config, note = enforcing.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert config == "CodeDeployDefault.LambdaAllAtOnce"
    assert "low risk" in note


def test_medium_risk_deploys_as_a_canary(enforcing, monkeypatch):
    wire_dynamo(enforcing, monkeypatch, FakeDynamo(item=record("medium")))

    config, _ = enforcing.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert config == CANARY


def test_the_risk_level_is_read_case_insensitively(enforcing):
    assert enforcing.read_risk_level(record("MEDIUM")) == "medium"
    assert enforcing.read_risk_level(record(" low ")) == "low"


# --- Shadow mode ----------------------------------------------------------


def test_shadow_mode_never_stops_a_deploy(shadow, monkeypatch):
    """The point of arming the switch separately. Every failure that WOULD stop
    the deploy is logged and ignored while the switch is off, so the shadow
    period can run against real pipeline traffic at zero risk."""
    wire_dynamo(shadow, monkeypatch, FakeDynamo(pages=[[]]))

    config, note = shadow.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert config is None
    assert "would have stopped" in note


def test_shadow_mode_reports_the_config_it_would_have_used(shadow, monkeypatch):
    """The log line must be the decision the enforcing version WOULD make.

    A shadow mode that only proved the lookup did not crash would prove
    nothing. What makes the flip safe is that these notes are readable across a
    week of real runs before anyone arms the switch.
    """
    wire_dynamo(shadow, monkeypatch, FakeDynamo(item=record("low")))

    config, note = shadow.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert config is None, "shadow mode must not change the deployment"
    assert "low risk would have used CodeDeployDefault.LambdaAllAtOnce" in note


def test_shadow_mode_survives_a_high_risk_verdict(shadow, monkeypatch):
    wire_dynamo(shadow, monkeypatch, FakeDynamo(item=record("high")))

    config, note = shadow.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert config is None
    assert "would have stopped" in note


def test_shadow_mode_survives_an_unreadable_verdict_store(shadow, monkeypatch):
    class Broken:
        def query(self, **_):
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "Q")

    monkeypatch.setattr(shadow.boto3, "client", lambda name, **kw: Broken())

    config, note = shadow.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert config is None
    assert "AccessDeniedException" in note


# --- The eventually-consistent read ---------------------------------------


def test_the_lookup_retries_because_the_index_is_eventually_consistent(enforcing, monkeypatch):
    """DynamoDB offers no strongly consistent read on a GSI -- that is a
    property of the index, not a setting we declined to enable.

    Without the retry, a verdict written moments earlier could read as absent
    and halt a perfectly good deploy.
    """
    fake = FakeDynamo(pages=[[], [], [{"verdict_id": {"S": "job-gate-1"}}]])
    wire_dynamo(enforcing, monkeypatch, fake)

    config, _ = enforcing.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert len(fake.queries) == 3
    assert config == CANARY


def test_the_retry_is_bounded(enforcing, monkeypatch):
    """A retry that never gives up would hold the pipeline action open until it
    timed out, which reads as a hang rather than as a refusal."""
    fake = FakeDynamo(pages=[[]])
    wire_dynamo(enforcing, monkeypatch, fake)

    with pytest.raises(enforcing.VerdictUnavailable):
        enforcing.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert len(fake.queries) == enforcing.VERDICT_LOOKUP_ATTEMPTS


def test_the_full_record_is_fetched_with_a_consistent_read(enforcing, monkeypatch):
    """The index is KEYS_ONLY, so the query picks the record and the GetItem
    reads it -- and that second read CAN be strongly consistent, which confines
    eventual consistency to choosing a key rather than to reading a verdict."""
    fake = FakeDynamo()
    wire_dynamo(enforcing, monkeypatch, fake)

    enforcing.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert fake.gets[0]["ConsistentRead"] is True
    assert fake.gets[0]["Key"] == {"verdict_id": {"S": "job-gate-1"}}


def test_the_newest_verdict_wins(enforcing, monkeypatch):
    """A re-run of the Gate action inside one execution writes a second record.
    The later one is the current answer, so the query must scan backwards."""
    fake = FakeDynamo()
    wire_dynamo(enforcing, monkeypatch, fake)

    enforcing.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert fake.queries[0]["ScanIndexForward"] is False
    assert fake.queries[0]["Limit"] == 1


def test_the_lookup_is_keyed_on_the_execution_not_the_job(enforcing, monkeypatch):
    """The gate's job ID and the executor's are different strings for the same
    deploy. Keying on the job ID would find nothing, every time."""
    fake = FakeDynamo()
    wire_dynamo(enforcing, monkeypatch, fake)

    enforcing.resolve_deployment_config(verdict_job("exec-42", "job-executor")["CodePipeline.job"])

    assert fake.queries[0]["ExpressionAttributeValues"] == {":execution": {"S": "exec-42"}}


# --- Wiring ---------------------------------------------------------------


def test_a_blocked_deploy_fails_the_job_rather_than_crashing(enforcing, monkeypatch):
    """A refusal has to reach CodePipeline as a failed job. An exception that
    escapes leaves the action hanging until it times out an hour later, which
    looks like a broken executor rather than a working one."""
    failures: list[str] = []
    monkeypatch.setattr(enforcing, "_fail", lambda job_id, message: failures.append(message))
    monkeypatch.setattr(
        enforcing,
        "start_deployment",
        lambda *a: (_ for _ in ()).throw(enforcing.VerdictUnavailable("verdict is HIGH risk")),
    )

    result = enforcing.lambda_handler(verdict_job(), None)

    assert result["status"] == "blocked"
    assert "Deploy blocked" in failures[0]
    assert "HIGH risk" in failures[0]


def test_the_verdict_is_checked_before_the_artifact_is_downloaded(enforcing, monkeypatch):
    """Ordering, and it is not cosmetic.

    Resolving the verdict after publishing a version would leave a numbered
    Lambda version behind on every blocked deploy -- a half-performed deploy
    rather than a refused one.
    """
    order: list[str] = []
    monkeypatch.setattr(
        enforcing,
        "resolve_deployment_config",
        lambda job: (order.append("verdict"), (None, "note"))[1],
    )
    monkeypatch.setattr(
        enforcing,
        "fetch_artifact",
        lambda data: (order.append("artifact"), b"zip")[1],
    )
    monkeypatch.setattr(
        enforcing.boto3, "client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here"))
    )

    with pytest.raises(RuntimeError):
        enforcing.start_deployment("job-1", verdict_job()["CodePipeline.job"])

    assert order == ["verdict"], "the verdict must be resolved before anything else happens"


def test_an_unconfigured_verdict_table_does_not_break_shadow_mode(monkeypatch):
    """boto3 raises ParamValidationError -- not a ClientError -- for an empty
    TableName, so an unguarded lookup would escape the shadow handler and fail
    the deploy. An unconfigured executor must be as harmless as a working one
    while the switch is off."""
    monkeypatch.setenv("TARGET_FUNCTION", "demo-app")
    monkeypatch.setenv("CODEDEPLOY_APP", "app")
    monkeypatch.setenv("CODEDEPLOY_GROUP", "group")
    monkeypatch.delenv("VERDICT_TABLE", raising=False)
    monkeypatch.delenv("EXECUTOR_ENFORCES_VERDICT", raising=False)
    module = load_handler("executor")

    config, note = module.resolve_deployment_config(verdict_job()["CodePipeline.job"])

    assert config is None
    assert "VERDICT_TABLE is not configured" in note


def test_an_unconfigured_verdict_table_stops_the_deploy_when_enforcing(monkeypatch):
    """Same condition, opposite outcome. Enforcing with nowhere to read from is
    not a reason to guess."""
    monkeypatch.setenv("TARGET_FUNCTION", "demo-app")
    monkeypatch.setenv("CODEDEPLOY_APP", "app")
    monkeypatch.setenv("CODEDEPLOY_GROUP", "group")
    monkeypatch.delenv("VERDICT_TABLE", raising=False)
    monkeypatch.setenv("EXECUTOR_ENFORCES_VERDICT", "true")
    module = load_handler("executor")

    with pytest.raises(module.VerdictUnavailable):
        module.resolve_deployment_config(verdict_job()["CodePipeline.job"])
