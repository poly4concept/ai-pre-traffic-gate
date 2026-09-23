"""Asking Google Gemini the same question, for comparison. Phase 4c.

WHY THIS EXISTS, AND WHY IT IS ONLY IN THE EVAL PATH

Phase 4 measured three models and found Haiku 4.5 and Sonnet 4.5 failing the
same scenarios, in the same direction, with the same reasoning (D-080). That is
the signature of a specification gap rather than a capability gap -- but two
models from one vendor is thin evidence for a claim that broad. A model trained
by somebody else entirely is the test that makes it stand up or fall over.

The gate itself stays Bedrock-only. This is a fourth opinion for the benchmark,
not a second production path, and the distinction is deliberate: "model
agnostic" is a claim about an abstraction, and the honest way to support it is
to run a second provider through the same abstraction rather than to assert it
in a README.

HOW LITTLE OF THIS IS ACTUALLY GEMINI

Everything below is a `Transport`: one API call, one extraction, one error
classification. The retry loop, the deadline arithmetic, the repair-retry
corrections, schema validation, the security floor and the fail-closed
guarantee all come from `client.py` unchanged. If this file were longer, the
abstraction would be wrong.

THE PROMPT IS THE SAME PROMPT

`SYSTEM_PROMPT`, `render_bundle` and `render_correction` are shared with the
Bedrock path, byte for byte. A benchmark where each provider got its own
slightly-tuned wording would measure the wording. The transports differ; the
question does not.

THE SCHEMA IS THE SAME SCHEMA

`FunctionDeclaration.parameters_json_schema` takes raw JSON Schema, so
`VERDICT_SCHEMA` goes across untranslated. This was the part expected to need an
adapter -- Gemini's older `parameters` field wants an OpenAPI subset -- and not
needing one matters for a reason beyond convenience: an adapter would be a
second description of the verdict contract, and two descriptions drift. The
validator on the way back is the same one too, so a Gemini answer has to clear
exactly the bar a Bedrock answer clears.

WHAT IS NOT USED, ON PURPOSE

Gemini has a native structured-output mode (`response_schema`) that would also
produce JSON. Forced function calling is used instead, because the Bedrock path
uses forced tool use and a comparison between two different output mechanisms
would confound the thing being measured. D-006's argument -- that a forced tool
call is a structural constraint applied during decoding, where "reply with JSON"
is merely a request -- applies to both providers identically.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from signals import SignalBundle

from .client import (
    MAX_TOKENS,
    TEMPERATURE,
    ModelReply,
    ModelResponseError,
    VerdictClient,
)
from .prompt import SYSTEM_PROMPT, render_bundle, render_correction
from .schema import VERDICT_SCHEMA, VERDICT_TOOL_NAME

logger = logging.getLogger(__name__)

# HTTP status codes worth a second attempt.
#
# 429 is the free tier's daily and per-minute quota, and it is included for the
# same reason Bedrock's ThrottlingException is: "briefly rate limited" and "out
# of allowance until midnight Pacific" arrive identically, and the deadline is
# what bounds the pointless case. 408 and the 5xx family are transient by
# definition.
#
# Deliberately absent: 400, 401, 403, 404. Those describe a bad key, a wrong
# model ID or a malformed request, and repeating a misconfigured call three
# times only makes the caller wait longer to be told the same thing.
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})

# Per-request ceiling, in milliseconds.
#
# NOT the same number as Bedrock's 20s read timeout, and copying that value here
# was the first thing this transport got wrong. It produced three consecutive
# `504 DEADLINE_EXCEEDED` on the very first real run, which reads like an outage
# and is not one: on a free-tier key the request QUEUES before the model starts,
# and the deadline covers the wait as well as the work. A probe measured the
# actual inference at 4.7 seconds, so 20s was never tight on latency -- it was
# tight on queue time, which is a property of the tier rather than of the model.
#
# 60s is generous on purpose. The cost of being too generous is a slow failure;
# the cost of being too tight is a fail-closed verdict recorded against a model
# that would have answered, which pollutes the eval with UNMEASURED rows (F-018).
REQUEST_TIMEOUT_MS = 60_000

# Deliberately absent: any `thinking_config`.
#
# Gemini 3.x models think by default, which raised a fair question -- thinking
# tokens are drawn from the same `max_output_tokens` budget, so a thinking model
# could in principle spend all 1024 on thoughts and never emit the function
# call. Measured rather than assumed: a real call reported `thoughts=None` and
# 126 output tokens, so nothing needs capping.
#
# And the obvious lever does not exist anyway. `ThinkingConfig(thinking_budget=0)`
# -- documented as DISABLED -- is rejected by this model with
# `400 INVALID_ARGUMENT`. The budget's allowed range is model-dependent and zero
# is not in it here. Left alone entirely.

# Minimum seconds between calls, to stay inside the free tier's requests-per-
# minute ceiling.
#
# The eval fires 22 scenarios back to back with no natural pacing, which is a
# reliable way to turn a quota that would have been sufficient into a wall of
# 429s. Free-tier Flash-Lite allows roughly 15 requests per minute, so 4.5
# seconds leaves margin without materially lengthening a run that is dominated
# by inference time anyway. Set to 0 for a paid key.
MIN_SECONDS_BETWEEN_CALLS = 4.5


def verdict_function_declaration() -> dict[str, Any]:
    """Our one tool, in the shape `generate_content` wants.

    `parameters_json_schema` rather than `parameters`: the former takes JSON
    Schema as written, the latter an OpenAPI subset that would need VERDICT_SCHEMA
    translated. See the module docstring -- the translation is the problem, not
    the effort.
    """
    return {
        "name": VERDICT_TOOL_NAME,
        "description": (
            "Record a structured risk assessment for the proposed deployment. "
            "Call this exactly once."
        ),
        "parameters_json_schema": VERDICT_SCHEMA,
    }


def extract_function_call(response: Any) -> dict[str, Any]:
    """Pull our function call's arguments out of a response, or raise.

    A deliberate mirror of `bedrock.extract_tool_input`, failure kind for failure
    kind, so that the two providers produce comparable audit records. Phase 4
    counts these by name; if Gemini reported `no_tool_call` as something else,
    the two columns of the report would not be addable.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        # Usually a prompt-level safety block: no candidate was generated at all.
        feedback = getattr(response, "prompt_feedback", None)
        raise ModelResponseError(
            f"response contained no candidates (prompt_feedback={feedback}); "
            "no assessment was produced",
            kind="content_filtered",
        )

    candidate = candidates[0]
    finish = getattr(candidate, "finish_reason", None)
    finish_name = getattr(finish, "name", None) or str(finish or "")

    # Truncation gets its own branch for the same reason it does on Bedrock: the
    # model was mid-function-call when it hit the ceiling, so any arguments below
    # are TRUNCATED JSON. They may still parse and may still validate while being
    # incomplete, which would produce a verdict formed from half a sentence.
    if finish_name == "MAX_TOKENS":
        raise ModelResponseError(
            f"model hit the {MAX_TOKENS}-token ceiling mid-response; any function "
            "arguments it produced are truncated and cannot be trusted",
            kind="max_tokens",
        )

    if finish_name in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}:
        raise ModelResponseError(
            f"generation was stopped by a content filter (finishReason={finish_name}); "
            "no assessment was produced",
            kind="content_filtered",
        )

    # Gemini names this failure where Bedrock leaves it to schema validation: the
    # model tried to call the function and produced arguments that are not valid
    # JSON at all. Mapped onto the same kind a malformed body gets elsewhere so
    # the correction logic treats it the same way.
    if finish_name == "MALFORMED_FUNCTION_CALL":
        raise ModelResponseError(
            "model produced a malformed function call that could not be parsed",
            kind="malformed_response",
        )

    content = getattr(candidate, "content", None)
    parts = getattr(content, "parts", None) or []
    calls = [part.function_call for part in parts if getattr(part, "function_call", None)]

    if not calls:
        # The model answered in prose despite mode=ANY forcing the call. Worth
        # naming rather than folding into a generic error: it is the failure mode
        # that proves forcing is a strong steer and not a guarantee -- the same
        # lesson, on a second provider, as the schema not being validated.
        text = " ".join(part.text for part in parts if getattr(part, "text", None))
        preview = text[:200] if text else "(no text either)"
        raise ModelResponseError(
            f"model did not call {VERDICT_TOOL_NAME} despite mode=ANY forcing it "
            f"(finishReason={finish_name}); it replied: {preview}",
            kind="no_tool_call",
        )

    if len(calls) > 1:
        # Ambiguity, and ambiguity fails closed. Taking the first would be picking
        # one of two disagreeing answers at random and calling it a decision.
        raise ModelResponseError(
            f"model returned {len(calls)} function calls; expected exactly one",
            kind="multiple_tool_calls",
        )

    call = calls[0]
    if call.name != VERDICT_TOOL_NAME:
        raise ModelResponseError(
            f"model called {call.name!r}, not {VERDICT_TOOL_NAME!r}",
            kind="wrong_tool",
        )

    return call.args


