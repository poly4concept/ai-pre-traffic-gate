"""The immutable audit record. Phase 3.4.

CLAUDE.md constraint 3: every verdict is auditable and overridable, and the audit
trail is a demo asset rather than mere hygiene. This is that record.

WHAT GOES IN, AND THE ONE THING THAT DELIBERATELY DOES NOT

Stored: the full input signals, the rendered prompt, the raw model output before
validation, the validated verdict, the action taken, the mode, and the
operational metadata of the call.

NOT stored: the system prompt. Only `prompt_version` goes in. Copying two and a
half kilobytes of unchanging instructions into every record would double the item
size to preserve information that git already holds perfectly, and a version
pointer answers the same question -- "what was it asked?" -- without the
duplication. The rendered USER message IS stored, because that part is different
every time and cannot be reconstructed from the signals alone once the renderer
changes.

WHY THE RAW MODEL OUTPUT IS KEPT SEPARATELY FROM THE VALIDATED VERDICT

Because they disagree sometimes, and the disagreements are the interesting part.
A record holding only the validated verdict cannot tell you that the model said
`"critical"` and validation rejected it, or that reasoning was truncated. Those
are exactly the events Phase 4 needs to count, and they are invisible if the only
thing kept is the answer that survived.

This is also the honest version of a demo. Showing an audience a clean verdict
record proves nothing; showing them a record where the model got it wrong and the
validator caught it proves the design.

WHY WRITES ARE CONDITIONAL AND THE GATE CANNOT DELETE

The table's IAM policy grants `PutItem` and nothing else -- no `UpdateItem`, no
`DeleteItem`. On top of that, the write is conditional on the item not already
existing. Immutability here is enforced in two independent places, because a
verdict record that can be quietly rewritten after the fact is not evidence, it
is a note.

The condition also makes retries safe: if the Lambda is invoked twice for one
pipeline job -- which CodePipeline can do -- the second write fails harmlessly
instead of overwriting the first verdict with a possibly different one.

WHY A FAILED WRITE DOES NOT FAIL THE DEPLOY

`record()` returns a status rather than raising, and the handler logs it. That
looks like a violation of fail-closed and is not, for the same reason D-030 gave
for deploy cadence: the verdict has ALREADY been decided by this point. Failing
the pipeline because DynamoDB was briefly unavailable would halt a deploy the
gate had just judged safe, on the basis of a storage problem that says nothing
about the change. The verdict also always reaches CloudWatch Logs, so a failed
write degrades the audit trail rather than losing the record.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from signals import SignalBundle

from .bedrock import ModelCall
from .prompt import render_bundle
from .types import Verdict

logger = logging.getLogger(__name__)

# DynamoDB's hard item limit is 400 KB. A record is normally around 5 KB, so this
# is not a routine concern -- but `raw_model_output` and the rendered prompt both
# contain attacker-influenced text, which makes item size an input an attacker
# has some influence over. Bounded so a hostile commit cannot make the write fail
# and thereby remove itself from the audit trail.
MAX_FIELD_CHARS = 20_000


class AuditWriteStatus:
    """Why the flat strings rather than an enum: these land in a log line and a
    metric dimension, and both want a stable literal."""

    WRITTEN = "written"
    DUPLICATE = "duplicate"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AuditWriteResult:
    status: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        # A duplicate is a success: it means the verdict for this job is already
        # recorded, which is the state we wanted.
        return self.status in (AuditWriteStatus.WRITTEN, AuditWriteStatus.DUPLICATE)


def build_record(
    *,
    verdict_id: str,
    bundle: SignalBundle,
    verdict: Verdict,
    call: ModelCall,
    mode: str,
    action_taken: str,
    raw_model_output: Any = None,
    pipeline_execution_id: str | None = None,
    override: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the audit item.

    A plain dict of JSON-safe values, built separately from the write so the
    shape can be asserted in tests without a DynamoDB client anywhere near it.

    `now` is injectable for the same reason it is on `collect_signals`: a record
    that stamps itself with `datetime.now()` cannot be compared byte for byte
    across a replay.
    """
    change = bundle.change.data
    stamped = (now or datetime.now(UTC)).isoformat()

    return {
        # Partition key. One item per pipeline job, which is the granularity a
        # verdict is actually made at.
        "verdict_id": verdict_id,
        # Sort key on the GSI: lets "every verdict for this service, newest
        # first" be a query rather than a scan. A scan would work fine at demo
        # volume and be the wrong thing to teach.
        "service_name": bundle.target.service_name,
        "recorded_at": stamped,
        "pipeline_execution_id": pipeline_execution_id or "",
        "commit_sha": change.commit_sha if change else "",
        # --- the decision ------------------------------------------------
        "mode": mode,
        "action_taken": action_taken,
        "verdict": verdict.to_dict(),
        # CLAUDE.md constraint 3: "every verdict is auditable and OVERRIDABLE",
        # and an override nobody recorded is indistinguishable from the gate
        # having decided that way on its own. Stored even when it agrees with the
        # model, because "a human forced allow and the model also said allow" and
        # "the model said allow" are different events -- only one of them means
        # the gate was actually trusted. Empty map when nobody intervened, rather
        # than absent, so the field is safe to query on.
        "override": override or {},
        # --- how it was reached ------------------------------------------
        "model_call": call.to_dict(),
        # Before validation, exactly as the model produced it. See the module
        # docstring: the disagreements between this and `verdict` are the point.
        "raw_model_output": _clip(_jsonify(raw_model_output)),
        # --- what it was based on ----------------------------------------
        "signals": bundle.to_dict(),
        # The rendered user message. Stored because it changes every run and
        # cannot be reconstructed from `signals` once the renderer changes --
        # unlike the system prompt, which `prompt_version` locates in git.
        "prompt": _clip(render_bundle(bundle)),
    }


