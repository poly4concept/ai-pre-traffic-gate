"""The verdict layer. Phase 3.

Where `signals/` is everything the gate knows, this is everything it is allowed
to conclude -- and the boundary between a language model's output and a decision
about production.

    types.py       the vocabulary: RiskLevel, Action, Verdict, and the
                   risk-to-action mapping the model cannot reach
    schema.py      the tool schema handed to Bedrock. A prompt, not a contract.
    validation.py  the actual contract. Read this one closely.

Start with `schema.py`, specifically the note at the top: Bedrock does not
validate tool input against the schema you give it. Everything else in this
package follows from that single fact.
"""

from .schema import (
    MAX_CONCERN_CHARS,
    MAX_CONCERNS,
    MAX_REASONING_CHARS,
    VERDICT_FIELDS,
    VERDICT_SCHEMA,
    VERDICT_TOOL_NAME,
    verdict_tool_config,
)
from .types import (
    ACTION_FOR_RISK,
    RISK_ORDER,
    Action,
    RiskLevel,
    Verdict,
    VerdictSource,
    action_for,
)
from .validation import VerdictValidationError, parse_verdict

__all__ = [
    "ACTION_FOR_RISK",
    "MAX_CONCERNS",
    "MAX_CONCERN_CHARS",
    "MAX_REASONING_CHARS",
    "RISK_ORDER",
    "VERDICT_FIELDS",
    "VERDICT_SCHEMA",
    "VERDICT_TOOL_NAME",
    "Action",
    "RiskLevel",
    "Verdict",
    "VerdictSource",
    "VerdictValidationError",
    "action_for",
    "parse_verdict",
    "verdict_tool_config",
]
