#!/usr/bin/env python3
"""Is this stack actually configured to do what you think? Phase 5.4d.

    python scripts/preflight.py                 # what state am I in?
    python scripts/preflight.py --demo halt     # can I demo a blocked deploy?
    python scripts/preflight.py --demo canary   # ...a gradual rollout?
    python scripts/preflight.py --demo clean    # ...a routine green deploy?

WHY THIS EXISTS

A demo run needs six things to be true at once, and they live in five different
places:

    gate_mode                  terraform.tfvars -> Lambda env
    executor_enforces_verdict  terraform.tfvars -> Lambda env
    MODEL_VERDICT_CAN_ACT      a CODE CONSTANT, invisible from the console
    SNS subscription           confirmed out of band, by clicking an email
    fault injection            a DynamoDB row
    alarm state                emergent, from traffic rate vs alarm period

Nothing showed them together, and nothing checked they were consistent. The
consequence was a run where the gate correctly returned `high`, correctly
escalated, and nothing was blocked -- because a `terraform apply` had quietly
reset the mode to `advisory` from a tfvars file, and the operator had no way to
see that without reading three consoles.

Every failure in that run was a precondition being wrong. None was a bug in the
gate. That distinction is the whole argument for this file: the system worked
and the CONFIGURATION was invisible, which in a real environment is the more
dangerous of the two.

WHY IT READS AWS AND NOT THE REPOSITORY

`terraform.tfvars` says what the last person intended. The Lambda's environment
says what is actually running. Those differ the moment somebody applies with a
`-var` flag, or forgets to apply at all, and the second one is the truth. F-023
cost two increments to learn this: check reality, not the source that hopes to
describe it.

`MODEL_VERDICT_CAN_ACT` is the exception and the interesting case -- it is a
code constant, so no AWS API can report it. It is read from the most recent
verdict record instead, because the gate stamps every verdict with the value it
ran under. The deployed system describing itself, rather than a file describing
what it should be.

READ-ONLY. Runs as `ai-agent`; needs no admin profile.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import ClientError

PROJECT = os.environ.get("PROJECT_NAME", "ai-pre-traffic-gate")
GATE = f"{PROJECT}-gate"
EXECUTOR = f"{PROJECT}-executor"
DEMO_APP = f"{PROJECT}-demo-app"
VERDICTS = f"{PROJECT}-verdicts"
FAULTS = f"{DEMO_APP}-faults"

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(text: str, colour: str) -> str:
    if not _USE_COLOUR:
        return text
    codes = {"green": "32", "red": "31", "yellow": "33", "dim": "90", "bold": "1"}
    return f"\033[{codes[colour]}m{text}\033[0m"


class Check:
    """One precondition, its observed value, and what to do if it is wrong."""

    def __init__(self, name: str, value: str, ok: bool | None, fix: str = ""):
        self.name = name
        self.value = value
        self.ok = ok  # None means "informational, not pass/fail"
        self.fix = fix

    def render(self) -> str:
        if self.ok is None:
            mark, colour = " ", "dim"
        elif self.ok:
            mark, colour = "+", "green"
        else:
            mark, colour = "!", "red"
        return f"  {_c(mark, colour)} {self.name:32} {_c(self.value, colour)}"


def env_of(client, function: str) -> dict[str, str]:
    try:
        conf = client.get_function_configuration(FunctionName=function)
    except ClientError as exc:
        raise SystemExit(f"cannot read {function}: {exc.response['Error']['Code']}") from exc
    return (conf.get("Environment") or {}).get("Variables") or {}


def latest_verdict(ddb) -> dict:
    """The newest verdict for the demo app, or {}.

    Via the GSI, newest first, one row. This is how MODEL_VERDICT_CAN_ACT is
    discovered -- see the module docstring.
    """
    try:
        page = ddb.query(
            TableName=VERDICTS,
            IndexName="by_service_recorded_at",
            KeyConditionExpression="service_name = :s",
            ExpressionAttributeValues={":s": {"S": DEMO_APP}},
            ScanIndexForward=False,
            Limit=1,
        )
        keys = page.get("Items") or []
        if not keys:
            return {}
        got = ddb.get_item(TableName=VERDICTS, Key={"verdict_id": keys[0]["verdict_id"]})
        return got.get("Item") or {}
    except ClientError:
        return {}


def can_act_from_logs(session) -> bool | None:
    """`MODEL_VERDICT_CAN_ACT` as the DEPLOYED gate last reported it.

    Returns None when the gate has not logged a decision recently enough to
    tell. None means "could not find out", never "false" -- guessing `false`
    here would report the model as inert when it may be live, which is the more
    dangerous direction to be wrong in.

    Searches 24 hours. Beyond that the answer is too stale to trust anyway, and
    the log group's retention is 7 days.
    """
    logs = session.client("logs")
    end = datetime.now(UTC)
    kwargs = {
        "logGroupName": f"/aws/lambda/{GATE}",
        "startTime": int((end - timedelta(hours=24)).timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
        "filterPattern": '"model_verdict_can_act"',
    }

    # MUST PAGE TO THE END, and getting this wrong reported the opposite of the
    # truth on the first attempt.
    #
    # `filter_log_events` returns matches in ASCENDING time order, so `limit=20`
    # does not mean "the 20 most recent" -- it means the 20 OLDEST in the
    # window. Taking the newest of those still gave a stale answer: the gate had
    # logged `true` minutes earlier and preflight confidently said `false`.
    #
    # A check that reports a stale value as current is worse than one that
    # reports nothing, because it is believed. Hence: page to the end, keep the
    # genuinely latest event, and bound the loop so a busy log group cannot make
    # preflight hang.
    latest: tuple[int, str] | None = None
    for _ in range(20):
        try:
            page = logs.filter_log_events(**kwargs)
        except ClientError:
            return None
        for event in page.get("events", []):
            if latest is None or event["timestamp"] > latest[0]:
                latest = (event["timestamp"], event.get("message", ""))
        token = page.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token

    if latest is None:
        return None
    if '"model_verdict_can_act": true' in latest[1]:
        return True
    if '"model_verdict_can_act": false' in latest[1]:
        return False
    return None


def collect(session) -> tuple[list[Check], dict[str, str]]:
    lam = session.client("lambda")
    ddb = session.client("dynamodb")
    cw = session.client("cloudwatch")
    sns = session.client("sns")

    gate_env = env_of(lam, GATE)
    exec_env = env_of(lam, EXECUTOR)
    state: dict[str, str] = {}
    checks: list[Check] = []

    # --- 1. the gate's mode ------------------------------------------------
    mode = gate_env.get("GATE_MODE", "").strip().lower() or "(unset)"
    state["mode"] = mode
    checks.append(
        Check(
            "gate_mode",
            mode,
            None,
            'terraform apply -var="gate_mode=enforcing"',
        )
    )

    # --- 2. can the model's verdict act? -----------------------------------
    #
    # Not available from any AWS API -- it is a constant in the deployed zip.
    # The gate stamps it on every verdict, so the last verdict is the only
    # honest source. Records written before Phase 5.4d do not carry it.
    record = latest_verdict(ddb)
    raw = record.get("model_verdict_can_act", {})
    if "BOOL" in raw:
        state["can_act"] = "true" if raw["BOOL"] else "false"
        when = record.get("recorded_at", {}).get("S", "?")[:16]
        value = f"{state['can_act']}  (from the verdict at {when})"
    else:
        # FALLBACK, and it exists because the obvious design had a
        # chicken-and-egg: the audit field only appears once a pipeline has run
        # under the new code, so immediately after a deploy -- exactly when you
        # most want to check -- preflight could not answer.
        #
        # The gate logs its full decision record on EVERY invocation, so
        # CloudWatch Logs knows sooner than DynamoDB does. Slower and scrappier
        # than a table read, which is why it is second rather than first.
        found = can_act_from_logs(session)
        if found is None:
            state["can_act"] = "unknown"
            value = "unknown -- the gate has not run since this field was added"
        else:
            state["can_act"] = "true" if found else "false"
            value = f"{state['can_act']}  (from the gate's logs)"
    checks.append(
        Check(
            "MODEL_VERDICT_CAN_ACT",
            value,
            None,
            "a code constant in handler.py; deploy the gate to change it",
        )
    )

    # --- 3. the executor's switch ------------------------------------------
    enforces = exec_env.get("EXECUTOR_ENFORCES_VERDICT", "").strip().lower()
    state["executor"] = enforces or "(unset)"
    checks.append(Check("executor_enforces_verdict", state["executor"], None))

    # --- 4. the manual override --------------------------------------------
    #
    # Normally empty. A leftover value here silently decides every deploy, which
    # is the failure mode that motivated the per-execution override path (D-071).
    decision = gate_env.get("GATE_DECISION", "").strip().lower()
    state["gate_decision"] = decision
    checks.append(
        Check(
            "GATE_DECISION",
            decision or "(unset -- normal)",
            not decision,
            'a leftover break-glass override. terraform apply -var="gate_decision="',
        )
    )

    # --- 5. can a halt reach a human? --------------------------------------
    topic = gate_env.get("ESCALATION_TOPIC_ARN", "")
    confirmed = False
    detail = "no topic configured"
    if topic:
        try:
            subs = sns.list_subscriptions_by_topic(TopicArn=topic).get("Subscriptions", [])
        except ClientError:
            subs = []
        live = [s for s in subs if not s["SubscriptionArn"].startswith("PendingConfirmation")]
        pending = len(subs) - len(live)
        confirmed = bool(live)
        if live:
            detail = ", ".join(s["Endpoint"] for s in live)
        elif pending:
            # SNS reports a publish to an unconfirmed subscription as SUCCESS.
            # Silence downstream is therefore indistinguishable from working.
            detail = f"{pending} subscription(s) PENDING CONFIRMATION -- click the AWS email"
        else:
            detail = "topic exists with NO subscribers"
    state["escalation_ok"] = "yes" if confirmed else "no"
    checks.append(Check("escalation reaches", detail, confirmed, "check the SNS topic subscribers"))

    # --- 6. is the target broken right now? --------------------------------
    fault = {}
    try:
        got = ddb.get_item(TableName=FAULTS, Key={"config_key": {"S": "demo-app"}})
        fault = {k: list(v.values())[0] for k, v in (got.get("Item") or {}).items()}
    except ClientError:
        pass
    rate = float(fault.get("error_rate", 0) or 0)
    latency = float(fault.get("latency_ms", 0) or 0)
    state["fault_rate"] = str(rate)
    injected = rate > 0 or latency > 0
    checks.append(
        Check(
            "fault injection",
            f"errors={rate:.0%} latency={latency:.0f}ms" if injected else "off",
            None,
            "python scripts/inject_fault.py off",
        )
    )

    # --- 7. what the gate will actually see --------------------------------
    firing: list[str] = []
    try:
        alarms = cw.describe_alarms(AlarmNamePrefix=DEMO_APP).get("MetricAlarms", [])
        firing = [a["AlarmName"] for a in alarms if a["StateValue"] == "ALARM"]
        # Strip the shared project prefix rather than splitting on hyphens --
        # the resource names contain them, so `rsplit` labelled the throttles
        # alarm "app".
        states = ", ".join(
            f"{a['AlarmName'].removeprefix(DEMO_APP + '-')}={a['StateValue']}" for a in alarms
        )
    except ClientError:
        states = "could not read alarms"
    state["alarms_firing"] = str(len(firing))
    checks.append(Check("alarms", states or "none configured", None))

    # Traffic rate, because the alarm's 60-second period needs roughly ten
    # invocations a minute to be stable under intermittent faults. At the
    # heartbeat's one-per-minute every evaluation is a sample of size one and
    # the alarm flaps -- which looks exactly like the injection not working.
    per_min = 0.0
    end = datetime.now(UTC)
    try:
        res = cw.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="Invocations",
            Dimensions=[{"Name": "FunctionName", "Value": DEMO_APP}],
            StartTime=end - timedelta(minutes=15),
            EndTime=end,
            Period=300,
            Statistics=["Sum"],
        )
    except ClientError:
        res = {"Datapoints": []}
    if res.get("Datapoints"):
        per_min = max(d["Sum"] for d in res["Datapoints"]) / 5.0
    state["per_min"] = f"{per_min:.0f}"
    checks.append(
        Check(
            "traffic",
            f"~{per_min:.0f}/min"
            + ("  (too thin to hold an alarm; see --demo halt)" if 0 < per_min < 10 else ""),
            None,
        )
    )

    return checks, state


DEMOS = {
    "halt": {
        "what": "the gate blocks a deploy and emails a human",
        "needs": [
            ("mode", "enforcing", "the mode governs all blocking (D-076)"),
            ("can_act", "true", "otherwise the model's verdict is recorded and inert"),
            ("gate_decision", "", "a leftover override decides the outcome instead"),
            ("escalation", "yes", "otherwise the halt is silent"),
            ("unhealthy", "yes", "a healthy target will not produce a `high` verdict"),
        ],
    },
    "canary": {
        "what": "a medium verdict ships gradually",
        "needs": [
            ("executor", "true", "otherwise every risk level deploys identically"),
            ("gate_decision", "", "a leftover override decides the outcome instead"),
            ("healthy", "yes", "CodeDeploy rolls back a canary into a firing alarm (D-057)"),
        ],
    },
    "clean": {
        "what": "a routine change deploys green, end to end",
        "needs": [
            ("gate_decision", "", "a leftover override decides the outcome instead"),
            ("healthy", "yes", "an unhealthy target pushes even a safe change upward"),
        ],
    },
}


def judge(demo: str, state: dict[str, str]) -> list[tuple[bool, str, str]]:
    """Evaluate one demo's preconditions against observed state."""
    healthy = state["alarms_firing"] == "0" and float(state.get("fault_rate", 0) or 0) == 0
    results = []
    for key, want, why in DEMOS[demo]["needs"]:
        if key == "escalation":
            ok = state.get("escalation_ok") == "yes"
            got = "reaches a human" if ok else "goes nowhere"
        elif key == "unhealthy":
            ok = state["alarms_firing"] != "0"
            got = f"{state['alarms_firing']} alarm(s) firing"
        elif key == "healthy":
            ok = healthy
            got = "target is healthy" if healthy else "target is broken or alarming"
        else:
            ok = state.get(key, "") == want
            got = state.get(key) or "(unset)"
        results.append((ok, f"{key} = {got}", why))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", choices=sorted(DEMOS), help="check readiness for one scenario")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    checks, state = collect(session)

    print(_c("\n" + "=" * 74, "bold"))
    print(_c(f"PREFLIGHT -- {PROJECT}", "bold"))
    print(_c("=" * 74, "bold"))
    print(_c("\n  observed on AWS, not read from terraform.tfvars\n", "dim"))
    for check in checks:
        print(check.render())

    if not args.demo:
        print(_c("\n  Add --demo halt|canary|clean to check readiness for a scenario.\n", "dim"))
        return 0

    spec = DEMOS[args.demo]
    print(_c(f"\n  READY FOR --demo {args.demo}?", "bold"))
    print(_c(f"  ({spec['what']})\n", "dim"))

    results = judge(args.demo, state)
    for ok, got, why in results:
        mark = _c("+", "green") if ok else _c("!", "red")
        print(f"  {mark} {got}")
        if not ok:
            print(_c(f"      {why}", "yellow"))

    blocked = [r for r in results if not r[0]]
    if blocked:
        print(_c(f"\n  NOT READY -- {len(blocked)} precondition(s) unmet.\n", "red"))
        return 1

    print(_c("\n  READY.\n", "green"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
