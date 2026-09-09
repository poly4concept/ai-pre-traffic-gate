"""The verdict layer. Phase 3.

Where `signals/` is everything the gate knows, this is everything it is allowed
to conclude -- and the boundary between a language model's output and a decision
about production.

    types.py       the vocabulary: RiskLevel, Action, Verdict, and the
                   risk-to-action mapping the model cannot reach
    schema.py      the tool schema handed to Bedrock. A prompt, not a contract.
    validation.py  the actual contract. Read this one closely.
    prompt.py      the question: a signal bundle rendered for Converse
    bedrock.py     asking it, and failing closed when that does not go well
    audit.py       the immutable record of what was decided and why

Start with `schema.py`, specifically the note at the top: Bedrock does not
validate tool input against the schema you give it. Everything else in this
package follows from that single fact.
"""

from .audit import (
    MAX_FIELD_CHARS,
    AuditWriteResult,
    AuditWriteStatus,
    VerdictAuditWriter,
    build_dynamodb_client,
    build_record,
)
from .bedrock import (
    BACKOFF_SECONDS,
    DEADLINE_SECONDS,
    MAX_ATTEMPTS,
    MAX_TOKENS,
    RETRYABLE_ERROR_CODES,
    TEMPERATURE,
    BedrockVerdictClient,
    ModelCall,
    ModelResponseError,
    VerdictOutcome,
    build_bedrock_client,
    extract_tool_input,
)
from .floor import Floor, apply_floor, required_floor
from .prompt import (
    MAX_COMMIT_MESSAGE_CHARS,
    MAX_FINDINGS_IN_PROMPT,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_messages,
    render_bundle,
    system_blocks,
)
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
    "Floor",
    "apply_floor",
    "required_floor",
    "ACTION_FOR_RISK",
    "BACKOFF_SECONDS",
    "MAX_FIELD_CHARS",
    "AuditWriteResult",
    "AuditWriteStatus",
    "VerdictAuditWriter",
    "DEADLINE_SECONDS",
    "MAX_COMMIT_MESSAGE_CHARS",
    "MAX_CONCERNS",
    "MAX_CONCERN_CHARS",
    "MAX_FINDINGS_IN_PROMPT",
    "MAX_ATTEMPTS",
    "MAX_REASONING_CHARS",
    "MAX_TOKENS",
    "PROMPT_VERSION",
    "RETRYABLE_ERROR_CODES",
    "RISK_ORDER",
    "SYSTEM_PROMPT",
    "TEMPERATURE",
    "VERDICT_FIELDS",
    "VERDICT_SCHEMA",
    "VERDICT_TOOL_NAME",
    "Action",
    "BedrockVerdictClient",
    "ModelCall",
    "ModelResponseError",
    "RiskLevel",
    "Verdict",
    "VerdictOutcome",
    "VerdictSource",
    "VerdictValidationError",
    "action_for",
    "build_bedrock_client",
    "build_dynamodb_client",
    "build_record",
    "build_messages",
    "extract_tool_input",
    "parse_verdict",
    "render_bundle",
    "system_blocks",
    "verdict_tool_config",
]
