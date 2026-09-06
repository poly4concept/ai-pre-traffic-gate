"""Phase 1 increment 3 -- the executor.

This is the half of the architecture that HOLDS deploy permissions. Its
counterpart, the gate, holds none. That split is the project's central security
claim and it exists from the first version of both functions rather than being
retrofitted in Phase 5.

Why this function exists at all
-------------------------------
CodePipeline has no native action for canarying a Lambda. Checked, not assumed:
`list-action-types` offers `CodeDeploy` (EC2/on-premises only) and
`CodeDeployToECS`. There is no CodeDeployToLambda. The two ways to drive a
Lambda canary from a pipeline are:

  1. A CloudFormation/SAM deploy action, where SAM's DeploymentPreference
     creates the CodeDeploy deployment for you.
  2. A Lambda invoke action that calls CreateDeployment itself.

Option 1 means adopting a second IaC tool alongside Terraform and letting
CloudFormation own resources Terraform also cares about. Option 2 is a hundred
lines of Python and puts the deploy permissions in exactly one identifiable
place. We take option 2, and that place is this file.

The continuation-token pattern
------------------------------
A canary takes minutes; a Lambda invocation should not. CodePipeline's answer is
the continuation token: report success WITH a token and CodePipeline re-invokes
this same action later, handing the token back. So this function is a state
machine with two entry paths:

  no token   -> publish the new version, start the deployment, return the
                deployment ID as the token
  has token  -> look up that deployment's status; still running means return the
                token again, finished means report the final result

Without this, the pipeline would report the deploy stage green the instant the
deployment was *created*, and any later rollback would happen after a pipeline
that already claimed success.

Failure direction
-----------------
The gate fails closed toward "halt". This function fails closed toward "fail the
pipeline job". Every unhandled path ends at PutJobFailureResult. A deploy that
cannot be confirmed is not a deploy that succeeded.
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TARGET_FUNCTION = os.environ.get("TARGET_FUNCTION", "")
TARGET_ALIAS = os.environ.get("TARGET_ALIAS", "live")
CODEDEPLOY_APP = os.environ.get("CODEDEPLOY_APP", "")
CODEDEPLOY_GROUP = os.environ.get("CODEDEPLOY_GROUP", "")

# Phase 5.1 -- the verdict finally reaches the thing that deploys.
VERDICT_TABLE = os.environ.get("VERDICT_TABLE", "")
VERDICT_INDEX = os.environ.get("VERDICT_INDEX", "by_pipeline_execution")

# The executor's equivalent of the gate's MODEL_VERDICT_CAN_ACT, and armed the
# same way: separately, deliberately, and off by default.
#
# False -> read the verdict, log which deployment config it WOULD have chosen,
#          then deploy exactly as before. Shadow mode for the executor.
# True  -> the risk level selects the deployment config, and an unreadable
#          verdict stops the deploy.
#
# Two switches rather than one because they fail in different directions and
# arming them together would confuse which one broke. The gate's switch controls
# whether a model verdict can HALT a pipeline. This one controls whether it can
# choose HOW MUCH TRAFFIC a new version gets. A bug in the first stops deploys
# that should have shipped; a bug in the second ships a change faster than it
# should have. Flipping both at once means a bad pipeline run has two candidate
# causes and no way to separate them.
EXECUTOR_ENFORCES_VERDICT = os.environ.get("EXECUTOR_ENFORCES_VERDICT", "").strip().lower() in {
    "true",
    "1",
    "yes",
}

# CodeDeploy deployment states that mean "stop asking".
SUCCESS_STATES = frozenset({"Succeeded"})
FAILURE_STATES = frozenset({"Failed", "Stopped"})

# The whole point of the phase, in one mapping.
#
# `low` gets AWS's built-in all-at-once config rather than a custom one: a full
# deploy has no parameters to get wrong, and using the managed config means
# there is no second custom config to keep in step with the first.
#
# `high` is deliberately absent, and absent rather than mapped to a "safest"
# config. There is no traffic percentage that makes a high-risk change safe to
# ship unattended, so the only correct response is not to ship it. See
# `deployment_config_for`.
DEPLOYMENT_CONFIG_FOR_RISK = {
    "low": "CodeDeployDefault.LambdaAllAtOnce",
    "medium": os.environ.get("CANARY_CONFIG", ""),
}

# A GSI read is ALWAYS eventually consistent -- DynamoDB offers no strongly
# consistent read on a global secondary index, so this is a property of the
# lookup and not a setting we declined to switch on.
#
# In practice the gap is comfortable: the gate writes the verdict, reports its
# job result, and CodePipeline then transitions to the Deploy stage, which is
# seconds at minimum. But "comfortable in practice" is how you get a failure
# that only appears under load, and the failure here would halt a legitimate
# deploy. So the query is retried briefly before the executor concludes there is
# no verdict.
#
# Note the direction: this retry exists to avoid a FALSE HALT. Not finding a
# verdict still stops the deploy -- the retry only makes sure that when we say
# there is no verdict, there really is not one.
VERDICT_LOOKUP_ATTEMPTS = 3
VERDICT_LOOKUP_DELAY_SECONDS = 1.0

# PutJobFailureResult truncates at 265 characters. Truncating deliberately beats
# having the API reject the call and leaving the pipeline job hanging until it
# times out an hour later.
MAX_FAILURE_MESSAGE = 265


def build_appspec(function: str, alias: str, current: str, target: str) -> str:
    """Build the CodeDeploy AppSpec naming what to shift.

    NOTE: `scripts/deploy_canary.py` contains a deliberate copy of this. The two
    live in different deployment units -- one is a local operator script, the
    other is bundled into a Lambda zip -- and sharing code across that boundary
    would mean a build step for a fifteen-line function. `tests/test_executor.py`
    asserts the two produce byte-identical output, so drift fails the build
    rather than surfacing as a rejected deployment months later.

    `version` must be the number 0.0, not the string "0.0". CodeDeploy rejects
    the quoted form with an error that does not name the offending field.
    """
    return json.dumps(
        {
            "version": 0.0,
            "Resources": [
                {
                    "demo_app": {
                        "Type": "AWS::Lambda::Function",
                        "Properties": {
                            "Name": function,
                            "Alias": alias,
                            "CurrentVersion": current,
                            "TargetVersion": target,
                        },
                    }
                }
            ],
        }
    )


class VerdictUnavailable(Exception):
    """No usable verdict for this pipeline execution.

    Its own type so the caller cannot mistake it for a transport error and
    retry it into a deploy. Raised for every distinct reason -- no execution ID,
    no record, an unreadable record, an unknown risk level -- because they all
    have the same correct response and enumerating them at the call site would
    invite someone to make one of them an exception.
    """


def pipeline_execution_id(job: dict[str, Any]) -> str | None:
    """The one identifier the gate and the executor genuinely share.

    Not the job ID: each pipeline ACTION gets its own, so the gate's job ID and
    the executor's are different strings for the same deploy. The execution ID
    is the same for every action in one run through the pipeline, which is
    exactly the join key needed here.

    READ FROM USERPARAMETERS, NOT pipelineContext, AND THAT WAS A BUG (F-023).

    This read `job.data.pipelineContext.pipelineExecutionId`, which does not
    exist in a Lambda-invoke event -- `pipelineContext` belongs to the custom
    action job structure returned by `PollForJobs`. It returned None on every
    run, so `find_verdict` was never once called and the risk branching added in
    5.1 had never executed. The logs said `VERDICT SHADOW: would have STOPPED
    this deploy -- job carries no pipelineExecutionId`, which reads like the
    shadow mode working rather than like a lookup that could never succeed.

    CodePipeline exposes the value as `#{codepipeline.PipelineExecutionId}`,
    interpolated into this action's UserParameters in pipeline.tf.
    """
    config = job.get("data", {}).get("actionConfiguration", {}).get("configuration", {})
    raw = config.get("UserParameters")
    if isinstance(raw, str) and raw:
        try:
            execution = json.loads(raw).get("pipeline_execution_id")
            if isinstance(execution, str) and execution:
                return execution
        except (ValueError, AttributeError):
            logger.warning("UserParameters is not readable JSON; no execution ID")

    # Fallback, kept because it costs nothing and would start working if a
    # future action type populated it.
    context = job.get("data", {}).get("pipelineContext", {})
    execution = context.get("pipelineExecutionId")
    return execution if isinstance(execution, str) and execution else None


def find_verdict(execution_id: str, *, sleep=time.sleep) -> dict[str, Any]:
    """The verdict the gate recorded for this pipeline execution.

    Two calls, on purpose. The index is KEYS_ONLY, so the query answers "which
    record" and the GetItem fetches it. That costs one extra read unit -- about
    a ten-millionth of a dollar -- and buys two things worth more: the executor
    reads the FULL record including any human override, and the GetItem is a
    strongly consistent read of the base table, so the only eventually
    consistent step is deciding which key to fetch.

    Newest first, because a re-run of the Gate action within one execution
    writes a second record and the later one is the current answer.
    """
    if not VERDICT_TABLE:
        # Checked here rather than left to boto3, which raises
        # ParamValidationError for an empty TableName -- and that is NOT a
        # ClientError, so it would sail past the handler below and fail the
        # deploy even in shadow mode. An unconfigured executor must be as
        # harmless in shadow as a misconfigured one.
        raise VerdictUnavailable("VERDICT_TABLE is not configured")

    dynamodb = boto3.client("dynamodb")

    key = None
    for attempt in range(1, VERDICT_LOOKUP_ATTEMPTS + 1):
        page = dynamodb.query(
            TableName=VERDICT_TABLE,
            IndexName=VERDICT_INDEX,
            KeyConditionExpression="pipeline_execution_id = :execution",
            ExpressionAttributeValues={":execution": {"S": execution_id}},
            ScanIndexForward=False,
            Limit=1,
        )
        items = page.get("Items") or []
        if items:
            key = {"verdict_id": items[0]["verdict_id"]}
            break
        if attempt < VERDICT_LOOKUP_ATTEMPTS:
            logger.info(
                "no verdict for execution %s yet (attempt %d/%d); the index is "
                "eventually consistent, retrying",
                execution_id,
                attempt,
                VERDICT_LOOKUP_ATTEMPTS,
            )
            sleep(VERDICT_LOOKUP_DELAY_SECONDS)

    if key is None:
        raise VerdictUnavailable(
            f"no verdict recorded for pipeline execution {execution_id}. The gate "
            "either did not run or could not write its record."
        )

    record = dynamodb.get_item(TableName=VERDICT_TABLE, Key=key, ConsistentRead=True).get("Item")
    if not record:
        # The index named a record the table does not have. Should not happen;
        # worth its own message if it ever does, because the alternative is
        # debugging a "no verdict" that is really an index inconsistency.
        raise VerdictUnavailable(
            f"index named verdict {key['verdict_id'].get('S')} but the table has no such record"
        )
    return record


def read_risk_level(record: dict[str, Any]) -> str:
    """Pull the risk level out of a raw DynamoDB item.

    Raw item shape rather than a deserialiser: this is the only field the
    executor reads, and pulling one string out of `{"S": "medium"}` does not
    justify bundling boto3.dynamodb.types into a Lambda that currently has no
    dependency on it.
    """
    verdict = record.get("verdict", {}).get("M", {})
    level = verdict.get("risk_level", {}).get("S", "")
    if not level:
        raise VerdictUnavailable("verdict record carries no risk_level")
    return level.strip().lower()


def read_override(record: dict[str, Any]) -> tuple[str, str]:
    """(decision, reason) from a human override, or ("", "") if nobody intervened.

    THE GAP THIS CLOSES, AND IT MADE PHASE 5.3 NON-FUNCTIONAL (F-025).

    `read_risk_level` reads the MODEL's verdict, and a human override does not
    change it -- the audit record deliberately keeps `verdict.risk_level` as
    what the model said and records the override as a separate field, because
    "a human overrode a high-risk verdict" and "the model said low" are
    different events and flattening them would destroy the audit trail.

    The consequence nobody traced: the executor read only `verdict.risk_level`,
    so an override could never reach it. In advisory mode -- where the gate does
    not block and the EXECUTOR is the thing that refuses -- writing an override
    and retrying the Gate stage produced a fresh verdict record saying
    `decision: allow`, and the executor refused it again on the unchanged
    `risk_level: high`. The override path worked only in the one configuration
    where the gate was the blocker.

    Worse, `find_verdict`'s docstring claimed this function's caller "reads the
    FULL record including any human override". It fetched the override and then
    never looked at it -- a comment describing an intention rather than the
    behaviour, which is exactly F-022 again.
    """
    override = record.get("override", {}).get("M", {})
    decision = override.get("decision", {}).get("S", "").strip().lower()
    reason = override.get("reason", {}).get("S", "")
    return decision, reason


def deployment_config_for(risk_level: str) -> str:
    """Which CodeDeploy config a risk level selects.

    Raises rather than defaulting, for both unknown values and `high`.

    A default here would be the single most dangerous line in the executor. Give
    this function a `risk_level` it does not recognise -- a typo, a new level
    added to the enum and not to this map, a corrupted record -- and a default
    of "canary" would quietly ship it. The gate's `action_for` raises for the
    same reason (D-032): a mapping that always answers is a mapping that answers
    wrongly when it does not know.
    """
    config = DEPLOYMENT_CONFIG_FOR_RISK.get(risk_level)
    if config:
        return config
    if risk_level == "high":
        raise VerdictUnavailable(
            "verdict is HIGH risk. There is no traffic percentage that makes a "
            "high-risk change safe to ship without a human, so this deploy stops here."
        )
    raise VerdictUnavailable(f"risk level {risk_level!r} has no deployment config")


def config_for(risk_level: str, override: str = "") -> str:
    """Which config to deploy with, once a human override is taken into account.

    Three rules, and the middle one is the interesting decision:

      halt      refuse, whatever the model said. A human stopping a deploy the
                model was happy with is the case the override path exists for
                just as much as the reverse, and it must not be defeatable by a
                `low` verdict.

      allow     CANARY, not a full deploy -- even for a verdict of `high`.

                An override says "I accept this risk", not "I am certain this is
                fine". The model flagged something; a human decided to ship
                anyway; shifting 10% of traffic for a minute still catches it if
                the model was right, and costs a minute if it was wrong. Going
                straight to a full deploy would treat a human's willingness to
                proceed as evidence about the change, which it is not.

                This is also the only path by which a `high` verdict ever
                deploys, and that asymmetry is deliberate: it takes a named
                person, a written reason, and admin credentials.

      no override   the risk level decides, exactly as before.
    """
    if override == "halt":
        raise VerdictUnavailable(
            "a human overrode this deploy to HALT, which stands regardless of "
            f"the model's {risk_level!r} verdict"
        )
    if override == "allow":
        canary = DEPLOYMENT_CONFIG_FOR_RISK.get("medium")
        if not canary:
            # Same rule as everywhere else here: no default. If the canary
            # config is unset there is no safe way to honour an override.
            raise VerdictUnavailable(
                "a human overrode this deploy to ALLOW, but no canary config is "
                "configured to ship it gradually with"
            )
        return canary
    return deployment_config_for(risk_level)


def resolve_deployment_config(job: dict[str, Any]) -> tuple[str | None, str]:
    """(config to use, human-readable note). Never raises.

    Returns `None` for the config when the caller should use the deployment
    group's own default -- which is what happens in shadow, and only in shadow.

    THE ASYMMETRY THAT MAKES THIS SAFE TO SWITCH ON: when the executor is not
    enforcing, every failure to read a verdict is logged and ignored. When it
    is, every one of them stops the deploy. So the shadow period is not a
    rehearsal that proves nothing -- the log line it emits is exactly the
    decision the enforcing version would have made, and if that line says
    "would have STOPPED" on runs that ought to have shipped, the switch is not
    ready to flip.
    """
    try:
        execution_id = pipeline_execution_id(job)
        if not execution_id:
            raise VerdictUnavailable("job carries no pipelineExecutionId")
        record = find_verdict(execution_id)
        risk_level = read_risk_level(record)
        override, override_reason = read_override(record)
        config = config_for(risk_level, override)
    except VerdictUnavailable as exc:
        if EXECUTOR_ENFORCES_VERDICT:
            raise
        logger.warning("VERDICT SHADOW: would have STOPPED this deploy -- %s", exc)
        return None, f"shadow: would have stopped ({exc})"
    except ClientError as exc:
        # A DynamoDB failure is not a verdict. Same rule, same direction.
        code = exc.response["Error"]["Code"]
        if EXECUTOR_ENFORCES_VERDICT:
            raise VerdictUnavailable(f"could not read the verdict store ({code})") from exc
        logger.warning("VERDICT SHADOW: would have STOPPED this deploy -- %s", code)
        return None, f"shadow: would have stopped (verdict store {code})"

    # Named in the log line and in the pipeline's job result, because a deploy
    # that only happened because a human insisted is the single most important
    # thing to be able to find afterwards.
    overridden = f", OVERRIDDEN to {override} ({override_reason})" if override else ""

    if EXECUTOR_ENFORCES_VERDICT:
        logger.info("Verdict is %s%s; deploying with %s", risk_level, overridden, config)
        return config, f"{risk_level} risk{overridden} -> {config}"

    logger.info(
        "VERDICT SHADOW: verdict is %s%s; would have used %s", risk_level, overridden, config
    )
    return None, f"shadow: {risk_level} risk{overridden} would have used {config}"


def fetch_artifact(data: dict[str, Any]) -> bytes:
    """Download the build artifact using CodePipeline's per-job credentials.

    CodePipeline hands each job short-lived credentials scoped to the artifact it
    is allowed to read. Using them rather than the function's own role means this
    Lambda holds no standing read access to the artifact bucket -- there is no
    permission here to abuse outside the context of a job the pipeline chose to
    give us.

    The artifact IS the deployment package: the buildspec sets
    `base-directory: services/demo_app`, so the zip CodePipeline produces has
    handler.py at its root, which is exactly what Lambda expects.
    """
    creds = data["artifactCredentials"]
    location = data["inputArtifacts"][0]["location"]["s3Location"]

    s3 = boto3.client(
        "s3",
        aws_access_key_id=creds["accessKeyId"],
        aws_secret_access_key=creds["secretAccessKey"],
        aws_session_token=creds["sessionToken"],
    )

    buf = io.BytesIO()
    s3.download_fileobj(location["bucketName"], location["objectKey"], buf)
    return buf.getvalue()


def start_deployment(job_id: str, job: dict[str, Any]) -> None:
    """Publish the built code as a new version and begin shifting traffic."""
    # FIRST. Before the clients, before the artifact download, and well before
    # publishing a version -- because a verdict that stops the deploy should
    # stop it without having minted a Lambda version nothing will ever point
    # at. Ordering is the whole difference between refusing to deploy and
    # half-deploying, and it is asserted by a test rather than left to whoever
    # edits this function next.
    deployment_config, verdict_note = resolve_deployment_config(job)

    data = job.get("data", {})
    lam = boto3.client("lambda")
    codedeploy = boto3.client("codedeploy")

    package = fetch_artifact(data)
    logger.info("Fetched artifact: %d bytes", len(package))

    current = lam.get_alias(FunctionName=TARGET_FUNCTION, Name=TARGET_ALIAS)["FunctionVersion"]

    # Publish=True makes this atomic: update the code and freeze it as a
    # numbered version in one call, with no window in which $LATEST holds code
    # that no version corresponds to.
    published = lam.update_function_code(
        FunctionName=TARGET_FUNCTION,
        ZipFile=package,
        Publish=True,
    )
    target = published["Version"]
    logger.info("Published v%s (alias currently v%s)", target, current)

    # Lambda deduplicates identical code: republishing an unchanged package
    # returns the existing version rather than minting a new one. CodeDeploy
    # rejects a deployment whose source and target match, so this is a normal
    # outcome to report as success, not an error. It happens whenever the
    # pipeline runs on a commit that touched no application code -- a README
    # edit, or an infrastructure change.
    if target == current:
        logger.info("Code unchanged; alias already at v%s. Nothing to deploy.", target)
        _succeed(job_id, note=f"no code change; alias remains at v{target}")
        return

    request: dict[str, Any] = {
        "applicationName": CODEDEPLOY_APP,
        "deploymentGroupName": CODEDEPLOY_GROUP,
        "revision": {
            "revisionType": "AppSpecContent",
            "appSpecContent": {
                "content": build_appspec(TARGET_FUNCTION, TARGET_ALIAS, current, target)
            },
        },
        "description": f"Pipeline deploy {TARGET_ALIAS}: v{current} -> v{target} ({verdict_note})",
    }
    # Omitted rather than passed as None when not enforcing: leaving the key out
    # lets the deployment group's own config apply, which is the pre-Phase-5
    # behaviour and the thing shadow mode must not alter.
    if deployment_config:
        request["deploymentConfigName"] = deployment_config

    resp = codedeploy.create_deployment(**request)
    deployment_id = resp["deploymentId"]
    logger.info("Created deployment %s", deployment_id)

    # Hand the deployment ID back as the continuation token. CodePipeline will
    # re-invoke this function with it, which is how a minutes-long canary is
    # tracked by a function that returns in seconds.
    _succeed(job_id, continuation_token=deployment_id)


def check_deployment(job_id: str, deployment_id: str) -> None:
    """Re-invoked with a token: report on the deployment it identifies."""
    codedeploy = boto3.client("codedeploy")
    info = codedeploy.get_deployment(deploymentId=deployment_id)["deploymentInfo"]
    status = info["status"]
    logger.info("Deployment %s is %s", deployment_id, status)

    if status in SUCCESS_STATES:
        _succeed(job_id, note=f"deployment {deployment_id} succeeded")
        return

    if status in FAILURE_STATES:
        err = info.get("errorInformation", {})
        detail = f"{err.get('code', 'unknown')}: {err.get('message', 'no detail')}"
        # An auto-rollback has already returned the alias to the previous
        # version by this point. Failing the job stops the pipeline so the
        # rollback is not immediately followed by another attempt.
        _fail(job_id, f"Deployment {deployment_id} {status}. {detail}")
        return

    _succeed(job_id, continuation_token=deployment_id)


def _succeed(job_id: str, continuation_token: str | None = None, note: str = "") -> None:
    client = boto3.client("codepipeline")
    if continuation_token:
        client.put_job_success_result(jobId=job_id, continuationToken=continuation_token)
        logger.info("Reported success with continuation token %s", continuation_token)
    else:
        client.put_job_success_result(jobId=job_id)
        logger.info("Reported final success. %s", note)


def _fail(job_id: str, message: str) -> None:
    client = boto3.client("codepipeline")
    client.put_job_failure_result(
        jobId=job_id,
        failureDetails={"type": "JobFailed", "message": message[:MAX_FAILURE_MESSAGE]},
    )
    logger.error("Reported failure: %s", message)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point. Every path out of here reports to CodePipeline exactly once."""
    job = event.get("CodePipeline.job")
    if not job:
        # Not from a pipeline -- a manual test invoke. Do nothing rather than
        # guess: this function's actions are irreversible and it has the
        # permissions to take them.
        logger.warning("Invoked without a CodePipeline job. Taking no action.")
        return {"status": "ignored", "reason": "no CodePipeline.job in event"}

    job_id = job["id"]
    data = job.get("data", {})
    token = data.get("continuationToken")

    try:
        missing = [
            name
            for name, value in (
                ("TARGET_FUNCTION", TARGET_FUNCTION),
                ("CODEDEPLOY_APP", CODEDEPLOY_APP),
                ("CODEDEPLOY_GROUP", CODEDEPLOY_GROUP),
            )
            if not value
        ]
        if missing:
            # Same reasoning as the gate: unconfigured is not a reason to
            # improvise, it is a reason to stop.
            _fail(job_id, f"Executor is misconfigured; missing {', '.join(missing)}")
            return {"status": "failed", "reason": "misconfigured"}

        if token:
            check_deployment(job_id, token)
        else:
            start_deployment(job_id, job)

    except VerdictUnavailable as exc:
        # Its own branch above ClientError, and the message is written for the
        # person reading a red pipeline stage rather than for a log parser.
        # "Deploy blocked: verdict is HIGH risk" is a different morning from
        # "AccessDeniedException".
        logger.warning("Deploy blocked by the verdict: %s", exc)
        _fail(job_id, f"Deploy blocked: {exc}")
        return {"status": "blocked", "reason": "verdict"}

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        message = exc.response["Error"].get("Message", "")
        logger.exception("AWS call failed")
        _fail(job_id, f"{code}: {message}")
        return {"status": "failed", "reason": code}

    except Exception as exc:  # noqa: BLE001 - deliberate catch-all
        # The catch-all is the point. An unanticipated exception in the thing
        # holding deploy permissions must stop the pipeline, not fall through to
        # whatever CodePipeline does when an action never reports.
        logger.exception("Unhandled error")
        _fail(job_id, f"Unhandled executor error: {type(exc).__name__}: {exc}")
        return {"status": "failed", "reason": "unhandled"}

    return {"status": "reported", "job_id": job_id}
