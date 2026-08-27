#!/usr/bin/env python3
"""Run the eval set. Phase 4a.

    # the baseline -- no AWS, no cost, runnable right now
    python -m evals.run --baseline

    # a real model, once Bedrock has quota
    python -m evals.run --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0

    # verdict stability on identical input
    python -m evals.run --model-id <id> --repeats 5

    # one scenario, while iterating on the prompt
    python -m evals.run --baseline --scenario revert_of_a_bad_deploy

`--baseline` is the default and deliberately so: the first thing anyone runs
should be the thing the model has to beat, not the model. Otherwise the first
number you ever see has nothing to be compared against, and it will be believed.

COST, STATED PLAINLY: a full run with a real model is 22 scenarios x repeats
inferences, each roughly 1,300 input and 150 output tokens. One pass is about
30,000 input tokens. Small, but it is not free, and `--repeats 5` is five times
that. The baseline is free.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# The decision service is not an installed package -- the Lambda imports these
# modules from the zip root, so the eval has to put them on the path the same
# way the test suite does.
for path in (str(REPO_ROOT), str(REPO_ROOT / "services" / "decision_service")):
    if path not in sys.path:
        sys.path.insert(0, path)

from evals.harness import run_eval  # noqa: E402
from evals.labels import coverage_report  # noqa: E402
from evals.report import format_run  # noqa: E402
from evals.stub import AttributeCountingClient  # noqa: E402


def build_client(args) -> tuple[object, str]:
    if args.baseline:
        client = AttributeCountingClient()
        return client, client.MODEL_ID

    from verdict import BedrockVerdictClient, build_bedrock_client

    return (
        BedrockVerdictClient(args.model_id, build_bedrock_client(args.region)),
        args.model_id,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--baseline",
        action="store_true",
        help="run the attribute-counting baseline instead of a model (default)",
    )
    group.add_argument("--model-id", help="Bedrock inference profile ID")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="runs per scenario; >1 measures verdict stability on identical input",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        help="limit to one scenario (repeatable)",
    )
    parser.add_argument("--json", dest="json_path", help="also write raw results to this path")
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="print how the fixture set is weighted by kind, then exit",
    )
    args = parser.parse_args()

    # Several scenarios contain a deliberately failed collector, and the signals
    # package logs those with a traceback -- correct behaviour in Lambda, pure
    # noise here, where a failed collector is the fixture rather than a fault.
    logging.getLogger("signals").setLevel(logging.CRITICAL)

    if args.coverage:
        counts = coverage_report()
        total = sum(counts.values())
        print(f"{total} labelled scenarios")
        for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {kind:15} {n:3}   {100 * n / total:4.1f}%")
        print()
        print("A set weighted toward risky scenarios rewards a gate that flags")
        print("everything, and measures the over-flagging rate against almost nothing.")
        return 0

    if not args.baseline and not args.model_id:
        args.baseline = True

    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    client, model_id = build_client(args)
    run = run_eval(
        client,
        scenarios=tuple(args.scenarios) if args.scenarios else None,
        repeats=args.repeats,
        model_id=model_id,
    )

    print(format_run(run))

    if args.json_path:
        payload = {
            "model_id": run.model_id,
            "repeats": run.repeats,
            "over_flagging": run.over_flagging,
            "under_flagging": run.under_flagging,
            "passes": run.passes,
            "stability": run.stability,
            "tokens": run.tokens,
            "fail_closed_attempts": run.fail_closed_count,
            "pair_violations": [
                {
                    "lax": v.lax,
                    "strict": v.strict,
                    "lax_level": str(v.lax_level),
                    "strict_level": str(v.strict_level),
                }
                for v in run.pair_violations
            ],
            "scenarios": [
                {
                    "scenario": r.scenario,
                    "kind": str(r.label.kind),
                    "levels": [str(x) for x in r.levels],
                    "modal": str(r.modal_level),
                    "acceptable": sorted(str(a) for a in r.label.acceptable),
                    "passed": r.passed,
                    "direction": r.direction,
                    "stable": r.is_stable,
                    "concern_coverage": r.concern_coverage,
                    "reasoning": r.attempts[0].reasoning,
                }
                for r in run.results
            ],
        }
        Path(args.json_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {args.json_path}")

    # Exit code reflects UNDER-flagging and structural violations only.
    #
    # Over-flagging is a quality problem to iterate on, not a build breaker --
    # wiring it to a non-zero exit would make the natural fix "relax the labels",
    # which is how a benchmark stops measuring anything. A risky change waved
    # through, or the model being consulted when it must not be, is different in
    # kind and should stop a pipeline.
    under_n, _ = run.under_flagging
    return 1 if under_n or run.model_call_violations else 0


if __name__ == "__main__":
    sys.exit(main())
