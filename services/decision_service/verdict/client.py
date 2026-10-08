"""The provider-neutral half of asking a model for a verdict. Phase 4c.

WHY THIS MODULE EXISTS

Everything here was originally inside `bedrock.py`, and moving it out was worth
doing for one reason: when a second provider arrived, it turned out that
`get_verdict()` is 120 lines of which only four are about Bedrock.

    the API call                      provider-specific
    pulling the tool arguments out    provider-specific
    mapping an error to "retryable"   provider-specific
    token and latency field names     provider-specific
    ------------------------------------------------------
    the required-signals short circuit   NOT
    the retry loop and its backoff       NOT
    the repair-retry correction          NOT
    the predictive deadline check        NOT
    schema validation                    NOT
    the security floor                   NOT
    failing closed, nine different ways  NOT

That list is the argument. The four provider lines are transport; the rest is
the gate's actual behaviour, and it is the part that took the longest to get
right. Duplicating it per provider would mean the deadline arithmetic, the
retry-correction logic and the fail-closed guarantee each existing in two places
and drifting apart -- with the second copy getting less scrutiny precisely
because it was written second.

So a provider supplies a `Transport` and inherits the rest. Adding a third one
is one class, not one client.

WHAT A TRANSPORT OWES THE CLIENT

`invoke()` either returns a `ModelReply` -- the tool arguments plus whatever
usage metadata the provider reports -- or raises. It must raise
`ModelResponseError` for a response that arrived but was not a single call to
our tool, because those failure kinds are named in the audit record and Phase 4
counts them. Anything else it raises is classified by `classify_failure()`.

`retryable()` answers one question about a transport-level exception: would
trying again plausibly help? Returning None means "not an error I recognise",
and the shared classifier takes over.

THE GUARANTEE THIS MODULE MAKES, unchanged from where it started

`get_verdict()` does not raise. Not "tries not to" -- it has no failure path
that escapes. Everything that can go wrong returns a `Verdict.fail_closed(...)`
carrying the reason. That is docs/design-constraints.md constraint 2 taken literally: fail closed
is the DEFAULT BRANCH, not an exception handler bolted on later. Written the
other way round -- a try/except wrapped around the happy path -- the safe
behaviour depends on someone keeping that except clause correct forever. Here
the return type is "a verdict, always", and the only question is which one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from signals import SignalBundle

from .floor import apply_floor
from .prompt import PROMPT_VERSION
from .schema import VERDICT_FIELDS, VERDICT_TOOL_NAME
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

# The wall clock the retry loop spends against, measured in seconds of pipeline
# time rather than an attempt count, because attempts are the wrong unit: three
# fast throttles and three slow timeouts cost wildly different amounts.
DEADLINE_SECONDS = 35.0

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
    """Operational metadata about the model call.

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


@dataclass(frozen=True, slots=True)
class ModelReply:
    """One successful call, normalised across providers.

    The usage fields are all optional because providers disagree about what they
    report and when. A missing token count is recorded as missing rather than as
    zero -- the same absent-is-not-zero rule the signal collectors follow, for
    the same reason: a cost chart built from silent zeroes understates the bill
    and nothing in it looks wrong.
    """

    tool_input: Any
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    stop_reason: str | None = None


class ModelResponseError(Exception):
    """The response was not shaped the way a tool call should be.

    Distinct from `VerdictValidationError`, which means the tool WAS called and
    its arguments were wrong. This means we never got as far as arguments. They
    are separated because they point at different fixes -- a wrong shape is
    usually a tool-choice or model-capability problem, while bad arguments are
    a prompt or schema problem.
    """

    def __init__(self, message: str, *, kind: str):
        super().__init__(message)
        self.kind = kind


class Transport(Protocol):
    """The provider-specific quarter of a verdict call."""

    def invoke(self, bundle: SignalBundle, correction: str | None) -> ModelReply:
        """Call the model once. Return its tool arguments, or raise."""
        ...

    def retryable(self, exc: Exception) -> tuple[str, bool] | None:
        """Classify a transport error as (failure_kind, retryable), or None.

        None means "not an exception this transport recognises", and the shared
        classifier handles it.
        """
        ...