class GeminiTransport:
    """One `generate_content` call, and which HTTP statuses are worth repeating.

    The SDK client is injected for the same reason boto3 is on the Bedrock side:
    it is what lets this whole path be tested offline against realistic response
    objects, with no `if testing:` branch in the code being tested.
    """

    def __init__(
        self,
        model_id: str,
        client: Any,
        *,
        min_interval_seconds: float = MIN_SECONDS_BETWEEN_CALLS,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ):
        self._model_id = model_id
        self._client = client
        self._min_interval = min_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call_at: float | None = None

    def invoke(self, bundle: SignalBundle, correction: str | None) -> ModelReply:
        from google.genai import types

        self._wait_for_rate_limit()

        prompt = render_bundle(bundle)
        if correction:
            prompt += render_correction(correction)

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=TEMPERATURE,
            max_output_tokens=MAX_TOKENS,
            tools=[types.Tool(function_declarations=[verdict_function_declaration()])],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    # The equivalent of Bedrock's `toolChoice: {tool: ...}`.
                    # ANY forces a function call; naming the allowed function
                    # leaves it no other one to choose.
                    mode=types.FunctionCallingConfigMode.ANY,
                    allowed_function_names=[VERDICT_TOOL_NAME],
                )
            ),
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )

        started = self._monotonic()
        try:
            response = self._client.models.generate_content(
                model=self._model_id,
                contents=prompt,
                config=config,
            )
        finally:
            # Recorded even on failure, so a run that spends its quota on errors
            # still paces the retries rather than hammering a 429.
            self._last_call_at = self._monotonic()

        # MEASURED HERE, and not the same number Bedrock reports.
        #
        # Converse returns a server-side `latencyMs`; the Gemini API returns no
        # latency at all, so this is wall clock and includes network time from
        # wherever the eval happens to run. Comparable between Gemini runs, NOT
        # comparable to the Bedrock column without saying so. Recording the
        # honest number and naming its meaning beats leaving the field empty.
        elapsed_ms = int((self._monotonic() - started) * 1000)

        usage = getattr(response, "usage_metadata", None)
        candidate = (getattr(response, "candidates", None) or [None])[0]
        finish = getattr(candidate, "finish_reason", None)

        return ModelReply(
            tool_input=extract_function_call(response),
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            latency_ms=elapsed_ms,
            stop_reason=getattr(finish, "name", None) or (str(finish) if finish else None),
        )

    def retryable(self, exc: Exception) -> tuple[str, bool] | None:
        """Classify an SDK error by HTTP status.

        Duck-typed on `code` rather than `isinstance(exc, APIError)` so this
        module stays importable, and testable, without the Google SDK installed
        -- the same reason `bedrock._error_code` does not import botocore.
        """
        code = getattr(exc, "code", None)
        if not isinstance(code, int):
            return None
        return f"gemini:{code}", code in RETRYABLE_STATUS_CODES

    def _wait_for_rate_limit(self) -> None:
        if not self._min_interval or self._last_call_at is None:
            return
        remaining = self._min_interval - (self._monotonic() - self._last_call_at)
        if remaining > 0:
            logger.debug("pacing for the free tier: sleeping %.1fs", remaining)
            self._sleep(remaining)


