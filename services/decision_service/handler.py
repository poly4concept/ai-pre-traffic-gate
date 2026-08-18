"""The decision service. Hardcoded verdict (Phase 1) + real signals (Phase 2).

The decision itself is still a single environment variable -- no Bedrock, no
schema, no judgement. Everything *around* that variable is real: the fail-closed
default, the three operating modes, the audit line, the CodePipeline contract,
and since Phase 2.2b a genuine signal bundle collected from the pipeline event.

The signals are collected and logged but deliberately do not influence the
verdict. That is Phase 3's job. Wiring collection in first, while the decision
stays hardcoded, means that when a model finally arrives the signals feeding it
are already known to be real -- the same reason Phase 1 built and proved the
deploy path before any AI touched it.

The point of building it this way is to make one claim testable before any model
exists: *the pipeline can be halted, and the halt cannot be bypassed.* If that
is not provably true with a hardcoded verdict, it will not become true by adding
an LLM. This is the phase CLAUDE.md warns about skipping, and this file is the
part people skip within it.

Two behaviours here are worth reading closely, because they are the design:

1. FAIL CLOSED IS THE DEFAULT BRANCH, NOT AN EXCEPT HANDLER. An unset variable,
   a typo, an unrecognised value, an unexpected exception -- every one of those
   paths ends at `halt`, because `halt` is what the function does unless it is
   specifically told otherwise. There is no `except: return allow` anywhere, and
   there is no code path that reaches `allow` by accident.

2. MODE IS SEPARATE FROM VERDICT. The verdict says what the gate thinks; the
   mode says whether anyone acts on it. Keeping them apart is what makes shadow
   mode a real mode rather than a disabled feature -- in shadow the verdict is
   computed and recorded in full, and then deliberately not acted upon.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from signals import (
    DisabledCollector,
    InspectorFindingsCollector,
    MockChangeContextCollector,
    PipelineChangeContextCollector,
    TargetHealthCloudWatchCollector,
    collect_signals,
)
from signals.types import DeploymentTarget

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The stand-in verdict. Phase 3 replaces this single lookup with signal
# collection plus a Bedrock call; nothing else in this file needs to change,
# which is the test of whether the seam is in the right place.
GATE_DECISION = os.environ.get("GATE_DECISION", "").strip().lower()

# shadow | advisory | enforcing -- see variables.tf for what each means.
GATE_MODE = os.environ.get("GATE_MODE", "").strip().lower()

ALLOW = "allow"
HALT = "halt"

VALID_DECISIONS = frozenset({ALLOW, HALT})
VALID_MODES = frozenset({"shadow", "advisory", "enforcing"})

# Modes in which the gate is permitted to actually stop a deployment. Shadow and
# advisory both record and report; only enforcing acts. Expressed as a set so
# adding a mode later is a data change, not a new branch in the control flow.
BLOCKING_MODES = frozenset({"enforcing"})


def resolve_decision(raw: str) -> tuple[str, str]:
    """Map the configured value to a verdict. Anything unrecognised halts.

    Returns (decision, reason). The reason exists so the audit record can
    distinguish "someone chose to halt this" from "the gate could not tell what
    it was being asked and defaulted to safety" -- operationally those are very
    different events that would otherwise look identical in the logs.
    """
    if raw == ALLOW:
        return ALLOW, "explicitly configured to allow"
    if raw == HALT:
        return HALT, "explicitly configured to halt"
    if not raw:
        # Not an error case to be handled -- the ordinary path for an
        # unconfigured gate. A gate that does not know its verdict has not
        # approved anything.
        return HALT, "GATE_DECISION is not set; failing closed"
    return HALT, f"GATE_DECISION={raw!r} is not a recognised verdict; failing closed"


def resolve_mode(raw: str) -> tuple[str, str | None]:
    """Map the configured mode. Anything unrecognised becomes enforcing.

    Note which direction this fails. An unreadable *verdict* becomes `halt`; an
    unreadable *mode* becomes `enforcing`. Both choices pick the outcome that
    stops a deploy, because the failure we are unwilling to have is a bad change
    reaching production because a config value was misspelled.

    The consequence is a real cost, and it belongs on a slide: a typo in
    GATE_MODE turns a shadow-mode rollout into an enforcing one, and the gate
    starts blocking deploys nobody expected it to touch. That is the correct
    trade -- an over-eager gate is visible within minutes, while a silently
    disabled one is discovered by the incident it failed to prevent -- but it is
    a trade, not a free win.
    """
    if raw in VALID_MODES:
        return raw, None
    if not raw:
        return "enforcing", "GATE_MODE is not set; assuming enforcing"
    return "enforcing", f"GATE_MODE={raw!r} is not a recognised mode; assuming enforcing"


def build_verdict(request_id: str) -> dict[str, Any]:
    """Assemble the audit record. Shaped like the Phase 3 verdict on purpose."""
    decision, decision_reason = resolve_decision(GATE_DECISION)
    mode, mode_warning = resolve_mode(GATE_MODE)

    # The two independent questions, kept independent:
    #   what does the gate think?      -> decision
    #   is the gate allowed to act?    -> mode
    blocking = decision == HALT and mode in BLOCKING_MODES

    verdict: dict[str, Any] = {
        "schema_version": 0,
        "source": "hardcoded-stub",
        "decision": decision,
        "decision_reason": decision_reason,
        "mode": mode,
        "action_taken": "halt_pipeline" if blocking else "none",
        "would_have_halted": decision == HALT,
        "request_id": request_id,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if mode_warning:
        verdict["mode_warning"] = mode_warning

    # `would_have_halted` is the field that makes shadow mode worth running. In
    # shadow, `action_taken` is always "none" and carries no information; this
    # is what you count to measure how often the gate would have blocked a
    # deploy that in fact went out fine. That ratio is the over-flagging rate
    # Phase 4 exists to measure, and Phase 8 exists to measure at scale.
    return verdict


def report_to_codepipeline(job: dict[str, Any], verdict: dict[str, Any]) -> None:
    """Report the result back to CodePipeline, if we were invoked by one.

    The contract is the reason this function exists at all: CodePipeline does not
    read the response payload of a Lambda invoke action -- it waits for an
    out-of-band PutJobSuccessResult or PutJobFailureResult call. A gate that
    returns a beautifully structured "halt" verdict and never calls
    PutJobFailureResult reports success to the pipeline, and the deploy proceeds.

    That failure mode is silent, which is the only kind worth writing a comment
    about: the logs show a halt verdict, the audit record shows a halt verdict,
    and the change ships anyway.
    """
    import boto3

    client = boto3.client("codepipeline")
    job_id = job["id"]

    if verdict["action_taken"] == "halt_pipeline":
        # 265 characters is the documented ceiling on failureDetails.message.
        # Truncating deliberately beats having the API reject the call and
        # leaving the job hanging until it times out.
        message = f"Gate halted deploy: {verdict['decision_reason']}"[:265]
        client.put_job_failure_result(
            jobId=job_id,
            failureDetails={"type": "JobFailed", "message": message},
        )
        logger.info("Reported job failure to CodePipeline: %s", job_id)
    else:
        client.put_job_success_result(jobId=job_id)
        logger.info("Reported job success to CodePipeline: %s", job_id)


SERVICE_NAME = os.environ.get("TARGET_SERVICE", "ai-pre-traffic-gate-demo-app")

# Amazon Inspector carries a standing per-function cost, so not enabling it is a
# legitimate choice rather than a misconfiguration. This toggle distinguishes the
# two: false means SKIPPED ("we chose not to look"), true means the real
# collector runs and reports honestly -- which, while Inspector is switched off,
# means UNAVAILABLE with a reason rather than a fabricated clean result.
SECURITY_SCANNING = os.environ.get("SECURITY_SCANNING", "true").strip().lower() != "false"

# CloudWatch metric queries and DescribeAlarms are billed per API request at a
# rate that rounds to nothing at one verdict per deploy, so unlike Inspector this
# collector has no standing cost and defaults on.
HEALTH_WINDOW_MINUTES = int(os.environ.get("HEALTH_WINDOW_MINUTES", "60"))


def collect_bundle(
    event: dict[str, Any],
    security_collector: Any = None,
    health_collector: Any = None,
) -> Any:
    """Collect the signal bundle. Phase 2 -- logged, and acted on by nothing.

    Deliberately does NOT influence the verdict yet. Phase 2's job is to produce
    a normalized bundle; Phase 3 is where a verdict starts depending on one.
    Wiring collection in first, with the decision still hardcoded, means that
    when the model arrives we already know the signals are real -- the same
    reason Phase 1 built the deploy path before any AI touched it.

    From Phase 2.4 all three collectors are real. Note what none of them are:
    mocks. A mock in this path would put fabricated health or security data into
    the audit trail of a real deployment, which is precisely the "absent signal
    read as a reassuring one" failure the package exists to prevent. Where a real
    collector cannot answer, it says so -- UNAVAILABLE or DEGRADED with a reason,
    never a plausible zero.
    """
    change_collector: Any
    if event.get("CodePipeline.job"):
        change_collector = PipelineChangeContextCollector(event)
    else:
        # A direct invoke -- someone testing the function by hand. There is no
        # pipeline job and therefore no change to describe. Left as a mock with
        # nothing to return, which fails closed and says so, rather than
        # pretending a change exists.
        change_collector = MockChangeContextCollector()

    # Injectable, defaulting to the real thing. Same discipline as
    # `collect_signals` one level down: the production path and the test path run
    # identical code, with no `is_mock` branch anywhere inside it. It also keeps
    # the test suite offline -- constructing InspectorFindingsCollector here with
    # no client builds a real boto3 client, which is how the suite briefly
    # started calling AWS for real.
    if security_collector is None:
        if SECURITY_SCANNING:
            security_collector = InspectorFindingsCollector(SERVICE_NAME)
        else:
            security_collector = DisabledCollector(
                "security_findings",
                "SECURITY_SCANNING is false; Inspector deliberately not consulted",
            )

    if health_collector is None:
        health_collector = TargetHealthCloudWatchCollector(
            SERVICE_NAME, window_minutes=HEALTH_WINDOW_MINUTES
        )

    return collect_signals(
        target=DeploymentTarget(service_name=SERVICE_NAME),
        change_collector=change_collector,
        security_collector=security_collector,
        health_collector=health_collector,
    )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Evaluate the gate. Halts unless positively told to allow."""
    request_id = getattr(context, "aws_request_id", "unknown")

    # Collected before the verdict and logged separately, so that a bug in
    # collection cannot take the gate's decision path down with it. In Phase 2
    # the gate must keep working exactly as it did in Phase 1.
    try:
        bundle = collect_bundle(event)
        logger.info(json.dumps({"signal_bundle": bundle.to_dict()}))
        logger.info(
            "signals: %s | required present: %s",
            bundle.completeness_summary,
            bundle.has_required_signals,
        )
        if not bundle.has_required_signals:
            # Phase 3 turns this into a halt. Saying so out loud now means the
            # log already shows what the gate WILL do, which is the same shadow
            # -mode discipline applied to a feature that does not exist yet.
            logger.warning(
                "change context unavailable: %s -- from Phase 3 this routes to human review",
                bundle.change.error,
            )
    except Exception:
        logger.exception("signal collection failed; continuing, verdict is unaffected in Phase 2")

    try:
        verdict = build_verdict(request_id)
    except Exception:
        # There is no recovery path here and deliberately no attempt at one.
        # If verdict construction itself failed we know nothing about the
        # change, and knowing nothing is grounds to stop.
        logger.exception("Verdict construction failed; failing closed")
        verdict = {
            "schema_version": 0,
            "source": "hardcoded-stub",
            "decision": HALT,
            "decision_reason": "internal error during verdict construction; failing closed",
            "mode": "enforcing",
            "action_taken": "halt_pipeline",
            "would_have_halted": True,
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    # One structured line per evaluation. Phase 3 adds the DynamoDB record;
    # until then this log IS the audit trail, and it is already complete enough
    # to answer "what did the gate decide, and did it act" after the fact.
    logger.info(json.dumps(verdict))

    job = event.get("CodePipeline.job")
    if job:
        report_to_codepipeline(job, verdict)

    return verdict
