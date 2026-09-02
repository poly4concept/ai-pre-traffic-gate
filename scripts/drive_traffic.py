#!/usr/bin/env python3
"""Phase 2.5b -- invoke the demo app repeatedly and watch the alarms respond.

WHY THIS IS A SCRIPT AND NOT A ONE-LINER

The obvious version is a PowerShell loop around `aws lambda invoke`. That works
until it does not, and when it does not it fails in ways that look like an AWS
problem: quoting around `--payload`, a path containing a backslash, a backtick
continuation inside a script block. The first attempt at this test produced zero
invocations and an alarm stuck in INSUFFICIENT_DATA -- which reads exactly like a
broken alarm and was actually a shell problem.

More importantly, a loop that only fires requests cannot tell you whether the
fault injection is working. This counts outcomes, so one command answers all
three questions:

    is the app being invoked?      -> invocations sent
    is the injected fault firing?  -> observed error rate, measured locally
    do the alarms notice?          -> --watch

WHY IT COUNTS ERRORS ITSELF RATHER THAN TRUSTING THE METRICS

CloudWatch metrics lag by a minute or two, so "did my fault injection work?" is
unanswerable from metrics for exactly as long as it takes to start doubting the
setup. The invoke response says immediately: a raised exception comes back with
`FunctionError` set. Counting those locally gives an instant, exact answer, and
whether the ALARM fires then becomes a separate question rather than a
confounded one.

NEEDS INVOKE PERMISSION, SO RUN IT AS THE ADMIN PROFILE

    $env:AWS_PROFILE = 'poly4'
    python scripts/drive_traffic.py --count 60 --watch

`ReadOnlyAccess` deliberately excludes `lambda:InvokeFunction`, which is the
right call -- a read-only identity should not be able to generate load or cost.

COST: free-tier noise. A hundred calls to a small function that returns in tens
of milliseconds is a fraction of a cent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

FUNCTION = "ai-pre-traffic-gate-demo-app"
ALIAS = "live"
ALARM_PREFIX = "ai-pre-traffic-gate-demo-app"

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, colour: str) -> str:
    if not _USE_COLOUR:
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "dim": "90", "bold": "1"}
    return f"\033[{codes[colour]}m{text}\033[0m"


@dataclass
class TrafficResult:
    """Outcomes of a traffic run, plus what the responses revealed.

    `fault_field_seen` is the field that turns a mystery into a diagnosis. See
    `diagnose` below.
    """

    counts: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    # True once any response body carried a `faults` key, which only code from
    # Phase 2.5a onwards emits.
    fault_field_seen: bool = False

    @property
    def attempted(self) -> int:
        """Invocations that actually reached the function."""
        return self.counts["ok"] + self.counts["faulted"]

    @property
    def observed_error_rate(self) -> float | None:
        """None, not 0.0, when nothing reached the function.

        A rate with a zero denominator is undefined. Reporting 0% here would
        say "the app is healthy" about an app that was never called -- the same
        mistake the whole signals package exists to prevent (D-026).
        """
        if not self.attempted:
            return None
        return 100 * self.counts["faulted"] / self.attempted

    def diagnose(self) -> tuple[str, str]:
        """(headline, what to do about it).

        THE DISTINCTION THIS EXISTS FOR, and it cost an afternoon to learn:

        `faulted == 0` has three completely different causes, and they were
        indistinguishable until this method existed.

          1. nothing was invoked          -> a shell or permissions problem
          2. invoked, no `faults` field   -> the DEPLOYED CODE predates 2.5a.
                                             Terraform does not own the demo
                                             app's code (the pipeline does), so
                                             an apply delivers the FAULT_TABLE
                                             env var and the IAM policy while
                                             leaving the code untouched. The
                                             switch is wired to nothing.
          3. invoked, `faults` present    -> the code is fine; the injection is
                                             off or set to zero.

        Case 2 is invisible by design: the demo app fails SAFE, so an absent
        fault subsystem behaves exactly like a healthy app. That was the right
        call for the app and it made the failure silent, which is the cost.
        """
        if not self.attempted:
            return (
                "Nothing reached the function.",
                "Fix the invoke error above. No metric was produced, so no alarm can fire.",
            )
        if self.counts["faulted"]:
            return ("Fault injection is working -- the app really did raise.", "")
        if not self.fault_field_seen:
            return (
                "The DEPLOYED CODE has no fault support.",
                "Responses carry no `faults` field, so this version predates Phase 2.5a.\n"
                "    Terraform does not deploy the demo app's code -- the pipeline does.\n"
                "    Run the pipeline, then retry:\n"
                "      git commit --allow-empty -m 'Deploy fault injection' && git push",
            )
        return (
            "The code supports faults, but none fired.",
            "Injection is off or set to zero:  python scripts/inject_fault.py status",
        )


def invoke_many(client, *, count: int, interval: float, qualifier: str) -> TrafficResult:
    """Invoke the function `count` times, tallying outcomes.

    Three outcomes are tracked separately and the distinction matters:

        ok        the function ran and returned
        faulted   the function RAISED -- what fault injection is supposed to do,
                  and what CloudWatch counts as an Error
        refused   the invoke call itself failed (permissions, throttling). The
                  function never ran, so this contributes to no metric at all.

    Collapsing `faulted` and `refused` would make a permissions problem look
    like a successful fault injection, which is the single most confusing
    outcome available here.
    """
    result = TrafficResult()
    counts = result.counts
    notes = result.notes
    target = f"{FUNCTION}:{qualifier}"

    for i in range(count):
        try:
            response = client.invoke(
                FunctionName=target,
                InvocationType="RequestResponse",
                Payload=b"{}",
            )
        except ClientError as exc:
            counts["refused"] += 1
            code = exc.response["Error"]["Code"]
            if code not in notes:
                notes.append(code)
                print(_c(f"  invoke refused: {code}", "red"))
                print(f"    {exc.response['Error']['Message'][:160]}")
                if code == "AccessDeniedException":
                    print(_c("    Run as the admin profile: $env:AWS_PROFILE = 'poly4'", "yellow"))
                    break
        else:
            if response.get("FunctionError"):
                counts["faulted"] += 1
            else:
                counts["ok"] += 1
                # Read the body to find out whether this build even KNOWS about
                # faults. Wrapped because a payload we cannot parse is a
                # curiosity, not a reason to abandon a traffic run.
                try:
                    payload = json.loads(response["Payload"].read())
                    body = json.loads(payload.get("body", "{}"))
                except (ValueError, KeyError, TypeError):
                    body = {}
                if "faults" in body:
                    result.fault_field_seen = True

        done = i + 1
        if done % 10 == 0:
            print(
                f"  {done}/{count}  ok={counts['ok']} faulted={counts['faulted']} "
                f"refused={counts['refused']}"
            )
        if interval and done < count:
            time.sleep(interval)

    return result


def describe_alarms(client) -> list[dict]:
    resp = client.describe_alarms(AlarmNamePrefix=ALARM_PREFIX)
    return sorted(resp.get("MetricAlarms", []), key=lambda a: a["AlarmName"])


def print_alarms(alarms: list[dict]) -> None:
    for alarm in alarms:
        state = alarm["StateValue"]
        colour = {"ALARM": "red", "OK": "green"}.get(state, "yellow")
        print(f"  {alarm['AlarmName']:48} {_c(state, colour)}")
        reason = alarm.get("StateReason", "")
        if reason:
            print(f"    {_c(reason[:150], 'dim')}")


def watch_alarms(client, *, timeout_seconds: int, poll_seconds: int) -> bool:
    """Poll until any alarm reaches ALARM, or time out.

    Returns True if one fired. A timeout is NOT reported as success -- the
    whole point of the exercise is finding out whether the thresholds actually
    trip, and a hopeful "probably fine" would defeat it.
    """
    deadline = time.monotonic() + timeout_seconds
    print(_c(f"\nWatching alarms for up to {timeout_seconds}s", "bold"))

    while True:
        alarms = describe_alarms(client)
        firing = [a for a in alarms if a["StateValue"] == "ALARM"]
        remaining = int(deadline - time.monotonic())

        print(f"\n  [{timeout_seconds - max(remaining, 0)}s elapsed]")
        print_alarms(alarms)

        if firing:
            print(_c(f"\n  {len(firing)} alarm(s) FIRING -- 2.5b works.", "green"))
            return True
        if remaining <= 0:
            print(_c("\n  Timed out with no alarm firing.", "yellow"))
            return False

        time.sleep(min(poll_seconds, max(remaining, 1)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=60, help="invocations to send")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between invocations")
    parser.add_argument(
        "--qualifier",
        default=ALIAS,
        help=f"alias or version to invoke (default: {ALIAS})",
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="after sending traffic, poll the alarms until one fires",
    )
    parser.add_argument("--watch-timeout", type=int, default=420)
    parser.add_argument("--poll", type=int, default=30)
    parser.add_argument(
        "--alarms-only",
        action="store_true",
        help="just print current alarm state and exit (no invocations, no cost)",
    )
    args = parser.parse_args()

    cw = boto3.client("cloudwatch", region_name=args.region)

    if args.alarms_only:
        print(_c("Current alarm state", "bold"))
        print_alarms(describe_alarms(cw))
        return 0

    lam = boto3.client(
        "lambda",
        region_name=args.region,
        # A faulted invocation is a real response, not something to retry --
        # retrying would inflate the invocation count and understate the
        # observed error rate, which is the one number this script exists to
        # measure honestly.
        config=Config(retries={"max_attempts": 0}, read_timeout=40, connect_timeout=5),
    )

    print(_c(f"\nInvoking {FUNCTION}:{args.qualifier} x{args.count}", "bold"))
    result = invoke_many(lam, count=args.count, interval=args.interval, qualifier=args.qualifier)
    counts = result.counts

    print(_c("\nResults", "bold"))
    print(f"  ok        {counts['ok']}")
    print(f"  faulted   {counts['faulted']}")
    print(f"  refused   {counts['refused']}")
    print(f"  fault-aware responses: {'yes' if result.fault_field_seen else 'NO'}")

    rate = result.observed_error_rate
    if rate is None:
        # Not 0% -- there was no denominator. See TrafficResult.
        print("\n  observed error rate  n/a (nothing reached the function)")
    else:
        print(f"\n  observed error rate  {rate:.1f}%  ({counts['faulted']}/{result.attempted})")

    headline, action = result.diagnose()
    good = bool(counts["faulted"])
    print(_c(f"\n  {headline}", "green" if good else "yellow"))
    if action:
        print(f"    {action}")

    if not result.attempted:
        return 1

    print(_c("\n  Metrics lag 1-2 minutes; alarms need a full period after that.", "dim"))

    if args.watch:
        fired = watch_alarms(cw, timeout_seconds=args.watch_timeout, poll_seconds=args.poll)
        if not fired:
            print("\n  Next steps if nothing fired:")
            print("    - re-run with --count 120 (more datapoints in the period)")
            print("    - check the threshold against the observed rate above")
            print("    - python scripts/drive_traffic.py --alarms-only")
            return 1
    else:
        print("\n  Current alarm state:")
        print_alarms(describe_alarms(cw))
        print(_c("\n  Add --watch to poll until one fires.", "dim"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