class VerdictClient:
    """Asks a model for a risk verdict. Never raises.

    The transport is injected rather than constructed, for the same reason the
    signal collectors take their clients that way: it is what lets the whole
    path be tested offline against realistic responses, with no `if testing:`
    branch anywhere in the code being tested.
    """

    def __init__(
        self,
        model_id: str,
        transport: Transport,
        *,
        deadline_seconds: float = DEADLINE_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ):
        self._model_id = model_id
        self._transport = transport
        self._deadline_seconds = deadline_seconds
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._monotonic = monotonic

    def get_verdict(self, bundle: SignalBundle) -> VerdictOutcome:
        """Produce a verdict for this bundle. Always returns; never raises."""
        if not bundle.has_required_signals:
            # No API call. A model asked to assess a change it was never told
            # about will not refuse -- it will produce a fluent, confident,
            # entirely baseless verdict, and that output is indistinguishable
            # from a real one. It is the single worst thing this system could
            # emit. Not calling is also free, which is a pleasant coincidence
            # rather than the reason.
            reason = (
                "required signals are missing, so there is nothing to assess: "
                f"{bundle.completeness_summary}"
            )
            logger.warning("skipping the model: %s", reason)
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
        # Set after a rejected answer so the next attempt asks a DIFFERENT
        # question. Without this the retry is a repeat -- see _correction_for.
        correction: str | None = None

        while True:
            attempts += 1
            attempt_started = self._monotonic()
            try:
                reply = self._transport.invoke(bundle, correction)
                last_raw = reply.tool_input
                verdict = parse_verdict(reply.tool_input, model_id=self._model_id)
                # Applied here rather than in the handler so that EVERY caller
                # of the verdict client gets it -- including the eval harness,
                # which would otherwise measure a gate that does not exist.
                verdict = apply_floor(verdict, bundle)

            except Exception as exc:  # noqa: BLE001 - the whole point is that nothing escapes
                last_kind, last_error, retryable = self._classify(exc)
                logger.warning(
                    "verdict attempt %d failed (%s, retryable=%s): %s",
                    attempts,
                    last_kind,
                    retryable,
                    last_error,
                )

                delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
                elapsed = self._monotonic() - started
                # PREDICTIVE, and the estimate is the attempt that just failed.
                #
                # `elapsed + delay >= deadline` only asks whether there is time
                # to START another attempt. It does not ask whether there is time
                # to FINISH one, and the deadline cannot interrupt a call already
                # in flight -- so a retry begun at 20.5s against a 35s deadline
                # runs to 40.5s and overshoots every time it is hit.
                #
                # Budgeting the full read timeout for the next attempt fixes the
                # overshoot and creates the opposite error: it refuses a retry
                # that would have taken six seconds because it MIGHT have taken
                # twenty, and refusing a retry means failing closed and halting a
                # deploy that was never risky.
                #
                # So the estimate is what just happened. A schema violation
                # returns in ~6s and predicts ~6s, leaving room to try again with
                # a correction. A read timeout consumed 20s and predicts 20s,
                # which correctly uses up the budget. The loop spends its
                # remaining time on the failures that retrying can actually fix.
                attempt_seconds = self._monotonic() - attempt_started
                out_of_time = elapsed + delay + attempt_seconds >= self._deadline_seconds
                out_of_attempts = attempts >= self._max_attempts
                if not retryable or out_of_time or out_of_attempts:
                    break

                # Only overwritten when there is something to correct, so a
                # throttle between two format failures does not erase the
                # instruction the next attempt still needs.
                correction = _correction_for(last_kind) or correction

                self._sleep(delay)
                continue

            return VerdictOutcome(
                verdict=verdict,
                call=ModelCall(
                    model_id=self._model_id,
                    prompt_version=PROMPT_VERSION,
                    attempts=attempts,
                    succeeded=True,
                    latency_ms=reply.latency_ms,
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                    stop_reason=reply.stop_reason,
                ),
                raw_model_output=reply.tool_input,
            )

        reason = f"could not obtain a valid verdict ({last_kind}): {last_error}"
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

    def _classify(self, exc: Exception) -> tuple[str, str, bool]:
        """Map an exception to (failure_kind, message, retryable).

        A schema-validation failure is retryable, but Phase 4b corrected WHY, and
        the original reasoning was wrong in an instructive way.

        What this used to say: at temperature 0 a model occasionally slips, so one
        retry takes a 1% failure rate to 0.01%. That argument assumes the failures
        are independent. They are not. Measured on 22 scenarios, 4 failed validation
        and every one of them failed on all three attempts with the byte-identical
        error -- because at temperature 0 an identical prompt yields an identical
        answer, so an identical retry is not a second chance, it is the same chance
        taken again at full price. Twelve inferences, no new information.

        Retries are now worth making because `_correction_for` changes the question:
        the next attempt is told what was wrong with the last one. The retry is
        still recorded in `attempts`, so the underlying slip rate stays visible
        rather than being hidden by the repair -- the condition that makes the
        repair honest rather than a cover-up.
        """
        if isinstance(exc, VerdictValidationError):
            return f"invalid_verdict:{exc.field or 'unknown'}", str(exc), True

        if isinstance(exc, ModelResponseError):
            # A prose reply is worth one more try; a content filter or a truncated
            # response will reproduce identically, so retrying only burns the clock.
            retryable = exc.kind in {"no_tool_call", "multiple_tool_calls"}
            return exc.kind, str(exc), retryable

        provider = self._transport.retryable(exc)
        if provider is not None:
            kind, retryable = provider
            return kind, str(exc), retryable

        # Anything unrecognised is retried once rather than assumed fatal: an
        # unclassified error is more often a transient network fault than a
        # permanent one, and the deadline bounds the cost of being wrong.
        return f"unexpected:{type(exc).__name__}", str(exc), True


