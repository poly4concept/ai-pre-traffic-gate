"""The decision service. Phase 3.5 -- the model is now in the loop.

Until this increment the verdict was a single environment variable. Everything
around it was real -- signal collection, the fail-closed default, the three
modes, the CodePipeline contract -- and the decision itself was a lookup. That
was deliberate: Phase 1 proved the pipeline could be halted before any model
existed, and Phase 2 proved the signals were real before anything consumed them.

Now the four Phase 3 pieces are wired together:

    signals/          what the gate knows                   (Phase 2)
    verdict/prompt    how it asks                           (3.2)
    verdict/bedrock   asking, and failing closed            (3.3)
    verdict/audit     writing down what happened            (3.4)

THE PHASE 3 RESTRICTION, STATED IN CODE

`MODEL_VERDICT_CAN_ACT = False`. The gate forms a real opinion, records it in
full, and takes no action on it. That is CLAUDE.md's "shadow mode only", and it
is one constant rather than a scattering of `if` statements so that Phase 5 is a
visible, reviewable, one-line change rather than an archaeology exercise.

Shadow mode is worth more than it sounds. `would_have_halted` accumulates in
DynamoDB from today, so by the time enforcement is switched on there is a real
measured over-flagging rate to switch it on *with* -- rather than a guess and an
apology.

WHAT CHANGED ABOUT GATE_DECISION, AND WHY IT IS NOT A REGRESSION

It used to be the verdict. It is now a manual OVERRIDE, and it is normally
unset.

The direction of its default flipped as a result, which is worth understanding
rather than glossing: an unset `GATE_DECISION` used to mean HALT, because a gate
with no way to form an opinion has not approved anything. The gate now has a way
to form an opinion, so "unset" means "no human has intervened; use the model's
verdict" -- and the model's verdict fails closed on its own when Bedrock is
unreachable, the schema is violated, or the signals are missing.

Fail-closed did not weaken. It moved down a layer, to where the judgement
actually happens. What survives here is the kill switch: `GATE_DECISION=halt`
still halts, in enforcing mode, no matter what the model thinks -- which is both
the Phase 1 demo and the thing you want on the day the model is wrong.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from escalation import (
    EscalationStatus,
    SnsEscalator,
    build_body,
    build_sns_client,
    build_subject,
    escalation_topic_arn,
)
from overrides import (
    Override,
    OverrideReader,
    OverrideSource,
)
from overrides import build_dynamodb_client as build_override_client
from signals import (
    DeployCadenceCollector,
    DisabledCollector,
    InspectorFindingsCollector,
    MockChangeContextCollector,
    PipelineChangeContextCollector,
    TargetHealthCloudWatchCollector,
    collect_signals,
)
from signals.types import DeploymentTarget
from verdict import (
    BedrockVerdictClient,
    ModelCall,
    Verdict,
    VerdictAuditWriter,
    VerdictOutcome,
    build_bedrock_client,
    build_dynamodb_client,
    build_record,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Phase 3 restriction --------------------------------------------------
#
# The gate judges and records; it does not act. Phase 5 sets this True, adds the
# executor's risk branching, and only then moves through advisory to enforcing.
#
# The manual override below is deliberately NOT subject to this flag: a human
# typing `halt` is not the model acting, and losing the kill switch during the
# shadow period would be the wrong kind of caution.
MODEL_VERDICT_CAN_ACT = False

ALLOW = "allow"
HALT = "halt"

VALID_OVERRIDES = frozenset({ALLOW, HALT})
VALID_MODES = frozenset({"shadow", "advisory", "enforcing"})

# Modes in which the gate may actually stop a deployment. Shadow and advisory
# both record and report; only enforcing acts. A set so that adding a mode later
# is a data change rather than a new branch in the control flow.
BLOCKING_MODES = frozenset({"enforcing"})

# Modes in which the gate tells a human. This is what finally makes the three
# modes distinct -- until Phase 5.2 `shadow` and `advisory` behaved identically,
# which meant CLAUDE.md's "advisory mode before enforcing mode" was a rollout
# step with nothing in it.
#
#   shadow     record it, say nothing        (measure the over-flagging rate)
#   advisory   record it, TELL SOMEBODY      (find out if the emails are useful
#                                             before they can also block you)
#   enforcing  record it, tell somebody, act
#
# The ordering is the point. Advisory is where you learn whether a halt email is
# signal or noise, at a stage where being wrong costs an unnecessary email
# rather than a blocked release.
NOTIFYING_MODES = frozenset({"advisory", "enforcing"})

GATE_DECISION = os.environ.get("GATE_DECISION", "").strip().lower()
GATE_MODE = os.environ.get("GATE_MODE", "").strip().lower()

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

# The CodeDeploy application and deployment group whose history describes this
# service's deploy cadence. Read-only: the gate holds ListDeployments and
# BatchGetDeployments and deliberately NOT CreateDeployment, which stays with the
# executor (D-016).
CODEDEPLOY_APP = os.environ.get("CODEDEPLOY_APP", "")
CODEDEPLOY_GROUP = os.environ.get("CODEDEPLOY_GROUP", "")

# Env-driven because Phase 4 picks the real model on measured over-flagging rate
# and cost per verdict, not on reputation. Changing it must not be a code change,
# because the eval harness needs to sweep several models over one fixture set.
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

# Absent means the gate still judges and still logs, and records nothing durably.
# A legitimate degraded mode rather than a failure, for the same reason a failed
# write does not halt a judged deploy.
VERDICT_TABLE = os.environ.get("VERDICT_TABLE", "")

# Phase 5.3. Where per-execution human overrides live. Absent means the only
# override path is GATE_DECISION, which is the break-glass lever rather than the
# everyday one -- a working configuration, and the one this repo ships with.
#
# The gate holds GetItem on this table and no write action at all. A gate that
# could write its own override could approve itself.
OVERRIDE_TABLE = os.environ.get("OVERRIDE_TABLE", "")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Where Bedrock is called, which is not necessarily where this function runs.
# Bedrock token quotas are provisioned per region and are zero in most of them
# on this account (F-014), so the gate stays next to its pipeline and reaches
# out to a region that can actually serve a request. DynamoDB and CodePipeline
# stay local; only the model call travels.
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "").strip() or AWS_REGION


def resolve_env_override(raw: str) -> Override | None:
    """Map GATE_DECISION to an override, or to none at all.

    This is the break-glass lever, not the everyday one. It is a Terraform
    variable, so it applies to every execution until somebody changes it back --
    which is exactly what you want for "stop all deploys, something is wrong"
    and exactly what you do not want for "let this one change through". Phase
    5.3 added the per-execution path for the second case and kept this for the
    first.

    An *unrecognised* value still halts. A misspelled override is somebody
    trying to steer the gate and failing, which is when guessing their intent is
    least appropriate.
    """
    if not raw:
        return None
    if raw in (ALLOW, HALT):
        return Override(
            decision=raw,
            reason=f"GATE_DECISION={raw} set at the infrastructure level",
            source=OverrideSource.ENV,
        )
    return Override(
        decision=HALT,
        reason=f"GATE_DECISION={raw!r} is not a recognised override; failing closed",
        source=OverrideSource.ENV,
    )


def resolve_override(raw: str, record: Override | None = None) -> tuple[str | None, str]:
    """Combine the two override sources into one decision.

    Returns (decision, reason). `None` means nobody intervened and the model's
    verdict stands -- the normal case.

    THE PRECEDENCE RULE: THE MOST RESTRICTIVE WINS.

    Not "the more specific wins", which is the instinct and is wrong here. If
    somebody has set `GATE_DECISION=halt` at the infrastructure level they have
    stopped all deploys, and a per-execution row saying `allow` must not be able
    to defeat that -- otherwise the global stop is not a stop, it is a
    suggestion, and the person who set it has no way to know.

    It runs the other way too: a per-execution `halt` beats an environment-level
    `allow`. Whoever is closest to the specific change gets to be more cautious
    than the default, never less.

    So: if either source says halt, halt. That is one rule covering both
    directions, and it is the rule that cannot produce a deploy nobody
    authorised.
    """
    env = resolve_env_override(raw)
    sources = [o for o in (env, record) if o is not None]

    if not sources:
        return None, "no manual override; the model's verdict stands"

    halts = [o for o in sources if o.decision == HALT]
    if halts:
        # Named so the audit record says which lever stopped it. "A human
        # halted this" and "the infrastructure is in a global stop" are
        # different mornings.
        chosen = halts[0]
        if len(sources) > 1:
            return HALT, f"{chosen.describe()} (most restrictive of {len(sources)} overrides)"
        return HALT, chosen.describe()

    return ALLOW, sources[0].describe()


def load_record_override(
    execution_id: str | None,
    *,
    reader: Any = None,
) -> Override | None:
    """Look up a per-execution override. Never raises.

    Absent everything -- no table, no execution ID, no row, an unreadable table
    -- means no override, which means the model's verdict stands, which already
    fails closed on its own. There is nothing here that needs to invent a
    decision.
    """
    if not execution_id:
        return None
    if reader is None:
        if not OVERRIDE_TABLE:
            return None
        reader = OverrideReader(OVERRIDE_TABLE, build_override_client(AWS_REGION))
    try:
        return reader.load(execution_id)
    except Exception:
        logger.exception("override lookup failed; proceeding without one")
        return None


def resolve_mode(raw: str) -> tuple[str, str | None]:
    """Map the configured mode. Anything unrecognised becomes enforcing.

    Note which direction this fails. An unreadable *verdict* becomes a halt; an
    unreadable *mode* becomes `enforcing`. Both pick the outcome that stops a
    deploy, because the failure we are unwilling to have is a bad change reaching
    production because a config value was misspelled.

    The consequence is a real cost and belongs on a slide: a typo in GATE_MODE
    turns a shadow rollout into an enforcing one. That is the correct trade -- an
    over-eager gate is visible within minutes, a silently disabled one is
    discovered by the incident it failed to prevent -- but it is a trade.
    """
    if raw in VALID_MODES:
        return raw, None
    if not raw:
        return "enforcing", "GATE_MODE is not set; assuming enforcing"
    return "enforcing", f"GATE_MODE={raw!r} is not a recognised mode; assuming enforcing"


def resolve_action(*, decision: str, mode: str, from_override: bool) -> str:
    """Decide whether the gate actually stops the pipeline.

    Three independent questions, deliberately not collapsed into one boolean:

        does the gate want to halt?      -> decision
        is the gate allowed to act?      -> mode
        is this the model or a human?    -> from_override

    The third is what keeps Phase 3 honest. A model halt is recorded and not
    acted on; a human halt still works. Written as early returns because the
    equivalent boolean expression is four terms long and nobody reviewing it
    would be certain which case they were looking at.
    """
    if decision != HALT:
        return "none"
    if mode not in BLOCKING_MODES:
        return "none"
    if from_override:
        return "halt_pipeline"
    if not MODEL_VERDICT_CAN_ACT:
        # Phase 3: the gate has an opinion and no authority. Removing this line
        # is what Phase 5 does.
        return "none"
    return "halt_pipeline"


def build_gate_record(
    *,
    outcome: VerdictOutcome,
    request_id: str,
    now: datetime | None = None,
    record_override: Override | None = None,
) -> dict[str, Any]:
    """Assemble the structured line that says what the gate decided and did."""
    verdict = outcome.verdict
    override, override_reason = resolve_override(GATE_DECISION, record_override)
    mode, mode_warning = resolve_mode(GATE_MODE)

    model_decision = HALT if verdict.is_blocking else ALLOW
    decision = override if override is not None else model_decision

    action_taken = resolve_action(decision=decision, mode=mode, from_override=override is not None)

    record: dict[str, Any] = {
        "schema_version": 1,
        "decision": decision,
        "decision_reason": override_reason if override else verdict.reasoning,
        "mode": mode,
        "action_taken": action_taken,
        # The field that makes shadow mode worth running. In shadow,
        # `action_taken` is always "none" and carries no information; this is
        # what you count to measure how often the gate would have blocked a
        # deploy that in fact went out fine. That ratio is the over-flagging
        # rate Phase 4 measures and Phase 8 measures at scale.
        "would_have_halted": model_decision == HALT,
        "model_verdict_can_act": MODEL_VERDICT_CAN_ACT,
        "risk_level": str(verdict.risk_level),
        "recommended_action": str(verdict.action),
        "verdict_source": str(verdict.source),
        "confidence": verdict.confidence,
        "primary_concerns": list(verdict.primary_concerns),
        "model_call": outcome.call.to_dict(),
        "request_id": request_id,
        "timestamp": (now or datetime.now(UTC)).isoformat(),
    }
    if override is not None:
        # Recorded even when it agrees with the model. "A human forced allow and
        # the model also said allow" and "the model said allow" are different
        # events, and only one of them means the gate was trusted.
        record["override"] = {"decision": override, "reason": override_reason}
        if record_override is not None:
            # Who, and from where. The old GATE_DECISION path could say a human
            # overrode the gate and could never say which human.
            record["override"]["actor"] = record_override.actor
            record["override"]["source"] = record_override.source
            record["override"]["created_at"] = record_override.created_at
        record["model_decision"] = model_decision
    if mode_warning:
        record["mode_warning"] = mode_warning

    return record


def report_to_codepipeline(job: dict[str, Any], gate: dict[str, Any]) -> None:
    """Report the result back to CodePipeline, if we were invoked by one.

    The contract is the reason this function exists: CodePipeline does not read
    the response payload of a Lambda invoke action -- it waits for an
    out-of-band PutJobSuccessResult or PutJobFailureResult call. A gate that
    returns a beautifully structured "halt" verdict and never calls
    PutJobFailureResult reports success, and the deploy proceeds.

    That failure mode is silent, which is the only kind worth a comment: the logs
    show a halt, the audit record shows a halt, and the change ships anyway.
    """
    import boto3

    client = boto3.client("codepipeline")
    job_id = job["id"]

    if gate["action_taken"] == "halt_pipeline":
        # 265 characters is the documented ceiling on failureDetails.message.
        # Truncating deliberately beats having the API reject the call and
        # leaving the job hanging until it times out.
        message = f"Gate halted deploy: {gate['decision_reason']}"[:265]
        client.put_job_failure_result(
            jobId=job_id,
            failureDetails={"type": "JobFailed", "message": message},
        )
        logger.info("Reported job failure to CodePipeline: %s", job_id)
    else:
        client.put_job_success_result(jobId=job_id)
        logger.info("Reported job success to CodePipeline: %s", job_id)


def collect_bundle(
    event: dict[str, Any],
    security_collector: Any = None,
    health_collector: Any = None,
) -> Any:
    """Collect the signal bundle that the verdict will be based on.

    From Phase 2.4 all three collectors are real. Note what none of them are:
    mocks. A mock in this path would put fabricated health or security data into
    the audit trail of a real deployment, which is precisely the "absent signal
    read as a reassuring one" failure the package exists to prevent. Where a real
    collector cannot answer it says so -- UNAVAILABLE or DEGRADED with a reason,
    never a plausible zero.
    """
    change_collector: Any
    if event.get("CodePipeline.job"):
        # Cadence is an enrichment of change context rather than a signal of its
        # own, so it is injected into the change collector rather than occupying
        # a fourth slot in the bundle. If it fails, one field is absent and the
        # change context is still usable.
        cadence = None
        if CODEDEPLOY_APP and CODEDEPLOY_GROUP:
            cadence = DeployCadenceCollector(CODEDEPLOY_APP, CODEDEPLOY_GROUP)
        change_collector = PipelineChangeContextCollector(event, cadence_collector=cadence)
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


def judge(bundle: Any, verdict_client: Any = None) -> VerdictOutcome:
    """Ask Bedrock for a verdict. Never raises.

    The client is constructed here rather than at module scope so that importing
    this handler does not require boto3 or a region, and so tests can inject a
    fake without patching a global. The wrapping try/except is belt and braces:
    `BedrockVerdictClient.get_verdict` is written never to raise, and this
    catches the case where constructing the client itself fails -- a missing
    region, a bad model ID, boto3 unavailable in the runtime.
    """
    if verdict_client is None:
        verdict_client = BedrockVerdictClient(
            BEDROCK_MODEL_ID, build_bedrock_client(BEDROCK_REGION)
        )
    return verdict_client.get_verdict(bundle)


def write_audit_record(
    *,
    bundle: Any,
    outcome: VerdictOutcome,
    gate: dict[str, Any],
    verdict_id: str,
    pipeline_execution_id: str | None,
    writer: Any = None,
) -> str:
    """Persist the verdict. Returns a status string; never raises.

    A storage failure degrades the audit trail rather than halting a deploy the
    gate has already judged -- the same trade as D-030, for the same reason: the
    verdict is already made, and DynamoDB being unavailable is not evidence about
    the change. The verdict also reaches CloudWatch Logs on every path, so the
    record is degraded, not lost.
    """
    if not VERDICT_TABLE:
        return "no_table_configured"

    if writer is None:
        writer = VerdictAuditWriter(VERDICT_TABLE, build_dynamodb_client(AWS_REGION))

    item = build_record(
        verdict_id=verdict_id,
        bundle=bundle,
        verdict=outcome.verdict,
        call=outcome.call,
        mode=gate["mode"],
        action_taken=gate["action_taken"],
        raw_model_output=outcome.raw_model_output,
        pipeline_execution_id=pipeline_execution_id,
        override=gate.get("override"),
    )
    return writer.record(item).status


def should_escalate(*, decision: str, mode: str) -> bool:
    """Does this warrant waking somebody up?

    One condition, no special cases: the gate wanted to halt, and the mode is
    one that notifies. Everything worth escalating already collapses into
    `decision == HALT`:

      * the model returned high risk
      * the model could not be reached, so the verdict failed closed to high
      * signals were missing, so the gate refused to ask
      * a human forced a halt

    All four are "a deploy is not going out and somebody should know why", and
    writing them as one condition rather than four means a fifth way of halting
    -- whatever Phase 7 invents -- notifies without anybody remembering to add it
    here.

    Deliberately NOT escalated: medium risk. A canary is the system working as
    designed, and an email for every canary is how a person learns to filter
    this sender.
    """
    return decision == HALT and mode in NOTIFYING_MODES


def escalate(
    *,
    gate: dict[str, Any],
    bundle: Any,
    escalator: Any = None,
) -> str:
    """Notify a human. Returns a status; never raises.

    Fails OPEN, which is the opposite of every other decision in this file and
    is correct here. Everything in the verdict path fails closed because an
    absent signal might be hiding a problem with the change. A failed
    notification hides nothing about the change -- the deploy has already been
    judged and the pipeline has already been told. Halting on it would let an
    SNS outage take a release.
    """
    if not should_escalate(decision=gate["decision"], mode=gate["mode"]):
        return EscalationStatus.SKIPPED

    topic = escalation_topic_arn()
    if escalator is None:
        if not topic:
            # A legitimate degraded mode, same as an unset VERDICT_TABLE: the
            # gate still judges, still records, still halts. Named distinctly
            # from FAILED so "nobody configured a topic" cannot be mistaken in a
            # log for "the email did not arrive".
            logger.warning("gate wanted to escalate but ESCALATION_TOPIC_ARN is unset")
            return EscalationStatus.NOT_CONFIGURED
        escalator = SnsEscalator(topic, build_sns_client(AWS_REGION))

    subject = build_subject(gate, SERVICE_NAME)
    body = build_body(gate, bundle, SERVICE_NAME)
    return escalator.publish(subject, body).status


def _pipeline_execution_id(job: dict[str, Any] | None) -> str | None:
    """Best-effort extraction. Absent is fine; wrong would not be."""
    if not job:
        return None
    context = job.get("data", {}).get("pipelineContext", {})
    execution = context.get("pipelineExecutionId")
    return execution if isinstance(execution, str) and execution else None


def lambda_handler(
    event: dict[str, Any],
    context: Any,
    *,
    verdict_client: Any = None,
    audit_writer: Any = None,
    escalator: Any = None,
    override_reader: Any = None,
) -> dict[str, Any]:
    """Judge a deploy. Records everything; in Phase 3, acts on nothing.

    The two keyword-only collaborators are never supplied by Lambda, which calls
    this with exactly two positional arguments. They exist so the whole path --
    collect, judge, record, report -- can be exercised offline with fakes,
    without monkeypatching module globals and without an `if testing:` branch
    inside the code being tested. Same discipline as the signal collectors.
    """
    request_id = getattr(context, "aws_request_id", "unknown")
    job = event.get("CodePipeline.job")
    # One verdict per pipeline job, which is the granularity a decision is
    # actually made at, and what makes the conditional write meaningful when
    # CodePipeline invokes the same job twice.
    verdict_id = job.get("id") if job else request_id

    bundle = None
    outcome: VerdictOutcome | None = None

    try:
        bundle = collect_bundle(event)
        logger.info(json.dumps({"signal_bundle": bundle.to_dict()}))
        logger.info(
            "signals: %s | required present: %s",
            bundle.completeness_summary,
            bundle.has_required_signals,
        )
        outcome = judge(bundle, verdict_client=verdict_client)
    except Exception as exc:  # noqa: BLE001 - this is the outermost fail-closed boundary
        # Everything below this line is written never to raise. If something did
        # anyway, the gate knows nothing about the change, and knowing nothing is
        # grounds to stop. There is deliberately no recovery attempt.
        logger.exception("signal collection or judgement failed; failing closed")
        outcome = VerdictOutcome(
            verdict=Verdict.fail_closed(
                f"gate internal error before a verdict could be formed: {exc}"
            ),
            call=ModelCall(
                model_id=BEDROCK_MODEL_ID,
                prompt_version="unknown",
                attempts=0,
                succeeded=False,
                failure_kind="gate_internal_error",
                error=str(exc),
            ),
        )

    # Looked up AFTER the verdict, deliberately. The model is asked either way,
    # even when a human has already decided, because "the human overrode a
    # verdict the model got right" and "the human overrode one it got wrong" are
    # the two most useful rows in the whole table -- and you only have them if
    # the model was asked. One inference is a rounding error against knowing
    # whether your override rate is justified.
    execution_id = _pipeline_execution_id(job)
    record_override = load_record_override(execution_id, reader=override_reader)

    gate = build_gate_record(
        outcome=outcome,
        request_id=request_id,
        record_override=record_override,
    )
    gate["verdict_id"] = verdict_id
    # Carried so the escalation email can print the exact override command for
    # this deploy rather than a runbook reference.
    gate["pipeline_execution_id"] = execution_id

    # Written before the pipeline is told anything. If the audit write and the
    # pipeline report disagree about ordering, the record of a halt should exist
    # before the halt does -- not after.
    if bundle is not None:
        try:
            gate["audit"] = write_audit_record(
                bundle=bundle,
                outcome=outcome,
                gate=gate,
                verdict_id=verdict_id,
                pipeline_execution_id=execution_id,
                writer=audit_writer,
            )
        except Exception:
            # `write_audit_record` does not raise, so reaching here means the
            # record could not even be BUILT. Still not grounds to halt a judged
            # deploy; the log line below remains the audit trail.
            logger.exception("could not build or write the audit record")
            gate["audit"] = "failed"
    else:
        gate["audit"] = "no_bundle_to_record"

    # After the audit write, before the pipeline is told. Ordering is deliberate
    # on both sides: the durable record should exist before anyone is emailed a
    # link to it, and the human should be on their way before the pipeline goes
    # red -- so the email is not competing with a CI notification to explain why.
    try:
        gate["escalation"] = escalate(gate=gate, bundle=bundle, escalator=escalator)
    except Exception:
        # `escalate` does not raise, so reaching here means the message could not
        # be BUILT -- a field of an unexpected shape, most likely. Still not
        # grounds to change a decision already made and recorded.
        logger.exception("could not build or send the escalation")
        gate["escalation"] = EscalationStatus.FAILED

    # One structured line per evaluation. Always emitted, on every path, which is
    # what makes a DynamoDB failure a degradation rather than a loss.
    logger.info(json.dumps(gate))

    if job:
        report_to_codepipeline(job, gate)

    return gate
