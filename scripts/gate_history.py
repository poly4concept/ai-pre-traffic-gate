#!/usr/bin/env python3
"""What would this gate have done to my last N deploys? Phase 5.4.

    python scripts/gate_history.py
    python scripts/gate_history.py --limit 100 --json history.json

WHY THIS EXISTS, AND WHY IT SHOULD HAVE EXISTED SOONER

CLAUDE.md's argument for shadow mode is that `would_have_halted` accumulates so
that "by the time enforcement is switched on there is a real measured
over-flagging rate to switch it on WITH -- rather than a guess and an apology."

That argument only works if somebody reads the field. It has been written since
Phase 3.5 and nothing has ever read it back, which means the evidence for
turning enforcement on has been sitting in a table nobody opened. Building the
readout before flipping the switch is the whole point of the sequencing.

WHAT THIS MEASURES THAT THE EVAL DOES NOT

The eval (Phase 4) scores 22 fixtures whose answers were written by hand. It
measures calibration against a labeller's opinion. It says nothing about the
changes this repository actually produces.

This measures the real distribution: of the deploys that really happened, how
many would the gate have stopped? A gate that scores well on fixtures and halts
half of your real deploys is not deployable, and only one of these two tools can
tell you that.

Both numbers belong on the slide. They answer different questions and the eval
is the one that can be gamed by choosing fixtures.

READ-ONLY. Runs fine as `ai-agent`; needs no admin profile.

COST: one Query plus one BatchGetItem per 100 records. Fractions of a cent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

PROJECT = os.environ.get("PROJECT_NAME", "ai-pre-traffic-gate")
TABLE = os.environ.get("VERDICT_TABLE", f"{PROJECT}-verdicts")
SERVICE = os.environ.get("TARGET_SERVICE", f"{PROJECT}-demo-app")
INDEX = "by_service_recorded_at"

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, colour: str) -> str:
    if not _USE_COLOUR:
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "dim": "90", "bold": "1"}
    return f"\033[{codes[colour]}m{text}\033[0m"


def _s(item: dict, *path: str) -> str:
    """Walk a nested DynamoDB item and return a string, or ''."""
    node = item
    for key in path:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
        if node is None:
            return ""
        if "M" in node and key != path[-1]:
            node = node["M"]
    if not isinstance(node, dict):
        return ""
    for kind in ("S", "N"):
        if kind in node:
            return str(node[kind])
    if "BOOL" in node:
        return str(node["BOOL"]).lower()
    return ""


def _would_have_halted(item: dict) -> tuple[bool, bool]:
    """(would_have_halted, was_derived).

    Records written before Phase 5.4 do not carry the field (F-022), so it is
    derived from the risk level for those. Flagged rather than silently filled,
    because a derived value is computed under TODAY's policy -- if the mapping
    from risk to blocking ever changes, these rows would be reinterpreted while
    the stored ones would not.
    """
    stored = _s(item, "would_have_halted")
    if stored:
        return stored == "true", False
    return _s(item, "verdict", "risk_level") == "high", True


def fetch(client, *, limit: int) -> list[dict]:
    """Newest first, via the GSI rather than a scan.

    The index is KEYS_ONLY, so this is a Query for the keys and then a
    BatchGetItem for the bodies. A scan would work at this volume and would
    teach the wrong habit -- and would also read every verdict for every service
    rather than the one asked about.
    """
    keys: list[dict] = []
    kwargs = {
        "TableName": TABLE,
        "IndexName": INDEX,
        "KeyConditionExpression": "service_name = :s",
        "ExpressionAttributeValues": {":s": {"S": SERVICE}},
        "ScanIndexForward": False,  # newest first
        "Limit": min(limit, 100),
    }

    while len(keys) < limit:
        page = client.query(**kwargs)
        keys.extend(page.get("Items", []))
        token = page.get("LastEvaluatedKey")
        if not token:
            break
        kwargs["ExclusiveStartKey"] = token

    keys = keys[:limit]
    if not keys:
        return []

    items: list[dict] = []
    for start in range(0, len(keys), 100):
        chunk = keys[start : start + 100]
        response = client.batch_get_item(
            RequestItems={
                TABLE: {"Keys": [{"verdict_id": k["verdict_id"]} for k in chunk]},
            }
        )
        items.extend(response.get("Responses", {}).get(TABLE, []))

    items.sort(key=lambda i: _s(i, "recorded_at"), reverse=True)
    return items


def is_real_deploy(item: dict) -> bool:
    """Did this verdict gate an actual pipeline execution?

    THE DISTINCTION THAT DECIDES WHETHER THE RATE MEANS ANYTHING.

    The gate can be invoked by hand -- `aws lambda invoke` with an empty
    payload, which is how most of this project's early testing happened. Those
    produce perfectly real verdict records with no `pipeline_execution_id`,
    because there was no pipeline.

    Counting them in "what would this gate have done to my last N deploys"
    inflates the halt rate with invocations that were never deploys, and they
    skew it upward specifically: a hand invoke has no change context, so the
    gate correctly refuses to assess and fails closed to HIGH. Every manual test
    reads as a would-have-halted deploy.

    Same shape as the eval's `is_measured` (D-062) arriving from a third
    direction: a number is only a rate if the denominator is the thing you
    claim to be measuring.
    """
    return bool(_s(item, "pipeline_execution_id"))


def summarise(items: list[dict]) -> dict:
    risks = Counter()
    modes = Counter()
    sources = Counter()
    prompts = Counter()
    halts = 0
    derived = 0
    acted = 0
    overridden = 0
    tokens_in = 0
    latencies: list[int] = []

    deploys = [i for i in items if is_real_deploy(i)]
    manual = len(items) - len(deploys)

    for item in deploys:
        risks[_s(item, "verdict", "risk_level") or "unknown"] += 1
        modes[_s(item, "mode") or "unknown"] += 1
        sources[_s(item, "verdict", "source") or "unknown"] += 1
        prompts[_s(item, "model_call", "prompt_version") or "unknown"] += 1

        would, was_derived = _would_have_halted(item)
        halts += would
        derived += was_derived
        acted += _s(item, "action_taken") == "halt_pipeline"
        overridden += bool(_s(item, "override", "decision"))

        try:
            tokens_in += int(_s(item, "model_call", "input_tokens") or 0)
            latency = int(_s(item, "model_call", "latency_ms") or 0)
            if latency:
                latencies.append(latency)
        except ValueError:
            pass

    return {
        "total": len(deploys),
        "manual_invokes": manual,
        "would_have_halted": halts,
        "derived": derived,
        "actually_halted": acted,
        "overridden": overridden,
        "risks": dict(risks),
        "modes": dict(modes),
        "sources": dict(sources),
        "prompt_versions": dict(prompts),
        "input_tokens": tokens_in,
        "median_latency_ms": sorted(latencies)[len(latencies) // 2] if latencies else None,
    }


def report(items: list[dict], summary: dict) -> None:
    total = summary["total"]
    print(_c("\n" + "=" * 74, "bold"))
    print(_c(f"GATE HISTORY -- {SERVICE}", "bold"))
    print(_c("=" * 74, "bold"))

    deploys = [i for i in items if is_real_deploy(i)]

    if not total:
        print("\n  No verdicts from real pipeline executions.")
        if summary["manual_invokes"]:
            print(
                f"  ({summary['manual_invokes']} record(s) exist from manual "
                "`aws lambda invoke` testing.\n"
                "   Those are not deploys and are excluded -- see is_real_deploy.)"
            )
        else:
            print("  Run the pipeline at least once.")
        return

    halts = summary["would_have_halted"]
    pct = 100 * halts / total

    print(
        f"\n  {total} verdicts from real deploys, "
        f"{_s(deploys[-1], 'recorded_at')[:10]} to {_s(deploys[0], 'recorded_at')[:10]}"
    )
    if summary["manual_invokes"]:
        print(
            _c(
                f"  excluding {summary['manual_invokes']} manual `aws lambda invoke` test(s) --\n"
                "  no pipeline execution, so not a deploy. They fail closed to HIGH by\n"
                "  design, so counting them would skew the rate upward.",
                "dim",
            )
        )

    print(_c("\n  WOULD HAVE HALTED", "bold"))
    colour = "green" if pct <= 20 else "yellow" if pct <= 40 else "red"
    print(f"    {_c(f'{halts}/{total}  ({pct:.0f}%)', colour)}")
    print(f"    actually halted: {summary['actually_halted']}")
    if summary["derived"]:
        # Honest about which rows are weaker evidence.
        print(
            _c(
                f"    {summary['derived']} of the {total} predate the stored field;\n"
                "    their value is DERIVED from risk_level (F-022).",
                "dim",
            )
        )

    print(_c("\n  RISK DISTRIBUTION", "bold"))
    for level in ("low", "medium", "high", "unknown"):
        n = summary["risks"].get(level, 0)
        if n:
            bar = "#" * max(1, round(30 * n / total))
            print(f"    {level:8} {n:4}  {bar}")

    print(_c("\n  VERDICT SOURCE", "bold"))
    for source, n in sorted(summary["sources"].items(), key=lambda kv: -kv[1]):
        note = "  <-- error handling, not judgement" if source == "fail_closed" else ""
        print(f"    {source:14} {n:4}{note}")

    print(_c("\n  CONTEXT", "bold"))
    print(f"    modes:           {summary['modes']}")
    print(f"    prompt versions: {summary['prompt_versions']}")
    print(f"    overridden:      {summary['overridden']}")
    if summary["median_latency_ms"]:
        print(f"    median latency:  {summary['median_latency_ms']}ms")
    print(f"    input tokens:    {summary['input_tokens']:,}")

    print(_c("\n  THE DECISIONS", "bold"))
    print(f"    {'when':17} {'risk':7} {'source':12} {'mode':9} commit")
    print(f"    {'-' * 17} {'-' * 7} {'-' * 12} {'-' * 9} {'-' * 12}")
    for item in deploys[:20]:
        risk = _s(item, "verdict", "risk_level") or "?"
        mark = _c("!", "red") if risk == "high" else " "
        print(
            f"  {mark} {_s(item, 'recorded_at')[:16]:17} {risk:7} "
            f"{_s(item, 'verdict', 'source'):12} {_s(item, 'mode'):9} "
            f"{_s(item, 'commit_sha')[:12]}"
        )

    _interpret(summary)


def _interpret(summary: dict) -> None:
    """Say what the number means for the decision it exists to inform.

    A rate with no reading attached gets read as whatever the reader already
    believed, which for a gate is usually "seems fine, turn it on".
    """
    total = summary["total"]
    pct = 100 * summary["would_have_halted"] / total
    fail_closed = summary["sources"].get("fail_closed", 0)

    print(_c("\n  READING THIS", "bold"))

    if total < 20:
        print(
            _c(
                f"    {total} records is not a rate, it is an anecdote. Nothing here\n"
                "    should decide whether to enforce. Phase 8's soak is what fills\n"
                "    this table; until then treat the number as a smoke test that the\n"
                "    plumbing works.",
                "yellow",
            )
        )

    if fail_closed:
        share = 100 * fail_closed / total
        print(
            f"    {fail_closed} verdict(s) ({share:.0f}%) were fail-closed -- the gate refused\n"
            "    rather than assessed. Those are error handling, not judgement, and\n"
            "    they inflate the halt rate above."
        )

    if pct > 40:
        print(
            _c(
                "    A halt rate this high will not survive contact with colleagues.\n"
                "    Enforcing now means blocking real deploys often enough that\n"
                "    somebody switches the gate off in week three.",
                "red",
            )
        )
    elif pct > 20:
        print("    Worth reading the halted commits before enforcing. Were they")
        print("    genuinely risky, or is the gate flagging ordinary work?")
    else:
        print("    A plausible rate to enforce on -- if the sample is large enough")
        print("    and the halts were the right ones.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--json", dest="json_path", help="also write the summary here")
    args = parser.parse_args()

    client = boto3.client("dynamodb", region_name=args.region)
    try:
        items = fetch(client, limit=args.limit)
    except ClientError as exc:
        print(_c(f"could not read {TABLE}: {exc.response['Error']['Code']}", "red"))
        return 1

    summary = summarise(items)
    report(items, summary)

    if args.json_path:
        summary["generated_at"] = datetime.now(UTC).isoformat()
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
        print(f"\n  wrote {args.json_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
