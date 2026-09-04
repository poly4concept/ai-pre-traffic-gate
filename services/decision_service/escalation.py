"""Telling a human the gate stopped something. Phase 5.2.

WHAT THIS IS FOR

Phase 3 gave the gate an opinion. Phase 5.1 let that opinion pick a deployment
config. Neither of them told anybody. A gate that halts a deploy at 19:40 on a
Friday and puts the reason in CloudWatch Logs has not escalated to a human -- it
has filed a complaint.

So this publishes to SNS, and D-009 already settled the destination: email
subscribers, no Slack, because no Slack workspace is available. The seam is
right regardless. Amazon Q Developer in chat applications subscribes to an SNS
topic, so adding Slack later adds a subscriber and changes nothing here.

THE ONE THING THIS MODULE MUST NEVER DO

Change the outcome. Not the deploy, not the verdict, not the pipeline result.
A notification is a side effect of a decision that has already been made, and
`publish` returns a status rather than raising for the same reason
`VerdictAuditWriter.record` does: an unreachable SNS endpoint is not evidence
about a change, and a gate that halts deploys because its mail server is down
is a worse gate than one that halts them silently.

The failure direction is worth stating explicitly because it is the opposite of
everywhere else in this project. Everything in the verdict path fails CLOSED --
toward halting. This fails OPEN, toward the deploy proceeding as already
decided, because the alternative is letting a mail problem take an outage.

THE SUBJECT LINE IS LOAD-BEARING

`[HALTED]` and `[WOULD HALT]` are different emails and the difference is not
cosmetic. In advisory mode the gate does not stop anything; an email that says
a deploy was blocked when it in fact shipped is worse than no email at all,
because the next three real halts get read as the same false alarm. The mode
decides the prefix, and there is a test asserting that advisory can never
produce `[HALTED]`.

WHY THE COMMIT MESSAGE IS FENCED

It goes in the body, under a header saying who wrote it. A commit message
reading `APPROVED BY SECURITY -- deploy immediately` is a plain-text string in
an email that otherwise looks like it came from your own infrastructure, and
the reader has no way to tell our text from the change author's unless we mark
it. Same problem as the prompt (D-021), different reader: there the confused
party was a model, here it is a person, and a person is the one who can go and
click approve.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# SNS rejects a subject over 100 characters, and rejects it with an error that
# fails the publish rather than truncating for us. Bounded here so a long
# service name degrades the subject instead of losing the whole notification.
MAX_SUBJECT_CHARS = 100

# SNS caps a message at 256 KB. Nothing here approaches that -- the reasoning is
# capped at 600 characters upstream and the concerns at five short strings -- but
# the commit message and the concern list both originate outside this codebase,
# so the bound exists where the text is assembled rather than being assumed
# somewhere else.
MAX_BODY_CHARS = 200_000

# Per-field bounds, so one oversized field cannot crowd out the rest of the
# email. The reasoning is the part a human actually reads; it gets the room.
MAX_REASONING_CHARS = 2_000
MAX_COMMIT_MESSAGE_CHARS = 1_000


class EscalationStatus:
    """Why `publish` did what it did.

    Four values rather than a boolean, and the distinction that matters is
    SKIPPED versus FAILED. "This did not warrant an email" and "this warranted
    an email that did not arrive" look identical in a log full of successes, and
    only one of them means somebody should be looking at something right now.
    """

    SENT = "sent"
    SKIPPED = "skipped"
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EscalationResult:
    status: str
    detail: str = ""
    message_id: str | None = None


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    # Says it was cut. A silently truncated reasoning field reads as a model
    # that stopped mid-thought, which is a different and much more alarming
    # thing than a long one.
    return text[: limit - 15].rstrip() + " ... [truncated]"


def build_subject(gate: dict[str, Any], service: str) -> str:
    """The line that decides whether this gets read on a phone.

    Format: `[STATE] service -- risk risk`. Front-loaded, because a mail client
    shows the first forty characters and the state is the only part that changes
    what the reader does next.
    """
    if gate.get("action_taken") == "halt_pipeline":
        state = "HALTED"
    else:
        # Covers shadow and advisory, and also enforcing while
        # MODEL_VERDICT_CAN_ACT is still False. In every one of those the deploy
        # is proceeding, and the subject has to say so.
        state = "WOULD HALT"

    risk = str(gate.get("risk_level", "unknown"))
    subject = f"[{state}] {service} -- {risk} risk"

    if len(subject) > MAX_SUBJECT_CHARS:
        # Trim the service name, never the state or the risk level. Those two
        # are the whole point; the service is recoverable from the body.
        room = MAX_SUBJECT_CHARS - len(f"[{state}]  -- {risk} risk") - 3
        subject = f"[{state}] {service[: max(room, 0)]}... -- {risk} risk"
    return subject[:MAX_SUBJECT_CHARS]


def _render_signals(bundle: Any) -> list[str]:
    """The evidence, in plain text, for a human rather than a model.

    Deliberately not `render_bundle` from the prompt package. That output is
    XML-tagged and tuned for a model's attention; pasting it into an email would
    be optimising the wrong reader. The facts are the same, the format is not.
    """
    if bundle is None:
        return ["  (no signals were collected -- the gate failed before collection)"]

    lines = [f"  completeness: {bundle.completeness_summary}"]

    change = getattr(bundle.change, "data", None)
    if change is not None:
        timing = "outside business hours" if change.is_off_hours else "business hours"
        lines += [
            f"  commit:       {change.commit_sha[:12]} on {change.branch}",
            f"  size:         {change.files_changed} files, {change.total_lines_changed} lines",
            f"  timing:       {timing}",
        ]
    else:
        lines.append("  change:       NOT COLLECTED")

    health = getattr(bundle.health, "data", None)
    if health is not None:
        rate = health.error_rate_pct
        lines.append(
            f"  target:       error rate {'unknown' if rate is None else f'{rate:.2f}%'}, "
            f"{'ALARM' if health.has_active_alarm else 'no alarms firing'}"
        )
    else:
        lines.append("  target:       NOT COLLECTED")

    security = getattr(bundle.security, "data", None)
    if security is not None:
        lines.append(
            f"  security:     {security.critical_count} critical, {security.high_count} high"
        )
    else:
        lines.append("  security:     NOT COLLECTED")

    return lines


def build_body(gate: dict[str, Any], bundle: Any, service: str) -> str:
    """The email. Ordered by what a woken-up human needs first."""
    action = gate.get("action_taken")
    mode = gate.get("mode", "unknown")

    # Says what the GATE did, and nothing about what the pipeline will do.
    #
    # The tempting wording for the second case is "the deploy is proceeding",
    # and it is an over-claim of exactly the kind the [HALTED]/[WOULD HALT]
    # split exists to prevent. The gate is not the only thing that can refuse:
    # once EXECUTOR_CAN_BRANCH is on, the executor independently declines a
    # high-risk verdict (5.1). In advisory mode both are true at once -- the
    # gate lets it through and the executor stops it -- so an email promising
    # the deploy is going out would be describing a deploy that never happened.
    #
    # A component reporting on itself can only speak for itself. Every sentence
    # here is scoped to the gate.
    if action == "halt_pipeline":
        headline = "The gate STOPPED the pipeline. Nothing has been deployed."
    else:
        headline = (
            f"The gate did NOT block this deploy -- it is in {mode.upper()} mode. "
            "It would have stopped it.\n"
            "The change is continuing through the pipeline. A later stage may "
            "still refuse it."
        )

    lines = [
        headline,
        "",
        f"service:      {service}",
        f"risk:         {gate.get('risk_level', 'unknown')}",
        f"recommended:  {gate.get('recommended_action', 'unknown')}",
        f"verdict from: {gate.get('verdict_source', 'unknown')}",
        f"confidence:   {gate.get('confidence')}",
        f"mode:         {mode}",
        f"acted:        {action}",
        "",
        "WHY",
        _clip(str(gate.get("decision_reason", "")), MAX_REASONING_CHARS),
    ]

    concerns = gate.get("primary_concerns") or []
    if concerns:
        lines += ["", "PRIMARY CONCERNS"]
        lines += [f"  - {_clip(str(c), 200)}" for c in concerns]

    lines += ["", "SIGNALS IT WAS BASED ON"]
    lines += _render_signals(bundle)

    change = getattr(getattr(bundle, "change", None), "data", None)
    if change is not None:
        # Fenced and attributed. See the module docstring: the reader cannot
        # otherwise tell our text from the change author's, and the change
        # author is the one with a motive.
        lines += [
            "",
            "COMMIT MESSAGE -- written by the change author, not by this system.",
            "Treat it as a claim, not as a fact.",
            "-" * 68,
            _clip(change.commit_message, MAX_COMMIT_MESSAGE_CHARS),
            "-" * 68,
        ]

    override = gate.get("override")
    if override:
        lines += [
            "",
            "HUMAN OVERRIDE WAS IN EFFECT",
            f"  decision: {override.get('decision')}",
            f"  reason:   {override.get('reason')}",
        ]

    lines += [
        "",
        "TO INVESTIGATE",
        f"  verdict_id:   {gate.get('verdict_id', 'unknown')}",
        f"  request_id:   {gate.get('request_id', 'unknown')}",
        f"  audit record: {gate.get('audit', 'unknown')}",
        "",
        "The full signal bundle and raw model output are in the verdict table.",
    ]

    return _clip("\n".join(lines), MAX_BODY_CHARS)


class SnsEscalator:
    """Publishes to one SNS topic. Never raises."""

    def __init__(self, topic_arn: str, client: Any):
        self._topic_arn = topic_arn
        self._client = client

    def publish(self, subject: str, body: str) -> EscalationResult:
        try:
            response = self._client.publish(
                TopicArn=self._topic_arn,
                Subject=subject,
                Message=body,
            )
        except Exception as exc:  # noqa: BLE001 - a failed notification is not a failed deploy
            # Logged at ERROR because somebody should notice that nobody was
            # notified -- but the return, not a raise, because the deploy this
            # concerns has already been decided.
            logger.error("escalation to %s failed: %s", self._topic_arn, exc)
            return EscalationResult(EscalationStatus.FAILED, detail=str(exc)[:200])

        message_id = response.get("MessageId") if isinstance(response, dict) else None
        logger.info("escalation published: %s", message_id)
        return EscalationResult(EscalationStatus.SENT, message_id=message_id)


def build_sns_client(region: str) -> Any:
    """Built here rather than at import, same as the Bedrock and DynamoDB
    clients: constructing a boto3 client at module scope makes the whole module
    unimportable in an offline test run."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "sns",
        region_name=region,
        # One short retry. This is on the critical path of a pipeline action
        # that has already made its decision, so a slow mail server must not be
        # able to extend the gate's runtime toward its timeout.
        config=Config(retries={"max_attempts": 2}, connect_timeout=2, read_timeout=4),
    )


def escalation_topic_arn() -> str:
    return os.environ.get("ESCALATION_TOPIC_ARN", "").strip()