# --- What we tell the model when we reject its answer ----------------------
#
# Phase 4b, measured: 4 of 22 scenarios failed validation, and each failed on
# all three attempts with the identical error. At temperature 0 that is what an
# identical retry buys -- the same answer, twice more, billed each time. The
# retry only becomes worth making if the second question differs from the first.
#
# These are FIXED sentences keyed on the failure, deliberately not the
# validator's own message. Two validator messages interpolate model-supplied
# text (an unexpected field name, an out-of-enum risk_level value), and model
# output derives in part from an untrusted commit message. Echoing it into the
# next prompt would route attacker-influenced text back through the model for no
# benefit -- a fixed instruction is both safer and a clearer correction.
#
# Note what these do and do not say. They correct FORM, never content: no
# correction mentions a risk level, a signal, or what the answer should be. A
# retry that nudged the verdict would be the gate arguing with itself until it
# got the answer it wanted, which is not a retry at all.
_GENERIC_CORRECTION = (
    "Your previous tool call was rejected: its arguments did not match the "
    "tool's input schema. Call the tool again, with the same assessment, in "
    "valid form."
)

_FIELD_CORRECTIONS: dict[str, str] = {
    "primary_concerns": (
        "primary_concerns must be a JSON array of plain strings, like "
        '["first concern", "second concern"]. Not one string containing all of '
        "them, and no tags, markup or numbering inside the strings."
    ),
    "risk_level": (
        'risk_level must be exactly one of the strings "low", "medium" or '
        '"high" -- lowercase, no other value, no explanation in the field.'
    ),
    "confidence": "confidence must be a JSON number between 0 and 1, such as 0.8.",
    "reasoning": "reasoning must be a single plain string, with no tags or markup.",
}

_CORRECTABLE_KINDS = {
    "no_tool_call": (
        f"You replied in prose. You must call the {VERDICT_TOOL_NAME} tool "
        "instead, with your assessment as its arguments."
    ),
    "multiple_tool_calls": (
        f"You called {VERDICT_TOOL_NAME} more than once. Call it exactly once, "
        "with a single combined assessment."
    ),
}


def _correction_for(failure_kind: str) -> str | None:
    """The instruction to add on retry, or None if there is nothing to correct.

    Returns None for transport and throttling failures: a retry after a 429 is
    already a different request because time has passed, and there is nothing
    the model did wrong to tell it about.
    """
    if failure_kind in _CORRECTABLE_KINDS:
        return _CORRECTABLE_KINDS[failure_kind]

    if failure_kind.startswith("invalid_verdict:"):
        field = failure_kind.split(":", 1)[1]
        # Only OUR field names are trusted here. `field` comes from the
        # validator, but the unexpected-field case derives it from model output,
        # so anything unrecognised falls back to the generic sentence rather
        # than being interpolated into a prompt.
        if field in VERDICT_FIELDS:
            return _FIELD_CORRECTIONS.get(field, _GENERIC_CORRECTION)
        return _GENERIC_CORRECTION

    return None
