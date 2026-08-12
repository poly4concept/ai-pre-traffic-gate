#!/usr/bin/env python3
"""Drive a CodeDeploy canary on the demo app and watch the traffic split move.

Phase 1 increment 2. Two jobs:

  1. Create a CodeDeploy deployment shifting the `live` alias to a new version.
  2. Sample the alias while the deployment runs, so the split is OBSERVED rather
     than assumed.

Point 2 is the reason this script exists instead of a bare `aws deploy
create-deployment`. CodeDeploy reporting "Succeeded" tells you CodeDeploy is
satisfied; it does not tell you that callers of the alias actually received a
mix of both versions. Those are different claims, and only the second one means
the canary works. The most common way to get this wrong -- a function URL or
alias reference that quietly resolves to $LATEST -- produces a green deployment
and a traffic split of exactly 0%.

Requires write permissions (codedeploy:CreateDeployment, lambda:InvokeFunction),
so run it under the admin profile, not the read-only agent identity:

    $env:AWS_PROFILE = "poly4"
    python scripts/deploy_canary.py --to 2

    python scripts/deploy_canary.py --to 2 --dry-run   # print the AppSpec, exit
    python scripts/deploy_canary.py --rollback         # shift back to current-1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from typing import Any

import boto3
from botocore.exceptions import ClientError

DEFAULT_REGION = "us-east-1"
DEFAULT_APP = "ai-pre-traffic-gate-demo-app"
DEFAULT_GROUP = "ai-pre-traffic-gate-demo-app"
DEFAULT_FUNCTION = "ai-pre-traffic-gate-demo-app"
DEFAULT_ALIAS = "live"

TERMINAL_STATES = frozenset({"Succeeded", "Failed", "Stopped"})

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, colour: str) -> str:
    if not _USE_COLOUR:
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "dim": "90", "bold": "1"}
    return f"\033[{codes[colour]}m{text}\033[0m"


def ok(msg: str) -> None:
    print(f"  {_c('PASS', 'green')}  {msg}")


def fail(msg: str) -> None:
    print(f"  {_c('FAIL', 'red')}  {msg}")


def warn(msg: str) -> None:
    print(f"  {_c('WARN', 'yellow')}  {msg}")


def info(msg: str) -> None:
    print(f"  {_c('....', 'dim')}  {msg}")


def header(msg: str) -> None:
    print(f"\n{_c(msg, 'bold')}")


def current_alias_version(lam: Any, function: str, alias: str) -> str:
    return lam.get_alias(FunctionName=function, Name=alias)["FunctionVersion"]


def published_versions(lam: Any, function: str) -> list[str]:
    """Numbered versions only, ascending. $LATEST is not a deploy target."""
    versions: list[str] = []
    paginator = lam.get_paginator("list_versions_by_function")
    for page in paginator.paginate(FunctionName=function):
        versions.extend(v["Version"] for v in page["Versions"] if v["Version"] != "$LATEST")
    return sorted(versions, key=int)


def build_appspec(function: str, alias: str, current: str, target: str) -> str:
    """Build the AppSpec. This, not the deployment group, names the target.

    For the Lambda compute platform the deployment group carries only policy --
    traffic-shifting shape and rollback rules. Which function and which alias
    move is supplied per deployment, here.

    `version` must be the number 0.0, not the string "0.0"; CodeDeploy rejects
    the quoted form. The resource key ("demo_app") is an arbitrary label.
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


def sample_traffic(lam: Any, function: str, alias: str, samples: int) -> Counter[str]:
    """Invoke the alias N times and tally which version answered.

    Reads `lambda_version` out of the response body, which the demo app takes
    from the runtime's own context object rather than from configuration -- so it
    cannot drift from the version that actually executed.
    """
    seen: Counter[str] = Counter()
    for _ in range(samples):
        try:
            resp = lam.invoke(FunctionName=f"{function}:{alias}", Payload=b"{}")
            payload = json.loads(resp["Payload"].read())
            body = json.loads(payload.get("body", "{}"))
            seen[body.get("lambda_version", "unknown")] += 1
        except ClientError as exc:
            seen[f"error:{exc.response['Error']['Code']}"] += 1
        except (KeyError, ValueError):
            seen["error:unparseable"] += 1
    return seen


def render_split(seen: Counter[str], total: int) -> str:
    if not total:
        return "no samples"
    parts = []
    for version, count in sorted(seen.items()):
        pct = 100 * count / total
        parts.append(f"v{version}={pct:.0f}%")
    return "  ".join(parts)