class VerdictAuditWriter:
    """Writes verdict records to DynamoDB. Never raises.

    Takes a boto3 DynamoDB *client* rather than a Table resource. The client is
    more verbose -- values must be type-annotated -- and it is what lets
    `ConditionExpression` failures be distinguished from real errors by error
    code, which the resource interface makes fiddlier.
    """

    def __init__(self, table_name: str, client: Any):
        self._table_name = table_name
        self._client = client

    def record(self, item: dict[str, Any]) -> AuditWriteResult:
        try:
            self._client.put_item(
                TableName=self._table_name,
                # Each top-level value is converted individually. `Item` is a map
                # of ATTRIBUTE NAME to typed value -- passing `_to_dynamo(item)`
                # would wrap the whole record in a single {"M": ...} and DynamoDB
                # would reject it, since that is one anonymous attribute rather
                # than a set of named ones.
                Item={key: _to_dynamo(value) for key, value in item.items()},
                # Immutability, enforced a second time. IAM already withholds
                # UpdateItem and DeleteItem; this stops PutItem being used to
                # overwrite, which IAM cannot express.
                ConditionExpression="attribute_not_exists(verdict_id)",
            )
        except Exception as exc:  # noqa: BLE001 - a storage failure must not halt a judged deploy
            code = _error_code(exc)
            if code == "ConditionalCheckFailedException":
                # Already recorded. CodePipeline can invoke a Lambda more than
                # once for one job, and the first verdict is the one that counts.
                logger.warning("verdict %s already recorded; not overwriting", item["verdict_id"])
                return AuditWriteResult(AuditWriteStatus.DUPLICATE)

            logger.error("failed to write verdict %s: %s", item.get("verdict_id"), exc)
            return AuditWriteResult(AuditWriteStatus.FAILED, error=str(exc))

        return AuditWriteResult(AuditWriteStatus.WRITTEN)


# --- serialisation --------------------------------------------------------


def _jsonify(value: Any) -> str:
    """Raw model output as a JSON string rather than a nested map.

    Deliberate. The whole reason this field exists is to hold whatever the model
    produced, INCLUDING shapes that violate the schema -- extra keys, wrong
    types, nulls. Storing it as a DynamoDB map would mean converting something
    already known to be malformed into a typed structure, which can fail on
    exactly the inputs worth keeping. A string always stores.
    """
    if value is None:
        return ""
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


def _clip(text: str) -> str:
    if len(text) <= MAX_FIELD_CHARS:
        return text
    return text[:MAX_FIELD_CHARS] + f"...[clipped at {MAX_FIELD_CHARS} chars]"


def _to_dynamo(value: Any) -> Any:
    """Convert plain Python to DynamoDB's typed attribute-value format.

    Hand-rolled rather than using boto3's TypeSerializer, for one reason worth
    naming: DynamoDB has no float type, and TypeSerializer raises on a Python
    float rather than converting it. Our records are full of floats -- confidence,
    error rate, p99 latency, hours since last deploy -- so something has to make
    that choice explicitly. Here it is `Decimal(str(x))`, which round-trips the
    decimal representation rather than the binary one.

    An EMPTY STRING is stored as a string, not as NULL. DynamoDB has allowed
    empty non-key string attributes since 2020, and the distinction matters here:
    "" means the model returned nothing, NULL would mean the field was absent.
    Same absent-versus-empty rule as everywhere else in this project.
    """
    if isinstance(value, bool):
        return {"BOOL": value}
    if value is None:
        return {"NULL": True}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, (int, float, Decimal)):
        return {"N": str(Decimal(str(value)))}
    if isinstance(value, dict):
        return {"M": {k: _to_dynamo(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"L": [_to_dynamo(v) for v in value]}
    return {"S": str(value)}


def _error_code(exc: Exception) -> str | None:
    """Duck-typed so this module does not import botocore. Same as bedrock.py."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if isinstance(code, str) and code:
            return code
    return None


def build_dynamodb_client(region: str) -> Any:
    """Imported lazily so `verdict` stays importable without boto3."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "dynamodb",
        region_name=region,
        # One attempt, for the same reason as the Bedrock client: retry policy in
        # one place. Unlike Bedrock there is no retry loop above this -- a failed
        # audit write is logged and accepted, because the deploy has already been
        # judged and storage trouble is not evidence about the change.
        config=Config(read_timeout=4, connect_timeout=2, retries={"max_attempts": 2}),
    )
