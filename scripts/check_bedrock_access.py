#!/usr/bin/env python3
"""Phase 0.4 - confirm Bedrock is actually usable for the verdict layer.

This deliberately goes further than "can I reach Bedrock". Three separate
things have to be true before Phase 3 can be built, and each fails differently:

  1. The model EXISTS in the region.          -> ListFoundationModels
  2. We are ALLOWED to invoke it.             -> Converse returns 200
  3. Tool use returns SCHEMA-VALID JSON.      -> the part that actually matters

Step 3 is the one people skip. The whole design rests on getting structured
output out of the Converse API's tool-use interface rather than parsing prose,
so we prove that mechanism works on day zero instead of discovering a surprise
in Phase 3 with a prompt half-written.

Usage:
    python scripts/check_bedrock_access.py
    python scripts/check_bedrock_access.py --model-id us.anthropic.claude-sonnet-4-5-20250929-v1:0
    python scripts/check_bedrock_access.py --list-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

DEFAULT_REGION = "us-east-1"

# Haiku by default: this script may be run repeatedly while wiring things up,
# and the smoke test does not need judgment quality. Phase 3 picks the real
# model on the strength of Phase 4 eval results, not on vibes.
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# The smoke-test tool. Shaped deliberately like the eventual risk verdict so
# this doubles as a dry run of the Phase 3 contract.
#
# Schema design notes, because these choices are load-bearing:
#
#  * risk_level is an ENUM, not a free string. The executor branches on it, so
#    an unexpected value must be a hard validation failure, not a surprise
#    default. Enums also stop the model inventing "medium-high".
#  * confidence is a plain number and NOT used for routing. It is recorded for
#    analysis only. Self-reported model confidence is not calibrated and must
#    not gate a deploy.
#  * reasoning has a maxLength. Unbounded free text is where prompt-injected
#    content would ride along into logs and Slack messages.
#  * additionalProperties is false so an extra key is a failure rather than
#    something silently ignored.
#  * every field is required. Optional fields invite partial verdicts, and a
#    partial verdict is exactly the ambiguous case that must fail closed.
VERDICT_TOOL_NAME = "record_risk_verdict"

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "risk_level": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Overall deployment risk for this change.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Self-reported confidence. Recorded for analysis; never used for routing.",
        },
        "reasoning": {
            "type": "string",
            "maxLength": 600,
            "description": "Short justification referencing the supplied signals.",
        },
        "primary_concerns": {
            "type": "array",
            "items": {"type": "string", "maxLength": 120},
            "maxItems": 5,
            "description": "Specific concerns driving the risk level. Empty list if none.",
        },
    },
    "required": ["risk_level", "confidence", "reasoning", "primary_concerns"],
    "additionalProperties": False,
}

# A trivially safe change. If the model calls this "high risk" we have a
# prompt problem, not an access problem - worth knowing on day zero.
SMOKE_TEST_PROMPT = """\
You are a deployment risk assessor. Assess the following change.

<change>
  <description>Bump the `requests` library from 2.31.0 to 2.32.3 in requirements.txt</description>
  <files_changed>1</files_changed>
  <lines_added>1</lines_added>
  <lines_removed>1</lines_removed>
  <deploy_time>Tuesday 10:14 UTC</deploy_time>
  <target_service_health>No active alarms. Error rate 0.02%. p99 latency 180ms.</target_service_health>
  <security_findings>None.</security_findings>
</change>

