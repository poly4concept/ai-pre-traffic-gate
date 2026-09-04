#!/usr/bin/env python3
"""Override the gate for ONE pipeline execution. Phase 5.3.

    # the gate halted a deploy you want to ship anyway
    python scripts/override.py allow <execution-id> --reason "known flaky alarm, fix is urgent"

    # ...and retry the stage in one step
    python scripts/override.py allow <execution-id> --reason "..." --retry

    # stop a deploy the gate was happy with
    python scripts/override.py halt <execution-id> --reason "customer incident, freeze"

    python scripts/override.py show <execution-id>
    python scripts/override.py revoke <execution-id>
    python scripts/override.py list

WHY THIS EXISTS RATHER THAN `terraform apply -var gate_decision=allow`

That works, and it is not an override path. It is a configuration change, and it
has one property that makes it dangerous: IT IS NOT SCOPED TO ONE DEPLOY.
Somebody bypasses the gate for an urgent Friday fix and the gate is off until
Tuesday, silently, still writing verdicts that look completely normal.

This writes a row keyed by pipeline execution ID. A CodePipeline execution ID is
unique and never reused, so there is no way to write one of these that affects
the next deploy. It also expires on its own -- an hour by default -- so the
failure mode where somebody forgets to revert is bounded by a clock rather than
by memory.

NEEDS ADMIN CREDENTIALS, AND THAT IS THE DESIGN

    $env:AWS_PROFILE = 'poly4'

The gate holds `dynamodb:GetItem` on the override table and no write action at
all. Nothing in the running system can write here. A gate that could write its
own override could approve itself, and every other control in the project would
be decorative.

WHO DID IT

Taken from `sts:GetCallerIdentity` rather than a `--user` flag, because an
attribution somebody types is not an attribution. The ARN in the audit record is
whoever's credentials actually ran this.

COST: a handful of DynamoDB writes on an on-demand table. Free-tier noise.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import ClientError

PROJECT = os.environ.get("PROJECT_NAME", "ai-pre-traffic-gate")
TABLE = os.environ.get("OVERRIDE_TABLE", f"{PROJECT}-overrides")
PIPELINE = os.environ.get("PIPELINE_NAME", f"{PROJECT}-pipeline")
GATE_STAGE = os.environ.get("GATE_STAGE", "Gate")
DEFAULT_TTL_MINUTES = 60

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, colour: str) -> str:
    if not _USE_COLOUR:
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "dim": "90", "bold": "1"}
    return f"\033[{codes[colour]}m{text}\033[0m"


def caller_arn(session) -> str:
    """Who is doing this, according to AWS rather than according to them."""
    try:
        return session.client("sts").get_caller_identity()["Arn"]
    except ClientError as exc:
        print(_c(f"could not identify the caller: {exc}", "red"))
        raise SystemExit(1) from exc


def create(session, args) -> int:
    if not args.reason.strip():
        # Mandatory because this row is the only explanation the audit trail
        # will ever have for why the gate was bypassed. The gate rejects a
        # reasonless override by failing closed; catching it here saves a
        # confusing pipeline run.
        print(_c("--reason is required and must not be blank.", "red"))
        return 1

    actor = caller_arn(session)
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=args.ttl_minutes)

    item = {
        "pipeline_execution_id": {"S": args.execution_id},
        "decision": {"S": args.decision},
        "reason": {"S": args.reason.strip()},
        "actor_arn": {"S": actor},
        "created_at": {"S": now.isoformat()},
        "expires_at": {"N": str(int(expires.timestamp()))},
    }

    ddb = session.client("dynamodb")
    try:
        ddb.put_item(TableName=TABLE, Item=item)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        print(_c(f"could not write the override: {code}", "red"))
        if code == "AccessDeniedException":
            print(_c("  Run as the admin profile: $env:AWS_PROFILE = 'poly4'", "yellow"))
        return 1

    colour = "yellow" if args.decision == "halt" else "green"
    print(_c(f"\noverride written: {args.decision.upper()}", colour))
    print(f"  execution:  {args.execution_id}")
    print(f"  reason:     {args.reason.strip()}")
    print(f"  by:         {actor}")
    print(f"  expires:    {expires.isoformat()}  ({args.ttl_minutes} minutes)")
    print(
        _c(
            "\n  Scoped to this execution only. It cannot affect the next deploy,\n"
            "  and it stops applying by itself when it expires.",
            "dim",
        )
    )

    if args.retry:
        return retry_stage(session, args.execution_id)

    print("\n  The gate has already run. Nothing happens until you retry the stage:")
    print(_c(f"    python scripts/override.py retry {args.execution_id}", "bold"))
    return 0


def retry_stage(session, execution_id: str) -> int:
    """Re-run the Gate stage so it reads the override we just wrote.

    Writing the row changes nothing on its own -- the gate ran minutes ago and
    already reported. This is the step people forget, so `create --retry` does
    both and the message above says so when it does not.
    """
    pipeline = session.client("codepipeline")
    try:
        pipeline.retry_stage_execution(
            pipelineName=PIPELINE,
            stageName=GATE_STAGE,
            pipelineExecutionId=execution_id,
            retryMode="FAILED_ACTIONS",
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        print(_c(f"\ncould not retry the {GATE_STAGE} stage: {code}", "red"))
        if code == "StageNotRetryableException":
            # The usual cause, and the message AWS returns does not say it.
            print(
                "  The stage is not in a failed state. An override only needs a\n"
                "  retry if the gate actually halted -- if it did not, the change\n"
                "  is already through and there is nothing to re-run."
            )
        elif code == "AccessDeniedException":
            print(_c("  Run as the admin profile: $env:AWS_PROFILE = 'poly4'", "yellow"))
        return 1

    print(_c(f"\n  retried the {GATE_STAGE} stage. The gate will re-read the override.", "green"))
    return 0


def show(session, args) -> int:
    ddb = session.client("dynamodb")
    response = ddb.get_item(
        TableName=TABLE,
        Key={"pipeline_execution_id": {"S": args.execution_id}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not item:
        print("no override for that execution")
        return 0

    expires = int(item.get("expires_at", {}).get("N", "0"))
    live = datetime.now(UTC).timestamp() < expires
    print(f"\n  decision:  {item.get('decision', {}).get('S', '?')}")
    print(f"  reason:    {item.get('reason', {}).get('S', '')}")
    print(f"  by:        {item.get('actor_arn', {}).get('S', '')}")
    print(f"  created:   {item.get('created_at', {}).get('S', '')}")
    print(f"  expires:   {datetime.fromtimestamp(expires, UTC).isoformat()}")
    print(f"  status:    {_c('LIVE', 'green') if live else _c('EXPIRED', 'dim')}")
    if not live:
        # The row can outlive its expiry by up to 48 hours -- DynamoDB deletes
        # lazily. The gate ignores it regardless; this line explains why a row
        # you can still see is doing nothing.
        print(
            _c(
                "\n  Still visible because DynamoDB TTL deletes lazily (up to 48h).\n"
                "  The gate checks the timestamp itself, so this row is inert.",
                "dim",
            )
        )
    return 0


def revoke(session, args) -> int:
    ddb = session.client("dynamodb")
    ddb.delete_item(
        TableName=TABLE,
        Key={"pipeline_execution_id": {"S": args.execution_id}},
    )
    print(_c("override revoked", "green"))
    return 0


def list_overrides(session, args) -> int:
    """Scan. Fine here and nowhere else: this table holds a handful of rows that
    expire within the hour, so there is no growth for a scan to become a problem
    on."""
    ddb = session.client("dynamodb")
    items = ddb.scan(TableName=TABLE, Limit=args.limit).get("Items", [])
    if not items:
        print("no overrides")
        return 0

    now = datetime.now(UTC).timestamp()
    print(f"\n  {'execution':40} {'decision':9} {'status':8} reason")
    print(f"  {'-' * 40} {'-' * 9} {'-' * 8} {'-' * 30}")
    for item in items:
        expires = int(item.get("expires_at", {}).get("N", "0"))
        status = "LIVE" if now < expires else "expired"
        print(
            f"  {item.get('pipeline_execution_id', {}).get('S', '?'):40} "
            f"{item.get('decision', {}).get('S', '?'):9} {status:8} "
            f"{item.get('reason', {}).get('S', '')[:40]}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        # Raw, because the docstring is the operator's reference and argparse
        # otherwise reflows it into a paragraph -- losing the example commands,
        # which are the only part somebody reads while a release is blocked.
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    sub = parser.add_subparsers(dest="command", required=True)

    for decision in ("allow", "halt"):
        p = sub.add_parser(decision, help=f"override this execution to {decision}")
        p.add_argument("execution_id", help="CodePipeline execution ID")
        p.add_argument("--reason", required=True, help="why (recorded in the audit trail)")
        p.add_argument("--ttl-minutes", type=int, default=DEFAULT_TTL_MINUTES)
        p.add_argument("--retry", action="store_true", help="also retry the Gate stage")
        p.set_defaults(func=create, decision=decision)

    p = sub.add_parser("retry", help="retry the Gate stage for an execution")
    p.add_argument("execution_id")
    p.set_defaults(func=lambda s, a: retry_stage(s, a.execution_id))

    p = sub.add_parser("show", help="show the override for an execution")
    p.add_argument("execution_id")
    p.set_defaults(func=show)

    p = sub.add_parser("revoke", help="delete the override for an execution")
    p.add_argument("execution_id")
    p.set_defaults(func=revoke)

    p = sub.add_parser("list", help="list recent overrides")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=list_overrides)

    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    return args.func(session, args)


if __name__ == "__main__":
    sys.exit(main())