def watch(
    cd: Any,
    lam: Any,
    deployment_id: str,
    function: str,
    alias: str,
    samples: int,
    poll_seconds: int,
) -> str:
    """Poll deployment status, sampling the alias between polls."""
    header("3. Watching the traffic shift")
    info("Sampling the alias between status polls. Both versions should appear")
    info("while the canary is in progress -- that is the thing being proven.")
    print()

    saw_split = False
    status = "Created"

    while status not in TERMINAL_STATES:
        info_resp = cd.get_deployment(deploymentId=deployment_id)
        status = info_resp["deploymentInfo"]["status"]

        seen = sample_traffic(lam, function, alias, samples)
        distinct = {k for k in seen if not k.startswith("error:")}
        if len(distinct) > 1:
            saw_split = True

        marker = _c("SPLIT", "green") if len(distinct) > 1 else _c("     ", "dim")
        print(f"  {marker}  {status:<12}  {render_split(seen, samples)}")

        if status in TERMINAL_STATES:
            break
        time.sleep(poll_seconds)

    print()
    if status == "Succeeded":
        ok(f"Deployment {status}")
    else:
        fail(f"Deployment {status}")
        err = info_resp["deploymentInfo"].get("errorInformation", {})
        if err:
            info(f"{err.get('code', '?')}: {err.get('message', '')}")

    if saw_split:
        ok("Observed BOTH versions serving the alias during the deployment")
        info("Traffic shifting is real, not just reported. This is the increment's")
        info("actual deliverable.")
    else:
        warn("Never observed both versions serving simultaneously.")
        info("The deployment may simply have completed between two polls -- at a")
        info("1-minute canary interval that is easy to miss. But rule out the")
        info("failure this is designed to catch:")
        info("  * is the caller addressing the ALIAS, not $LATEST?")
        info("  * did the alias actually move? check get-alias before and after")
        info("  * lower --poll and raise --samples, then redeploy")

    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", DEFAULT_REGION))
    parser.add_argument("--function", default=DEFAULT_FUNCTION)
    parser.add_argument("--alias", default=DEFAULT_ALIAS)
    parser.add_argument("--app", default=DEFAULT_APP)
    parser.add_argument("--group", default=DEFAULT_GROUP)
    parser.add_argument("--to", help="Target version. Defaults to the newest published.")
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Shift to the version below the current one instead of forward.",
    )
    parser.add_argument("--samples", type=int, default=20, help="Invocations per poll.")
    parser.add_argument("--poll", type=int, default=5, help="Seconds between polls.")
    parser.add_argument("--dry-run", action="store_true", help="Print the AppSpec and exit.")
    args = parser.parse_args()

    lam = boto3.client("lambda", region_name=args.region)
    cd = boto3.client("codedeploy", region_name=args.region)

    header("1. Current state")
    try:
        current = current_alias_version(lam, args.function, args.alias)
        versions = published_versions(lam, args.function)
    except ClientError as exc:
        fail(f"Could not read function state: {exc.response['Error']['Code']}")
        info(exc.response["Error"].get("Message", ""))
        return 1

    info(f"function        {args.function}")
    info(f"alias           {args.alias} -> v{current}")
    info(f"published       {', '.join(f'v{v}' for v in versions) or 'none'}")

    if args.to:
        target = args.to
    elif args.rollback:
        below = [v for v in versions if int(v) < int(current)]
        if not below:
            fail(f"Nothing to roll back to; v{current} is the earliest version.")
            return 1
        target = below[-1]
    else:
        target = versions[-1] if versions else current

    if target not in versions:
        fail(f"v{target} is not a published version of this function.")
        info(f"Published: {', '.join(versions) or 'none'}")
        return 1

    if target == current:
        fail(f"Alias already points at v{target}; there is nothing to shift.")
        info("Publish a new version first: bump demo_app_version and apply.")
        return 1

    info(f"target          v{current} -> v{target}")

    appspec = build_appspec(args.function, args.alias, current, target)
    if args.dry_run:
        header("AppSpec (dry run -- nothing created)")
        print(json.dumps(json.loads(appspec), indent=2))
        return 0

    header("2. Creating deployment")
    try:
        resp = cd.create_deployment(
            applicationName=args.app,
            deploymentGroupName=args.group,
            revision={"revisionType": "AppSpecContent", "appSpecContent": {"content": appspec}},
            description=f"Canary {args.alias}: v{current} -> v{target}",
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        fail(f"CreateDeployment failed: {code}")
        info(exc.response["Error"].get("Message", ""))
        if code == "AccessDeniedException":
            info("The read-only agent identity cannot create deployments by design.")
            info("Run under the admin profile: $env:AWS_PROFILE = 'poly4'")
        return 1

    deployment_id = resp["deploymentId"]
    ok(f"Deployment {deployment_id}")

    status = watch(cd, lam, deployment_id, args.function, args.alias, args.samples, args.poll)

    header("Result")
    final = current_alias_version(lam, args.function, args.alias)
    info(f"alias {args.alias} now -> v{final}")
    if status == "Succeeded" and final == target:
        ok("Canary complete and alias moved.")
        return 0
    if status != "Succeeded" and final == current:
        ok(f"Deployment {status} and the alias rolled back to v{current}, as configured.")
        return 1
    warn(f"Deployment {status}, alias at v{final} (expected v{target} or v{current}).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