Call the record_risk_verdict tool with your assessment."""


# Output is deliberately ASCII-only. This runs in PowerShell on Windows, where
# the console codepage mangles anything outside cp1252 into replacement
# characters -- an em-dash in a diagnostic tool is not worth a mojibake bug
# report during a live demo.
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


def list_models(region: str) -> list[dict[str, Any]]:
    """Step 1 - what Anthropic text models does this region expose?"""
    header("1. Foundation models visible in this region")

    bedrock = boto3.client("bedrock", region_name=region)
    try:
        resp = bedrock.list_foundation_models(byProvider="anthropic")
    except ClientError as exc:
        fail(f"ListFoundationModels denied: {exc.response['Error']['Code']}")
        info("Needs bedrock:ListFoundationModels - included in the ReadOnlyAccess policy.")
        return []

    models = [
        m
        for m in resp.get("modelSummaries", [])
        if "TEXT" in m.get("outputModalities", [])
    ]

    def types(m: dict[str, Any]) -> list[str]:
        return m.get("inferenceTypesSupported", [])

    on_demand = [m for m in models if "ON_DEMAND" in types(m)]
    profile_only = [
        m for m in models if "ON_DEMAND" not in types(m) and "INFERENCE_PROFILE" in types(m)
    ]
    # Neither on-demand nor profile: provisioned-throughput-only SKUs. Counted
    # separately so the three numbers actually add up to the total.
    provisioned_only = [
        m for m in models if "ON_DEMAND" not in types(m) and "INFERENCE_PROFILE" not in types(m)
    ]

    ok(f"{len(models)} Anthropic text models visible")
    info(f"{len(on_demand)} invokable directly by model ID (ON_DEMAND)")
    info(f"{len(profile_only)} require an inference profile")
    if provisioned_only:
        info(f"{len(provisioned_only)} provisioned-throughput only (not usable on-demand)")

    # The interesting case is not "zero on-demand models" -- a couple of legacy
    # ones usually linger. It is that every model you would actually choose is
    # inference-profile only, so a policy granting bare foundation-model ARNs
    # looks correct and grants nothing. Warn on the ratio, not on absence.
    if len(profile_only) > len(on_demand):
        warn("Most models here are inference-profile only.")
        info("Bare `anthropic.*` IDs will NOT work for them. Use a `us.*` profile ID.")
        if on_demand:
            info("The only directly-invokable models are legacy:")
            for m in on_demand:
                info(f"    {m['modelId']}")

    return models


def list_profiles(region: str) -> list[str]:
    """Step 1b - which inference profiles can we actually target?"""
    header("2. Inference profiles")

    bedrock = boto3.client("bedrock", region_name=region)
    try:
        resp = bedrock.list_inference_profiles()
    except ClientError as exc:
        fail(f"ListInferenceProfiles denied: {exc.response['Error']['Code']}")
        return []

    profiles = [
        p["inferenceProfileId"]
        for p in resp.get("inferenceProfileSummaries", [])
        if p.get("status") == "ACTIVE" and "anthropic" in p["inferenceProfileId"]
    ]

    us_profiles = sorted(p for p in profiles if p.startswith("us."))
    global_profiles = sorted(p for p in profiles if p.startswith("global."))

    ok(f"{len(us_profiles)} active `us.*` Anthropic profiles")
    if global_profiles:
        info(
            f"{len(global_profiles)} `global.*` profiles also exist - not used here. "
            "Global profiles may route outside US regions, which widens both the "
            "IAM surface and the data-residency story."
        )

    return us_profiles


def validate_verdict(payload: Any) -> list[str]:
    """Validate the tool input against VERDICT_SCHEMA.

    Uses jsonschema when available and falls back to a structural check so this
    script stays runnable before anyone has set up a virtualenv.
    """
    try:
        import jsonschema
    except ImportError:
        errors = []
        if not isinstance(payload, dict):
            return ["payload is not an object"]
        for field in VERDICT_SCHEMA["required"]:
            if field not in payload:
                errors.append(f"missing required field: {field}")
        if payload.get("risk_level") not in ("low", "medium", "high"):
            errors.append(f"risk_level not in enum: {payload.get('risk_level')!r}")
        extra = set(payload) - set(VERDICT_SCHEMA["properties"])
        if extra:
            errors.append(f"unexpected fields: {sorted(extra)}")
        return errors

    validator = jsonschema.Draft202012Validator(VERDICT_SCHEMA)
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in validator.iter_errors(payload)]


def check_tool_use(region: str, model_id: str) -> bool:
    """Steps 2 and 3 - can we invoke, and does tool use return valid JSON?"""
    header(f"3. Converse + tool use  ({model_id})")

    # Retries off: this is a diagnostic. A throttle should be visible, not
    # silently absorbed into a slower success.
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(retries={"max_attempts": 1, "mode": "standard"}, read_timeout=60),
    )

    tool_config = {
        "tools": [
            {
                "toolSpec": {
                    "name": VERDICT_TOOL_NAME,
                    "description": "Record a structured deployment risk verdict.",
                    "inputSchema": {"json": VERDICT_SCHEMA},
                }
            }
        ],
        # Forcing a specific tool is what turns "please reply in JSON" into a
        # structural guarantee. Without this the model may answer in prose and
        # we are back to parsing text, which is the thing we are avoiding.
        "toolChoice": {"tool": {"name": VERDICT_TOOL_NAME}},
    }

    started = time.monotonic()
    try:
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": SMOKE_TEST_PROMPT}]}],
            toolConfig=tool_config,
            # temperature 0 for reproducibility. Phase 4's eval harness needs
            # repeated identical inputs to produce comparable verdicts;
            # sampling noise would show up as prompt drift that isn't real.
            inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        message = exc.response["Error"].get("Message", "")
        fail(f"Converse failed: {code}")
        info(message)

        # Three independent gates sit in front of a Bedrock call, and the error
        # CODE only identifies one of them correctly. Notably a missing
        # account-level use case approval surfaces as ResourceNotFoundException,
        # which sends you hunting for a typo in a model ID that is perfectly
        # fine. Match on the message text, not just the code. See FAILURES.md
        # F-004.
        if "use case" in message.lower():
            info("")
            info("This is an ACCOUNT-level gate, not an IAM or model-ID problem.")
            info("Bedrock console -> Model access -> Anthropic -> submit the")
            info("use case details form. Approval can take ~15 minutes.")
            info("Re-run this script afterwards; nothing else needs changing.")
        elif code == "AccessDeniedException":
            info("Check the inference-profile ARN in the IAM policy names THIS account.")
            info("See docs/iam/ai-agent-bedrock-policy.json")
        elif code == "ValidationException":
            info("Model ID is malformed or not valid in this region.")
            info("Run with --list-only to see the profile IDs actually available.")
        return False

    elapsed_ms = (time.monotonic() - started) * 1000
    ok(f"Converse returned in {elapsed_ms:.0f}ms")

    stop_reason = resp.get("stopReason")
    if stop_reason != "tool_use":
        fail(f"Expected stopReason 'tool_use', got {stop_reason!r}")
        return False
    ok("stopReason is 'tool_use' - model chose the tool, not prose")

    blocks = resp["output"]["message"]["content"]
    tool_uses = [b["toolUse"] for b in blocks if "toolUse" in b]
    if len(tool_uses) != 1:
        fail(f"Expected exactly 1 toolUse block, got {len(tool_uses)}")
        return False

    payload = tool_uses[0]["input"]

    # The model calling the tool does NOT guarantee the arguments match the
    # schema. Bedrock does not validate tool input for you. This is the
    # fail-closed boundary and it has to be ours.
    errors = validate_verdict(payload)
    if errors:
        fail("Tool input failed schema validation:")
        for err in errors:
            info(err)
        return False
    ok("Tool input passed schema validation")

    usage = resp.get("usage", {})
    print()
    info(f"tokens in/out: {usage.get('inputTokens')}/{usage.get('outputTokens')}")
    print(_c(json.dumps(payload, indent=2), "dim"))

    if payload["risk_level"] != "low":
        print()
        warn(
            f"Model rated a trivial dependency bump as {payload['risk_level']!r}. "
            "Access works, but that is an over-flagging signal worth noting for Phase 4."
        )

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only enumerate models and profiles. Makes no billable call.",
    )
    args = parser.parse_args()

    sts = boto3.client("sts", region_name=args.region)
    try:
        ident = sts.get_caller_identity()
    except ClientError as exc:
        print(f"Cannot resolve AWS credentials: {exc}")
        return 1

    print(f"{_c('Account', 'bold')}  {ident['Account']}")
    print(f"{_c('Identity', 'bold')} {ident['Arn']}")
    print(f"{_c('Region', 'bold')}   {args.region}")

    list_models(args.region)
    profiles = list_profiles(args.region)

    if args.list_only:
        header("Skipping the Converse call (--list-only). No charge incurred.")
        if profiles:
            info("Available us.* profile IDs:")
            for p in profiles:
                print(f"        {p}")
        return 0

    if args.model_id not in profiles and args.model_id.startswith("us."):
        warn(f"{args.model_id} is not in the active profile list. Trying anyway.")

    passed = check_tool_use(args.region, args.model_id)

    header("Result")
    if passed:
        ok("Bedrock is usable for the verdict layer. Phase 3 is unblocked.")
        info(f"Record this model ID in DECISIONS.md: {args.model_id}")
        return 0

    fail("Bedrock is NOT yet usable. Fix the above before starting Phase 3.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
