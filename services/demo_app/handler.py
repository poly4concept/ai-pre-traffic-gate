"""The demo application. Phase 1.

This service exists only to be deployed, canaried, and halted. It has no
business logic, holds no data, and talks to nothing. That is deliberate: when
the pipeline misbehaves, the application must never be a plausible suspect.

The one job it does have is to make a canary *visible*. During a CodeDeploy
traffic shift, two Lambda versions serve the same alias simultaneously. Every
response reports which version produced it, so a loop of requests against the
function URL shows the traffic split moving in real time -- 100/0, then 90/10,
then 0/100. That is the demo, and it is why the response echoes version
information that a real service would have no reason to expose.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

# Set by Terraform. APP_VERSION is what we bump to produce a visibly different
# deployment; it is not read from anywhere else and has no meaning beyond
# "this is a different build from the last one".
APP_VERSION = os.environ.get("APP_VERSION", "unknown")
COMMIT_SHA = os.environ.get("COMMIT_SHA", "unknown")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return a small JSON document identifying this exact Lambda version."""
    body = {
        "service": "demo-app",
        "app_version": APP_VERSION,
        "commit_sha": COMMIT_SHA,
        # context.function_version is the load-bearing field. It comes from the
        # runtime, not from configuration, so it cannot drift from reality --
        # during a canary this is what differs between two otherwise identical
        # responses.
        "lambda_version": getattr(context, "function_version", "unknown"),
        "request_id": getattr(context, "aws_request_id", "unknown"),
        "timestamp": datetime.now(UTC).isoformat(),
    }

    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
