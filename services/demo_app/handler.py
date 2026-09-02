"""The demo application. Phase 1, with controllable faults from Phase 2.5.

This service exists only to be deployed, canaried, halted -- and, since 2.5, to
be made unhealthy on demand. It has no business logic, holds no data, and talks
to nothing except its own fault-config record. That is deliberate: when the
pipeline misbehaves, the application must never be a plausible suspect.

The one job it does have is to make a canary *visible*. During a CodeDeploy
traffic shift, two Lambda versions serve the same alias simultaneously. Every
response reports which version produced it, so a loop of requests against the
function URL shows the traffic split moving in real time -- 100/0, then 90/10,
then 0/100. That is the demo, and it is why the response echoes version
information that a real service would have no reason to expose.

PHASE 2.5: WHY THE INJECTED FAULT IS RAISED RATHER THAN RETURNED AS A 500

Because CloudWatch's `Errors` metric counts *unhandled exceptions*, not HTTP
status codes. A handler that catches its own fault and returns
`{"statusCode": 500}` is, as far as Lambda is concerned, a complete success --
`Errors` stays at zero, the alarm never fires, and the gate reads a target with
a 0% error rate while every request to it is failing.

That is the same absent-versus-zero trap the signals package is built around,
arriving from the other direction, and it would quietly break every synthesized
health scenario. So the exception propagates.

See services/demo_app/faults.py for why the config lives in DynamoDB rather than
in an environment variable -- the short version is that env vars are snapshotted
into published versions, and the alias serves published versions.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from faults import apply_faults, load_faults

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Set by Terraform. APP_VERSION is what we bump to produce a visibly different
# deployment; it is not read from anywhere else and has no meaning beyond
# "this is a different build from the last one".
APP_VERSION = os.environ.get("APP_VERSION", "unknown")
COMMIT_SHA = os.environ.get("COMMIT_SHA", "unknown")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Return a small JSON document identifying this exact Lambda version."""
    version = getattr(context, "function_version", "unknown")

    # Reading the config is wrapped, applying it is NOT.
    #
    # A config read that fails must leave the app healthy (faults.py explains
    # why), but once a fault has been read and is meant to fire, it has to be
    # allowed to -- swallowing it here would defeat the entire point of the
    # module and produce a service that is only ever synthetically healthy.
    try:
        config = load_faults()
    except Exception:  # noqa: BLE001 - defence in depth; load_faults already catches
        logger.exception("fault config unreadable; serving normally")
        config = None

    applied: dict[str, Any] = {"injected": False}
    if config is not None:
        applied = apply_faults(config, version)
        if applied["injected"]:
            logger.warning("injected fault: %s", json.dumps(applied))

    body = {
        "service": "demo-app",
        "app_version": APP_VERSION,
        "commit_sha": COMMIT_SHA,
        # context.function_version is the load-bearing field. It comes from the
        # runtime, not from configuration, so it cannot drift from reality --
        # during a canary this is what differs between two otherwise identical
        # responses.
        "lambda_version": version,
        "request_id": getattr(context, "aws_request_id", "unknown"),
        "timestamp": datetime.now(UTC).isoformat(),
        # Echoed so a demo can prove which requests were deliberately degraded
        # rather than asking an audience to take it on trust.
        "faults": applied,
    }

    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
