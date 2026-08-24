"""Calling Bedrock, and failing closed when it does not go well. Phase 3.3.

3.1 defined the answer. 3.2 defined the question. This asks it.

THE SHAPE OF THIS MODULE IS THE POINT

`get_verdict()` does not raise. Not "tries not to" -- it has no failure path that
escapes, and the tests assert that against every exception type the AWS SDK can
produce. Everything that can go wrong returns a `Verdict.fail_closed(...)`
carrying the reason.

That is CLAUDE.md constraint 2 taken literally: fail closed is the DEFAULT
BRANCH, not an exception handler bolted on later. Written the other way round --
`try: return parse(call_bedrock())` with `except Exception: return fail_closed()`
wrapped around it -- the safe path depends on someone remembering to keep the
try/except correct forever. Here the function's return type is "a verdict,
always", and the only question is which one.

WHAT CAN ACTUALLY GO WRONG, because it is a longer list than it first looks:

  * the required signals are missing, so there is nothing to ask about
  * Bedrock is throttled, unavailable, or times out
  * we lack permission, or the model ID is wrong
  * the model answers in prose despite being told it must call a tool
  * it calls a tool, but not ours
  * it calls ours twice
  * it hits the token ceiling mid-tool-call and returns truncated arguments
  * a content filter or guardrail stops generation
  * the tool input arrives and fails schema validation

Nine failure modes, one outcome: a human looks at it. The value of enumerating
them is not that they behave differently -- it is that the audit record says
WHICH one happened, and Phase 4 cannot tell a prompt problem from an
infrastructure problem without that.

WHY THE MODEL IS NEVER ASKED WHEN CHANGE CONTEXT IS MISSING

`bundle.has_required_signals` short-circuits before any API call. A model asked
to assess a change it was never told about will not refuse -- it will produce a
fluent, confident, entirely baseless verdict, and that output is
indistinguishable from a real one. It is the single worst thing this system
could emit. Not calling is also free, which is a pleasant coincidence rather
than the reason.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from signals import SignalBundle

from .prompt import PROMPT_VERSION, build_messages, system_blocks
from .schema import VERDICT_TOOL_NAME, verdict_tool_config
from .types import Verdict
from .validation import VerdictValidationError, parse_verdict

logger = logging.getLogger(__name__)

# Temperature 0 for repeatability. Worth being precise about what that buys:
# it makes sampling greedy, which REDUCES variance. It does not eliminate it --
# floating-point non-associativity across batched requests means identical
# inputs can still produce different outputs. Phase 4 measures the residual
# rate rather than this file claiming determinism it cannot deliver.
TEMPERATURE = 0.0

# A verdict is small: 600 characters of reasoning and at most five short
# concerns. 1024 tokens is generous. It is bounded at all because an unbounded
# ceiling turns a runaway generation into a latency problem inside a pipeline
# stage, and because hitting the ceiling is a diagnosable event worth naming.
MAX_TOKENS = 1024

# Total wall-clock budget for all attempts, including backoff.
#
# A DEADLINE rather than an attempt count, because attempts are the wrong unit:
# three fast throttles and three slow timeouts cost wildly different amounts of
# the pipeline's time. The gate Lambda's timeout is 30s, so this leaves room for
# collection, logging and the DynamoDB write around it.
DEADLINE_SECONDS = 18.0

# Per-request timeouts handed to botocore.
READ_TIMEOUT_SECONDS = 8
CONNECT_TIMEOUT_SECONDS = 3

# Errors where trying again might genuinely help.
#
# ThrottlingException is included even though the daily-token-quota flavour of
# it will never succeed on retry. We cannot distinguish "briefly rate limited"
# from "no allowance at all" -- both arrive as ThrottlingException -- so the
# deadline is what bounds the pointless case rather than a special case here.
RETRYABLE_ERROR_CODES = frozenset(
    {
        "InternalServerException",
        "ModelNotReadyException",
        "ModelTimeoutException",
        "ServiceQuotaExceededException",
        "ServiceUnavailableException",
        "ThrottlingException",
        "TooManyRequestsException",
    }
)

# Deliberately NOT retried: AccessDeniedException, ValidationException,
# ResourceNotFoundException. These describe a misconfiguration, and repeating a
# misconfigured call three times only makes the pipeline wait longer to be told
# the same thing.

# Backoff between attempts, in seconds. No jitter: jitter exists to desynchronise
# a thundering herd, and there is exactly one caller per pipeline execution.
# Adding randomness would cost the reproducibility that makes these tests exact,
# for a benefit this call pattern cannot realise.
BACKOFF_SECONDS = (0.5, 1.5, 3.0)

# A second, independent bound -- and it is not redundant with the deadline,
# because the two limit different resources.
#
# DEADLINE_SECONDS bounds how long the pipeline waits. MAX_ATTEMPTS bounds how
# much the pipeline SPENDS. Those come apart precisely where it matters: a
# schema-validation failure is retryable, and every one of those retries means
# the model actually ran and we actually paid for the tokens. Under the deadline
# alone, an 18-second budget with this backoff allows roughly seven attempts, so
# a model that systematically misformats its output would bill seven full
# inferences per pipeline run and still fail closed at the end.
#
# Three is enough for the case retries exist to cover -- an occasional slip -- and
# far too few to make a systematic problem expensive. Whichever bound trips
# first wins.
MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class ModelCall:
    """Operational metadata about the Bedrock call.

    Kept separate from `Verdict` on the same principle that excluded
    `duration_ms` from `SignalBundle.to_dict()`: a verdict is EVIDENCE and must
    be byte-identical on replay, while latency and token counts vary run to run
    on identical input. Mixing them would make two identical decisions look
    different.

    Both are stored in the audit record. They are just not the same kind of
    thing, and Phase 4 compares one while Phase 8 charts the other.
    """

    model_id: str
    prompt_version: str
    attempts: int
    succeeded: bool
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str | None = None
    failure_kind: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "attempts": self.attempts,
            "succeeded": self.succeeded,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "stop_reason": self.stop_reason,
            "failure_kind": self.failure_kind,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class VerdictOutcome:
    """What the verdict layer returns: a decision, and how it was reached."""

    verdict: Verdict
    call: ModelCall
    # What the model actually put in the tool call, before validation touched it.
    #
    # Carried out of the client rather than logged and dropped, because the audit
    # record's central claim is that the raw answer and the accepted verdict can
    # be compared. On the FAILURE path this is the only place a rejected answer
    # survives at all -- `call.error` holds the validator's complaint, not the
    # text that provoked it, and "the model said 'critical'" is precisely the
    # detail Phase 4 needs to count.
    #
    # None when the model was never reached, or when the response was malformed
    # enough that no tool arguments were found.
    raw_model_output: Any = None


class ModelResponseError(Exception):
    """The Converse response was not shaped the way a tool call should be.

    Distinct from `VerdictValidationError`, which means the tool WAS called and
    its arguments were wrong. This means we never got as far as arguments. They
    are separated because they point at different fixes -- a wrong shape is
    usually a `toolChoice` or model-capability problem, while bad arguments are
    usually a schema-wording problem.
    """

    def __init__(self, message: str, *, kind: str):
        super().__init__(message)
        self.kind = kind


def extract_tool_input(response: dict[str, Any]) -> dict[str, Any]:
    """Pull our tool's arguments out of a Converse response, or raise.

    Separated from the client so it can be tested against realistic response
    shapes without a client, and so each failure mode gets its own name in the
    audit record instead of one undifferentiated "bad response".
    """
    stop_reason = response.get("stopReason")

    # `max_tokens` deserves its own branch. It means the model was mid-tool-call
    # when it hit the ceiling, so the arguments below are TRUNCATED JSON -- they
    # may still parse, and they may still validate, while being incomplete.
    # Silently accepting that would produce a verdict formed from half a
    # sentence of reasoning.
    if stop_reason == "max_tokens":
        raise ModelResponseError(
            f"model hit the {MAX_TOKENS}-token ceiling mid-response; any tool "
            "arguments it produced are truncated and cannot be trusted",
            kind="max_tokens",
        )

    if stop_reason in {"content_filtered", "guardrail_intervened"}:
        raise ModelResponseError(
            f"generation was stopped by a content filter (stopReason={stop_reason}); "
            "no assessment was produced",
            kind="content_filtered",
        )

    content = response.get("output", {}).get("message", {}).get("content", [])
    if not isinstance(content, list):
        raise ModelResponseError(
            f"response content is {type(content).__name__}, not a list of blocks",
            kind="malformed_response",
        )

    tool_uses = [
        block["toolUse"] for block in content if isinstance(block, dict) and "toolUse" in block
    ]

    if not tool_uses:
        # The model answered in prose despite `toolChoice` forcing the tool.
        # Rare, and worth naming rather than folding into a generic error: it is
        # the failure mode that proves toolChoice is a strong steer and not a
        # guarantee, which is the same lesson as the schema not being validated.
        text = " ".join(
            block["text"] for block in content if isinstance(block, dict) and "text" in block
        )
        preview = text[:200] if text else "(no text either)"
        raise ModelResponseError(
            f"model did not call {VERDICT_TOOL_NAME} despite toolChoice forcing it "
            f"(stopReason={stop_reason}); it replied: {preview}",
            kind="no_tool_call",
        )

    if len(tool_uses) > 1:
        # Ambiguity, and ambiguity fails closed. Taking the first would be
        # picking one of two disagreeing answers at random and calling it a
        # decision.
        raise ModelResponseError(
            f"model returned {len(tool_uses)} tool calls; expected exactly one",
            kind="multiple_tool_calls",
        )

    tool_use = tool_uses[0]
    name = tool_use.get("name")
    if name != VERDICT_TOOL_NAME:
        raise ModelResponseError(
            f"model called {name!r}, not {VERDICT_TOOL_NAME!r}",
            kind="wrong_tool",
        )

    return tool_use.get("input")


class BedrockVerdictClient:
    """Asks Bedrock for a risk verdict. Never raises.

    The boto3 client is injected rather than constructed, for the same reason
    the signal collectors take theirs: it is what lets the whole path be tested
    offline against realistic responses, with no `if testing:` branch anywhere in
    the code being tested.
    """

    def __init__(
        self,
        model_id: str,
        client: Any,
        *,
        deadline_seconds: float = DEADLINE_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ):
        self._model_id = model_id
        self._client = client
        self._deadline_seconds = deadline_seconds
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._monotonic = monotonic

    def get_verdict(self, bundle: SignalBundle) -> VerdictOutcome:
        """Produce a verdict for this bundle. Always returns; never raises."""
        if not bundle.has_required_signals:
            # No API call. See the module docstring: a model asked about a change
            # it was never shown produces a confident, baseless answer that is
            # indistinguishable from a real verdict.
            reason = (
                "required signals are missing, so there is nothing to assess: "
                f"{bundle.completeness_summary}"
            )
            logger.warning("skipping Bedrock: %s", reason)
            return VerdictOutcome(
                verdict=Verdict.fail_closed(reason),
                call=ModelCall(
                    model_id=self._model_id,
                    prompt_version=PROMPT_VERSION,
                    attempts=0,
                    succeeded=False,
                    failure_kind="required_signals_missing",
                    error=reason,
                ),
            )

        started = self._monotonic()
        attempts = 0
        last_error = "no attempt was made"
        last_kind = "unknown"
        # Survives the loop so a rejected answer reaches the audit record. Only
        # overwritten when an attempt actually produces tool arguments, so a
        # later transport failure cannot erase an earlier bad answer.
        last_raw: Any = None

        while True:
            attempts += 1
            try:
                response = self._converse(bundle)
                raw = extract_tool_input(response)
                last_raw = raw
                verdict = parse_verdict(raw, model_id=self._model_id)

            except Exception as exc:  # noqa: BLE001 - the whole point is that nothing escapes
                last_kind, last_error, retryable = _classify(exc)
                logger.warning(
                    "bedrock attempt %d failed (%s, retryable=%s): %s",
                    attempts,
                    last_kind,
                    retryable,
                    last_error,
                )

                delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
                elapsed = self._monotonic() - started
                out_of_time = elapsed + delay >= self._deadline_seconds
                out_of_attempts = attempts >= self._max_attempts
                if not retryable or out_of_time or out_of_attempts:
                    break

                self._sleep(delay)
                continue

            usage = response.get("usage") or {}
            metrics = response.get("metrics") or {}
            return VerdictOutcome(
                verdict=verdict,
                call=ModelCall(
                    model_id=self._model_id,
                    prompt_version=PROMPT_VERSION,
                    attempts=attempts,
                    succeeded=True,
                    latency_ms=metrics.get("latencyMs"),
                    input_tokens=usage.get("inputTokens"),
                    output_tokens=usage.get("outputTokens"),
                    stop_reason=response.get("stopReason"),
                ),
                raw_model_output=raw,
            )

        reason = f"could not obtain a valid verdict from Bedrock ({last_kind}): {last_error}"
        return VerdictOutcome(
            verdict=Verdict.fail_closed(reason, model_id=self._model_id),
            call=ModelCall(
                model_id=self._model_id,
                prompt_version=PROMPT_VERSION,
                attempts=attempts,
                succeeded=False,
                failure_kind=last_kind,
                error=last_error,
            ),
            raw_model_output=last_raw,
        )

    def _converse(self, bundle: SignalBundle) -> dict[str, Any]:
        return self._client.converse(
            modelId=self._model_id,
            system=system_blocks(),
            messages=build_messages(bundle),
            toolConfig=verdict_tool_config(),
            inferenceConfig={"temperature": TEMPERATURE, "maxTokens": MAX_TOKENS},
        )


def _classify(exc: Exception) -> tuple[str, str, bool]:
    """Map an exception to (failure_kind, message, retryable).

    Retrying a schema-validation failure IS worth one attempt, which is not
    obvious. The reasoning: at temperature 0 a model still occasionally emits an
    out-of-enum value, and if that happens on say 1% of calls then refusing
    outright halts 1% of deploys for no real reason. One retry takes that to
    0.01%. The retry is recorded in `attempts`, so Phase 4 can still see the
    underlying slip rate rather than having it hidden by the repair -- which is
    the condition that makes the repair honest rather than a cover-up.
    """
    if isinstance(exc, VerdictValidationError):
        return f"invalid_verdict:{exc.field or 'unknown'}", str(exc), True

    if isinstance(exc, ModelResponseError):
        # A prose reply is worth one more try; a content filter or a truncated
        # response will reproduce identically, so retrying only burns the clock.
        retryable = exc.kind in {"no_tool_call", "multiple_tool_calls"}
        return exc.kind, str(exc), retryable

    code = _error_code(exc)
    if code:
        return f"aws:{code}", str(exc), code in RETRYABLE_ERROR_CODES

    # Anything unrecognised is retried once rather than assumed fatal: an
    # unclassified error is more often a transient network fault than a
    # permanent one, and the deadline bounds the cost of being wrong.
    return f"unexpected:{type(exc).__name__}", str(exc), True


def _error_code(exc: Exception) -> str | None:
    """Pull the AWS error code out of a botocore ClientError, if that is what this is.

    Duck-typed rather than `isinstance(exc, ClientError)` so this module does not
    import botocore, which keeps it importable in a test environment that has
    not installed the AWS SDK.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if isinstance(code, str) and code:
            return code
    return None


def build_bedrock_client(region: str) -> Any:
    """Construct the boto3 client with the timeouts this module assumes.

    `retries={"max_attempts": 0}` is the important line and the easiest to omit.
    botocore retries throttles and 5xx by default, silently, underneath us. With
    that on, `attempts` in the audit record would be a fiction, the deadline
    could be blown by retries we never authorised, and a "single" call could
    quietly cost several times what we budgeted. Retry policy belongs in one
    place, and this module is it.

    Imported lazily so that importing `verdict` does not require boto3 -- the
    test suite exercises every path in this file without it.
    """
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            read_timeout=READ_TIMEOUT_SECONDS,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            retries={"max_attempts": 0},
        ),
    )