class GeminiVerdictClient(VerdictClient):
    """Asks Gemini for a risk verdict. Never raises.

    Same guarantee, same retry budget and same floor as the Bedrock client --
    all of it inherited rather than restated.
    """

    def __init__(
        self,
        model_id: str,
        client: Any,
        *,
        min_interval_seconds: float = MIN_SECONDS_BETWEEN_CALLS,
        **kwargs: Any,
    ):
        transport = GeminiTransport(
            model_id,
            client,
            min_interval_seconds=min_interval_seconds,
            sleep=kwargs.get("sleep", time.sleep),
            monotonic=kwargs.get("monotonic", time.monotonic),
        )
        super().__init__(model_id, transport, **kwargs)


def build_genai_client(api_key: str | None = None) -> Any:
    """Construct the Gemini SDK client.

    The key is read from `GEMINI_API_KEY` by the SDK when not passed, which is
    where it should live: never a Terraform variable, never a committed file.
    `.env` is already git-ignored.

    Imported lazily so that importing `verdict` does not require the Google SDK.
    `google-genai` is an optional `[evals]` dependency and is deliberately not in
    the Lambda bundle -- the gate never calls Gemini, and shipping an SDK for a
    provider the running system cannot reach would be dead weight in the one
    place where cold-start size is a real cost.
    """
    from google import genai

    return genai.Client(api_key=api_key) if api_key else genai.Client()
