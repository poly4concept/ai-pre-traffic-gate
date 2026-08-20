"""The tool schema we hand Bedrock. Phase 3.1.

THE MOST IMPORTANT SENTENCE IN THIS PROJECT

Bedrock does not validate tool input against this schema.

That is worth stating flatly because almost everything about the Converse
tool-use API implies otherwise. You declare a JSON Schema, you set
`toolChoice` to force the tool, and back comes a neat JSON object. It is easy to
conclude that the platform enforced the contract. It did not. The schema is
given to the model as part of its context and shapes what it generates; it is a
very strong steer, not a gate. A model can, and occasionally does, return an
out-of-enum value, a missing field, or a number outside its stated range.

So the schema below is a PROMPT, and `validation.py` is the CONTRACT. Everything
this project claims about failing closed rests on that separation. If you take
one thing from the Phase 3 talk section, take that.

(Forcing the tool with `toolChoice` still buys something real: the model cannot
answer in prose, so we never have to parse English into a decision. It removes a
whole failure mode. It just is not validation.)

WHY THE BOUNDS ARE MODULE CONSTANTS

`MAX_REASONING_CHARS` and friends are named here and imported by the validator,
because the schema we advertise and the rules we enforce drifting apart is a
genuinely likely bug: they live in different files, they are edited at different
times, and nothing about a passing test suite would notice. Sharing the
constants makes the numbers impossible to desynchronise, and
`test_verdict_schema.py` additionally asserts that the field NAMES agree.
"""

from __future__ import annotations

from typing import Any

from .types import RiskLevel

VERDICT_TOOL_NAME = "record_risk_verdict"

# Bounds shared with validation.py so the advertised schema and the enforced
# contract cannot drift apart.
MAX_REASONING_CHARS = 600
MAX_CONCERN_CHARS = 120
MAX_CONCERNS = 5

# Every field the model may emit, and nothing else.
VERDICT_FIELDS = ("risk_level", "confidence", "reasoning", "primary_concerns")


VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # An enum, not a free string. See types.RiskLevel for why this is not a
        # numeric score. Generated from the enum so the two cannot disagree.
        "risk_level": {
            "type": "string",
            "enum": [str(level) for level in RiskLevel],
            "description": (
                "Overall risk of deploying this change to this service right now. "
                "low = deploy fully. medium = worth a gradual rollout. "
                "high = a human should look before this ships."
            ),
        },
        # Recorded, never routed on. Self-reported model confidence is not
        # calibrated -- a model saying 0.95 does not mean it is right 95% of the
        # time -- so using it as a threshold would dress an uncalibrated number
        # up as a safety mechanism. It is here because Phase 4 wants to know
        # whether confidence correlates with correctness at all.
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": (
                "Your confidence in this assessment, 0 to 1. Recorded for analysis only; "
                "it does not affect what happens to the deploy."
            ),
        },
        # maxLength is not cosmetic. This string ends up in CloudWatch Logs, a
        # DynamoDB record, and (Phase 5) a Slack message. It is also the field
        # most able to carry text a commit message talked the model into
        # repeating. Bounding it bounds how far injected content travels.
        "reasoning": {
            "type": "string",
            "maxLength": MAX_REASONING_CHARS,
            "description": (
                "Two or three sentences justifying the risk level, referring to specific "
                "signals you were given. Say which signals were missing if that affected "
                "your assessment."
            ),
        },
        "primary_concerns": {
            "type": "array",
            "items": {"type": "string", "maxLength": MAX_CONCERN_CHARS},
            "maxItems": MAX_CONCERNS,
            "description": (
                "The specific things driving the risk level, most important first. "
                "An empty list is correct for a low-risk change."
            ),
        },
    },
    # All four required. Optional fields invite partial verdicts, and a partial
    # verdict is precisely the ambiguous case that has to fail closed -- so
    # allowing one would mean building the ambiguity in on purpose.
    "required": list(VERDICT_FIELDS),
    # An extra key means the model produced something outside the contract it
    # was given. Ignoring it would discard evidence that the model misunderstood
    # the task, which is exactly when its risk_level deserves least trust.
    "additionalProperties": False,
}


def verdict_tool_config() -> dict[str, Any]:
    """The `toolConfig` argument for a Converse call.

    `toolChoice: {"tool": ...}` is the part that makes this structured output
    rather than a suggestion: the model is required to call this specific tool,
    so it cannot reply in prose and cannot pick a different tool. One tool is
    declared because offering a choice would be offering a way to decline.
    """
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": VERDICT_TOOL_NAME,
                    "description": (
                        "Record your risk assessment for this deployment. "
                        "You must call this tool exactly once."
                    ),
                    "inputSchema": {"json": VERDICT_SCHEMA},
                }
            }
        ],
        "toolChoice": {"tool": {"name": VERDICT_TOOL_NAME}},
    }
