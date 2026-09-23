"""Calling Bedrock, and failing closed when it does not go well. Phase 3.3.

3.1 defined the answer. 3.2 defined the question. This asks it.

WHAT IS AND IS NOT IN HERE

The retry loop, the deadline arithmetic, the repair-retry corrections and the
fail-closed guarantee moved to `client.py` when a second provider arrived --
none of them were ever about Bedrock. What remains here is the Bedrock-shaped
quarter: the Converse call, pulling our tool's arguments out of the response,
and deciding which AWS error codes are worth a second attempt.

`BedrockVerdictClient` still exists with the same constructor and the same
behaviour. It is now a `VerdictClient` holding a `BedrockTransport`.

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
"""

from __future__ import annotations

import logging
from typing import Any

from signals import SignalBundle

from .client import (
    BACKOFF_SECONDS,
    DEADLINE_SECONDS,
    MAX_ATTEMPTS,
    MAX_TOKENS,
    TEMPERATURE,
    ModelCall,
    ModelReply,
    ModelResponseError,
    VerdictClient,
    VerdictOutcome,
)
from .prompt import build_messages, system_blocks
from .schema import VERDICT_TOOL_NAME, verdict_tool_config

# Re-exported so that `from .bedrock import ModelCall` and friends keep working
# for every caller written before the split. The names live in client.py now;
# this is the compatibility surface, not a second definition.
__all__ = [
    "BACKOFF_SECONDS",
    "CONNECT_TIMEOUT_SECONDS",
    "DEADLINE_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_TOKENS",
    "READ_TIMEOUT_SECONDS",
    "RETRYABLE_ERROR_CODES",
    "TEMPERATURE",
    "BedrockTransport",
    "BedrockVerdictClient",
    "ModelCall",
    "ModelReply",
    "ModelResponseError",
    "VerdictClient",
    "VerdictOutcome",
    "build_bedrock_client",
    "extract_tool_input",
]

logger = logging.getLogger(__name__)

# --- The timeout budget, and all four numbers move together -----------------
#
# These were originally derived against Haiku 4.5 and had to be redone when the
# verdict model changed to Sonnet 4.5 (D-080). The point of writing the
# derivation down is that raising any ONE of them in isolation is a bug: the read
# timeout, the backoff, the attempt ceiling and the deadline are one budget.
#
# 20s is roughly 3.5x Sonnet's median and well past anything observed, which is
# the point -- a timeout should mean "something is wrong", not "the model was
# having a slow afternoon". The old value of 8 was 1.4x Sonnet's median and
# tripped on ordinary variance.
READ_TIMEOUT_SECONDS = 20
CONNECT_TIMEOUT_SECONDS = 3

# WORST CASE, and it is worth being able to state it:
#
#   fast failure then slow one   6.0 + 0.5 + 20.0            = 26.5s
#   then the predictive check    26.5 + 1.5 + 20 = 48 > 35   -> stops
#   plus collection and write    26.5 + 3.6 + 0.5            = 30.6s
#
# against a 60-second Lambda timeout (gate_stub.tf). `tests/test_timeout_budget.py`
# asserts that arithmetic so the next person to change one number is told about
# the other three.

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


class BedrockTransport:
    """One Converse call, and which AWS errors are worth repeating.

    The boto3 client is injected rather than constructed, which is what lets the
    whole path be tested offline against realistic responses with no
    `if testing:` branch anywhere in the code being tested.
    """

    def __init__(self, model_id: str, client: Any):
        self._model_id = model_id
        self._client = client

    def invoke(self, bundle: SignalBundle, correction: str | None) -> ModelReply:
        response = self._client.converse(
            modelId=self._model_id,
            system=system_blocks(),
            messages=build_messages(bundle, correction),
            toolConfig=verdict_tool_config(),
            inferenceConfig={"temperature": TEMPERATURE, "maxTokens": MAX_TOKENS},
        )
        usage = response.get("usage") or {}
        metrics = response.get("metrics") or {}
        return ModelReply(
            tool_input=extract_tool_input(response),
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
            latency_ms=metrics.get("latencyMs"),
            stop_reason=response.get("stopReason"),
        )

    def retryable(self, exc: Exception) -> tuple[str, bool] | None:
        code = _error_code(exc)
        if not code:
            return None
        return f"aws:{code}", code in RETRYABLE_ERROR_CODES


class BedrockVerdictClient(VerdictClient):
    """Asks Bedrock for a risk verdict. Never raises.

    Unchanged in signature and behaviour from before the provider split: it is
    the shared client wired to a Bedrock transport.
    """

    def __init__(self, model_id: str, client: Any, **kwargs: Any):
        super().__init__(model_id, BedrockTransport(model_id, client), **kwargs)


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
    place, and `client.py` is it.

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
