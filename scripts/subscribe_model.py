#!/usr/bin/env python3
"""Create the AWS Marketplace subscription for one Bedrock model, by invoking it.

WHY THIS SCRIPT EXISTS

Third-party models on Bedrock (Anthropic, AI21, Cohere, Meta, Mistral) are sold
through AWS Marketplace, and the subscription is created implicitly by the FIRST
invocation. That first call therefore needs `aws-marketplace:Subscribe`, which a
read-only identity does not and should not have.

So the sequence for every new model is:

    1. an admin runs this once   -> subscription created, account-wide
    2. everyone else just calls it

There is no console page for this any more. The old "Model access" page in the
Bedrock console is retired, so an invocation is the mechanism.

WHY IT SENDS A BARE REQUEST WITH NO TOOL CONFIG

Bedrock validates the request shape before it checks entitlement, so a request
carrying a `toolConfig` that the target model does not support fails with a
ValidationException *before* the subscription is ever attempted. That is how
this project briefly concluded a model had cleared its quota when it had not
even been authorised. The minimal request removes that confusion: whatever comes
back is about access, not about the shape of what we sent.

READING THE RESULT

    success            -> subscribed AND the account has quota. Usable now.
    ThrottlingException-> subscribed, but a quota is zero. Access is fine.
    AccessDenied       -> not subscribed. Are you running as an admin?
    ValidationException-> subscribed and reachable; the request shape was wrong.

The distinction between the first two is the whole point of running this: it
separates "we are not allowed to call this model" from "we are allowed and have
no capacity", which produce identical symptoms from the caller's point of view
but need completely different fixes.

Usage:
    $env:AWS_PROFILE = 'poly4'
    python scripts/subscribe_model.py ai21.jamba-1-5-mini-v1:0
"""

from __future__ import annotations

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

DEFAULT_REGION = "us-east-1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_id", help="e.g. ai21.jamba-1-5-mini-v1:0")
    parser.add_argument("--region", default=DEFAULT_REGION)
    args = parser.parse_args()

    sts = boto3.client("sts", region_name=args.region)
    who = sts.get_caller_identity()
    print(f"Account  {who['Account']}")
    print(f"Identity {who['Arn']}")
    print(f"Region   {args.region}")
    print(f"Model    {args.model_id}")

    if "ai-agent" in who["Arn"]:
        print()
        print("  STOP  This is the read-only identity. It has no")
        print("        aws-marketplace:Subscribe, so this call cannot create a")
        print("        subscription. Re-run with the admin profile:")
        print("            $env:AWS_PROFILE = 'poly4'")
        return 2

    client = boto3.client("bedrock-runtime", region_name=args.region)

    print("\nInvoking once, minimally, to create the subscription...")
    try:
        response = client.converse(
            modelId=args.model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with exactly: OK"}]}],
            inferenceConfig={"maxTokens": 16, "temperature": 0},
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        message = exc.response["Error"]["Message"]
        print(f"  FAIL  {code}")
        print(f"        {message}")
        print()
        return _explain(code, message)
    except Exception as exc:  # noqa: BLE001 - a diagnostic script reports, it does not raise
        print(f"  FAIL  {type(exc).__name__}: {exc}")
        return 1

    text = response["output"]["message"]["content"][0].get("text", "").strip()
    usage = response.get("usage", {})
    print("  PASS  the model answered")
    print(f"        reply  {text!r}")
    print(f"        tokens in={usage.get('inputTokens')} out={usage.get('outputTokens')}")
    print()
    print("  This model is subscribed AND has usable quota. Two conclusions:")
    print("    * the account is not blocked at the account level")
    print("    * a zero 'tokens per day' quota is therefore NOT being enforced,")
    print("      so the per-minute quotas are what actually bind")
    return 0


def _explain(code: str, message: str) -> int:
    """Say what the failure means for the account, not just what it was."""
    lowered = message.lower()

    if code == "AccessDeniedException" and "marketplace" in lowered:
        print("  MEANING  The subscription was not created.")
        print("           This identity lacks aws-marketplace:Subscribe, or the")
        print("           marketplace call itself failed. Confirm you are running")
        print("           as an admin, then retry after two minutes.")
        return 1

    if code == "AccessDeniedException" and "not available for this account" in lowered:
        print("  MEANING  The model is not offered to this account at all.")
        print("           Not a quota problem and not fixable by subscribing.")
        print("           Pick a different model.")
        return 1

    if code == "ThrottlingException":
        print("  MEANING  ACCESS IS FINE -- this is purely a quota problem.")
        print("           The subscription exists and the call was authorised;")
        print("           it was refused for capacity. Check which quota is zero:")
        print("               aws service-quotas list-service-quotas \\")
        print("                 --service-code bedrock --region us-east-1")
        print("           If the message says 'per day' and that quota is not")
        print("           adjustable, only AWS Support can raise it.")
        return 1

    if code == "ValidationException":
        print("  MEANING  Access and quota are both fine -- the request shape was")
        print("           rejected. Note this check happens BEFORE entitlement, so")
        print("           a ValidationException says nothing about access.")
        return 1

    print("  MEANING  Unrecognised. Record the code and message verbatim.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
