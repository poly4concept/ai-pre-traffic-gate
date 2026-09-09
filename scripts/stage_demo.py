#!/usr/bin/env python3
"""The stage demo, as one command. Phase 6.1.

    $env:AWS_PROFILE = 'poly4'
    python scripts/stage_demo.py halt          # the gate blocks a deploy
    python scripts/stage_demo.py --teardown    # put everything back

WHY THIS EXISTS

Until now the demo was: open two terminals, inject a fault in one, start traffic
in the other, wait about two minutes, check the alarms, then remember to push
the crafted commit while the traffic is still running. Six steps, an ordering
constraint, and a wait whose length you have to judge by eye.

That is fine at a desk. In front of a room it is six things that can go wrong,
and the two most likely -- pushing before the alarm has fired, or letting the
traffic stop before the gate runs -- both produce a demo that silently does the
wrong thing rather than failing visibly. The gate returns `medium` instead of
`high`, nothing is blocked, and you are explaining an anticlimax live.

So: one command, which narrates what it is doing and refuses to continue when a
precondition is not met.

WHAT IT WILL NOT DO

Push anything until the alarm is actually firing. That check is the whole point.
`craft_commit.py --push` starts a pipeline, and a pipeline started against a
healthy target cannot produce the verdict this demo is about.

TEARDOWN IS NOT OPTIONAL

Ctrl+C at any point runs it. So does `--teardown` on its own, and so does a
crash. Fault injection left switched on makes every subsequent deploy roll
itself back (D-057), which is a genuinely confusing state to leave an account
in -- and the person most likely to hit it is you, tomorrow, having forgotten.

COST: a few hundred Lambda invocations and one Bedrock verdict. Under a cent.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

SCRIPTS = Path(__file__).resolve().parent
PROJECT = os.environ.get("PROJECT_NAME", "ai-pre-traffic-gate")
DEMO_APP = f"{PROJECT}-demo-app"
PIPELINE = f"{PROJECT}-pipeline"


# Enough traffic that a 60-second alarm period is a real measurement rather than
# a coin flip. At the 1/minute heartbeat each period is a sample of size one and
# the alarm flaps every minute; at 1/second it holds. See D-077.
TRAFFIC_INTERVAL_SECONDS = 1.0
ERROR_RATE = 0.5

# How long to wait for CloudWatch to catch up. Metrics lag one to two minutes,
# and then the alarm needs a full period on top.
ALARM_TIMEOUT_SECONDS = 420

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, colour: str) -> str:
    if not _USE_COLOUR:
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "dim": "90", "bold": "1", "cyan": "36"}
    return f"\033[{codes[colour]}m{text}\033[0m"


def _load(name: str):
    """Import a sibling script by path.

    These are scripts rather than a package -- they are run directly and are not
    importable as `scripts.foo` without a package marker. Reusing them beats
    duplicating the traffic loop and the readiness checks, and duplication here
    would drift exactly where correctness matters.
    """
    spec = importlib.util.spec_from_file_location(f"_stage_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load("preflight")
drive_traffic = _load("drive_traffic")
inject_fault = _load("inject_fault")
craft_commit = _load("craft_commit")

# TAKEN FROM THE MODULE THAT OWNS THEM, not redeclared here.
#
# This originally read `f"{PROJECT}-faults"`, which is a perfectly reasonable
# guess and wrong -- the table is `ai-pre-traffic-gate-demo-app-faults`, because
# the faults belong to the demo app rather than to the project. The result was a
# ResourceNotFoundException at step 2 of a demo that had already passed
# preflight.
#
# Worth the comment because the docstring on `_load` above argues against
# duplicating these exact values, and I duplicated one anyway on the next
# screenful. `tests/test_stage_demo.py` now asserts they match.
FAULT_TABLE = inject_fault.DEFAULT_TABLE
FAULT_KEY = inject_fault.DEFAULT_KEY


def step(n: int, total: int, title: str) -> None:
    print()
    print(_c(f"{'=' * 74}", "dim"))
    print(_c(f"  STEP {n}/{total}  {title}", "bold"))
    print(_c(f"{'=' * 74}", "dim"))


# The model writes em-dashes and curly quotes. The Windows console codepage is
# cp1252 and renders them as a replacement glyph -- which is exactly the mojibake
# `evals/report.py` was made ASCII-only to avoid, arriving here through model
# prose rather than through our own strings.
#
# Folded at DISPLAY time, not at capture: the stored replay is an audit artifact
# and should hold what the model actually wrote.
_ASCII_FOLD = str.maketrans(
    {
        "—": "--",  # em dash
        "–": "-",  # en dash
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "…": "...",
        " ": " ",
    }
)


def _ascii(text: str) -> str:
    return text.translate(_ASCII_FOLD).encode("ascii", "replace").decode("ascii")


def say(text: str, colour: str = "") -> None:
    text = _ascii(text)
    print(f"    {_c(text, colour) if colour else text}")


class TrafficDriver:
    """Keeps the demo app under load in a background thread.

    A thread rather than a subprocess because the teardown has to be able to
    stop it reliably from a signal handler, and because a subprocess whose
    parent dies mid-demo keeps hammering the function.
    """

    def __init__(self, region: str):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._region = region
        self.sent = 0
        self.faulted = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        lam = boto3.client(
            "lambda",
            region_name=self._region,
            config=Config(retries={"max_attempts": 0}, read_timeout=30, connect_timeout=5),
        )
        target = f"{DEMO_APP}:live"
        while not self._stop.is_set():
            try:
                response = lam.invoke(
                    FunctionName=target, InvocationType="RequestResponse", Payload=b"{}"
                )
                self.sent += 1
                if response.get("FunctionError"):
                    self.faulted += 1
            except ClientError:
                # A refused invoke is a permissions problem and will not fix
                # itself. Stop rather than spin -- and the caller notices
                # because `sent` stops climbing.
                self._stop.set()
                return
            self._stop.wait(TRAFFIC_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def observed_error_rate(self) -> float | None:
        return 100 * self.faulted / self.sent if self.sent else None


def firing_alarms(cw) -> list[str]:
    return [a["AlarmName"] for a in drive_traffic.describe_alarms(cw) if a["StateValue"] == "ALARM"]


def teardown(driver: TrafficDriver | None, region: str, *, cleanup_commits: bool) -> None:
    print()
    print(_c("=" * 74, "dim"))
    print(_c("  TEARDOWN", "bold"))
    print(_c("=" * 74, "dim"))

    if driver:
        driver.stop()
        say(f"traffic stopped after {driver.sent} invocations")

    try:
        inject_fault.write(FAULT_TABLE, FAULT_KEY, region, {"note": "stage demo teardown"})
        say("fault injection cleared", "green")
    except SystemExit:
        say("could not clear fault injection -- run: python scripts/inject_fault.py off", "red")

    if cleanup_commits:
        # Checked BEFORE calling, so the message matches what happened. Saying
        # "removed" when nothing existed is the same class of small lie this
        # project keeps finding in its own status output (F-016, F-018) -- and a
        # teardown you cannot trust is one you end up running twice.
        had_commits = craft_commit.SYNTHETIC_DIR.exists()
        try:
            craft_commit.cleanup(push=True, dry_run=False)
            say(
                "synthetic commits removed" if had_commits else "no synthetic commits to remove",
                "green" if had_commits else "dim",
            )
        except Exception as exc:  # noqa: BLE001 - teardown reports, never raises
            say(f"could not clean up commits ({exc}); run craft_commit.py cleanup --push", "yellow")

    say("alarms return to OK on their own once the errors stop.", "dim")


def run_halt_demo(args) -> int:
    region = args.region
    total = 5
    driver: TrafficDriver | None = None

    cw = boto3.client("cloudwatch", region_name=region)
    session = boto3.Session(region_name=region)

    try:
        # --- 1 ------------------------------------------------------------
        step(1, total, "Preflight -- is the system configured for this demo?")
        checks, state = preflight.collect(session)
        results = preflight.judge("halt", state)
        blockers = []
        for ok, label, why in results:
            say(f"{_c('+', 'green') if ok else _c('!', 'red')} {label}")
            if not ok:
                say(f"    {why}", "dim")
                blockers.append(label)

        # `unhealthy` is the one precondition this script creates itself, so a
        # healthy target here is the expected starting state rather than a
        # blocker. Everything else has to be true before we touch anything.
        real = [b for b in blockers if not b.startswith("unhealthy")]
        if real:
            say("")
            say(f"{len(real)} precondition(s) unmet. Fix them before running the demo.", "red")
            return 1
        say("")
        say("configuration is ready.", "green")

        # --- 2 ------------------------------------------------------------
        step(2, total, "Break the demo app")
        try:
            inject_fault.write(
                FAULT_TABLE,
                FAULT_KEY,
                region,
                {"error_rate": ERROR_RATE, "note": "stage demo"},
            )
        except SystemExit:
            # `inject_fault.write` exits on AccessDenied and prints the fix.
            # Caught so this reads as a demo that could not start rather than a
            # stack of errors, and so the teardown below still runs.
            say("")
            say("cannot break the app, so there is no demo to run.", "red")
            return 1
        say(f"fault injection on: {ERROR_RATE:.0%} of requests will raise", "yellow")
        say("the app reads this on every request, so it takes effect within ~5s", "dim")

        # --- 3 ------------------------------------------------------------
        step(3, total, "Drive traffic until the alarm fires")
        driver = TrafficDriver(region)
        driver.start()
        say(f"sending ~{60 / TRAFFIC_INTERVAL_SECONDS:.0f} requests/minute in the background")
        say("the heartbeat alone is 1/minute -- too thin for a 60s alarm period (D-077)", "dim")

        deadline = time.monotonic() + ALARM_TIMEOUT_SECONDS
        firing: list[str] = []
        while time.monotonic() < deadline:
            time.sleep(15)
            if driver.sent == 0:
                say("no invocations are reaching the function -- check credentials", "red")
                return 1
            firing = firing_alarms(cw)
            rate = driver.observed_error_rate
            elapsed = int(ALARM_TIMEOUT_SECONDS - (deadline - time.monotonic()))
            say(
                f"[{elapsed:>3}s] sent={driver.sent:<4} observed error rate="
                f"{rate:.0f}%  alarms firing={len(firing)}"
            )
            if firing:
                break

        if not firing:
            say("")
            say(f"no alarm fired within {ALARM_TIMEOUT_SECONDS}s. NOT pushing.", "red")
            say("Pushing now would produce a `medium` verdict and no halt.", "dim")
            return 1

        say("")
        say(f"ALARM: {', '.join(firing)}", "green")
        say("the target is now genuinely unhealthy, and the gate can see it.", "dim")

        # --- 4 ------------------------------------------------------------
        step(4, total, "Push a change the gate should refuse")
        if args.dry_run:
            say("--dry-run: stopping before the push", "yellow")
            return 0

        say(f"recipe: {args.recipe} -- a payments change, backdated to Friday evening")
        craft_commit.create(craft_commit.BY_NAME[args.recipe], push=True, dry_run=False)
        say("pushed. the pipeline is running.", "green")

        # --- 5 ------------------------------------------------------------
        step(5, total, "Watch the gate decide")
        say("traffic keeps flowing so the alarm is still firing when the gate looks", "dim")
        say("")
        say("watch here:", "bold")
        say(f"  https://console.aws.amazon.com/codesuite/codepipeline/pipelines/{PIPELINE}/view")
        say("")
        say("or follow the verdict as it lands:", "bold")
        say(f"  aws logs tail /aws/lambda/{PROJECT}-gate --follow")
        say("")
        say("expect: Gate stage FAILS, an email arrives, and the deploy never happens.", "cyan")
        say("")
        input(_c("    press Enter when the demo is finished, to tear down... ", "yellow"))
        return 0

    except KeyboardInterrupt:
        print()
        say("interrupted", "yellow")
        return 130
    finally:
        teardown(driver, region, cleanup_commits=not args.keep_commits)


REPLAYS = SCRIPTS.parent / "demo" / "replays"


def _attr(item: dict, *path: str) -> str:
    """Read a nested DynamoDB attribute as a string."""
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
    return ""


def _health_of(record: dict) -> dict:
    """Pull the health numbers out from two maps deep, flat.

    Done here rather than in `replay` so the replay never has to know the shape
    of an audit record -- if that shape changes, this is the only thing to fix.
    """
    health = (
        record.get("signals", {})
        .get("M", {})
        .get("signals", {})
        .get("M", {})
        .get("target_health", {})
        .get("M", {})
        .get("data", {})
        .get("M", {})
    )
    return {
        "error_rate_pct": health.get("error_rate_pct", {}).get("N", ""),
        "invocations_last_hour": health.get("invocations_last_hour", {}).get("N", ""),
        "alarms_firing": sum(
            1
            for a in health.get("alarms", {}).get("L", [])
            if a.get("M", {}).get("state", {}).get("S") == "ALARM"
        ),
    }


def capture(args) -> int:
    """Save a real verdict so the demo can be replayed without AWS.

    Only from a real pipeline execution -- a manual invoke has no change context
    and fails closed, which would make a replay of the gate refusing to answer
    rather than of the gate deciding. The same distinction `gate_history.py`
    draws for the same reason.
    """
    ddb = boto3.client("dynamodb", region_name=args.region)
    items = [
        i
        for i in ddb.scan(TableName=f"{PROJECT}-verdicts").get("Items", [])
        if _attr(i, "pipeline_execution_id")
    ]
    if args.verdict_id:
        items = [i for i in items if _attr(i, "verdict_id") == args.verdict_id]
        if not items:
            say(f"no verdict with id {args.verdict_id}", "red")
            return 1
    if not items:
        say("no verdicts from real pipeline executions to capture", "red")
        say("run the pipeline once, then capture.", "dim")
        return 1

    items.sort(key=lambda i: _attr(i, "recorded_at"), reverse=True)
    record = items[0]
    concerns = record.get("verdict", {}).get("M", {}).get("primary_concerns", {}).get("L", [])

    payload = {
        "captured_at": _attr(record, "recorded_at"),
        "commit_sha": _attr(record, "commit_sha"),
        "mode": _attr(record, "mode"),
        "action_taken": _attr(record, "action_taken"),
        "verdict_id": _attr(record, "verdict_id"),
        "pipeline_execution_id": _attr(record, "pipeline_execution_id"),
        "risk_level": _attr(record, "verdict", "risk_level"),
        "reasoning": _attr(record, "verdict", "reasoning"),
        "confidence": _attr(record, "verdict", "confidence"),
        "source": _attr(record, "verdict", "source"),
        "floor_raised_from": _attr(record, "verdict", "floor_raised_from"),
        "primary_concerns": [c.get("S", "") for c in concerns],
        "model_id": _attr(record, "model_call", "model_id"),
        "prompt_version": _attr(record, "model_call", "prompt_version"),
        "latency_ms": _attr(record, "model_call", "latency_ms"),
        "input_tokens": _attr(record, "model_call", "input_tokens"),
        "health": _health_of(record),
    }

    REPLAYS.mkdir(parents=True, exist_ok=True)
    dest = REPLAYS / f"{args.name}.json"
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    say(f"captured a {payload['risk_level']} verdict from {payload['captured_at'][:16]}", "green")
    say(f"  commit {payload['commit_sha'][:12]}, action {payload['action_taken']}")
    say(f"  wrote {dest}")

    # THE NEWEST VERDICT IS OFTEN THE WRONG ONE, and quietly so.
    #
    # Overriding a halt makes the gate re-run, and that re-run is a SECOND
    # verdict -- same high risk, but `action_taken: none` because the override
    # allowed it. It is newer than the halt it followed, so "most recent" lands
    # on the anticlimax: a replay that says the gate was worried and then shipped
    # anyway.
    #
    # Named rather than auto-corrected, because which run you want to show is a
    # decision about the talk, not something this script should make.
    if payload["action_taken"] != "halt_pipeline":
        halted = [i for i in items if _attr(i, "action_taken") == "halt_pipeline"]
        say("")
        say(f"NOTE: this verdict took no action ({payload['action_taken']}).", "yellow")
        if halted:
            best_id = _attr(halted[0], "verdict_id")
            say("      A halting verdict is usually the one worth replaying:", "yellow")
            say(f"      python scripts/stage_demo.py capture --verdict-id {best_id}", "bold")
        else:
            say("      No halting verdict exists yet to capture instead.", "dim")

    say("")
    say(f"replay it:  python scripts/stage_demo.py replay --name {args.name}", "bold")
    return 0


def replay(args) -> int:
    """Play back a captured run. Makes NO network calls of any kind.

    A live demo depends on AWS behaving for four minutes in front of a room, and
    that is a dependency you can simply remove. Everything below comes out of a
    JSON file.

    THE BANNER IS NOT OPTIONAL and there is no flag to suppress it. Playing a
    recording while implying it is live would be lying to an audience, and a
    talk whose entire argument is about honest measurement cannot open by faking
    its own demo. A recording is a perfectly respectable thing to show; passing
    one off as live is not.
    """
    path = REPLAYS / f"{args.name}.json"
    if not path.exists():
        say(f"no replay named {args.name!r} at {path}", "red")
        say("capture one first:  python scripts/stage_demo.py capture", "dim")
        return 1

    r = json.loads(path.read_text(encoding="utf-8"))
    pause = max(args.speed, 0.0)

    print()
    print(_c("=" * 74, "yellow"))
    print(_c(f"  REPLAY -- recorded {r['captured_at'][:16]} UTC. No AWS calls.", "yellow"))
    print(_c("=" * 74, "yellow"))

    total = 4
    step(1, total, "The target service is unhealthy")
    time.sleep(pause)
    health = r["health"]
    say(f"error rate         {float(health['error_rate_pct'] or 0):.1f}%")
    say(f"invocations/hour   {health['invocations_last_hour']}")
    say(f"alarms firing      {health['alarms_firing']}", "red")

    step(2, total, "A change arrives")
    time.sleep(pause)
    say(f"commit    {r['commit_sha'][:12]}")
    say(f"execution {r['pipeline_execution_id'][:8]}...")

    step(3, total, "The gate forms a verdict")
    time.sleep(pause)
    say(f"model     {r['model_id']}")
    say(f"prompt    {r['prompt_version']}")
    say(f"took      {r['latency_ms']}ms, {r['input_tokens']} input tokens")
    say("")
    say(f"RISK: {r['risk_level'].upper()}", "red" if r["risk_level"] == "high" else "yellow")
    if r.get("floor_raised_from"):
        say(f"  raised from {r['floor_raised_from']} by the deterministic floor", "cyan")
    say(f"confidence {r['confidence']}")
    say("")
    for line in textwrap.wrap(r["reasoning"], width=66) or [""]:
        say(line)
    if r["primary_concerns"]:
        say("")
        say("PRIMARY CONCERNS", "bold")
        for concern in r["primary_concerns"]:
            for i, line in enumerate(textwrap.wrap(concern, width=64) or [""]):
                say(f"  {'-' if i == 0 else ' '} {line}")

    step(4, total, "What happened next")
    time.sleep(pause)
    if r["action_taken"] == "halt_pipeline":
        say("the gate STOPPED the pipeline. nothing was deployed.", "red")
        say("an escalation email went to a human, carrying the reasoning above.", "dim")
    else:
        say(f"mode was {r['mode']}, so the gate recorded and did not block.", "yellow")
        say("in `enforcing` this deploy would have been refused.", "dim")
    say("")
    say(f"verdict_id {r['verdict_id']}", "dim")
    say("the full signal bundle and raw model output are in DynamoDB.", "dim")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        default="halt",
        choices=["halt", "capture", "replay"],
        help="halt: run it live. capture: save the last real verdict. replay: play it back.",
    )
    parser.add_argument("--name", default="halt", help="replay file to write or read")
    parser.add_argument("--verdict-id", help="capture a specific verdict rather than the newest")
    parser.add_argument(
        "--speed",
        type=float,
        default=1.5,
        help="seconds between replay steps (0 for instant)",
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument(
        "--recipe",
        default="payments-friday",
        choices=sorted(craft_commit.BY_NAME),
        help="craft_commit recipe to push",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="everything up to the push, then stop. Safe rehearsal.",
    )
    parser.add_argument(
        "--keep-commits",
        action="store_true",
        help="leave the synthetic commit in place (still clears fault injection)",
    )
    parser.add_argument(
        "--teardown",
        action="store_true",
        help="just put everything back, and exit",
    )
    args = parser.parse_args()

    if args.teardown:
        teardown(None, args.region, cleanup_commits=True)
        return 0
    if args.mode == "capture":
        return capture(args)
    if args.mode == "replay":
        return replay(args)

    return run_halt_demo(args)


if __name__ == "__main__":
    sys.exit(main())
