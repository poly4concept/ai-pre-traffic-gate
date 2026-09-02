#!/usr/bin/env python3
"""Turn the demo app unhealthy on demand, and turn it back. Phase 2.5.

    python scripts/inject_fault.py status
    python scripts/inject_fault.py errors --rate 0.5
    python scripts/inject_fault.py slow --ms 3000
    python scripts/inject_fault.py memory --mb 256
    python scripts/inject_fault.py errors --rate 1.0 --version 7   # canary only
    python scripts/inject_fault.py off

THE TEARDOWN IS `off`, AND IT IS THE IMPORTANT COMMAND

CLAUDE.md requires every synthesized condition to have a documented recipe and a
documented teardown. `off` is the teardown, it takes effect within the app's
cache window (5 seconds by default), and it is the one command worth memorising
before a live demo.

WHY --version MATTERS MORE THAN IT LOOKS

`--version` scopes the fault to a single published Lambda version, which means
the CANARY can be broken while the stable version stays healthy. That is the
scenario CodeDeploy's automatic rollback exists for, and it is the most
convincing thing this project can show on stage: a bad version goes out to 10%
of traffic, alarms fire, and the deployment rolls itself back while the audience
watches.

Find the version the canary is serving with:

    aws lambda get-alias --function-name ai-pre-traffic-gate-demo-app --name live

WRITING THIS RECORD NEEDS CREDENTIALS THE READ-ONLY IDENTITY DOES NOT HAVE.
Run it as the admin profile. That is deliberate: turning a service unhealthy is
an operator action, and the demo app itself holds GetItem only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

DEFAULT_TABLE = os.environ.get("FAULT_TABLE", "ai-pre-traffic-gate-demo-app-faults")
DEFAULT_KEY = os.environ.get("FAULT_KEY", "demo-app")
DEFAULT_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Mirrors the caps in services/demo_app/faults.py. Duplicated deliberately: the
# app clamps because it must never trust a record, and the script rejects early
# so an operator gets told at the terminal instead of silently getting something
# other than what they asked for.
MAX_LATENCY_MS = 30_000
MAX_MEMORY_MB = 512

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, colour: str) -> str:
    if not _USE_COLOUR:
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "dim": "90", "bold": "1"}
    return f"\033[{codes[colour]}m{text}\033[0m"


def client(region: str):
    return boto3.client("dynamodb", region_name=region)


def read(table: str, key: str, region: str) -> dict:
    try:
        response = client(region).get_item(
            TableName=table, Key={"config_key": {"S": key}}, ConsistentRead=True
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        print(_c(f"FAIL  {code}", "red"))
        if code == "ResourceNotFoundException":
            print(f"      Table {table!r} does not exist. Has terraform apply run?")
        elif code == "AccessDeniedException":
            print("      This identity cannot read the table. Run as the admin profile.")
        raise SystemExit(2) from exc

    item = response.get("Item")
    if not item:
        return {}
    return {
        "error_rate": float(item.get("error_rate", {}).get("N", 0)),
        "latency_ms": int(float(item.get("latency_ms", {}).get("N", 0))),
        "memory_mb": int(float(item.get("memory_mb", {}).get("N", 0))),
        "applies_to_version": item.get("applies_to_version", {}).get("S"),
        "note": item.get("note", {}).get("S", ""),
    }


def write(table: str, key: str, region: str, config: dict) -> None:
    item = {
        "config_key": {"S": key},
        "error_rate": {"N": str(config.get("error_rate", 0))},
        "latency_ms": {"N": str(config.get("latency_ms", 0))},
        "memory_mb": {"N": str(config.get("memory_mb", 0))},
        "note": {"S": config.get("note", "")},
    }
    version = config.get("applies_to_version")
    if version:
        item["applies_to_version"] = {"S": str(version)}

    try:
        client(region).put_item(TableName=table, Item=item)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        print(_c(f"FAIL  {code}", "red"))
        if code == "AccessDeniedException":
            print("      Writing the fault record needs dynamodb:PutItem.")
            print("      The read-only agent identity deliberately does not have it.")
            print("      Run as the admin profile:  $env:AWS_PROFILE = 'poly4'")
        raise SystemExit(2) from exc


def show(config: dict) -> None:
    if not config or not any(
        (config.get("error_rate"), config.get("latency_ms"), config.get("memory_mb"))
    ):
        print(_c("  HEALTHY  no faults configured", "green"))
        return

    print(_c("  DEGRADED  faults are active", "yellow"))
    if config.get("error_rate"):
        pct = config["error_rate"] * 100
        print(f"    error_rate   {config['error_rate']}  ({pct:.0f}% of requests raise)")
    if config.get("latency_ms"):
        print(f"    latency_ms   {config['latency_ms']}")
    if config.get("memory_mb"):
        print(f"    memory_mb    {config['memory_mb']}")
    scope = config.get("applies_to_version")
    print(f"    scope        {'version ' + scope if scope else 'ALL versions'}")
    if config.get("note"):
        print(f"    note         {config['note']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument("--region", default=DEFAULT_REGION)

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show the current fault configuration")
    sub.add_parser("off", help="clear all faults (THE TEARDOWN)")

    errors = sub.add_parser("errors", help="make a fraction of requests raise")
    errors.add_argument("--rate", type=float, required=True, help="0.0 to 1.0")

    slow = sub.add_parser("slow", help="add artificial latency")
    slow.add_argument("--ms", type=int, required=True)

    memory = sub.add_parser("memory", help="hold extra memory per container")
    memory.add_argument("--mb", type=int, required=True)

    for p in (errors, slow, memory):
        p.add_argument(
            "--version",
            help="published Lambda version to scope the fault to; omit for all",
        )
        p.add_argument("--note", default="", help="free text recorded alongside")

    args = parser.parse_args()

    print(f"Table   {args.table}")
    print(f"Region  {args.region}\n")

    if args.command == "status":
        show(read(args.table, args.key, args.region))
        return 0

    if args.command == "off":
        write(args.table, args.key, args.region, {"note": "cleared"})
        print(_c("  CLEARED  all faults removed", "green"))
        print("\n  Takes effect within the app's cache window (5s by default).")
        print("  Warm containers may serve one or two more degraded requests.")
        return 0

    # Start from whatever is already set, so `slow` after `errors` produces both
    # rather than silently clearing the first one. Combining faults is a normal
    # thing to want and a surprising thing to lose.
    config = read(args.table, args.key, args.region)

    if args.command == "errors":
        if not 0.0 <= args.rate <= 1.0:
            parser.error("--rate must be between 0.0 and 1.0")
        config["error_rate"] = args.rate
    elif args.command == "slow":
        if not 0 <= args.ms <= MAX_LATENCY_MS:
            parser.error(f"--ms must be between 0 and {MAX_LATENCY_MS}")
        config["latency_ms"] = args.ms
    elif args.command == "memory":
        if not 0 <= args.mb <= MAX_MEMORY_MB:
            parser.error(f"--mb must be between 0 and {MAX_MEMORY_MB}")
        config["memory_mb"] = args.mb

    if args.version:
        config["applies_to_version"] = args.version
    if args.note:
        config["note"] = args.note

    write(args.table, args.key, args.region, config)
    show(read(args.table, args.key, args.region))

    print()
    print(_c("  REMEMBER THE TEARDOWN:", "bold"))
    print("    python scripts/inject_fault.py off")
    print()
    print("  If nothing seems to happen, the traffic-serving version probably")
    print("  predates FAULT_TABLE being set. Lambda snapshots environment")
    print("  variables into published versions, so run the pipeline once.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(json.dumps({"interrupted": True}))
        sys.exit(130)
