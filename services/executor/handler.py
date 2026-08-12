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
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TARGET_FUNCTION = os.environ.get("TARGET_FUNCTION", "")
TARGET_ALIAS = os.environ.get("TARGET_ALIAS", "live")
CODEDEPLOY_APP = os.environ.get("CODEDEPLOY_APP", "")
CODEDEPLOY_GROUP = os.environ.get("CODEDEPLOY_GROUP", "")

# CodeDeploy deployment states that mean "stop asking".
SUCCESS_STATES = frozenset({"Succeeded"})
FAILURE_STATES = frozenset({"Failed", "Stopped"})

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


def start_deployment(job_id: str, data: dict[str, Any]) -> None:
    """Publish the built code as a new version and begin shifting traffic."""
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

    resp = codedeploy.create_deployment(
        applicationName=CODEDEPLOY_APP,
        deploymentGroupName=CODEDEPLOY_GROUP,
        revision={
            "revisionType": "AppSpecContent",
            "appSpecContent": {
                "content": build_appspec(TARGET_FUNCTION, TARGET_ALIAS, current, target)
            },
        },
        description=f"Pipeline deploy {TARGET_ALIAS}: v{current} -> v{target}",
    )
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
            start_deployment(job_id, data)

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
