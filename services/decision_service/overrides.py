"""The human override path. Phase 5.3.

WHAT WAS WRONG WITH THE OLD ONE

`GATE_DECISION` has existed since Phase 1 and it works, in the sense that
setting it to `halt` halts. It is not an override path, though. It is a
Terraform variable, which means overriding one deploy requires:

    terraform -chdir=infra/personal apply -var="gate_decision=allow"

and that has four problems, in ascending order of how much they matter:

  1. It needs admin credentials and Terraform on the machine of whoever is
     trying to ship a fix at 20:00.
  2. It is slow -- an apply, then a new pipeline run.
  3. It is anonymous. The audit record says a human overrode the gate and
     cannot say which human.
  4. IT IS NOT SCOPED TO ONE DEPLOY. `gate_decision=allow` applies to every
     execution from then until somebody remembers to change it back. Somebody
     bypasses the gate for one urgent fix on Friday and the gate is off until
     Tuesday, silently, while continuing to write verdicts that look normal.

Number 4 is the one that turns a safety feature into a liability, and it is the
whole reason this module exists. An override should be a statement about ONE
CHANGE, made by ONE PERSON, that stops applying by itself.

HOW THIS ONE WORKS

A row in a DynamoDB table keyed by pipeline execution ID:

    human runs scripts/override.py    -> writes the row
    human retries the Gate stage      -> CodePipeline re-invokes the gate
    gate reads the row                -> honours it, records who wrote it

Scoped to one execution because the key is the execution ID, and a pipeline
execution ID is unique and never reused. There is no way to write this row such
that it affects the next deploy.

THE GATE CANNOT WRITE THESE

Read-only, enforced in IAM rather than by convention: the gate holds
`dynamodb:GetItem` on this table and no write action at all. A gate that could
write its own override could approve itself, which would make the entire
verdict path decorative. This is the same split as the executor holding deploy
permissions the gate does not -- the security claim of the whole project is that
no single component can both decide and act.

TTL IS HOUSEKEEPING, NOT EXPIRY

The table has a DynamoDB TTL attribute, and expiry is ALSO checked here in
Python, and the second one is the one that matters. DynamoDB deletes expired
items lazily -- the documented window is up to 48 hours after the timestamp
passes. Relying on TTL to stop honouring an override would leave a two-day hole
in which an expired override is still a live row. TTL keeps the table tidy; the
`expires_at` check keeps it correct.

Worth stating plainly because it is a very easy mistake to make: a lazy
deletion mechanism is not an access control.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

ALLOW = "allow"
HALT = "halt"
VALID_DECISIONS = frozenset({ALLOW, HALT})

# How long an override stays usable, unless the writer says otherwise. An
# override is a statement about a deploy happening NOW; if the pipeline has not
# been retried within an hour, whatever prompted it has moved on.
DEFAULT_TTL_MINUTES = 60

# Bounds on text read back out of the table. The reason reaches the audit record
# and the escalation email, both of which are read by people.
MAX_REASON_CHARS = 500
MAX_ACTOR_CHARS = 200


class OverrideSource:
    ENV = "env"
    RECORD = "record"


@dataclass(frozen=True, slots=True)
class Override:
    """A human decision that supersedes the model's, for one execution."""

    decision: str
    reason: str
    source: str
    actor: str = ""
    created_at: str = ""

    def describe(self) -> str:
        who = f" by {self.actor}" if self.actor else ""
        return f"manually overridden to {self.decision}{who}: {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "source": self.source,
            "actor": self.actor,
            "created_at": self.created_at,
        }


def _s(item: dict[str, Any], key: str) -> str:
    """Read a DynamoDB string attribute, tolerating its absence."""
    value = item.get(key)
    if not isinstance(value, dict):
        return ""
    got = value.get("S", "")
    return got if isinstance(got, str) else ""


def _n(item: dict[str, Any], key: str) -> float | None:
    value = item.get(key)
    if not isinstance(value, dict):
        return None
    try:
        return float(value.get("N"))
    except (TypeError, ValueError):
        return None


def parse_override(item: dict[str, Any], *, now: datetime | None = None) -> Override | None:
    """Turn a raw DynamoDB item into an Override, or refuse to.

    Returns None when there is nothing usable here and the model's verdict
    should stand. Returns a HALT override when the row exists but cannot be
    trusted -- and the asymmetry between those two is deliberate:

      expired            -> None. Somebody steered the gate an hour ago and this
                            is a different moment. Not an attempt to steer it
                            now, so nothing to honour and nothing to fear.

      malformed          -> HALT. A row exists, so somebody meant something by
                            it, and we cannot tell whether they meant allow or
                            halt. Ignoring it risks shipping a change a human
                            tried to stop; halting risks blocking one they tried
                            to release. Only one of those is recoverable by
                            trying again.
    """
    if not item:
        return None

    decision = _s(item, "decision").strip().lower()
    reason = _s(item, "reason").strip()[:MAX_REASON_CHARS]
    actor = _s(item, "actor_arn").strip()[:MAX_ACTOR_CHARS]
    created_at = _s(item, "created_at")

    expires_at = _n(item, "expires_at")
    if expires_at is not None:
        current = (now or datetime.now(UTC)).timestamp()
        if current >= expires_at:
            # Loud, because the person who wrote it is probably waiting for it
            # to work and will otherwise conclude the feature is broken.
            logger.warning(
                "override for this execution expired at %s; ignoring it and "
                "using the model's verdict",
                datetime.fromtimestamp(expires_at, UTC).isoformat(),
            )
            return None

    if decision not in VALID_DECISIONS:
        return Override(
            decision=HALT,
            reason=(
                f"override record has decision {decision!r}, which is not "
                "'allow' or 'halt'; failing closed rather than guessing"
            ),
            source=OverrideSource.RECORD,
            actor=actor,
            created_at=created_at,
        )

    if not reason:
        # A reason is mandatory because this row is the only explanation the
        # audit trail will ever have for why the gate was bypassed.
        return Override(
            decision=HALT,
            reason="override record carries no reason; failing closed",
            source=OverrideSource.RECORD,
            actor=actor,
            created_at=created_at,
        )

    return Override(
        decision=decision,
        reason=reason,
        source=OverrideSource.RECORD,
        actor=actor,
        created_at=created_at,
    )


class OverrideReader:
    """Reads one override row. Never raises."""

    def __init__(self, table: str, client: Any):
        self._table = table
        self._client = client

    def load(self, execution_id: str, *, now: datetime | None = None) -> Override | None:
        if not execution_id:
            return None
        try:
            response = self._client.get_item(
                TableName=self._table,
                Key={"pipeline_execution_id": {"S": execution_id}},
                # An override is read moments after being written by a human who
                # is watching. Eventually-consistent is the default and would
                # occasionally miss a row written seconds earlier, which reads
                # to that person as "the override did not work".
                ConsistentRead=True,
            )
        except Exception as exc:  # noqa: BLE001
            # An unreadable override table means no override, which means the
            # model's verdict stands -- and that path already fails closed on
            # its own. Nothing here needs to invent a decision.
            logger.error("could not read the override table: %s", exc)
            return None

        return parse_override(response.get("Item") or {}, now=now)


def build_dynamodb_client(region: str) -> Any:
    import boto3

    return boto3.client("dynamodb", region_name=region)
