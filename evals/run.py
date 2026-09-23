#!/usr/bin/env python3
"""Run the eval set. Phase 4a.

    # the baseline -- no AWS, no cost, runnable right now
    python -m evals.run --baseline

    # a real model, once Bedrock has quota
    python -m evals.run --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0

    # a fourth opinion from outside AWS (needs GEMINI_API_KEY)
    python -m evals.run --gemini-model gemini-3.5-flash-lite

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
import os
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

# The gate budgets 35 seconds because it is holding up a pipeline stage. An eval
# is not, and the two constraints genuinely differ: on a free-tier key a single
# scenario may spend most of a minute waiting out a rate limit, and failing that
# scenario closed would record a HIGH verdict the model never gave -- which
# Phase 4b already learned to treat as unmeasured rather than as judgement
# (F-018). Waiting is cheaper than an unmeasured row.
#
# Sized so all three attempts can actually happen: the Gemini transport allows
# 60s per request, and 60 + 0.5 + 60 + 1.5 + 60 is a little over 180. At the
# 120s this started out as, the predictive deadline check would refuse the
# SECOND attempt on any slow run -- a retry budget of three that spends one,
# which is the kind of number that looks configured and is not.
EVAL_DEADLINE_SECONDS = 240.0


def load_dotenv(path: Path) -> None:
    """Put `.env` into the environment, if it exists. Ten lines beats a dependency.

    Deliberately does NOT overwrite a variable that is already set: an explicitly
    exported key should win over a file the user forgot they wrote months ago.
    Silent when the file is absent, because it is optional -- exporting
    GEMINI_API_KEY in the shell is an equally valid way to run this.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def build_client(args) -> tuple[object, str]:
    if args.baseline:
        client = AttributeCountingClient()
        return client, client.MODEL_ID

    if args.gemini_model:
        load_dotenv(REPO_ROOT / ".env")
        # Imported here, not at module scope, so that `--baseline` and the
        # Bedrock path keep working on a machine that has never installed the
        # Google SDK. It is an optional `[evals]` dependency for exactly this
        # reason.
        from verdict.gemini import GeminiVerdictClient, build_genai_client

        return (
            GeminiVerdictClient(
                args.gemini_model,
                build_genai_client(),
                # The free tier's per-minute ceiling is the binding constraint,
                # and a paced call that succeeds beats a fast one that 429s.
                # The deadline has to allow for that pacing plus a real attempt,
                # so it is raised well above the 35s the gate runs with in
                # production -- an eval is not inside a pipeline stage.
                deadline_seconds=args.deadline,
            ),
            args.gemini_model,
        )

    from verdict import BedrockVerdictClient, build_bedrock_client

    return (
        BedrockVerdictClient(
            args.model_id, build_bedrock_client(args.region), deadline_seconds=args.deadline
        ),
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
    group.add_argument(
        "--gemini-model",
        help="Gemini model ID, e.g. gemini-3.5-flash-lite. Needs GEMINI_API_KEY",
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--deadline",
        type=float,
        default=EVAL_DEADLINE_SECONDS,
        help=(
            "seconds per scenario before failing closed. Higher than the gate's "
            "own 35s because an eval is not inside a pipeline stage and a "
            "rate-limited retry is worth waiting for"
        ),
    )
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

    if not args.baseline and not args.model_id and not args.gemini_model:
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
            "under_flagging_unaided": run.under_flagging_unaided,
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
                    # What the model said before the floor, so a later reader can
                    # separate the model's judgement from the arithmetic without
                    # re-running anything.
                    "unaided_levels": [str(x) for x in r.unaided_levels],
                    "passed_unaided": r.passed_unaided,
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
